from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import numpy as np

from sales_copilot.modules.diarizer.protocol import SpeakerSegment

logger = logging.getLogger(__name__)


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

            pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                use_auth_token=self.hf_token,
            )
            pipeline.to(torch.device(self.device))
            self._pipeline = pipeline
            logger.info("PyannoteV3Diarizer: model loaded on %s", self.device)
        except Exception:
            logger.warning("PyannoteV3Diarizer: load failed — diarization disabled for this session", exc_info=True)

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
