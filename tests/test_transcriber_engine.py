"""Tests for PartialTranscriptEvent and DirectWhisperEngine partial emit logic."""
from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from sales_copilot.core.config import WebSocketConfig
from sales_copilot.modules.transcriber.engine import PartialTranscriptEvent
from sales_copilot.modules.transcriber.whisper_direct import DirectWhisperEngine


def test_partial_transcript_event_to_dict() -> None:
    event = PartialTranscriptEvent(
        type="partial_transcript",
        text="test prefix",
        speaker="prospect",
        start_ms=1000,
        tentative_end_ms=1500,
        is_final=False,
    )
    result = event.to_dict()
    assert result == {
        "type": "partial_transcript",
        "text": "test prefix",
        "speaker": "prospect",
        "start_ms": 1000,
        "tentative_end_ms": 1500,
        "is_final": False,
    }


def test_partial_transcript_event_is_final_always_false() -> None:
    event = PartialTranscriptEvent(
        type="partial_transcript",
        text="",
        speaker="self",
        start_ms=0,
        tentative_end_ms=500,
        is_final=True,
    )
    assert event.to_dict()["is_final"] is False


class _StopOnFinalWs:
    """Records sent events and sets stop_event when a final transcript arrives."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        self.closed = False
        self.sent: list[dict] = []
        self._stop = stop_event

    async def send(self, data: str) -> None:
        payload = json.loads(data)
        self.sent.append(payload)
        if payload.get("type") == "transcript":
            self._stop.set()

    async def close(self) -> None:
        self.closed = True


class _InfiniteSpeechStream:
    """Returns speech frames indefinitely until told to switch to None."""

    def __init__(self, speech_frames: int, sample_rate: int = 16000) -> None:
        self._remaining = speech_frames
        self._sample_rate = sample_rate

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def read(self) -> np.ndarray | None:
        if self._remaining > 0:
            self._remaining -= 1
            return np.ones((self._sample_rate // 10, 1), dtype=np.float32)
        return None


class _FakeBackend:
    def __init__(self, text: str = "hallo wereld") -> None:
        self._text = text

    async def start(self, _stop_event: asyncio.Event) -> None:
        pass

    async def transcribe(self, _audio: np.ndarray) -> str:
        return self._text

    async def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_engine_emits_partial_before_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine emits partial_transcript events during speech, then transcript on flush."""
    stop_event = asyncio.Event()
    ws = _StopOnFinalWs(stop_event)

    async def _connect(*_args, **_kwargs):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.whisper_direct.websockets.connect",
        _connect,
    )
    monkeypatch.setattr(DirectWhisperEngine, "_load_vad", staticmethod(lambda: None))
    monkeypatch.setattr(DirectWhisperEngine, "_is_speech", lambda self, chunk: True)

    engine = DirectWhisperEngine(
        audio_stream=_InfiniteSpeechStream(speech_frames=3),
        ws_config=WebSocketConfig(),
        speaker="prospect",
        backend=_FakeBackend(text="hallo wereld"),
        silence_gap_seconds=0.0,
        partial_emit_interval=0.0,
    )
    await engine.run(stop_event)

    types = [msg["type"] for msg in ws.sent]
    assert "partial_transcript" in types, f"Expected partial_transcript events, got: {types}"

    final_events = [msg for msg in ws.sent if msg["type"] == "transcript"]
    assert len(final_events) == 1
    assert final_events[0]["is_final"] is True
    assert final_events[0]["text"] == "hallo wereld"

    partial_events = [msg for msg in ws.sent if msg["type"] == "partial_transcript"]
    assert len(partial_events) >= 1
    for evt in partial_events:
        assert evt["is_final"] is False
        assert evt["speaker"] == "prospect"


@pytest.mark.asyncio
async def test_engine_partials_precede_final(monkeypatch: pytest.MonkeyPatch) -> None:
    """All partial events must arrive before the final transcript event."""
    stop_event = asyncio.Event()
    ws = _StopOnFinalWs(stop_event)

    async def _connect(*_args, **_kwargs):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.whisper_direct.websockets.connect",
        _connect,
    )
    monkeypatch.setattr(DirectWhisperEngine, "_load_vad", staticmethod(lambda: None))
    monkeypatch.setattr(DirectWhisperEngine, "_is_speech", lambda self, chunk: True)

    engine = DirectWhisperEngine(
        audio_stream=_InfiniteSpeechStream(speech_frames=5),
        ws_config=WebSocketConfig(),
        speaker="self",
        backend=_FakeBackend(text="test utterance"),
        silence_gap_seconds=0.0,
        partial_emit_interval=0.0,
    )
    await engine.run(stop_event)

    final_idx = next(
        (i for i, m in enumerate(ws.sent) if m["type"] == "transcript"), None
    )
    assert final_idx is not None

    for i, msg in enumerate(ws.sent):
        if msg["type"] == "partial_transcript":
            assert i < final_idx, f"Partial at index {i} arrived after final at {final_idx}"


@pytest.mark.asyncio
async def test_engine_partial_reset_between_utterances(monkeypatch: pytest.MonkeyPatch) -> None:
    """_last_partial_emit_ts resets after flush so next utterance emits fresh partials."""
    finals_seen: list[dict] = []
    stop_event = asyncio.Event()

    class _CountingWs:
        def __init__(self) -> None:
            self.closed = False
            self.sent: list[dict] = []

        async def send(self, data: str) -> None:
            payload = json.loads(data)
            self.sent.append(payload)
            if payload.get("type") == "transcript":
                finals_seen.append(payload)
                if len(finals_seen) >= 2:
                    stop_event.set()

        async def close(self) -> None:
            self.closed = True

    ws = _CountingWs()

    async def _connect(*_args, **_kwargs):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.whisper_direct.websockets.connect",
        _connect,
    )
    monkeypatch.setattr(DirectWhisperEngine, "_load_vad", staticmethod(lambda: None))

    call_count = 0

    class _TwoUtteranceStream:
        """2 speech frames, 1 silence frame, 2 speech frames, then done."""

        def __init__(self) -> None:
            self._seq = [
                np.ones((1600, 1), dtype=np.float32),
                np.ones((1600, 1), dtype=np.float32),
                None,
                np.ones((1600, 1), dtype=np.float32),
                np.ones((1600, 1), dtype=np.float32),
            ]

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

        def read(self) -> np.ndarray | None:
            return self._seq.pop(0) if self._seq else None

    class _CountingBackend:
        async def start(self, _stop_event: asyncio.Event) -> None:
            pass

        async def transcribe(self, audio: np.ndarray) -> str:
            nonlocal call_count
            call_count += 1
            return f"utterance {call_count}"

        async def stop(self) -> None:
            pass

    engine = DirectWhisperEngine(
        audio_stream=_TwoUtteranceStream(),
        ws_config=WebSocketConfig(),
        speaker="prospect",
        backend=_CountingBackend(),
        silence_gap_seconds=0.0,
        partial_emit_interval=0.0,
    )
    monkeypatch.setattr(
        DirectWhisperEngine,
        "_is_speech",
        lambda self, chunk: chunk is not None and float(np.mean(chunk)) > 0,
    )
    await engine.run(stop_event)

    final_events = [m for m in ws.sent if m["type"] == "transcript"]
    assert len(final_events) == 2
    partial_events = [m for m in ws.sent if m["type"] == "partial_transcript"]
    assert len(partial_events) >= 2
