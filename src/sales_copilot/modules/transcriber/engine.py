from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Speaker = Literal["self", "prospect"]


@dataclass(frozen=True)
class PartialTranscriptEvent:
    type: Literal["partial_transcript"]
    text: str
    speaker: Speaker | None
    start_ms: int
    tentative_end_ms: int
    is_final: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "partial_transcript",
            "text": self.text,
            "speaker": self.speaker,
            "start_ms": self.start_ms,
            "tentative_end_ms": self.tentative_end_ms,
            "is_final": False,
        }


@dataclass(frozen=True)
class TranscriptEvent:
    type: Literal["transcript"]
    text: str
    speaker: Speaker | None
    start_ms: int
    end_ms: int
    is_final: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "transcript",
            "text": self.text,
            "speaker": self.speaker,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "is_final": self.is_final,
        }


def map_speaker_id(speaker_id: int) -> Speaker | None:
    if speaker_id == 0:
        return "self"
    if speaker_id == 1:
        return "prospect"
    return None


def swap_speaker(speaker: Speaker | None) -> Speaker | None:
    if speaker == "self":
        return "prospect"
    if speaker == "prospect":
        return "self"
    return None

