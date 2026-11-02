"""AudioTeeStream and BlackHoleStream keep the system track on the clock while detached."""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np
import pytest

import sales_copilot.audio.blackhole as bh
import sales_copilot.audio.capture as cap
from sales_copilot.audio.blackhole import BlackHoleStream
from sales_copilot.audio.capture import AudioConfig, AudioTeeStream
from sales_copilot.audio.tap_health import TapAttachState

CHUNK = 1600  # 0.1 s at 16 kHz
CHUNK_S = 0.1
PCM = (np.full(CHUNK, 3000, dtype=np.int16)).tobytes()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _wait(predicate, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not reached in time")


def _drain(stream) -> list[np.ndarray]:  # noqa: ANN001
    out = []
    while (chunk := stream.read()) is not None:
        out.append(chunk)
    return out


def _is_silence(chunk: np.ndarray) -> bool:
    return chunk.shape == (CHUNK, 1) and chunk.dtype == np.float32 and not chunk.any()


@pytest.fixture(autouse=True)
def _reset_portaudio_count(monkeypatch):
    monkeypatch.setattr(cap, "_active_stream_count", 0, raising=False)
    yield
    monkeypatch.setattr(cap, "_active_stream_count", 0, raising=False)


# --- AudioTee -----------------------------------------------------------------


class _Pipe:
    """Blocking stdout stand-in: ``feed`` pushes bytes, ``close`` is EOF."""

    def __init__(self) -> None:
        self._q: queue.Queue[bytes] = queue.Queue()
        self.closed = False

    def feed(self, data: bytes) -> None:
        self._q.put(data)

    def read(self, n: int) -> bytes:  # noqa: ARG002
        return self._q.get()

    def readline(self) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


class _Proc:
    def __init__(self) -> None:
        self.stdout = _Pipe()
        self.stderr = _Pipe()
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True
        self.stdout.feed(b"")

    def wait(self, timeout=None) -> int:  # noqa: ANN001, ARG002
        return 0

    def kill(self) -> None:
        return

    def die(self) -> None:
        """The subprocess exits on its own: stdout reaches EOF."""
        self.stdout.feed(b"")


class _Popen:
    def __init__(self) -> None:
        self.procs: list[_Proc] = []

    def __call__(self, *args, **kwargs) -> _Proc:  # noqa: ANN002, ANN003, ARG002
        proc = _Proc()
        self.procs.append(proc)
        return proc


def _tee(monkeypatch, tmp_path, **kwargs):
    path = tmp_path / "audiotee"
    path.write_text("fake")
    monkeypatch.setattr(cap, "_find_pid", lambda name: 1)
    popen = _Popen()
    monkeypatch.setattr(cap.subprocess, "Popen", popen)
    clock = _Clock()
    config = AudioConfig(
        sample_rate=16000,
        channels=1,
        dtype="float32",
        chunk_size=CHUNK,
        capture_method="audiotee",
        target_process="Some App",
        audiotee_path=str(path),
    )
    warnings: list[dict] = []
    stream = AudioTeeStream(
        config,
        queue_maxsize=10_000,
        on_warning=warnings.append,
        clock=clock,
        watchdog_poll_s=3600.0,  # the tests call _watchdog_step by hand
        **kwargs,
    )
    stream.start()
    return stream, popen, clock, warnings


def _feed(stream: AudioTeeStream, proc: _Proc, clock: _Clock, at: float, count: int = 1) -> None:
    for i in range(count):
        before = stream.chunks_received
        clock.now = round(at + i * CHUNK_S, 6)
        proc.stdout.feed(PCM)
        _wait(lambda before=before: stream.chunks_received == before + 1)


def test_audiotee_gap_is_filled_to_within_one_chunk_of_the_clock(monkeypatch, tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    stream, popen, clock, _ = _tee(monkeypatch, tmp_path)
    _feed(stream, popen.procs[0], clock, 0.1, 10)  # real audio until t=1.0
    popen.procs[0].die()
    _wait(lambda: stream._needs_restart)  # noqa: SLF001

    # The outage is filled at real-time pace: ten chunks per simulated second.
    clock.now = 2.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(_drain(stream)) == 10 + 10
    clock.now = 3.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(_drain(stream)) == 10
    clock.now = 4.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(popen.procs) == 2  # restarted
    clock.now = 4.0
    middle = _drain(stream)
    assert len(middle) == 10 and all(_is_silence(c) for c in middle)

    _feed(stream, popen.procs[1], clock, 4.1)
    tail = _drain(stream)
    assert len(tail) == 1 and tail[0].any()
    # 10 real + 30 silence + 1 real = the 41 chunks that 4.1 s of clock holds.
    assert stream._track._delivered == 41  # noqa: SLF001
    assert any("AudioTee tap lost:" in r.message for r in caplog.records)
    recovered = [r.message for r in caplog.records if "AudioTee tap re-attached:" in r.message]
    assert len(recovered) == 1 and "after 3.1s of silence filled." in recovered[0]
    assert stream.tap_health().attach_state is TapAttachState.ATTACHED
    stream.stop()


def test_audiotee_only_one_loss_warning_per_outage(monkeypatch, tmp_path) -> None:
    stream, popen, clock, warnings = _tee(monkeypatch, tmp_path)
    _feed(stream, popen.procs[0], clock, 0.1, 3)
    popen.procs[0].die()
    _wait(lambda: stream._needs_restart)  # noqa: SLF001
    assert len(warnings) == 1
    assert "automatisch opnieuw gestart" in warnings[0]["message"]
    stream.stop()


def test_audiotee_restart_backs_off_while_the_subprocess_keeps_dying(monkeypatch, tmp_path) -> None:
    stream, popen, clock, warnings = _tee(monkeypatch, tmp_path)
    _feed(stream, popen.procs[0], clock, 0.1, 3)
    popen.procs[0].die()
    _wait(lambda: stream._needs_restart)  # noqa: SLF001

    clock.now = 1.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(popen.procs) == 2  # first retry is immediate
    popen.procs[1].die()  # dies before delivering anything
    _wait(lambda: stream._needs_restart)  # noqa: SLF001

    clock.now = 1.5
    stream._watchdog_step()  # noqa: SLF001
    assert len(popen.procs) == 2  # inside the 1 s backoff
    clock.now = 2.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(popen.procs) == 3
    popen.procs[2].die()
    _wait(lambda: stream._needs_restart)  # noqa: SLF001

    clock.now = 3.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(popen.procs) == 3  # backoff doubled to 2 s
    clock.now = 4.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(popen.procs) == 4
    assert len(warnings) == 1  # the retries do not repeat the loss warning
    stream.stop()


def test_audiotee_reattach_fills_the_gap_between_stop_and_start(monkeypatch, tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    stream, popen, clock, _ = _tee(monkeypatch, tmp_path)
    _feed(stream, popen.procs[0], clock, 0.1, 5)
    _drain(stream)

    clock.now = 0.6
    stream.reattach()
    assert popen.procs[0].terminated and len(popen.procs) == 2
    clock.now = 2.5
    stream._watchdog_step()  # noqa: SLF001
    silence = _drain(stream)
    assert len(silence) == 20 and all(_is_silence(c) for c in silence)

    _feed(stream, popen.procs[1], clock, 2.6)
    assert stream._track._delivered == 26  # noqa: SLF001
    assert any("AudioTee tap re-attached:" in r.message for r in caplog.records)
    stream.stop()


def test_audiotee_stalled_but_alive_tap_is_filled_without_a_restart(monkeypatch, tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO)
    stream, popen, clock, _ = _tee(monkeypatch, tmp_path)
    _feed(stream, popen.procs[0], clock, 0.1, 5)
    _drain(stream)

    clock.now = 5.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(_drain(stream)) == 45
    assert len(popen.procs) == 1
    assert any("AudioTee tap lost:" in r.message and "no audio" in r.message for r in caplog.records)

    _feed(stream, popen.procs[0], clock, 5.1)
    assert stream._track._delivered == 51  # noqa: SLF001
    stream.stop()


def test_audiotee_stop_during_an_outage_ends_cleanly(monkeypatch, tmp_path) -> None:
    stream, popen, clock, _ = _tee(monkeypatch, tmp_path)
    _feed(stream, popen.procs[0], clock, 0.1, 5)
    popen.procs[0].die()
    _wait(lambda: stream._needs_restart)  # noqa: SLF001
    clock.now = 2.0
    stream._watchdog_step()  # noqa: SLF001
    _drain(stream)

    watchdog = stream._watchdog  # noqa: SLF001
    stream.stop()

    assert watchdog is not None and not watchdog.is_alive()
    assert stream._proc is None  # noqa: SLF001
    clock.now = 10.0
    stream._watchdog_step()  # noqa: SLF001
    assert stream.read() is None  # nothing is produced after stop()
    stream.stop()  # idempotent


def test_audiotee_that_never_delivered_is_not_filled_and_keeps_its_setup_failure(monkeypatch, tmp_path) -> None:
    stream, popen, clock, warnings = _tee(monkeypatch, tmp_path)
    popen.procs[0].die()
    _wait(lambda: len(warnings) == 1)
    assert "Systeeminstellingen" in warnings[0]["message"]

    clock.now = 30.0
    stream._watchdog_step()  # noqa: SLF001
    assert stream.read() is None
    assert len(popen.procs) == 1  # not restarted either
    assert not stream._track.in_outage  # noqa: SLF001
    stream.stop()


def test_audiotee_recovery_stops_the_fill(monkeypatch, tmp_path) -> None:
    stream, popen, clock, _ = _tee(monkeypatch, tmp_path)
    _feed(stream, popen.procs[0], clock, 0.1, 5)
    popen.procs[0].die()
    _wait(lambda: stream._needs_restart)  # noqa: SLF001
    clock.now = 2.0
    stream._watchdog_step()  # noqa: SLF001
    _feed(stream, popen.procs[1], clock, 2.1)
    _drain(stream)

    clock.now = 2.15
    stream._watchdog_step()  # noqa: SLF001
    assert stream.read() is None
    assert not stream._track.in_outage  # noqa: SLF001

    _feed(stream, popen.procs[1], clock, 2.2, 5)
    clock.now = 2.75
    stream._watchdog_step()  # noqa: SLF001
    chunks = _drain(stream)
    assert len(chunks) == 5 and all(c.any() for c in chunks)
    stream.stop()


# --- BlackHole ----------------------------------------------------------------


class _FakeInputStream:
    def __init__(self, owner: _FakeSd, callback) -> None:  # noqa: ANN001
        self.owner = owner
        self.callback = callback
        self.active = False
        self.closed = False

    def start(self) -> None:
        if self.owner.fail_opens > 0:
            self.owner.fail_opens -= 1
            raise RuntimeError("device unavailable")
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.closed = True

    def block(self) -> None:
        self.callback(np.full((CHUNK, 1), 0.1, dtype="float32"), CHUNK, None, None)


class _FakeSd:
    def __init__(self) -> None:
        self.streams: list[_FakeInputStream] = []
        self.fail_opens = 0

    def _terminate(self) -> None:
        pass

    def _initialize(self) -> None:
        pass

    def InputStream(self, **kwargs) -> _FakeInputStream:  # noqa: N802, ANN003
        stream = _FakeInputStream(self, kwargs["callback"])
        self.streams.append(stream)
        return stream

    @property
    def live(self) -> _FakeInputStream:
        return self.streams[-1]


def _hole(monkeypatch, **kwargs):
    sd = _FakeSd()
    monkeypatch.setattr(bh, "_import_sounddevice", lambda: sd)
    clock = _Clock()
    config = AudioConfig(sample_rate=16000, channels=1, dtype="float32", chunk_size=CHUNK, capture_method="blackhole")
    stream = BlackHoleStream(
        config,
        queue_maxsize=10_000,
        clock=clock,
        watchdog_poll_s=3600.0,
        **kwargs,
    )
    stream.start()
    return sd, stream, clock


def _tick(sd: _FakeSd, clock: _Clock, at: float, count: int = 1) -> None:
    for i in range(count):
        clock.now = round(at + i * CHUNK_S, 6)
        sd.live.block()


def test_blackhole_stalled_stream_is_filled_to_the_clock_and_reopened(monkeypatch, caplog) -> None:
    caplog.set_level(logging.INFO)
    sd, stream, clock = _hole(monkeypatch)
    _tick(sd, clock, 0.1, 10)
    _drain(stream)

    clock.now = 2.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(sd.streams) == 1  # inside the stall threshold nothing happens yet
    assert stream.read() is None

    clock.now = 4.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(sd.streams) == 2 and sd.streams[0].closed
    silence = _drain(stream)
    assert len(silence) == 30 and all(_is_silence(c) for c in silence)
    assert stream.tap_health().attach_state is TapAttachState.ATTACHED

    _tick(sd, clock, 4.1)
    tail = _drain(stream)
    assert len(tail) == 1 and tail[0].any()
    assert stream._track._delivered == 41  # noqa: SLF001
    assert any("BlackHole stream lost:" in r.message for r in caplog.records)
    recovered = [r.message for r in caplog.records if "BlackHole stream re-attached:" in r.message]
    assert len(recovered) == 1 and "after 3.1s of silence filled." in recovered[0]
    stream.stop()


def test_blackhole_errored_stream_is_detected_without_waiting_for_the_stall(monkeypatch) -> None:
    sd, stream, clock = _hole(monkeypatch)
    _tick(sd, clock, 0.1, 5)
    _drain(stream)

    sd.live.active = False  # PortAudio aborted the stream
    clock.now = 0.8
    stream._watchdog_step()  # noqa: SLF001
    assert len(sd.streams) == 2
    assert len(_drain(stream)) == 3  # 8 expected - 5 delivered
    stream.stop()


def test_blackhole_reopen_backs_off_and_keeps_filling_while_the_device_is_gone(monkeypatch) -> None:
    sd, stream, clock = _hole(monkeypatch)
    _tick(sd, clock, 0.1, 5)
    _drain(stream)
    sd.fail_opens = 100

    clock.now = 3.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(sd.streams) == 2  # first reopen attempt failed
    clock.now = 3.5
    stream._watchdog_step()  # noqa: SLF001
    assert len(sd.streams) == 2  # inside the 1 s backoff
    clock.now = 4.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(sd.streams) == 3
    clock.now = 5.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(sd.streams) == 3  # doubled to 2 s
    clock.now = 6.0
    stream._watchdog_step()  # noqa: SLF001
    assert len(sd.streams) == 4
    assert len(_drain(stream)) == 60 - 5  # the clock is filled all the way

    sd.fail_opens = 0
    clock.now = 8.0
    stream._watchdog_step()  # noqa: SLF001
    _tick(sd, clock, 8.1)
    assert stream._track._delivered == 81  # noqa: SLF001
    stream.stop()


def test_blackhole_stop_during_an_outage_ends_cleanly(monkeypatch) -> None:
    sd, stream, clock = _hole(monkeypatch)
    _tick(sd, clock, 0.1, 5)
    sd.fail_opens = 100
    clock.now = 3.0
    stream._watchdog_step()  # noqa: SLF001
    _drain(stream)

    watchdog = stream._watchdog  # noqa: SLF001
    stream.stop()

    assert watchdog is not None and not watchdog.is_alive()
    assert stream._stream is None  # noqa: SLF001
    clock.now = 10.0
    stream._watchdog_step()  # noqa: SLF001
    assert stream.read() is None
    stream.stop()  # idempotent
    assert cap._active_portaudio_stream_count() == 0  # noqa: SLF001


def test_blackhole_that_never_delivered_is_not_filled_or_reopened(monkeypatch) -> None:
    sd, stream, clock = _hole(monkeypatch)
    clock.now = 60.0
    stream._watchdog_step()  # noqa: SLF001
    assert stream.read() is None
    assert len(sd.streams) == 1
    stream.stop()


def test_blackhole_start_failure_still_raises(monkeypatch) -> None:
    sd = _FakeSd()
    sd.fail_opens = 1
    monkeypatch.setattr(bh, "_import_sounddevice", lambda: sd)
    stream = BlackHoleStream(AudioConfig(chunk_size=CHUNK, capture_method="blackhole"))
    with pytest.raises(RuntimeError):
        stream.start()
    assert stream.tap_health().attach_state is TapAttachState.ATTACH_FAILED
    assert stream._watchdog is None  # noqa: SLF001
    assert cap._active_portaudio_stream_count() == 0  # noqa: SLF001


def test_blackhole_recovery_stops_the_fill(monkeypatch) -> None:
    sd, stream, clock = _hole(monkeypatch)
    _tick(sd, clock, 0.1, 5)
    clock.now = 3.0
    stream._watchdog_step()  # noqa: SLF001
    _tick(sd, clock, 3.1)
    _drain(stream)

    clock.now = 3.15
    stream._watchdog_step()  # noqa: SLF001
    assert stream.read() is None
    assert not stream._track.in_outage  # noqa: SLF001
    stream.stop()


def test_track_clock_is_thread_safe_under_concurrent_fill_and_push() -> None:
    """The watchdog fills while the reader pushes; the counts must stay consistent."""
    clock = _Clock()
    q: queue.Queue[np.ndarray] = queue.Queue()
    track = cap._TrackClock(AudioConfig(chunk_size=CHUNK, channels=1, dtype="float32"), q, clock)  # noqa: SLF001
    clock.now = 0.1
    track.push(np.ones((CHUNK, 1), dtype="float32"), 1.0)
    track.begin_outage()
    stop = threading.Event()

    def filler() -> None:
        while not stop.is_set():
            track.fill(clock.now)

    thread = threading.Thread(target=filler)
    thread.start()
    clock.now = 5.0
    track.push(np.ones((CHUNK, 1), dtype="float32"), 1.0)
    stop.set()
    thread.join()
    assert track._delivered == q.qsize()  # noqa: SLF001
    assert abs(track._delivered - 50) <= 1  # noqa: SLF001
