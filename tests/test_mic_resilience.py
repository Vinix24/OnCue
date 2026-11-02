"""MicStream keeps the microphone track on the clock through device loss and pauses."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import sales_copilot.audio.capture as cap
from sales_copilot.audio.capture import AudioConfig, MicStream
from sales_copilot.audio.tap_health import TapAttachState

CHUNK_S = 0.1  # 1600 frames at 16 kHz


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakeInputStream:
    def __init__(self, owner: _FakeSd, *, callback, device) -> None:  # noqa: ANN001
        self.owner = owner
        self.callback = callback
        self.device = device
        self.started = False
        self.closed = False

    def start(self) -> None:
        if self.owner.fail_opens > 0:
            self.owner.fail_opens -= 1
            raise RuntimeError("device unavailable")
        self.started = True
        if self.owner.on_start is not None:
            self.owner.on_start(self)

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True

    def block(self) -> None:
        self.callback(np.full((1600, 1), 0.1, dtype="float32"), 1600, None, None)


class _FakeSd:
    """Fake sounddevice whose device list can be renumbered."""

    def __init__(self, devices: list[dict]) -> None:
        self.devices = devices
        self.default = SimpleNamespace(device=(0, -1))
        self.streams: list[_FakeInputStream] = []
        self.fail_opens = 0
        self.on_start = None  # runs inside stream.start(), like an early PortAudio callback

    def _terminate(self) -> None:
        pass

    def _initialize(self) -> None:
        pass

    def query_devices(self, device=None, kind=None):  # noqa: ANN001
        if device is None:
            return [dict(d) for d in self.devices]
        if isinstance(device, int):
            return dict(self.devices[device])
        raise ValueError(device)

    def InputStream(self, **kwargs):  # noqa: N802, ANN003
        stream = _FakeInputStream(self, callback=kwargs["callback"], device=kwargs["device"])
        self.streams.append(stream)
        return stream

    @property
    def live(self) -> _FakeInputStream:
        return self.streams[-1]


def _dev(index: int, name: str) -> dict:
    return {"index": index, "name": name, "hostapi": 0, "max_input_channels": 1}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(cap, "_active_stream_count", 0, raising=False)
    monkeypatch.setattr(cap, "_live_input_names", lambda: None)
    yield
    monkeypatch.setattr(cap, "_active_stream_count", 0, raising=False)


def _make(monkeypatch, sd: _FakeSd, *, device=None, clock: _Clock | None = None, **kwargs) -> tuple[MicStream, _Clock]:
    clock = clock or _Clock()
    monkeypatch.setattr(cap, "_import_sounddevice", lambda: sd)
    config = AudioConfig(sample_rate=16000, channels=1, dtype="float32", chunk_size=1600, capture_method="mic")
    mic = MicStream(
        config,
        device=device,
        clock=clock,
        watchdog_poll_s=0.01,
        queue_maxsize=10_000,
        **kwargs,
    )
    return mic, clock


def _park_watchdog(mic: MicStream) -> None:
    """Stop the real watchdog thread so tests drive ``_watchdog_step`` by hand."""
    mic._stop_event.set()
    assert mic._watchdog is not None
    mic._watchdog.join(timeout=2)
    mic._stop_event = threading.Event()


def _drain(mic: MicStream) -> list[np.ndarray]:
    out = []
    while (chunk := mic.read()) is not None:
        out.append(chunk)
    return out


def _tick(sd: _FakeSd, mic: MicStream, clock: _Clock, blocks: int) -> None:
    for _ in range(blocks):
        clock.now = round(clock.now + CHUNK_S, 6)
        sd.live.block()


def _start(monkeypatch, sd=None, **kwargs):
    sd = sd or _FakeSd([_dev(0, "Built-in Mic")])
    mic, clock = _make(monkeypatch, sd, **kwargs)
    mic.start()
    _park_watchdog(mic)
    return sd, mic, clock


def _is_silence(chunk: np.ndarray) -> bool:
    return chunk.shape == (1600, 1) and chunk.dtype == np.float32 and not chunk.any()


def test_short_gap_is_padded_with_exact_zero_chunks(monkeypatch) -> None:
    sd, mic, clock = _start(monkeypatch)
    _tick(sd, mic, clock, 10)
    _drain(mic)

    clock.now += 3.0
    sd.live.block()
    chunks = _drain(mic)

    assert len(chunks) == 30  # 29 silence (40 expected - 10 delivered - 1 real) + the real block
    assert all(_is_silence(c) for c in chunks[:-1])
    assert chunks[-1].any()
    mic.stop()


def test_half_second_pause_is_not_padded(monkeypatch) -> None:
    sd, mic, clock = _start(monkeypatch)
    _tick(sd, mic, clock, 10)
    _drain(mic)

    clock.now += 0.5
    sd.live.block()
    chunks = _drain(mic)

    assert len(chunks) == 1
    assert chunks[0].any()
    mic.stop()


def test_healthy_stream_never_pads_or_trips_watchdog(monkeypatch) -> None:
    sd, mic, clock = _start(monkeypatch)
    for _ in range(50):
        _tick(sd, mic, clock, 1)
        mic._watchdog_step()
    chunks = _drain(mic)

    assert len(chunks) == 50
    assert all(c.any() for c in chunks)
    assert len(sd.streams) == 1
    assert mic.tap_health().attach_state is TapAttachState.ATTACHED
    mic.stop()


def test_watchdog_not_armed_before_first_block(monkeypatch) -> None:
    sd, mic, clock = _start(monkeypatch)
    clock.now += 30.0
    mic._watchdog_step()

    assert len(sd.streams) == 1
    assert not sd.live.closed
    mic.stop()


def test_lost_device_is_closed_padded_at_clock_pace_and_reopened(monkeypatch) -> None:
    sd, mic, clock = _start(monkeypatch, device=0)
    _tick(sd, mic, clock, 10)
    _drain(mic)
    dead = sd.live

    sd.fail_opens = 1000
    clock.now += 2.5
    mic._watchdog_step()

    assert dead.closed
    assert cap._active_portaudio_stream_count() == 0
    assert mic.tap_health().attach_state is TapAttachState.DETACHED
    # Track brought up to the clock at the moment of loss: 35 chunks expected, 10 delivered.
    assert len(_drain(mic)) == 25

    clock.now += 1.0  # retry fails again, silence continues at real-time pace
    mic._watchdog_step()
    assert len(_drain(mic)) == 10

    sd.fail_opens = 0
    clock.now += 1.0
    mic._watchdog_step()
    assert len(_drain(mic)) == 10
    assert mic.tap_health().attach_state is TapAttachState.ATTACHED
    assert cap._active_portaudio_stream_count() == 1

    _tick(sd, mic, clock, 1)
    chunks = _drain(mic)
    assert len(chunks) == 1 and chunks[0].any()
    mic.stop()
    assert cap._active_portaudio_stream_count() == 0


def test_reopen_finds_configured_device_by_name_after_renumbering(monkeypatch) -> None:
    sd = _FakeSd([_dev(0, "Built-in Mic"), _dev(1, "Headset")])
    sd, mic, clock = _start(monkeypatch, sd, device=1)
    _tick(sd, mic, clock, 5)
    _drain(mic)

    # Headset gone and back: another microphone now owns index 1.
    sd.devices = [_dev(0, "Built-in Mic"), _dev(1, "USB Interface"), _dev(2, "Headset")]
    clock.now += 2.5
    mic._watchdog_step()

    assert sd.live.device == 2
    assert mic.device_label == "Headset"
    assert not mic._on_fallback
    mic.stop()


def test_absent_configured_device_falls_back_to_default_input(monkeypatch) -> None:
    sd = _FakeSd([_dev(0, "Built-in Mic"), _dev(1, "Headset")])
    sd, mic, clock = _start(monkeypatch, sd, device=1)
    _tick(sd, mic, clock, 5)

    sd.devices = [_dev(0, "Built-in Mic")]
    clock.now += 2.5
    mic._watchdog_step()

    assert sd.live.device == 0
    assert mic.device_label == "Built-in Mic"
    assert mic._on_fallback
    assert mic.tap_health().attach_state is TapAttachState.ATTACHED
    mic.stop()


def test_switches_back_when_original_returns(monkeypatch) -> None:
    sd = _FakeSd([_dev(0, "Built-in Mic"), _dev(1, "Headset (Long Vendor Name 12345)")])
    sd, mic, clock = _start(monkeypatch, sd, device=1)
    _tick(sd, mic, clock, 5)
    sd.devices = [_dev(0, "Built-in Mic")]
    clock.now += 2.5
    mic._watchdog_step()
    fallback = sd.live
    assert mic._on_fallback

    # Not back yet: nothing happens.
    monkeypatch.setattr(cap, "_live_input_names", lambda: ["Built-in Mic"])
    clock.now += 2.5
    sd.live.block()
    mic._watchdog_step()
    assert sd.live is fallback and not fallback.closed

    # Back (truncated to 31 chars by MME in the PortAudio list; full in soundcard).
    sd.devices = [_dev(0, "Built-in Mic"), _dev(1, "Headset (Long Vendor Name 12345)"[:31])]
    monkeypatch.setattr(cap, "_live_input_names", lambda: ["Built-in Mic", "Headset (Long Vendor Name 12345)"])
    clock.now += 2.5
    sd.live.block()
    mic._watchdog_step()

    assert fallback.closed
    assert sd.live.device == 1
    assert not mic._on_fallback
    assert mic.device_label == "Headset (Long Vendor Name 12345)"
    assert cap._active_portaudio_stream_count() == 1
    mic.stop()
    assert cap._active_portaudio_stream_count() == 0


def test_stays_on_fallback_without_live_list(monkeypatch) -> None:
    sd = _FakeSd([_dev(0, "Built-in Mic"), _dev(1, "Headset")])
    sd, mic, clock = _start(monkeypatch, sd, device=1)
    _tick(sd, mic, clock, 5)
    sd.devices = [_dev(0, "Built-in Mic")]
    clock.now += 2.5
    mic._watchdog_step()
    fallback = sd.live

    sd.devices = [_dev(0, "Built-in Mic"), _dev(1, "Headset")]
    for _ in range(3):
        clock.now += 2.5
        sd.live.block()
        mic._watchdog_step()

    assert sd.live is fallback and not fallback.closed
    assert mic._on_fallback
    mic.stop()


def test_failed_switch_back_keeps_a_microphone(monkeypatch) -> None:
    sd = _FakeSd([_dev(0, "Built-in Mic"), _dev(1, "Headset")])
    sd, mic, clock = _start(monkeypatch, sd, device=1)
    _tick(sd, mic, clock, 5)
    sd.devices = [_dev(0, "Built-in Mic")]
    clock.now += 2.5
    mic._watchdog_step()
    assert mic._on_fallback

    sd.devices = [_dev(0, "Built-in Mic"), _dev(1, "Headset")]
    monkeypatch.setattr(cap, "_live_input_names", lambda: ["Built-in Mic", "Headset"])
    original_open = sd.InputStream

    def _fail_original(**kwargs):  # noqa: ANN003
        stream = original_open(**kwargs)
        if kwargs["device"] == 1:
            sd.fail_opens = 1
        return stream

    sd.InputStream = _fail_original
    clock.now += 2.5
    sd.live.block()
    mic._watchdog_step()

    assert sd.live.device == 0 and sd.live.started
    assert mic._on_fallback
    assert cap._active_portaudio_stream_count() == 1
    mic.stop()
    assert cap._active_portaudio_stream_count() == 0


def test_register_count_balanced_when_reopen_fails_twice(monkeypatch) -> None:
    sd, mic, clock = _start(monkeypatch, device=0)
    _tick(sd, mic, clock, 5)
    sd.fail_opens = 2
    clock.now += 2.5
    mic._watchdog_step()
    assert cap._active_portaudio_stream_count() == 0
    clock.now += 1.0
    mic._watchdog_step()
    assert cap._active_portaudio_stream_count() == 0
    clock.now += 1.0
    mic._watchdog_step()
    assert cap._active_portaudio_stream_count() == 1
    mic.stop()
    assert cap._active_portaudio_stream_count() == 0
    assert all(s.closed for s in sd.streams)


def test_stop_during_retry_loop_returns_promptly(monkeypatch) -> None:
    sd = _FakeSd([_dev(0, "Built-in Mic")])
    clock = _Clock()
    mic, _ = _make(
        monkeypatch,
        sd,
        device=0,
        clock=clock,
        stall_threshold_s=0.0,
        reopen_interval_s=1000.0,
    )
    mic.start()
    sd.live.block()
    clock.now += 5.0
    sd.fail_opens = 10_000
    deadline = time.monotonic() + 2
    while mic._outage_since is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert mic._outage_since is not None
    watchdog = mic._watchdog

    began = time.monotonic()
    mic.stop()

    assert time.monotonic() - began < 1.0
    assert watchdog is not None and not watchdog.is_alive()
    assert cap._active_portaudio_stream_count() == 0
    assert all(s.closed or not s.started for s in sd.streams)
    assert not any(t.name == "mic-watchdog" and t.is_alive() for t in threading.enumerate())


def test_device_open_latency_does_not_become_leading_silence(monkeypatch) -> None:
    sd, mic, clock = _start(monkeypatch)
    clock.now = 1.5  # the device took 1.5 s to deliver its first block
    sd.live.block()
    _tick(sd, mic, clock, 2)
    chunks = _drain(mic)

    assert len(chunks) == 3
    assert all(c.any() for c in chunks)
    mic.stop()


def test_block_delivered_inside_stream_start_is_counted(monkeypatch) -> None:
    sd = _FakeSd([_dev(0, "Built-in Mic")])
    clock = _Clock()
    clock.now = 0.1
    sd.on_start = lambda stream: stream.block()
    mic, _ = _make(monkeypatch, sd, clock=clock)
    mic.start()
    _park_watchdog(mic)

    assert mic._last_block_at == 0.1  # watchdog armed by the early block
    assert mic._delivered == 1
    _drain(mic)

    clock.now += 0.9
    sd.live.block()
    chunks = _drain(mic)
    assert len(chunks) == 1 and chunks[0].any()  # no clock debt: 0.9 s pause is not padded

    clock.now += 1.2
    sd.live.block()
    chunks = _drain(mic)
    # Block at t=2.2: 22 expected - 2 delivered - 1 real = 19 silence chunks + the real block.
    assert len(chunks) == 20
    assert all(_is_silence(c) for c in chunks[:-1])
    assert chunks[-1].any()
    mic.stop()


def test_reopen_keeps_the_original_clock_origin(monkeypatch) -> None:
    sd, mic, clock = _start(monkeypatch, device=0)
    clock.now = 2.0  # slow open: first block arrives 2 s after start()
    sd.live.block()
    origin = mic._t0
    assert origin == pytest.approx(1.9)
    _tick(sd, mic, clock, 9)

    sd.fail_opens = 1000
    clock.now += 2.5
    mic._watchdog_step()
    sd.fail_opens = 0
    clock.now += 1.0
    mic._watchdog_step()
    _tick(sd, mic, clock, 1)

    assert mic._t0 == origin
    elapsed_chunks = int((clock.now - origin) / CHUNK_S + 1e-9)
    assert len(_drain(mic)) == elapsed_chunks
    mic.stop()
