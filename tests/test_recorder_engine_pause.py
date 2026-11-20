from __future__ import annotations

import threading
import wave

import numpy as np
import pytest

from sales_copilot.audio.recorder_engine import RecorderEngine
from tests.test_recorder_engine import FakeStream, _chunks, _metadata, _stop_at, _wav_frames

CHUNK = 1024
RATE = 16000


def _engine(tmp_path, **streams: FakeStream) -> RecorderEngine:
    engine = RecorderEngine(
        out_dir=tmp_path,
        streams=streams or {"prospect": FakeStream([]), "self": FakeStream([])},
    )
    engine.start(run_loop=False)
    return engine


def _feed(engine: RecorderEngine, n: int) -> None:
    for _label, stream in engine._streams:
        stream._frames.extend(_chunks(n))


def _pause_cycle(engine: RecorderEngine) -> None:
    """2 s recorded, pause at 2 s with 5 s of wall time, resume at 7 s, 1 s more."""
    t0 = engine._start_monotonic
    _feed(engine, 31)
    assert engine.pause(t0 + 2.0)
    _feed(engine, 20)
    assert engine.resume(t0 + 7.0)
    _feed(engine, 15)
    engine.pump(t0 + 8.0)


def test_tracks_have_equal_length_equal_to_recording_time(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    _pause_cycle(engine)
    directory = _stop_at(monkeypatch, engine, 8.0)
    frames = {_wav_frames(directory / f"{label}.wav") for label in ("prospect", "self")}
    assert len(frames) == 1
    assert frames.pop() == pytest.approx(3.0 * RATE, abs=CHUNK)
    assert _metadata(directory)["pauses"] == [{"offset_ms": 2000, "wall_duration_ms": 5000}]


def test_frames_fed_during_a_pause_are_not_written(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    t0 = engine._start_monotonic
    _feed(engine, 4)
    engine.pause(t0 + 0.5)
    _feed(engine, 10)
    engine.pump(t0 + 1.0)
    engine.resume(t0 + 1.5)
    directory = _stop_at(monkeypatch, engine, 0.5)
    assert _wav_frames(directory / "self.wav") == 4 * CHUNK


def test_pump_keeps_the_meters_moving_while_paused(tmp_path) -> None:
    engine = _engine(tmp_path)
    t0 = engine._start_monotonic
    engine.pause(t0 + 1.0)
    _feed(engine, 3)
    engine.pump(t0 + 1.5)
    snap = engine.snapshot(t0 + 1.5)
    assert snap.paused
    assert snap.streams[0].display_db > -50.0


def test_track_recovering_during_a_pause_lands_at_the_pause_start(tmp_path, monkeypatch) -> None:
    late = FakeStream(_chunks(8), fail_start=True)
    engine = _engine(tmp_path, prospect=FakeStream(_chunks(40)), self=late)
    t0 = engine._start_monotonic
    engine.pump(t0 + 2.0, retry=False)
    engine.pause(t0 + 2.0)
    late._fail_start = False
    engine.pump(t0 + 30.0)
    engine.resume(t0 + 31.0)
    directory = _stop_at(monkeypatch, engine, 31.0)
    assert _metadata(directory)["late_start_ms"]["self"] == 2000
    assert _wav_frames(directory / "self.wav") == pytest.approx((2.0 * RATE) + 8 * CHUNK, abs=1)


def test_marker_during_a_pause_gets_the_pause_start(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    t0 = engine._start_monotonic
    assert engine.add_marker(t0 + 1.0)
    engine.pause(t0 + 3.0)
    assert engine.add_marker(t0 + 9.0)
    assert engine.markers == [1000, 3000]
    directory = _stop_at(monkeypatch, engine, 9.0)
    assert _metadata(directory)["markers"] == [{"offset_ms": 1000}, {"offset_ms": 3000}]


def test_recording_without_pause_or_marker_has_todays_metadata_keys(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path, prospect=FakeStream(_chunks(4)), self=FakeStream(_chunks(4)))
    engine.pump(engine._start_monotonic)
    meta = _metadata(_stop_at(monkeypatch, engine, 0.256))
    assert set(meta) == {
        "session_id", "started_at", "ended_at", "duration_ms", "duration_ms_per_stream",
        "end_padding_ms", "sample_rate", "channels", "stream_labels",
    }


def test_stop_while_paused_closes_the_pause_and_ends_at_its_start(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    t0 = engine._start_monotonic
    _feed(engine, 31)
    engine.pause(t0 + 2.0)
    directory = _stop_at(monkeypatch, engine, 12.0)
    meta = _metadata(directory)
    assert meta["pauses"] == [{"offset_ms": 2000, "wall_duration_ms": 10000}]
    assert _wav_frames(directory / "self.wav") == pytest.approx(2.0 * RATE, abs=CHUNK)


def test_snapshot_elapsed_is_recording_time(tmp_path) -> None:
    engine = _engine(tmp_path)
    t0 = engine._start_monotonic
    engine.pause(t0 + 2.0)
    assert engine.snapshot(t0 + 9.0).elapsed_s == pytest.approx(2.0)
    engine.resume(t0 + 9.0)
    assert engine.snapshot(t0 + 10.0).elapsed_s == pytest.approx(3.0)


def test_calls_that_do_not_fit_return_false(tmp_path) -> None:
    engine = RecorderEngine(out_dir=tmp_path, streams={"self": FakeStream([])})
    assert (engine.pause(0.0), engine.resume(0.0), engine.add_marker(0.0)) == (False, False, False)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    assert engine.resume(t0) is False
    assert engine.pause(t0 + 1.0) is True
    assert engine.pause(t0 + 2.0) is False
    assert np.isclose(engine.snapshot(t0 + 5.0).elapsed_s, 1.0)


class _NumberedStream:
    """Hands out frames filled with their sequence number.

    The engine thread is held inside read() for one chosen frame, so a pause
    that does not wait for that drain would overtake it.
    """

    def __init__(self, limit: int, hold_at: int) -> None:
        self._next = 1
        self._limit = limit
        self._hold_at = hold_at
        self.held = threading.Event()
        self.release = threading.Event()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def read(self) -> np.ndarray | None:
        if self._next > self._limit:
            return None
        number = self._next
        self._next += 1
        if number == self._hold_at and threading.current_thread().name == "recorder-engine":
            self.held.set()
            self.release.wait(timeout=5)
        return np.full(CHUNK, number / 1000.0, dtype=np.float32)


def test_pause_waits_for_the_engine_drain_and_keeps_frame_order(tmp_path) -> None:
    limit = 40
    stream = _NumberedStream(limit, hold_at=3)
    engine = RecorderEngine(out_dir=tmp_path, streams={"self": stream})
    engine.start(run_loop=True)
    assert stream.held.wait(timeout=5)
    done = threading.Event()
    results: list[bool] = []

    def _pause() -> None:
        results.append(engine.pause())
        done.set()

    worker = threading.Thread(target=_pause)
    worker.start()
    done.wait(timeout=0.1)
    stream.release.set()
    worker.join(timeout=5)
    assert results == [True]
    assert engine.resume()
    directory = engine.stop()
    assert directory is not None
    with wave.open(str(directory / "self.wav"), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    frames = samples[: len(samples) // CHUNK * CHUNK].reshape(-1, CHUNK)
    sequence = np.round(frames[:, 0] / 32768.0 * 1000.0).astype(int)
    assert [n for n in sequence if n > 0] == list(range(1, limit + 1))
