from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from sales_copilot.core.config import WebSocketConfig
from sales_copilot.modules.transcriber.inference_queue import InferenceQueueItem, SharedInferenceQueue
from sales_copilot.modules.transcriber.inference_worker import InferenceWorker
from sales_copilot.modules.transcriber.normalize import NormalizationLists

_HIGH = 0
_LOW = 1


def _item(priority: int, speaker: str, text_hint: int = 0) -> InferenceQueueItem:
    audio = np.full(1600, float(text_hint), dtype=np.float32)
    return InferenceQueueItem(
        priority=priority,
        enqueued_at_ms=text_hint,
        audio=audio,
        speaker=speaker,
        start_ms=text_hint * 100,
        end_ms=text_hint * 100 + 500,
    )


class _FakeBackend:
    def __init__(self, text: str = "hallo wereld") -> None:
        self._text = text
        self.calls: list[np.ndarray] = []

    async def start(self, _stop: asyncio.Event) -> None:
        pass

    async def warmup(self) -> None:
        pass

    async def transcribe(self, audio: np.ndarray) -> str:
        self.calls.append(audio)
        return self._text

    async def stop(self) -> None:
        pass


class _CountingBackend(_FakeBackend):
    """Returns a unique string per call so deduplication never fires."""

    async def transcribe(self, audio: np.ndarray) -> str:
        self.calls.append(audio)
        return f"tekst {len(self.calls)}"


class _RaisingBackend(_FakeBackend):
    """Raises on first call, succeeds on subsequent calls."""

    async def transcribe(self, audio: np.ndarray) -> str:
        self.calls.append(audio)
        if len(self.calls) == 1:
            raise RuntimeError("backend exploded")
        return f"tekst {len(self.calls)}"


class _FakeWs:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _StopAfterWs(_FakeWs):
    def __init__(self, n: int, stop_event: asyncio.Event) -> None:
        super().__init__()
        self._n = n
        self._stop_event = stop_event

    async def send(self, data: str) -> None:
        await super().send(data)
        if len(self.sent) >= self._n:
            self._stop_event.set()


def _make_worker(backend: _FakeBackend) -> InferenceWorker:
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=32)
    return InferenceWorker(backend=backend, queue=queue, ws_config=WebSocketConfig())


async def test_worker_calls_backend_transcribe_exactly_once_per_item() -> None:
    backend = _FakeBackend()
    worker = _make_worker(backend)
    ws = _FakeWs()
    worker._ws = ws  # noqa: SLF001

    await worker._process_item(_item(_HIGH, "prospect", 0))  # noqa: SLF001

    assert len(backend.calls) == 1


async def test_worker_publishes_correct_speaker() -> None:
    backend = _FakeBackend(text="goed gesprek")
    worker = _make_worker(backend)
    ws = _FakeWs()
    worker._ws = ws  # noqa: SLF001

    await worker._process_item(_item(_HIGH, "prospect", 1))  # noqa: SLF001

    # partial_transcript emitted before transcription, then final transcript
    messages = [json.loads(m) for m in ws.sent]
    assert len(messages) == 2
    assert messages[0]["type"] == "partial_transcript"
    final = messages[1]
    assert final["type"] == "transcript"
    assert final["speaker"] == "prospect"


async def test_worker_publishes_correct_timing() -> None:
    backend = _FakeBackend(text="timing check")
    worker = _make_worker(backend)
    ws = _FakeWs()
    worker._ws = ws  # noqa: SLF001

    item = _item(_HIGH, "prospect", 5)
    await worker._process_item(item)  # noqa: SLF001

    # final transcript is the last sent message
    final = json.loads(ws.sent[-1])
    assert final["type"] == "transcript"
    assert final["start_ms"] == item.start_ms
    assert final["end_ms"] == item.end_ms


async def test_worker_hallucination_filter_drops_tv_gelderland() -> None:
    backend = _FakeBackend(text="TV Gelderland")
    worker = _make_worker(backend)
    ws = _FakeWs()
    worker._ws = ws  # noqa: SLF001

    await worker._process_item(_item(_HIGH, "prospect", 1))  # noqa: SLF001

    # partial is sent before transcription; hallucination filter prevents final
    messages = [json.loads(m) for m in ws.sent]
    assert len(messages) == 1
    assert messages[0]["type"] == "partial_transcript"


async def test_worker_backend_exception_is_logged_and_worker_continues() -> None:
    backend = _RaisingBackend()
    worker = _make_worker(backend)
    ws = _FakeWs()
    worker._ws = ws  # noqa: SLF001

    # item 1 raises: partial sent, no final
    await worker._process_item(_item(_HIGH, "prospect", 1))  # noqa: SLF001
    # item 2 succeeds: partial + final
    await worker._process_item(_item(_HIGH, "prospect", 2))  # noqa: SLF001

    assert len(backend.calls) == 2
    messages = [json.loads(m) for m in ws.sent]
    final_events = [m for m in messages if m["type"] == "transcript"]
    assert len(final_events) == 1


async def test_worker_hung_transcribe_is_bounded_and_dropped() -> None:
    class _HangingBackend(_FakeBackend):
        async def transcribe(self, audio: np.ndarray) -> str:
            self.calls.append(audio)
            await asyncio.sleep(10)
            return "nooit gepubliceerd"

    backend = _HangingBackend()
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=32)
    worker = InferenceWorker(
        backend=backend,
        queue=queue,
        ws_config=WebSocketConfig(),
        inference_timeout_s=0.05,
    )
    ws = _FakeWs()
    worker._ws = ws  # noqa: SLF001

    # Bounded: even though the backend hangs for 10s, the per-item timeout returns.
    await asyncio.wait_for(worker._process_item(_item(_HIGH, "prospect", 1)), timeout=2.0)  # noqa: SLF001

    assert len(backend.calls) == 1
    messages = [json.loads(m) for m in ws.sent]
    # Partial is emitted before transcription; the hang times out so no final transcript.
    assert all(m["type"] == "partial_transcript" for m in messages)
    assert not any(m["type"] == "transcript" for m in messages)


async def test_worker_stop_event_fires_during_inflight_item_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = asyncio.Event()

    class _SlowBackend(_FakeBackend):
        async def transcribe(self, audio: np.ndarray) -> str:
            await asyncio.sleep(0.05)
            stop_event.set()
            return "trage tekst"

    backend = _SlowBackend()
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=32)
    worker = InferenceWorker(backend=backend, queue=queue, ws_config=WebSocketConfig())

    ws = _FakeWs()

    async def _connect(*_a, **_kw):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.inference_worker.websockets.connect",
        _connect,
    )

    await queue.put(_item(_HIGH, "prospect", 1))

    await asyncio.wait_for(worker.run(stop_event), timeout=5.0)

    # partial sent before transcription, final sent after transcription completes
    messages = [json.loads(m) for m in ws.sent]
    assert any(m["type"] == "partial_transcript" for m in messages)
    assert any(m["type"] == "transcript" for m in messages)


async def test_worker_empty_queue_stop_event_exits_quickly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _FakeBackend()
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=32)
    worker = InferenceWorker(backend=backend, queue=queue, ws_config=WebSocketConfig())

    stop_event = asyncio.Event()
    stop_event.set()

    ws = _FakeWs()

    async def _connect(*_a, **_kw):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.inference_worker.websockets.connect",
        _connect,
    )

    await asyncio.wait_for(worker.run(stop_event), timeout=1.0)


async def test_worker_publishes_normalized_text(caplog: pytest.LogCaptureFixture) -> None:
    backend = _FakeBackend(text="we werken met HupSpot vandaag")
    worker = _make_worker(backend)
    worker._normalization_lists = NormalizationLists(  # noqa: SLF001
        enabled=True, terms=(), variants={"HupSpot": "HubSpot"}
    )
    ws = _FakeWs()
    worker._ws = ws  # noqa: SLF001

    with caplog.at_level("DEBUG", logger="sales_copilot.modules.transcriber.inference_worker"):
        await worker._process_item(_item(_HIGH, "prospect", 1))  # noqa: SLF001

    final = json.loads(ws.sent[-1])
    assert final["text"] == "we werken met HubSpot vandaag"
    # Payload schema is unchanged: no second text field carrying the original.
    assert set(final.keys()) == {"type", "text", "speaker", "start_ms", "end_ms", "is_final"}
    assert "HupSpot" not in json.dumps(final)
    # The original wording only ever reaches the local DEBUG log, never a payload.
    assert any("HupSpot" in record.getMessage() for record in caplog.records)


async def test_worker_dedup_uses_normalized_text(caplog: pytest.LogCaptureFixture) -> None:
    """Two chunks that normalize to the same text must be deduplicated."""

    class _AlternatingBackend(_FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self._texts = ["HupSpot gesprek", "Ruffspot gesprek"]

        async def transcribe(self, audio: np.ndarray) -> str:
            self.calls.append(audio)
            return self._texts[len(self.calls) - 1]

    backend = _AlternatingBackend()
    worker = _make_worker(backend)
    worker._normalization_lists = NormalizationLists(  # noqa: SLF001
        enabled=True, terms=(), variants={"HupSpot": "HubSpot", "Ruffspot": "HubSpot"}
    )
    ws = _FakeWs()
    worker._ws = ws  # noqa: SLF001

    await worker._process_item(_item(_HIGH, "prospect", 1))  # noqa: SLF001
    await worker._process_item(_item(_HIGH, "prospect", 2))  # noqa: SLF001

    messages = [json.loads(m) for m in ws.sent]
    final_events = [m for m in messages if m["type"] == "transcript"]
    assert len(final_events) == 1, "Both chunks normalize to the same text; the second is a dedup, not a new final."


async def test_worker_run_processes_high_priority_before_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _CountingBackend()
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=32)
    worker = InferenceWorker(backend=backend, queue=queue, ws_config=WebSocketConfig())

    stop_event = asyncio.Event()
    ws = _StopAfterWs(2, stop_event)

    async def _connect(*_a, **_kw):
        return ws

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.inference_worker.websockets.connect",
        _connect,
    )

    await queue.put(_item(_LOW, "self", 1))
    await queue.put(_item(_HIGH, "prospect", 2))

    await asyncio.wait_for(worker.run(stop_event), timeout=5.0)

    assert len(ws.sent) == 2
    first = json.loads(ws.sent[0])
    assert first["speaker"] == "prospect"


_CALL_TERMS_BASE = NormalizationLists(enabled=True, terms=(), variants={"VWA": "Fixed-VBA"})


async def test_worker_call_terms_reach_normalize_and_take_precedence() -> None:
    backend = _FakeBackend(text="de VWA sheet")
    worker = _make_worker(backend)
    worker._base_normalization_lists = _CALL_TERMS_BASE  # noqa: SLF001
    worker.set_call_terms(())
    ws = _FakeWs()
    worker._ws = ws  # noqa: SLF001

    await worker._process_item(_item(_HIGH, "prospect", 1))  # noqa: SLF001
    assert json.loads(ws.sent[-1])["text"] == "de Fixed-VBA sheet"

    worker.set_call_terms(("VWA -> VBA",))
    await worker._process_item(_item(_HIGH, "prospect", 2))  # noqa: SLF001
    final = json.loads(ws.sent[-1])
    assert final["text"] == "de VBA sheet"
    assert set(final.keys()) == {"type", "text", "speaker", "start_ms", "end_ms", "is_final"}

    worker.set_call_terms(())
    await worker._process_item(_item(_HIGH, "prospect", 3))  # noqa: SLF001
    assert json.loads(ws.sent[-1])["text"] == "de Fixed-VBA sheet"
