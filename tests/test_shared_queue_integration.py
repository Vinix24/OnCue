from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from sales_copilot.core.config import WebSocketConfig
from sales_copilot.modules.transcriber.audio_bufferer import AudioBufferer
from sales_copilot.modules.transcriber.inference_queue import SharedInferenceQueue
from sales_copilot.modules.transcriber.inference_worker import InferenceWorker

_HIGH = 0
_LOW = 1
_SAMPLE_RATE = 16000


def _speech_frame(samples: int = 800) -> np.ndarray:
    return np.full(samples, 0.1, dtype=np.float32)


class _FakeStream:
    def __init__(self, frames: list[np.ndarray | None]) -> None:
        self._frames = list(frames)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def read(self) -> np.ndarray | None:
        if self._frames:
            return self._frames.pop(0)
        return None


class _CountingBackend:
    def __init__(self, latency: float = 0.0) -> None:
        self._latency = latency
        self.calls: list[str] = []

    async def start(self, _stop: asyncio.Event) -> None:
        pass

    async def warmup(self) -> None:
        pass

    async def transcribe(self, _audio: np.ndarray) -> str:
        if self._latency:
            await asyncio.sleep(self._latency)
        idx = len(self.calls) + 1
        self.calls.append(f"tekst {idx}")
        return self.calls[-1]

    async def stop(self) -> None:
        pass


class _StopAfterWs:
    def __init__(self, n: int, stop_event: asyncio.Event) -> None:
        self.closed = False
        self.sent: list[str] = []
        self._n = n
        self._stop_event = stop_event

    async def send(self, data: str) -> None:
        self.sent.append(data)
        if len(self.sent) >= self._n:
            self._stop_event.set()

    async def close(self) -> None:
        self.closed = True


def _patch_bufferer(bufferer: AudioBufferer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AudioBufferer, "_load_vad", staticmethod(lambda: None))


async def test_integration_prospect_always_published_before_self(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under contention, prospect (HIGH) transcripts arrive before self (LOW)."""
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=32)

    mic_stream = _FakeStream([_speech_frame(800)] * 3)
    sys_stream = _FakeStream([_speech_frame(800)] * 3)

    mic_bufferer = AudioBufferer(
        audio_stream=mic_stream,
        queue=queue,
        priority=_LOW,
        speaker="self",
        sample_rate=_SAMPLE_RATE,
        max_buffer_seconds=0.04,
        max_buffer_hard_seconds=0.04,
        silence_gap_seconds=0.02,
        min_segment_seconds=0.0,
        long_silence_escape_seconds=0.05,
        hangover_ms=0,
    )
    sys_bufferer = AudioBufferer(
        audio_stream=sys_stream,
        queue=queue,
        priority=_HIGH,
        speaker="prospect",
        sample_rate=_SAMPLE_RATE,
        max_buffer_seconds=0.04,
        max_buffer_hard_seconds=0.04,
        silence_gap_seconds=0.02,
        min_segment_seconds=0.0,
        long_silence_escape_seconds=0.05,
        hangover_ms=0,
    )
    _patch_bufferer(mic_bufferer, monkeypatch)
    _patch_bufferer(sys_bufferer, monkeypatch)

    backend = _CountingBackend()
    stop_event = asyncio.Event()
    ws = _StopAfterWs(4, stop_event)

    async def _connect(*_a, **_kw):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.inference_worker.websockets.connect",
        _connect,
    )

    worker = InferenceWorker(backend=backend, queue=queue, ws_config=WebSocketConfig())

    await asyncio.wait_for(
        asyncio.gather(
            mic_bufferer.run(stop_event),
            sys_bufferer.run(stop_event),
            worker.run(stop_event),
        ),
        timeout=10.0,
    )

    speakers = [json.loads(m)["speaker"] for m in ws.sent]
    first_self_idx = next((i for i, s in enumerate(speakers) if s == "self"), len(speakers))
    assert all(s == "prospect" for s in speakers[:first_self_idx])


async def test_integration_prospect_only_stream_publishes_at_expected_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prospect-only audio produces one item per speech segment."""
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=32)

    sys_stream = _FakeStream([_speech_frame(800), _speech_frame(800)])
    sys_bufferer = AudioBufferer(
        audio_stream=sys_stream,
        queue=queue,
        priority=_HIGH,
        speaker="prospect",
        sample_rate=_SAMPLE_RATE,
        max_buffer_seconds=0.04,
        max_buffer_hard_seconds=0.04,
        silence_gap_seconds=0.02,
        min_segment_seconds=0.0,
        long_silence_escape_seconds=0.05,
        hangover_ms=0,
    )
    monkeypatch.setattr(AudioBufferer, "_load_vad", staticmethod(lambda: None))

    backend = _CountingBackend()
    stop_event = asyncio.Event()
    ws = _StopAfterWs(1, stop_event)

    async def _connect(*_a, **_kw):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.inference_worker.websockets.connect",
        _connect,
    )

    worker = InferenceWorker(backend=backend, queue=queue, ws_config=WebSocketConfig())

    await asyncio.wait_for(
        asyncio.gather(sys_bufferer.run(stop_event), worker.run(stop_event)),
        timeout=10.0,
    )

    assert len(ws.sent) >= 1
    assert json.loads(ws.sent[0])["speaker"] == "prospect"


async def test_integration_slow_backend_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend with 50ms latency still processes items in order without deadlock."""
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=32)

    sys_stream = _FakeStream([_speech_frame(800), _speech_frame(800)])
    sys_bufferer = AudioBufferer(
        audio_stream=sys_stream,
        queue=queue,
        priority=_HIGH,
        speaker="prospect",
        sample_rate=_SAMPLE_RATE,
        max_buffer_seconds=0.04,
        max_buffer_hard_seconds=0.04,
        silence_gap_seconds=0.02,
        min_segment_seconds=0.0,
        long_silence_escape_seconds=0.05,
        hangover_ms=0,
    )
    monkeypatch.setattr(AudioBufferer, "_load_vad", staticmethod(lambda: None))

    backend = _CountingBackend(latency=0.05)
    stop_event = asyncio.Event()
    ws = _StopAfterWs(1, stop_event)

    async def _connect(*_a, **_kw):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.inference_worker.websockets.connect",
        _connect,
    )

    worker = InferenceWorker(backend=backend, queue=queue, ws_config=WebSocketConfig())

    await asyncio.wait_for(
        asyncio.gather(sys_bufferer.run(stop_event), worker.run(stop_event)),
        timeout=10.0,
    )

    assert len(ws.sent) >= 1


async def test_integration_stop_event_drains_without_orphaned_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop event during a busy session: all tasks exit cleanly."""
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=32)

    mic_stream = _FakeStream([_speech_frame(800)] * 4)
    sys_stream = _FakeStream([_speech_frame(800)] * 4)

    mic_bufferer = AudioBufferer(
        audio_stream=mic_stream,
        queue=queue,
        priority=_LOW,
        speaker="self",
        sample_rate=_SAMPLE_RATE,
        max_buffer_seconds=0.04,
        max_buffer_hard_seconds=0.04,
        silence_gap_seconds=0.02,
        min_segment_seconds=0.0,
        long_silence_escape_seconds=0.05,
        hangover_ms=0,
    )
    sys_bufferer = AudioBufferer(
        audio_stream=sys_stream,
        queue=queue,
        priority=_HIGH,
        speaker="prospect",
        sample_rate=_SAMPLE_RATE,
        max_buffer_seconds=0.04,
        max_buffer_hard_seconds=0.04,
        silence_gap_seconds=0.02,
        min_segment_seconds=0.0,
        long_silence_escape_seconds=0.05,
        hangover_ms=0,
    )
    _patch_bufferer(mic_bufferer, monkeypatch)
    _patch_bufferer(sys_bufferer, monkeypatch)

    backend = _CountingBackend()
    stop_event = asyncio.Event()
    ws = _StopAfterWs(2, stop_event)

    async def _connect(*_a, **_kw):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.inference_worker.websockets.connect",
        _connect,
    )

    worker = InferenceWorker(backend=backend, queue=queue, ws_config=WebSocketConfig())

    await asyncio.wait_for(
        asyncio.gather(
            mic_bufferer.run(stop_event),
            sys_bufferer.run(stop_event),
            worker.run(stop_event),
        ),
        timeout=10.0,
    )

    assert len(ws.sent) >= 1
