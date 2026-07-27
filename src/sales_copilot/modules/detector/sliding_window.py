from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class TranscriptChunk:
    text: str
    speaker: str
    start_ms: int
    end_ms: int


class SlidingWindowBuffer:
    def __init__(self, max_chunks: int = 5) -> None:
        self._chunks: deque[TranscriptChunk] = deque(maxlen=max_chunks)

    def add(self, chunk: TranscriptChunk) -> None:
        self._chunks.append(chunk)

    def context_text(self) -> str:
        return " ".join(c.text for c in self._chunks if c.text.strip())

    def latest_chunk(self) -> TranscriptChunk | None:
        return self._chunks[-1] if self._chunks else None

    def __len__(self) -> int:
        return len(self._chunks)
