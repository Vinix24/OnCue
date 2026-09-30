"""Telephony call-tap prospect stream via an AudioTee process-tap.

Phone-call audio (iPhone-relay over Bluetooth/Continuity) bypasses the virtual
audio devices the BlackHole route depends on, so the prospect is never heard on
the system-output device. macOS routes that audio through the ``avconferenced``
telephony daemon instead. AudioTee can tap a single process with Core Audio
process taps, so pointing it at ``avconferenced`` recovers the prospect stream.

``CallTapStream`` exposes the exact same surface as ``BlackHoleStream`` and
``MicStream`` (``start``/``stop``/``read`` plus ``chunks_received`` and
``device_label``), so the recorder, talk-time VAD and ``check_streams_liveness``
all consume it unchanged. The tap may be opened before the call connects: it
then delivers silence (zero-amplitude PCM), which the liveness check surfaces as
an audio_warning without hard-failing the call.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from .capture import AudioConfig, _drop_oldest_and_put, _find_pid
from .tap_health import TapHealth, TapHealthTracker

logger = logging.getLogger(__name__)


class CallTapStream:
    def __init__(
        self,
        config: AudioConfig,
        *,
        queue_maxsize: int = 100,
        on_warning: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        self._process_name = config.call_process_name or "avconferenced"
        self._on_warning = on_warning
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_maxsize)
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop_event = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._chunks_received = 0
        self._restart_attempted = False
        self._degraded = False
        self._health = TapHealthTracker()

    @property
    def chunks_received(self) -> int:
        return self._chunks_received

    @property
    def device_label(self) -> str:
        return f"call-tap:{self._process_name}"

    def tap_health(self) -> TapHealth:
        """Report whether the call-tap is attached, and whether audio is flowing."""

        return self._health.snapshot()

    def start(self) -> None:
        """Resolve the call process and start the supervised AudioTee tap.

        Startup failures (missing binary, no telephony process) do not raise:
        they log a clear error, emit an audio_warning, and leave the stream in a
        degraded (silent) state. A raised exception here would take the
        orchestrator down with it, which the dispatch forbids.
        """

        with self._lock:
            if self._proc is not None or self._supervisor is not None:
                return

            if not os.path.exists(self._config.audiotee_path):
                self._emit_warning(
                    f"AudioTee-binary niet gevonden ({self._config.audiotee_path}); prospect-call-tap kan niet starten."
                )
                self._degraded = True
                self._health.mark_attach_failed("binary ontbreekt")
                return

            pid = _find_pid(self._process_name)
            if pid is None:
                self._emit_warning(
                    f"Telefonie-proces '{self._process_name}' niet gevonden. "
                    "Start een gesprek (of controleer of avconferenced draait) en probeer opnieuw."
                )
                self._degraded = True
                self._health.mark_attach_failed(f"'{self._process_name}' draait niet")
                return

            self._stop_event.clear()
            self._supervisor = threading.Thread(
                target=self._supervise_loop,
                args=(pid,),
                name="calltap-supervisor",
                daemon=True,
            )
            self._supervisor.start()

    def _supervise_loop(self, pid: int) -> None:
        """Run the tap, restarting it once if the subprocess dies mid-call."""

        current_pid = pid
        while not self._stop_event.is_set():
            proc = self._spawn(current_pid)
            if proc is None:
                self._emit_warning(f"Kon AudioTee-call-tap niet starten voor '{self._process_name}'.")
                self._health.mark_attach_failed("subprocess kon niet starten")
                return

            with self._lock:
                self._proc = proc
            self._health.mark_attached(f"{self.device_label} (pid {current_pid})")
            self._pump_stdout(proc)

            if self._stop_event.is_set():
                return

            # Subprocess exited unexpectedly. avconferenced can restart between
            # calls, so attempt one re-resolve + tap-restart before warning.
            if self._restart_attempted:
                self._emit_warning(f"AudioTee-call-tap voor '{self._process_name}' is opnieuw gestopt; geef het op.")
                self._health.mark_detached("subprocess opnieuw gestopt", already_warned=True)
                return
            self._restart_attempted = True
            logger.error(
                "Call-tap subprocess for '%s' exited unexpectedly; re-resolving PID and restarting once.",
                self._process_name,
            )
            current_pid = _find_pid(self._process_name) or 0
            if not current_pid:
                self._emit_warning(
                    f"Telefonie-proces '{self._process_name}' verdween en kwam niet terug; prospect-audio gestopt."
                )
                self._health.mark_detached(f"'{self._process_name}' verdween", already_warned=True)
                return

    def _spawn(self, pid: int) -> subprocess.Popen[bytes] | None:
        chunk_duration_s = self._config.chunk_size / float(self._config.sample_rate)
        args = [
            self._config.audiotee_path,
            "--sample-rate",
            str(self._config.sample_rate),
            "--chunk-duration",
            str(chunk_duration_s),
            "--include-processes",
            str(pid),
        ]
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except (OSError, ValueError):
            logger.error("Failed to launch AudioTee call-tap subprocess", exc_info=True)
            return None
        if proc.stdout is None:
            logger.error("AudioTee call-tap subprocess has no stdout pipe")
            return None

        stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(proc,),
            name="calltap-stderr",
            daemon=True,
        )
        stderr_thread.start()
        self._stderr_thread = stderr_thread
        return proc

    def _pump_stdout(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None

        input_dtype = np.dtype("int16")
        sample_bytes = input_dtype.itemsize
        bytes_per_chunk = self._config.chunk_size * self._config.channels * sample_bytes
        buffer = bytearray()

        while not self._stop_event.is_set():
            chunk = proc.stdout.read(bytes_per_chunk)
            if not chunk:
                break
            buffer.extend(chunk)
            while len(buffer) >= bytes_per_chunk:
                raw = bytes(buffer[:bytes_per_chunk])
                del buffer[:bytes_per_chunk]
                frame = np.frombuffer(raw, dtype=input_dtype).reshape((-1, self._config.channels))
                # Measure on the normalized float32 view whatever dtype the
                # consumer asked for: raw int16 sits thousands of times above a
                # float32 silence floor and would make every tap look alive.
                normalized = frame.astype(np.float32) / 32768.0
                if self._config.dtype == "float32":
                    frame = normalized
                else:
                    frame = frame.astype(np.dtype(self._config.dtype))
                self._chunks_received += 1
                if self._health.observe(normalized):
                    logger.info(
                        "Call-tap carrying signal: target=%s first audible frame after %s chunks.",
                        self.device_label,
                        self._chunks_received,
                    )
                _drop_oldest_and_put(self._queue, frame)

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.stderr is None:
            return
        while not self._stop_event.is_set():
            line = proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text:
                logger.debug("audiotee[%s] stderr: %s", self._process_name, text)

    def _emit_warning(self, message: str) -> None:
        logger.error("Call-tap: %s", message)
        if self._on_warning is None:
            return
        try:
            self._on_warning(
                {
                    "type": "audio_warning",
                    "stream": "prospect",
                    "message": message,
                }
            )
        except Exception:
            logger.warning("on_warning callback raised for call-tap", exc_info=True)

    def reattach(self) -> None:
        """Force a clean stop/start cycle, re-resolving the telephony PID.

        The fix for a call-tap that is attached but delivers nothing except
        digital silence (``ATTACHED_SIGNAL_LOST``). The death-triggered
        one-shot restart in ``_supervise_loop`` never fires for this case: the
        subprocess never exits, it just stops carrying anything real. ``start()``
        already re-resolves ``self._process_name`` to a fresh PID on every
        call, which is exactly what a route change needs. Safe to call while
        attached: it does not raise, matching every other public method on
        this stream.
        """

        logger.info("Reattaching call-tap (%s): stopping and restarting.", self.device_label)
        self.stop()
        self._restart_attempted = False
        self.start()

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            proc = self._proc
            self._proc = None

        if proc is not None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("AudioTee call-tap subprocess did not exit after kill")

        supervisor = self._supervisor
        self._supervisor = None
        if supervisor is not None and supervisor is not threading.current_thread():
            supervisor.join(timeout=2)
        stderr_thread = self._stderr_thread
        self._stderr_thread = None
        if stderr_thread is not None:
            stderr_thread.join(timeout=1)

    def read(self) -> np.ndarray | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None
