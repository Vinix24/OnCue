from __future__ import annotations

import asyncio
import inspect
import logging
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MlxWhisperBackend:
    model_repo: str = "mlx-community/whisper-large-v3-turbo"
    language: str = "nl"
    adapter_path: str | None = None
    timeout_s: float = 30.0
    initial_prompt: str | None = None
    _resolved_adapter_path: str | None = field(init=False, default=None)
    _warned_unsupported_adapter: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        cleaned = (self.adapter_path or "").strip()
        if not cleaned:
            return

        candidate_path = Path(cleaned).expanduser()
        if candidate_path.exists():
            self._resolved_adapter_path = str(candidate_path)
            logger.info("Whisper adapter enabled: %s", self._resolved_adapter_path)
            return

        logger.warning(
            "WHISPER_FINE_TUNED_MODEL_PATH is set but does not exist (%s). Falling back to base model.",
            cleaned,
        )

    async def start(self, _stop_event: asyncio.Event) -> None:
        return None

    async def warmup(self) -> None:
        logger.info("Whisper model warmup starting...")
        started_at = time.perf_counter()
        await asyncio.to_thread(self._transcribe_sync, np.zeros(16000, dtype=np.float32))
        elapsed = time.perf_counter() - started_at
        logger.info("Whisper model ready (%.1fs)", elapsed)

    async def transcribe(self, audio: np.ndarray) -> str:
        return await self._bounded(asyncio.to_thread(self._transcribe_sync, audio))

    async def transcribe_file(self, path: Path) -> str:
        return await self._bounded(asyncio.to_thread(self._transcribe_file_sync, Path(path)))

    async def _bounded(self, coro: Any) -> str:
        """Bound a transcription coroutine so a wedged decode degrades to empty.

        Consistent with the whisper.cpp backend's subprocess timeout: a hung
        inference returns an empty transcript for that chunk instead of stalling
        the inference worker indefinitely.
        """
        try:
            return await asyncio.wait_for(coro, timeout=self.timeout_s)
        except TimeoutError:
            logger.warning(
                "mlx-whisper transcribe timed out after %.1fs — returning empty",
                self.timeout_s,
            )
            return ""

    def _transcribe_file_sync(self, path: Path) -> str:
        with wave.open(str(path), "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        int16 = np.frombuffer(raw, dtype=np.int16)
        audio = int16.astype(np.float32) / 32768.0
        return self._transcribe_sync(audio)

    async def stop(self) -> None:
        return None

    def _transcribe_sync(self, audio: np.ndarray) -> str:
        import mlx_whisper

        kwargs: dict[str, Any] = {
            "path_or_hf_repo": self.model_repo,
            "language": self.language,
            "condition_on_previous_text": False,
            "compression_ratio_threshold": 2.4,
            "no_speech_threshold": 0.6,
        }
        if self.initial_prompt:
            kwargs["initial_prompt"] = self.initial_prompt

        if self._resolved_adapter_path:
            adapter_kwarg = _find_adapter_kwarg(mlx_whisper.transcribe)
            if adapter_kwarg:
                kwargs[adapter_kwarg] = self._resolved_adapter_path
            elif not self._warned_unsupported_adapter:
                logger.warning(
                    "Adapter path configured (%s), but mlx_whisper.transcribe does not accept adapter kwargs.",
                    self._resolved_adapter_path,
                )
                self._warned_unsupported_adapter = True

        result = mlx_whisper.transcribe(audio, **kwargs)
        if not isinstance(result, dict):
            return ""
        text = result.get("text", "")
        return text if isinstance(text, str) else ""


def _find_adapter_kwarg(transcribe_fn: Any) -> str | None:
    try:
        signature = inspect.signature(transcribe_fn)
    except (TypeError, ValueError):
        return None

    for keyword in ("adapter_path", "lora_path", "adapter", "adapters_path"):
        if keyword in signature.parameters:
            return keyword

    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return "adapter_path"
    return None
