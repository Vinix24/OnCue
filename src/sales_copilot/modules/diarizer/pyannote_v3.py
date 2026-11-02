from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field

import numpy as np

from sales_copilot.modules.diarizer.protocol import SpeakerSegment

logger = logging.getLogger(__name__)

_PIPELINE_ID = "pyannote/speaker-diarization-3.1"
_AUTH_ERROR_NAMES = frozenset({"GatedRepoError", "RepositoryNotFoundError"})


def _auth_argument(from_pretrained: object) -> str:
    """Name of the token argument the installed pyannote.audio accepts.

    pyannote.audio 4.x takes `token=`, 3.x takes `use_auth_token=`.
    """
    params = inspect.signature(from_pretrained).parameters  # type: ignore[arg-type]
    return "token" if "token" in params else "use_auth_token"


def _is_auth_error(exc: Exception) -> bool:
    """True for HuggingFace hub errors about a bad token or a gated, unaccepted model."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    return any(cls.__name__ in _AUTH_ERROR_NAMES for cls in type(exc).__mro__)


@dataclass
class PyannoteV3Diarizer:
    """pyannote/speaker-diarization-3.1 via HuggingFace.

    Uses MPS (Apple Silicon) by default; falls back gracefully on load failure.
    Model download (~500 MB) happens on first load() call, not at construction.

    Requires HF token with accepted pyannote/speaker-diarization-3.1 model card.
    See claudedocs/ADR-PYANNOTE-DIARIZATION.md for licensing and architecture notes.
    """

    hf_token: str
    min_speakers: int = 1
    max_speakers: int = 10
    device: str = "mps"
    timeout_s: float = 30.0
    _pipeline: object | None = field(init=False, default=None, repr=False)

    async def load(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._load_sync)

    def _load_sync(self) -> None:
        try:
            import torch
            from pyannote.audio import Pipeline  # heavy import, lazy
        except ImportError:
            logger.warning(
                "PyannoteV3Diarizer: pyannote.audio or torch is not installed "
                "(install the diarization extra) — diarization disabled for this session",
                exc_info=True,
            )
            return

        try:
            pipeline = Pipeline.from_pretrained(
                _PIPELINE_ID,
                **{_auth_argument(Pipeline.from_pretrained): self.hf_token},
            )
        except Exception as exc:
            if _is_auth_error(exc):
                logger.warning(
                    "PyannoteV3Diarizer: HuggingFace refused the token or the model card of %s "
                    "is not accepted (%s) — diarization disabled for this session",
                    _PIPELINE_ID,
                    exc,
                )
            else:
                logger.warning(
                    "PyannoteV3Diarizer: unexpected error while loading %s — "
                    "diarization disabled for this session",
                    _PIPELINE_ID,
                    exc_info=True,
                )
            return

        if pipeline is None:
            logger.warning(
                "PyannoteV3Diarizer: pipeline %s unavailable (missing or invalid HF token, "
                "or model card not accepted) — diarization disabled for this session",
                _PIPELINE_ID,
            )
            return

        try:
            pipeline.to(torch.device(self.device))
        except Exception:
            logger.warning(
                "PyannoteV3Diarizer: could not move pipeline to device %s — "
                "diarization disabled for this session",
                self.device,
                exc_info=True,
            )
            return
        self._pipeline = pipeline
        logger.info("PyannoteV3Diarizer: model loaded on %s", self.device)

    async def diarize_chunk(self, audio_chunk: bytes, sample_rate: int) -> list[SpeakerSegment]:
        if self._pipeline is None:
            return []
        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._diarize_sync, audio_chunk, sample_rate),
                timeout=self.timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "PyannoteV3Diarizer: diarize_chunk timed out after %.1fs, returning empty",
                self.timeout_s,
            )
            return []
        except Exception:
            logger.warning("PyannoteV3Diarizer: diarize_chunk failed, returning empty", exc_info=True)
            return []

    def _diarize_sync(self, audio_chunk: bytes, sample_rate: int) -> list[SpeakerSegment]:
        import torch  # noqa: PLC0415 — lazy import, pyannote is optional

        samples = np.frombuffer(audio_chunk, dtype=np.float32).copy()
        waveform = torch.from_numpy(samples).unsqueeze(0)  # [1, T] mono
        diarization = self._pipeline(
            {"waveform": waveform, "sample_rate": sample_rate},
            min_speakers=self.min_speakers,
            max_speakers=self.max_speakers,
        )
        return [
            SpeakerSegment(
                speaker_id=speaker,
                start_ms=int(turn.start * 1000),
                end_ms=int(turn.end * 1000),
            )
            for turn, _, speaker in diarization.itertracks(yield_label=True)
        ]

    async def unload(self) -> None:
        self._pipeline = None
        logger.info("PyannoteV3Diarizer: unloaded")
