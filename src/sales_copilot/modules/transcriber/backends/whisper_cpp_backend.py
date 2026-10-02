from __future__ import annotations

import asyncio
import logging
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import numpy as np

from sales_copilot.core.config import TranscriberConfig
from sales_copilot.modules.transcriber.orphans import stop_orphans
from sales_copilot.modules.transcriber.vocabulary import load_vocabulary_config
from sales_copilot.modules.transcriber.wav_utils import encode_wav_bytes, write_wav

logger = logging.getLogger(__name__)

_GUARD_MODULE = "sales_copilot.modules.transcriber.server_guard"
_GUARD_EXIT_TIMEOUT_S = 8.0


class WhisperCppBackend:
    """whisper.cpp transcription with a persistent-server fast path.

    By default the backend runs ``whisper-server`` once (model stays resident)
    and transcribes each chunk over HTTP. That removes the ~1.6GB model reload
    that dominated the per-chunk ``whisper-cli`` latency. When the server cannot
    start, or a request fails, the backend degrades to the one-shot
    ``whisper-cli`` subprocess path so a call never breaks.

    Non-shared transcriber mode (``TRANSCRIBER_SHARED_QUEUE=false``) constructs
    one backend per stream, so it spawns one server per stream (~1.5GB RAM each).
    The default shared-queue mode uses a single backend and a single server.
    """

    def __init__(
        self,
        config: TranscriberConfig,
        *,
        temp_dir: str | Path | None = None,
    ) -> None:
        self._config = config
        self._binary = Path(config.whisper_cpp_binary).expanduser()
        self._model_path = Path(config.whisper_cpp_model_path).expanduser()
        self._server_binary = Path(config.whisper_cpp_server_binary).expanduser()
        self._temp_dir = Path(temp_dir) if temp_dir is not None else None

        if not self._binary.exists():
            raise FileNotFoundError(f"whisper.cpp binary not found: {self._binary}")
        if not os.access(self._binary, os.X_OK):
            raise PermissionError(f"whisper.cpp binary is not executable: {self._binary}")
        if not self._model_path.exists():
            raise FileNotFoundError(f"whisper.cpp model not found: {self._model_path}")

        self._server_lock = threading.Lock()
        self._server_proc: subprocess.Popen[bytes] | None = None
        self._server_url: str | None = None
        self._http: httpx.Client | None = None
        self._server_started = False
        self._server_disabled = not config.whisper_cpp_server_enabled
        self._initial_prompt = self._resolve_initial_prompt()

    def _resolve_initial_prompt(self) -> str:
        if not self._config.vocabulary_enabled:
            return ""
        if self._config.vocabulary_initial_prompt is not None:
            return self._config.vocabulary_initial_prompt
        vocab = load_vocabulary_config(self._config.vocabulary_config)
        return vocab.initial_prompt

    # ------------------------------------------------------------------ #
    # Public protocol
    # ------------------------------------------------------------------ #
    def transcribe_chunk(self, audio: np.ndarray, *, start_ms: int, end_ms: int) -> dict[str, object] | None:
        served = self._transcribe_via_server(audio) if self._ensure_server() else None
        text = served if served is not None else self._transcribe_via_cli(audio)

        if not text:
            return None

        return {
            "text": text,
            "start": start_ms / 1000.0,
            "end": end_ms / 1000.0,
            "is_final": True,
        }

    async def start(self, _stop_event: asyncio.Event) -> None:
        if not self._server_disabled:
            await asyncio.to_thread(self._ensure_server)

    async def warmup(self) -> None:
        logger.info("Whisper model warmup starting...")
        started_at = time.perf_counter()
        await asyncio.to_thread(
            self.transcribe_chunk,
            np.zeros(16000, dtype=np.float32),
            start_ms=0,
            end_ms=1000,
        )
        elapsed = time.perf_counter() - started_at
        logger.info("Whisper model ready (%.1fs)", elapsed)

    async def transcribe(self, audio: np.ndarray) -> str:
        payload = await asyncio.to_thread(
            self.transcribe_chunk,
            audio,
            start_ms=0,
            end_ms=max(1, int((len(audio) / 16000.0) * 1000)),
        )
        if payload is None:
            return ""
        text = payload.get("text", "")
        return text if isinstance(text, str) else ""

    async def transcribe_file(self, path: Path) -> str:
        result = await asyncio.to_thread(self._run_whisper, self._command_for(Path(path)))
        if result is None:
            return ""
        if result.returncode != 0:
            logger.warning(
                "whisper.cpp file transcription failed (%s): %s",
                result.returncode,
                result.stderr.strip(),
            )
            return ""
        return self._parse_stdout_text(result.stdout)

    async def stop(self) -> None:
        await asyncio.to_thread(self._shutdown_server)

    # ------------------------------------------------------------------ #
    # Server fast path
    # ------------------------------------------------------------------ #
    def _ensure_server(self) -> bool:
        """Start ``whisper-server`` once; return True when it is usable.

        Idempotent and thread-safe. Returns False (so callers fall back to the
        one-shot CLI) when the server is disabled, its binary is missing, it
        failed to come up, or it died after starting.
        """
        if self._server_disabled:
            return False
        if self._server_started:
            if self._server_proc is not None and self._server_proc.poll() is None:
                return True
            logger.warning("whisper-server exited unexpectedly; falling back to whisper-cli")
            self._shutdown_server()
            self._server_disabled = True
            return False

        with self._server_lock:
            if self._server_disabled:
                return False
            if self._server_started:
                return self._server_proc is not None and self._server_proc.poll() is None
            return self._start_server_locked()

    def _start_server_locked(self) -> bool:
        if not self._server_binary.exists() or not os.access(self._server_binary, os.X_OK):
            logger.warning(
                "whisper-server binary not found/executable at %s; run scripts/install.sh to "
                "build it. Using one-shot whisper-cli instead.",
                self._server_binary,
            )
            self._server_disabled = True
            return False

        host = self._config.whisper_cpp_server_host
        port = self._pick_port(host)
        command = self._server_command(host, port)
        stop_orphans(self._server_binary)
        logger.info("Starting whisper-server: %s", shlex.join(command))
        try:
            # The guard owns the server and stops it when our end of the stdin
            # pipe closes, which the kernel does when this process dies, also on SIGKILL.
            proc = subprocess.Popen(  # noqa: S603 - args are config-derived, not user input
                [sys.executable, "-m", _GUARD_MODULE, "--", *command],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            logger.warning("Could not launch whisper-server (%s); using whisper-cli instead", exc)
            self._server_disabled = True
            return False

        self._server_proc = proc
        base_url = f"http://{host}:{port}"
        if not self._wait_for_server(base_url, proc):
            logger.warning(
                "whisper-server did not become ready within %.0fs; using whisper-cli instead",
                self._config.whisper_cpp_server_startup_timeout_s,
            )
            self._shutdown_server()
            self._server_disabled = True
            return False

        self._server_url = f"{base_url}/inference"
        self._http = httpx.Client(timeout=self._config.whisper_cpp_timeout_s)
        self._server_started = True
        logger.info("whisper-server ready at %s", self._server_url)
        return True

    def _pick_port(self, host: str) -> int:
        if self._config.whisper_cpp_server_port:
            return self._config.whisper_cpp_server_port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
            return int(probe.getsockname()[1])

    def _server_command(self, host: str, port: int) -> list[str]:
        return [
            str(self._server_binary),
            "-m",
            str(self._model_path),
            "-l",
            self._config.language,
            "-t",
            str(self._config.whisper_cpp_threads),
            "--host",
            host,
            "--port",
            str(port),
        ]

    def _prompt_args(self) -> list[str]:
        prompt = self._initial_prompt
        if not prompt:
            return []
        return ["--prompt", prompt]

    def _wait_for_server(self, base_url: str, proc: subprocess.Popen[bytes]) -> bool:
        deadline = time.monotonic() + self._config.whisper_cpp_server_startup_timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False
            try:
                response = httpx.get(base_url, timeout=1.0)
                if response.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        return False

    def _transcribe_via_server(self, audio: np.ndarray) -> str | None:
        """Transcribe one chunk over HTTP.

        Returns the (possibly empty) transcript on success, or ``None`` when the
        request failed so the caller falls back to the one-shot CLI path.
        """
        if self._http is None or self._server_url is None:
            return None
        wav_bytes = encode_wav_bytes(audio, 16000)
        try:
            response = self._http.post(
                self._server_url,
                files={"file": ("chunk.wav", wav_bytes, "audio/wav")},
                data={
                    "response_format": "json",
                    "temperature": "0",
                    "language": self._config.language,
                    **({"prompt": self._initial_prompt} if self._initial_prompt else {}),
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("whisper-server request failed (%s); falling back to whisper-cli", exc)
            if self._server_proc is not None and self._server_proc.poll() is not None:
                self._shutdown_server()
                self._server_disabled = True
            return None

        try:
            payload = response.json()
            text = payload.get("text", "") if isinstance(payload, dict) else ""
        except ValueError:
            text = response.text
        return text.strip() if isinstance(text, str) else ""

    def _shutdown_server(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
            finally:
                self._http = None
        proc = self._server_proc
        self._server_proc = None
        self._server_url = None
        self._server_started = False
        if proc is None:
            return
        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=_GUARD_EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("whisper-server did not exit after kill")

    # ------------------------------------------------------------------ #
    # One-shot CLI fallback path
    # ------------------------------------------------------------------ #
    def _transcribe_via_cli(self, audio: np.ndarray) -> str | None:
        with self._temp_wav_path() as wav_path:
            write_wav(wav_path, audio, sample_rate=16000)
            result = self._run_whisper(self._command_for(wav_path))

        if result is None:
            return None
        if result.returncode != 0:
            logger.warning("whisper.cpp chunk failed (%s): %s", result.returncode, result.stderr.strip())
            return None
        text = self._parse_stdout_text(result.stdout)
        return text or None

    def _run_whisper(self, command: list[str]) -> subprocess.CompletedProcess[str] | None:
        """Run the whisper.cpp binary with a hard wall-clock bound.

        Returns ``None`` when the subprocess exceeds ``whisper_cpp_timeout_s`` so a
        wedged transcription degrades to an empty result for that chunk instead of
        propagating and killing the engine.
        """
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._config.whisper_cpp_timeout_s,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "whisper.cpp timed out after %.1fs — dropping chunk",
                self._config.whisper_cpp_timeout_s,
            )
            return None

    def _command_for(self, wav_path: Path) -> list[str]:
        return [
            str(self._binary),
            "-m",
            str(self._model_path),
            "-f",
            str(wav_path),
            "-l",
            self._config.language,
            "-t",
            str(self._config.whisper_cpp_threads),
            "-mc",
            "0",
            "-tp",
            "0",
            *self._prompt_args(),
        ]

    @staticmethod
    def _parse_stdout_text(stdout: str) -> str:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            return ""

        extracted: list[str] = []
        for line in lines:
            candidate = line
            if "]" in candidate:
                candidate = candidate.rsplit("]", 1)[-1].strip()
            if candidate.startswith("[") and candidate.endswith("]"):
                continue
            if candidate:
                extracted.append(candidate)
        return " ".join(extracted).strip()

    def describe_command(self) -> str:
        return shlex.join(self._command_for(Path("<chunk.wav>")))

    @contextmanager
    def _temp_wav_path(self):
        if self._temp_dir is not None:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            suffix=".wav",
            dir=self._temp_dir,
            delete=True,
        ) as handle:
            yield Path(handle.name)
