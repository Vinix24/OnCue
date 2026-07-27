"""Integration tests for diarizer ↔ DirectWhisperEngine.

All mock-based — no HF token, no real audio hardware needed.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from sales_copilot.core.config import WebSocketConfig
from sales_copilot.modules.transcriber.whisper_direct import DirectWhisperEngine

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


class _FakeAudioStream:
    def __init__(self, frames: list[np.ndarray | None]) -> None:
        self._frames = list(frames)
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read(self) -> np.ndarray | None:
        if self._frames:
            return self._frames.pop(0)
        return None


class _FakeBackend:
    def __init__(self, text: str = "prospect zegt iets") -> None:
        self._text = text
        self.started = False
        self.stopped = False

    async def start(self, _stop_event: asyncio.Event) -> None:
        self.started = True

    async def transcribe(self, _audio: np.ndarray) -> str:
        return self._text

    async def stop(self) -> None:
        self.stopped = True


def _make_engine(
    monkeypatch: pytest.MonkeyPatch,
    ws: object,
    frames: list[np.ndarray | None],
    backend_text: str = "klant heeft budget probleem",
    *,
    disable_diarizer: bool = True,
) -> DirectWhisperEngine:
    """Build a DirectWhisperEngine wired to fake WS, fake audio, fake backend."""

    async def _connect(*_a: object, **_kw: object) -> object:
        return ws

    monkeypatch.setenv("DIARIZATION_ENABLED", "0" if disable_diarizer else "1")
    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.whisper_direct.websockets.connect",
        _connect,
    )
    monkeypatch.setattr(DirectWhisperEngine, "_load_vad", staticmethod(lambda: None))
    monkeypatch.setattr(DirectWhisperEngine, "_is_speech", lambda self, chunk: bool(np.mean(chunk) > 0))

    return DirectWhisperEngine(
        audio_stream=_FakeAudioStream(frames),
        ws_config=WebSocketConfig(),
        speaker="prospect",
        backend=_FakeBackend(backend_text),
        silence_gap_seconds=0.0,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcriber_shadow_mode_no_speaker_in_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """DIARIZATION_ENABLED=0: no speaker_segments events, _diarizer is None."""
    stop_event = asyncio.Event()
    frames = np.ones((1600, 1), dtype=np.float32)

    class _RecordingWs:
        closed = False
        sent: list[dict] = []

        async def send(self, data: str) -> None:
            msg = json.loads(data)
            self.sent.append(msg)
            if msg.get("type") == "transcript":
                stop_event.set()

        async def close(self) -> None:
            self.closed = True

    ws = _RecordingWs()
    engine = _make_engine(monkeypatch, ws, [frames, None], disable_diarizer=True)

    await engine.run(stop_event)

    transcript_events = [m for m in ws.sent if m.get("type") == "transcript"]
    speaker_events = [m for m in ws.sent if m.get("type") == "speaker_segments"]

    assert len(transcript_events) == 1
    assert len(speaker_events) == 0
    assert engine._diarizer is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_transcriber_emits_speaker_segments_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """With mock diarizer injected, speaker_segments event is emitted after transcript."""
    from sales_copilot.modules.diarizer.protocol import SpeakerSegment

    stop_event = asyncio.Event()
    frames = np.ones((1600, 1), dtype=np.float32)

    class _RecordingWs:
        closed = False
        sent: list[dict] = []

        async def send(self, data: str) -> None:
            msg = json.loads(data)
            self.sent.append(msg)
            if msg.get("type") == "speaker_segments":
                stop_event.set()

        async def close(self) -> None:
            self.closed = True

    ws = _RecordingWs()
    engine = _make_engine(monkeypatch, ws, [frames, None], disable_diarizer=True)

    # Inject mock diarizer after construction (simulates DIARIZATION_ENABLED=1)
    mock_diarizer = MagicMock()
    mock_diarizer.load = AsyncMock()
    mock_diarizer.diarize_chunk = AsyncMock(
        return_value=[
            SpeakerSegment(speaker_id="SPEAKER_00", start_ms=0, end_ms=800, confidence=0.92),
            SpeakerSegment(speaker_id="SPEAKER_01", start_ms=900, end_ms=1500, confidence=0.87),
        ]
    )
    mock_diarizer.unload = AsyncMock()
    engine._diarizer = mock_diarizer  # noqa: SLF001

    await engine.run(stop_event)

    transcript_events = [m for m in ws.sent if m.get("type") == "transcript"]
    speaker_events = [m for m in ws.sent if m.get("type") == "speaker_segments"]

    # Regular transcript must still arrive unchanged
    assert len(transcript_events) == 1

    # Speaker segments event must be emitted
    assert len(speaker_events) == 1
    seg_event = speaker_events[0]
    assert seg_event["channel"] == "transcript"
    assert len(seg_event["segments"]) == 2
    assert seg_event["segments"][0]["speaker_id"] == "SPEAKER_00"
    assert seg_event["segments"][1]["confidence"] == 0.87

    # Verify diarizer lifecycle was called
    mock_diarizer.load.assert_awaited_once()
    mock_diarizer.unload.assert_awaited_once()


@pytest.mark.asyncio
async def test_transcriber_falls_back_when_diarizer_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Diarizer exception must not prevent transcript from being delivered."""
    stop_event = asyncio.Event()
    frames = np.ones((1600, 1), dtype=np.float32)

    class _RecordingWs:
        closed = False
        sent: list[dict] = []

        async def send(self, data: str) -> None:
            msg = json.loads(data)
            self.sent.append(msg)
            if msg.get("type") == "transcript":
                stop_event.set()

        async def close(self) -> None:
            self.closed = True

    ws = _RecordingWs()
    engine = _make_engine(
        monkeypatch, ws, [frames, None], backend_text="tekst ondanks diarizer crash", disable_diarizer=True
    )

    mock_diarizer = MagicMock()
    mock_diarizer.load = AsyncMock()
    mock_diarizer.diarize_chunk = AsyncMock(side_effect=RuntimeError("GPU out of memory"))
    mock_diarizer.unload = AsyncMock()
    engine._diarizer = mock_diarizer  # noqa: SLF001

    await engine.run(stop_event)

    transcript_events = [m for m in ws.sent if m.get("type") == "transcript"]
    assert len(transcript_events) == 1
    assert transcript_events[0]["text"] == "tekst ondanks diarizer crash"

    # No speaker_segments event since diarizer errored
    speaker_events = [m for m in ws.sent if m.get("type") == "speaker_segments"]
    assert len(speaker_events) == 0
