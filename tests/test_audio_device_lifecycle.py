"""Tests for the PortAudio device-lifecycle hardening (PR-V3-AUDIO-DEVICE-FIX).

Covers:
- PortAudio re-enumeration (sd._terminate/_initialize) at session start, and the
  guard that suppresses re-init while any stream is still open.
- Robust teardown that never lets a dead device raise out of stop().
- The mic/prospect stream sanity check that broadcasts an audio_warning WS event
  when a stream delivers zero chunks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

import sales_copilot.audio.blackhole as bh
import sales_copilot.audio.capture as cap
from sales_copilot.audio.blackhole import BlackHoleStream
from sales_copilot.audio.capture import AudioConfig, MicStream, check_streams_liveness


class _FakeInputStream:
    def __init__(self, *, callback, fail_stop=False, fail_close=False, **kwargs):  # noqa: ANN001
        self.callback = callback
        self.started = False
        self.closed = False
        self._fail_stop = fail_stop
        self._fail_close = fail_close

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        if self._fail_stop:
            raise RuntimeError("PaMacCore (AUHAL) err='!obj'")
        self.started = False

    def close(self) -> None:
        if self._fail_close:
            raise RuntimeError("-10851 Invalid Property Value")
        self.closed = True


def _make_fake_sd(**input_stream_kwargs):
    counters = {"terminate": 0, "initialize": 0}

    def _terminate() -> None:
        counters["terminate"] += 1

    def _initialize() -> None:
        counters["initialize"] += 1

    def _input_stream(**kwargs):  # noqa: ANN001, ANN003
        return _FakeInputStream(**{**kwargs, **input_stream_kwargs})

    fake_sd = SimpleNamespace(
        InputStream=_input_stream,
        _terminate=_terminate,
        _initialize=_initialize,
    )
    return fake_sd, counters


@pytest.fixture(autouse=True)
def _reset_stream_count(monkeypatch):
    """Each test starts from a clean open-stream count."""

    monkeypatch.setattr(cap, "_active_stream_count", 0, raising=False)
    yield
    monkeypatch.setattr(cap, "_active_stream_count", 0, raising=False)


def _config() -> AudioConfig:
    return AudioConfig(sample_rate=16000, channels=1, dtype="float32", chunk_size=4, capture_method="mic")


def test_reinit_fires_on_session_start(monkeypatch) -> None:
    fake_sd, counters = _make_fake_sd()
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    stream = MicStream(_config())
    stream.start()

    assert counters["terminate"] == 1
    assert counters["initialize"] == 1
    assert cap._active_portaudio_stream_count() == 1

    stream.stop()
    assert cap._active_portaudio_stream_count() == 0


def test_no_reinit_while_a_stream_is_open(monkeypatch) -> None:
    fake_sd, counters = _make_fake_sd()
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)
    monkeypatch.setattr(bh, "_import_sounddevice", lambda: fake_sd)

    mic = MicStream(_config())
    mic.start()
    assert counters["terminate"] == 1

    # Opening a second stream while the first is still open must NOT re-init
    # PortAudio (terminating with live streams crashes it).
    prospect = BlackHoleStream(_config(), device_name="BlackHole 2ch")
    prospect.start()
    assert counters["terminate"] == 1
    assert cap._active_portaudio_stream_count() == 2

    mic.stop()
    prospect.stop()
    assert cap._active_portaudio_stream_count() == 0


def test_reinit_again_after_all_streams_closed(monkeypatch) -> None:
    fake_sd, counters = _make_fake_sd()
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    stream = MicStream(_config())
    stream.start()
    stream.stop()
    assert counters["terminate"] == 1

    # New session after the previous one fully closed: re-enumerate again.
    stream2 = MicStream(_config())
    stream2.start()
    assert counters["terminate"] == 2
    stream2.stop()


def test_failed_open_does_not_leak_stream_count(monkeypatch) -> None:
    counters = {"terminate": 0, "initialize": 0}

    def _terminate() -> None:
        counters["terminate"] += 1

    def _initialize() -> None:
        counters["initialize"] += 1

    def _raising_input_stream(**kwargs):  # noqa: ANN001, ANN003
        raise RuntimeError("-10851 Invalid Property Value")

    fake_sd = SimpleNamespace(
        InputStream=_raising_input_stream,
        _terminate=_terminate,
        _initialize=_initialize,
    )
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    stream = MicStream(_config())
    with pytest.raises(RuntimeError):
        stream.start()

    # A failed open must release its slot so the next session can re-init.
    assert cap._active_portaudio_stream_count() == 0


def test_teardown_swallows_dead_device_exceptions(monkeypatch) -> None:
    fake_sd, _ = _make_fake_sd(fail_stop=True, fail_close=True)
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    stream = MicStream(_config())
    stream.start()

    # A dead device raising on stop/close must never escape to the orchestrator.
    stream.stop()

    assert stream._stream is None  # noqa: SLF001
    assert cap._active_portaudio_stream_count() == 0


class _FakeLiveStream:
    def __init__(self, chunks: int, device_label: str = "ULT WEAR") -> None:
        self.chunks_received = chunks
        self.device_label = device_label


def test_sanity_check_warns_on_silent_stream() -> None:
    events: list[dict] = []

    async def _broadcast(payload: dict) -> None:
        events.append(payload)

    silent_self = _FakeLiveStream(chunks=0, device_label="ULT WEAR")
    live_prospect = _FakeLiveStream(chunks=12, device_label="BlackHole 2ch")

    flagged = asyncio.run(
        check_streams_liveness(
            [(silent_self, "self"), (live_prospect, "prospect")],
            _broadcast,
            timeout=0.0,
        )
    )

    assert flagged == ["self"]
    assert len(events) == 1
    assert events[0]["type"] == "audio_warning"
    assert events[0]["stream"] == "self"
    assert "ULT WEAR" in events[0]["message"]


def test_sanity_check_silent_when_streams_alive() -> None:
    events: list[dict] = []

    async def _broadcast(payload: dict) -> None:
        events.append(payload)

    live_self = _FakeLiveStream(chunks=5)
    live_prospect = _FakeLiveStream(chunks=8, device_label="BlackHole 2ch")

    flagged = asyncio.run(
        check_streams_liveness(
            [(live_self, "self"), (live_prospect, "prospect")],
            _broadcast,
            timeout=0.0,
        )
    )

    assert flagged == []
    assert events == []


def test_sanity_check_flags_both_streams_when_both_dead() -> None:
    events: list[dict] = []

    async def _broadcast(payload: dict) -> None:
        events.append(payload)

    flagged = asyncio.run(
        check_streams_liveness(
            [
                (_FakeLiveStream(chunks=0), "self"),
                (_FakeLiveStream(chunks=0, device_label="BlackHole 2ch"), "prospect"),
            ],
            _broadcast,
            timeout=0.0,
        )
    )

    assert set(flagged) == {"self", "prospect"}
    assert {e["stream"] for e in events} == {"self", "prospect"}


def test_chunk_counter_increments_on_callback(monkeypatch) -> None:
    fake_sd, _ = _make_fake_sd()
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: fake_sd)

    stream = MicStream(_config())
    stream.start()
    assert stream.chunks_received == 0

    callback = stream._stream.callback  # noqa: SLF001
    callback(np.zeros((4, 1), dtype=np.float32), 4, None, None)
    callback(np.zeros((4, 1), dtype=np.float32), 4, None, None)

    assert stream.chunks_received == 2
    stream.stop()
