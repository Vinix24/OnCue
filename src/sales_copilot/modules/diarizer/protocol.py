from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class SpeakerSegment:
    """Single speaker turn identified by diarization."""

    speaker_id: str
    start_ms: int
    end_ms: int
    confidence: float = field(default=1.0)


@runtime_checkable
class DiarizerProtocol(Protocol):
    """Interface for speaker diarization backends."""

    async def load(self) -> None: ...

    async def diarize_chunk(self, audio_chunk: bytes, sample_rate: int) -> list[SpeakerSegment]: ...

    async def unload(self) -> None: ...
