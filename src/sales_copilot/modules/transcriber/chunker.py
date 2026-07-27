from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True, slots=True)
class AudioChunk:
    audio: np.ndarray
    start_ms: int
    end_ms: int


@dataclass(slots=True)
class AudioChunker:
    sample_rate: int = 16000
    chunk_ms: int = 3000
    min_chunk_ms: int = 1200
    max_chunk_ms: int = 6000
    silence_flush_ms: int = 400
    _buffer: list[np.ndarray] = field(init=False, default_factory=list, repr=False)
    _chunk_start_ms: int | None = field(init=False, default=None, repr=False)
    _last_speech_end_ms: int | None = field(init=False, default=None, repr=False)
    _cursor_ms: int = field(init=False, default=0, repr=False)

    def add_frames(self, frames: np.ndarray, *, is_speech: bool) -> list[AudioChunk]:
        mono = self._to_mono(frames)
        frame_ms = self._frame_ms(mono)
        start_cursor = self._cursor_ms
        self._cursor_ms += frame_ms

        ready: list[AudioChunk] = []
        if is_speech:
            if self._chunk_start_ms is None:
                self._chunk_start_ms = start_cursor
            self._buffer.append(mono)
            self._last_speech_end_ms = self._cursor_ms
        elif self._should_flush_for_silence():
            chunk = self.flush()
            if chunk is not None:
                ready.append(chunk)

        duration_ms = self.buffer_duration_ms
        if duration_ms >= self.max_chunk_ms or duration_ms >= self.chunk_ms:
            chunk = self.flush()
            if chunk is not None:
                ready.append(chunk)
        return ready

    @property
    def buffer_duration_ms(self) -> int:
        if not self._buffer:
            return 0
        total_samples = sum(chunk.shape[0] for chunk in self._buffer)
        return int(round((total_samples / float(self.sample_rate)) * 1000))

    def flush(self) -> AudioChunk | None:
        if not self._buffer or self._chunk_start_ms is None:
            self._buffer.clear()
            self._chunk_start_ms = None
            self._last_speech_end_ms = None
            return None

        duration_ms = self.buffer_duration_ms
        if duration_ms < self.min_chunk_ms:
            return None

        audio = np.concatenate(self._buffer, dtype=np.float32)
        chunk = AudioChunk(
            audio=audio,
            start_ms=self._chunk_start_ms,
            end_ms=self._chunk_start_ms + duration_ms,
        )
        self._buffer.clear()
        self._chunk_start_ms = None
        self._last_speech_end_ms = None
        return chunk

    def _should_flush_for_silence(self) -> bool:
        if not self._buffer or self._last_speech_end_ms is None:
            return False
        if self.buffer_duration_ms < self.min_chunk_ms:
            return False
        silence_ms = self._cursor_ms - self._last_speech_end_ms
        return silence_ms >= self.silence_flush_ms

    def _frame_ms(self, audio: np.ndarray) -> int:
        if audio.shape[0] == 0:
            return 0
        return max(1, int(round((audio.shape[0] / float(self.sample_rate)) * 1000)))

    @staticmethod
    def _to_mono(frames: np.ndarray) -> np.ndarray:
        audio = np.asarray(frames, dtype=np.float32)
        if audio.ndim == 1:
            mono = audio
        elif audio.ndim == 2:
            mono = audio.mean(axis=1, dtype=np.float32)
        else:
            raise ValueError("frames must be 1D mono or 2D multi-channel")
        return np.clip(mono, -1.0, 1.0)
