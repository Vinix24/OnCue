from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

try:
    from sales_copilot.modules.talk_time.tracker import SpeechEvent as _SpeechEvent
except Exception:  # pragma: no cover - fallback until tracker exists
    _SpeechEvent = None


@dataclass
class SpeechEvent:
    speaker: Literal["self", "prospect"]
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def load_silero_model():
    try:
        from silero_vad import load_silero_vad
    except ModuleNotFoundError as exc:  # pragma: no cover - requires optional dependency
        raise RuntimeError("silero_vad is not installed. Install live-sales-copilot with core deps.") from exc
    return load_silero_vad()


class VADProcessor:
    def __init__(
        self,
        model: object,
        speaker: Literal["self", "prospect"],
        *,
        sample_rate: int = 16000,
        chunk_duration_ms: int | None = None,
        positive_threshold: float = 0.5,
        negative_threshold: float = 0.35,
        debounce_ms: int = 300,
    ) -> None:
        if speaker not in ("self", "prospect"):
            raise ValueError("speaker must be 'self' or 'prospect'")
        if positive_threshold <= negative_threshold:
            raise ValueError("positive_threshold must be greater than negative_threshold")
        if debounce_ms < 0:
            raise ValueError("debounce_ms must be non-negative")

        self._model = model
        self._speaker = speaker
        self._sample_rate = sample_rate
        self._chunk_duration_ms = chunk_duration_ms
        self._positive_threshold = positive_threshold
        self._negative_threshold = negative_threshold
        self._debounce_ms = debounce_ms

        self._chunk_index = 0
        self._in_speech = False
        self._speech_start_ms: int | None = None
        self._last_voice_ms: int | None = None
        self._silence_ms = 0

    def process_chunk(self, audio_chunk: np.ndarray) -> SpeechEvent | None:
        if audio_chunk is None or audio_chunk.size == 0:
            self._chunk_index += 1
            return None

        chunk_duration_ms = self._chunk_duration_ms
        if chunk_duration_ms is None:
            chunk_duration_ms = int(round(len(audio_chunk) / float(self._sample_rate) * 1000))
            if chunk_duration_ms <= 0:
                chunk_duration_ms = 1

        now_ms = self._chunk_index * chunk_duration_ms
        self._chunk_index += 1

        probability = self._score_chunk(audio_chunk)

        if probability >= self._positive_threshold:
            if not self._in_speech:
                self._in_speech = True
                self._speech_start_ms = now_ms
            self._last_voice_ms = now_ms + chunk_duration_ms
            self._silence_ms = 0
            return None

        if probability < self._negative_threshold:
            if self._in_speech:
                self._silence_ms += chunk_duration_ms
                if self._silence_ms >= self._debounce_ms:
                    event = self._build_event()
                    self._reset_state()
                    return event
            return None

        if self._in_speech:
            self._last_voice_ms = now_ms + chunk_duration_ms
            self._silence_ms = 0
        return None

    def _score_chunk(self, audio_chunk: np.ndarray) -> float:
        audio = np.asarray(audio_chunk, dtype=np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=-1)

        try:
            import torch

            audio_tensor = torch.from_numpy(audio)
            try:
                probability = self._model(audio_tensor, self._sample_rate)
            except TypeError:
                probability = self._model(audio_tensor)
            if hasattr(probability, "item"):
                return float(probability.item())
            return float(probability)
        except ModuleNotFoundError:
            try:
                probability = self._model(audio, self._sample_rate)
            except TypeError:
                probability = self._model(audio)
            return float(probability)

    def _build_event(self) -> SpeechEvent:
        if self._speech_start_ms is None or self._last_voice_ms is None:
            raise RuntimeError("Cannot build SpeechEvent without speech start and end timestamps")

        event_cls = _SpeechEvent or SpeechEvent
        return event_cls(
            speaker=self._speaker,
            start_ms=int(self._speech_start_ms),
            end_ms=int(self._last_voice_ms),
        )

    def _reset_state(self) -> None:
        self._in_speech = False
        self._speech_start_ms = None
        self._last_voice_ms = None
        self._silence_ms = 0
