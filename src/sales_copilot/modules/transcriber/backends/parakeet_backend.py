from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from sales_copilot.modules.transcriber.wav_utils import write_wav

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_MISSING_DEP_HINT = (
    "parakeet-mlx is not installed. The Parakeet backend is an optional extra; "
    "install it with `pip install '.[parakeet]'` (or `pip install parakeet-mlx`). "
    "ffmpeg must also be on PATH for audio decoding."
)


class ParakeetMlxBackend:
    """parakeet-mlx transcription backend (Apple Silicon, NVIDIA Parakeet on MLX).

    Wraps ``mlx-community/parakeet-tdt-0.6b-v3`` — a multilingual TDT model
    covering 25 European languages including Dutch. ``parakeet_mlx`` is lazily
    imported inside the methods that need it (mirroring ``MlxWhisperBackend``) so
    merely importing this module never forces the optional dependency.

    Batch ``transcribe`` / ``transcribe_file`` use the model's one-shot
    ``transcribe(path)`` API. The incremental ``transcribe_stream()`` context
    manager is exposed for the low-latency streaming path; its emission
    granularity is finer than the fixed chunk size of the whisper.cpp engine.
    """

    __slots__ = (
        "model_repo",
        "language",
        "timeout_s",
        "chunk_duration_s",
        "_temp_dir",
        "_model",
        "_load_lock",
    )

    def __init__(
        self,
        model_repo: str = "mlx-community/parakeet-tdt-0.6b-v3",
        language: str = "nl",
        timeout_s: float = 60.0,
        chunk_duration_s: float = 120.0,
        *,
        temp_dir: str | Path | None = None,
    ) -> None:
        self.model_repo = model_repo
        # parakeet-tdt-0.6b-v3 auto-detects language; kept for API parity/logging.
        self.language = language
        self.timeout_s = timeout_s
        # Long recordings are decoded in overlapping windows so the mel/feature
        # tensor never grows unbounded — a one-shot decode of a full call OOMs
        # the Metal allocator. Short live chunks fit in a single window (no-op).
        self.chunk_duration_s = chunk_duration_s
        self._temp_dir = Path(temp_dir) if temp_dir is not None else None
        self._model: Any | None = None
        self._load_lock = threading.Lock()

    async def start(self, _stop_event: asyncio.Event) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def warmup(self) -> None:
        logger.info("Parakeet model warmup starting (%s)...", self.model_repo)
        started_at = time.perf_counter()
        await asyncio.to_thread(self._warmup_sync)
        elapsed = time.perf_counter() - started_at
        logger.info("Parakeet model ready (%.1fs)", elapsed)

    async def transcribe(self, audio: np.ndarray) -> str:
        return await self._bounded(asyncio.to_thread(self._transcribe_array_sync, audio))

    async def transcribe_file(self, path: Path) -> str:
        return await self._bounded(asyncio.to_thread(self._transcribe_file_sync, Path(path)))

    async def transcribe_file_segments(self, path: Path) -> list[dict]:
        """Batch-transcribe a file into timestamped segments (post-call path).

        Reuses the chunked decode (``chunk_duration_s``) so a long recording never
        OOMs the Metal allocator. Returns ``[{"start", "end", "text"}]`` ordered as
        the model emits them, or ``[]`` on timeout — so a wedged decode degrades
        gracefully instead of stalling a batch job.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._transcribe_file_segments_sync, Path(path)),
                timeout=self.timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "parakeet-mlx segment transcribe timed out after %.1fs — returning []",
                self.timeout_s,
            )
            return []

    @contextmanager
    def transcribe_stream(
        self,
        context_size: tuple[int, int] = (256, 256),
        depth: int = 1,
    ) -> Iterator[Any]:
        """Yield a ``StreamingParakeet`` for incremental ``add_audio`` feeding.

        The model is loaded on first use. Callers feed 16 kHz mono audio as
        ``mlx.core.array`` via ``streamer.add_audio(...)`` and read the evolving
        transcript from ``streamer.result``.
        """
        model = self._ensure_model()
        with model.transcribe_stream(context_size=context_size, depth=depth) as streamer:
            yield streamer

    # -- internals ---------------------------------------------------------

    async def _bounded(self, coro: Any) -> str:
        """Bound a transcription coroutine so a wedged decode degrades to empty.

        Consistent with the mlx-whisper and whisper.cpp backends: a hung
        inference returns an empty transcript for that chunk instead of stalling
        the inference worker indefinitely.
        """
        try:
            return await asyncio.wait_for(coro, timeout=self.timeout_s)
        except TimeoutError:
            logger.warning(
                "parakeet-mlx transcribe timed out after %.1fs — returning empty",
                self.timeout_s,
            )
            return ""

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                import parakeet_mlx
            except ImportError as exc:
                raise RuntimeError(_MISSING_DEP_HINT) from exc
            self._model = parakeet_mlx.from_pretrained(self.model_repo)
            return self._model

    def _warmup_sync(self) -> None:
        model = self._ensure_model()
        # 1 s of silence through the batch path warms the encoder/decoder graph.
        with self._temp_wav_path() as wav_path:
            write_wav(wav_path, np.zeros(16000, dtype=np.float32), sample_rate=16000)
            model.transcribe(str(wav_path))

    def _transcribe_array_sync(self, audio: np.ndarray) -> str:
        model = self._ensure_model()
        samples = np.asarray(audio, dtype=np.float32)
        if samples.ndim == 2:
            samples = samples.mean(axis=1, dtype=np.float32)
        if samples.size == 0:
            return ""
        with self._temp_wav_path() as wav_path:
            write_wav(wav_path, samples, sample_rate=16000)
            return self._result_text(
                model.transcribe(str(wav_path), chunk_duration=self.chunk_duration_s)
            )

    def _transcribe_file_sync(self, path: Path) -> str:
        model = self._ensure_model()
        return self._result_text(
            model.transcribe(str(path), chunk_duration=self.chunk_duration_s)
        )

    def _transcribe_file_segments_sync(self, path: Path) -> list[dict]:
        model = self._ensure_model()
        result = model.transcribe(str(path), chunk_duration=self.chunk_duration_s)
        segments: list[dict] = []
        for sentence in getattr(result, "sentences", None) or []:
            text = (getattr(sentence, "text", "") or "").strip()
            if not text:
                continue
            segments.append(
                {
                    "start": float(getattr(sentence, "start", 0.0) or 0.0),
                    "end": float(getattr(sentence, "end", 0.0) or 0.0),
                    "text": text,
                }
            )
        if not segments:
            # Model exposed no sentence-level timings; fall back to one whole-text segment.
            whole = self._result_text(result)
            if whole:
                segments.append({"start": 0.0, "end": 0.0, "text": whole})
        return segments

    @staticmethod
    def _result_text(result: Any) -> str:
        text = getattr(result, "text", "")
        return text.strip() if isinstance(text, str) else ""

    @contextmanager
    def _temp_wav_path(self) -> Iterator[Path]:
        if self._temp_dir is not None:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=self._temp_dir, delete=True) as handle:
            yield Path(handle.name)
