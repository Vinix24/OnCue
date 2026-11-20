"""The notetaker's tap watchdog: exact zeros on the system track while the mic carries signal."""

from __future__ import annotations

import json
import logging

import numpy as np
import pytest

from sales_copilot.audio.recorder_engine import RecorderEngine
from sales_copilot.audio.tap_health import (
    DEFAULT_REATTACH_MAX_ATTEMPTS,
    ReattachAction,
    TapAttachState,
    TapHealth,
    TapSignalState,
    plan_reattach,
)

CHUNK = 1024
LOUD = np.full(CHUNK, 0.3, dtype=np.float32)
ZEROS = np.zeros(CHUNK, dtype=np.float32)
QUIET = np.full(CHUNK, 1e-6, dtype=np.float32)  # non-zero but far below the signal floor


def _noise_floor() -> np.ndarray:
    return (np.random.default_rng(1).standard_normal(CHUNK) * 0.01).astype(np.float32)


class PushStream:
    """A stream the test feeds one frame at a time; counts reattach calls."""

    def __init__(self, *, can_reattach: bool = True, heal_on_reattach: bool = False) -> None:
        self.frame: np.ndarray | None = None
        self.reattaches = 0
        self.healed = False
        self._heal = heal_on_reattach
        if can_reattach:
            self.reattach = self._reattach

    def _reattach(self) -> None:
        self.reattaches += 1
        self.healed = self._heal

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def read(self) -> np.ndarray | None:
        frame, self.frame = self.frame, None
        return frame


def _engine(tmp_path, prospect: PushStream, mic: PushStream) -> RecorderEngine:
    return RecorderEngine(out_dir=tmp_path, streams={"prospect": prospect, "self": mic}, tap_beat_seconds=30.0)


def _run(engine, prospect, mic, t0, seconds, system_frame):
    """Pump once per second; the mic always has a noise floor, the system track delivers ``system_frame``."""
    for second in range(1, seconds + 1):
        prospect.frame = LOUD if (second == 1 and system_frame is ZEROS) else system_frame
        mic.frame = _noise_floor()
        engine.pump(t0 + second)


def test_exact_zeros_are_lost_at_the_threshold_not_before(tmp_path) -> None:
    prospect, mic = PushStream(), PushStream()
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    _run(engine, prospect, mic, t0, 29, ZEROS)
    assert engine.snapshot(t0 + 29).tap_state == "ok"
    # The first zero frame arrived at t0+2, so the run reaches 30 s at t0+32. Pump just
    # past that boundary, not exactly on it: t0 is a real monotonic value, and
    # (t0+32)-(t0+2) can round a few ULP below 30.0 depending on t0's magnitude, which
    # made this assertion fail on some hosts while the product behaved correctly.
    prospect.frame, mic.frame = ZEROS, _noise_floor()
    engine.pump(t0 + 31.999)
    assert engine.snapshot(t0 + 31.999).tap_state == "ok"
    prospect.frame, mic.frame = ZEROS, _noise_floor()
    engine.pump(t0 + 32.001)
    assert engine.snapshot(t0 + 32.001).tap_state == "lost"


def test_a_quiet_but_non_zero_system_track_is_not_lost(tmp_path) -> None:
    prospect, mic = PushStream(), PushStream()
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    prospect.frame, mic.frame = LOUD, _noise_floor()
    engine.pump(t0 + 0.5)
    _run(engine, prospect, mic, t0 + 1, 120, QUIET)
    assert engine.snapshot(t0 + 121).tap_state == "ok"
    assert prospect.reattaches == 0


def test_both_tracks_zero_is_a_pause_not_a_dropout(tmp_path) -> None:
    prospect, mic = PushStream(), PushStream()
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    for second in range(0, 90):
        prospect.frame = LOUD if second == 0 else ZEROS
        mic.frame = LOUD if second == 0 else ZEROS
        engine.pump(t0 + second)
    assert engine.snapshot(t0 + 90).tap_state == "ok"
    assert prospect.reattaches == 0


def test_reattach_is_bounded_then_the_engine_gives_up(tmp_path, monkeypatch, caplog) -> None:
    prospect, mic = PushStream(), PushStream()
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    with caplog.at_level(logging.INFO, logger="sales_copilot.audio.recorder_engine"):
        _run(engine, prospect, mic, t0, 400, ZEROS)
    assert prospect.reattaches == DEFAULT_REATTACH_MAX_ATTEMPTS
    snap = engine.snapshot(t0 + 400)
    assert (snap.tap_state, snap.tap_attempts) == ("gave_up", DEFAULT_REATTACH_MAX_ATTEMPTS)
    monkeypatch.setattr("sales_copilot.audio.recorder_engine.time.monotonic", lambda: t0 + 400)
    directory = engine.stop()
    events = json.loads((directory / "metadata.json").read_text())["tap_events"]
    assert [e["event"] for e in events] == [
        "lost",
        "reattach_attempt_1",
        "reattach_attempt_2",
        "reattach_attempt_3",
        "gave_up",
    ]
    assert events[0]["t"] == pytest.approx(32, abs=1)
    assert all(set(e) == {"t", "event"} for e in events)
    for line in ("attached", "lost", "reattach attempt 1/3", "gave up", "heartbeat"):
        assert line in caplog.text


def test_a_reattach_that_works_is_recorded_and_shown_briefly(tmp_path, monkeypatch) -> None:
    prospect, mic = PushStream(heal_on_reattach=True), PushStream()
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    _run(engine, prospect, mic, t0, 40, ZEROS)
    assert prospect.reattaches == 1
    assert engine.snapshot(t0 + 40).tap_state == "lost"
    prospect.frame, mic.frame = LOUD, _noise_floor()
    engine.pump(t0 + 42)
    assert engine.snapshot(t0 + 43).tap_state == "recovered"
    assert engine.snapshot(t0 + 60).tap_state == "ok"
    monkeypatch.setattr("sales_copilot.audio.recorder_engine.time.monotonic", lambda: t0 + 60)
    events = json.loads((engine.stop() / "metadata.json").read_text())["tap_events"]
    assert [e["event"] for e in events] == ["lost", "reattach_attempt_1", "reattached"]


def test_a_stream_that_cannot_reattach_stays_lost_and_never_claims_to_give_up(tmp_path) -> None:
    prospect, mic = PushStream(can_reattach=False), PushStream()
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    _run(engine, prospect, mic, t0, 300, ZEROS)
    assert engine.snapshot(t0 + 300).tap_state == "lost"


def test_a_reattach_that_raises_does_not_stop_the_pump(tmp_path, caplog) -> None:
    prospect, mic = PushStream(), PushStream()

    def boom() -> None:
        raise OSError("no tap")

    prospect.reattach = boom
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    _run(engine, prospect, mic, t0, 40, ZEROS)
    assert "reattach raised" in caplog.text
    assert engine.snapshot(t0 + 40).tap_state == "lost"


def _health(state: TapSignalState) -> TapHealth:
    return TapHealth(state, TapAttachState.ATTACHED, "t", "", 1, 1, 1.0, 1.0, None, 0, None, -20.0, -20.0, False)


@pytest.mark.parametrize(
    ("state", "peer", "can", "attempts", "beats", "action"),
    [
        (TapSignalState.ATTACHED_SIGNAL_LOST, "self", True, 0, 0, ReattachAction.REATTACH),
        (TapSignalState.ATTACHED_SIGNAL_LOST, "self", True, 1, 1, ReattachAction.COOLDOWN),
        (TapSignalState.ATTACHED_SIGNAL_LOST, "self", True, 3, 99, ReattachAction.NONE),
        (TapSignalState.ATTACHED_SIGNAL_LOST, None, True, 0, 0, ReattachAction.NONE),
        (TapSignalState.ATTACHED_SIGNAL_LOST, "self", False, 0, 0, ReattachAction.NONE),
        (TapSignalState.ATTACHED_SILENT, "self", True, 0, 0, ReattachAction.NONE),
    ],
)
def test_plan_reattach_only_acts_on_a_lost_tap_with_a_carrying_peer(state, peer, can, attempts, beats, action) -> None:
    next_beat = 2 if attempts else 0
    plan = plan_reattach(
        _health(state), peer_label=peer, can_reattach=can, attempts=attempts, beats=beats, next_beat=next_beat
    )
    assert plan.action is action


def _hold(engine, prospect, mic, t0, start, end, system_frame, mic_frame=None):
    """Pump once per second from ``start`` to ``end`` (inclusive)."""
    for second in range(start, end + 1):
        prospect.frame = system_frame
        mic.frame = mic_frame if mic_frame is not None else _noise_floor()
        engine.pump(t0 + second)


def test_a_second_independent_dropout_gets_its_own_reattach_attempts(tmp_path, monkeypatch) -> None:
    prospect, mic = PushStream(heal_on_reattach=True), PushStream()
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    prospect.frame, mic.frame = LOUD, _noise_floor()
    engine.pump(t0 + 1)
    _hold(engine, prospect, mic, t0, 2, 40, ZEROS)
    assert prospect.reattaches == 1
    _hold(engine, prospect, mic, t0, 41, 45, LOUD)
    assert engine.snapshot(t0 + 46).tap_state == "recovered"
    assert engine.snapshot(t0 + 46).tap_attempts == 0
    _hold(engine, prospect, mic, t0, 46, 100, LOUD)
    _hold(engine, prospect, mic, t0, 101, 140, ZEROS)
    assert prospect.reattaches == 2
    _hold(engine, prospect, mic, t0, 141, 145, LOUD)
    monkeypatch.setattr("sales_copilot.audio.recorder_engine.time.monotonic", lambda: t0 + 146)
    events = json.loads((engine.stop() / "metadata.json").read_text())["tap_events"]
    names = [e["event"] for e in events]
    assert names == ["lost", "reattach_attempt_1", "reattached", "lost", "reattach_attempt_1", "reattached"]


def test_the_attempt_limit_still_holds_within_one_dropout_after_a_reset(tmp_path) -> None:
    prospect, mic = PushStream(), PushStream()
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    _run(engine, prospect, mic, t0, 400, ZEROS)
    assert prospect.reattaches == DEFAULT_REATTACH_MAX_ATTEMPTS


def _nosys_engine(tmp_path, prospect, mic, seconds=120.0) -> RecorderEngine:
    return RecorderEngine(
        out_dir=tmp_path,
        streams={"prospect": prospect, "self": mic},
        tap_beat_seconds=30.0,
        tap_no_system_audio_seconds=seconds,
    )


def test_no_system_audio_is_raised_at_the_threshold_with_mic_signal(tmp_path, caplog) -> None:
    prospect, mic = PushStream(), PushStream()
    engine = _nosys_engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    # Mic carries signal from the first pump; the system track is exact zero from the start.
    with caplog.at_level(logging.INFO, logger="sales_copilot.audio.recorder_engine"):
        _hold(engine, prospect, mic, t0, 1, 100, ZEROS)
        assert engine.snapshot(t0 + 100).tap_state == "ok"
        _hold(engine, prospect, mic, t0, 101, 130, ZEROS)
    assert engine.snapshot(t0 + 130).tap_state == "no_system_audio"
    assert prospect.reattaches == 0
    assert caplog.text.count("no system audio since the start") == 1
    events = json.loads((engine.stop() / "metadata.json").read_text())["tap_events"]
    assert [e["event"] for e in events] == ["no_system_audio"]


def test_no_system_audio_is_not_raised_when_the_mic_is_silent_too(tmp_path) -> None:
    prospect, mic = PushStream(), PushStream()
    engine = _nosys_engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    _hold(engine, prospect, mic, t0, 1, 300, ZEROS, mic_frame=ZEROS)
    assert engine.snapshot(t0 + 300).tap_state == "ok"


def test_no_system_audio_is_cleared_by_the_first_real_system_signal(tmp_path, monkeypatch) -> None:
    prospect, mic = PushStream(), PushStream()
    engine = _nosys_engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    _hold(engine, prospect, mic, t0, 1, 130, ZEROS)
    assert engine.snapshot(t0 + 130).tap_state == "no_system_audio"
    _hold(engine, prospect, mic, t0, 131, 131, LOUD)
    assert engine.snapshot(t0 + 131).tap_state == "ok"
    assert prospect.reattaches == 0
    monkeypatch.setattr("sales_copilot.audio.recorder_engine.time.monotonic", lambda: t0 + 132)
    events = json.loads((engine.stop() / "metadata.json").read_text())["tap_events"]
    assert [e["event"] for e in events] == ["no_system_audio", "system_audio_present"]


def test_a_quiet_non_zero_system_track_never_raises_no_system_audio(tmp_path) -> None:
    prospect, mic = PushStream(), PushStream()
    engine = _nosys_engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    _hold(engine, prospect, mic, t0, 1, 200, QUIET)
    assert engine.snapshot(t0 + 200).tap_state == "ok"


def test_no_system_audio_threshold_comes_from_the_env_setting(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUDIO_TAP_NO_SYSTEM_AUDIO_WARN_SECONDS", "10")
    prospect, mic = PushStream(), PushStream()
    engine = _engine(tmp_path, prospect, mic)
    engine.start(run_loop=False)
    t0 = engine._start_monotonic
    _hold(engine, prospect, mic, t0, 1, 15, ZEROS)
    assert engine.snapshot(t0 + 15).tap_state == "no_system_audio"


@pytest.mark.parametrize("raw", ["0", "-5"])
def test_a_non_positive_no_system_audio_setting_falls_back_to_the_default(monkeypatch, raw) -> None:
    from sales_copilot.audio.tap_health import DEFAULT_NO_SYSTEM_AUDIO_WARN_SECONDS, resolve_no_system_audio_seconds

    monkeypatch.setenv("AUDIO_TAP_NO_SYSTEM_AUDIO_WARN_SECONDS", raw)
    assert resolve_no_system_audio_seconds() == DEFAULT_NO_SYSTEM_AUDIO_WARN_SECONDS


def test_a_non_numeric_no_system_audio_setting_fails_loudly_like_its_siblings(monkeypatch) -> None:
    from sales_copilot.audio.tap_health import resolve_no_system_audio_seconds

    monkeypatch.setenv("AUDIO_TAP_NO_SYSTEM_AUDIO_WARN_SECONDS", "abc")
    with pytest.raises(ValueError, match="AUDIO_TAP_NO_SYSTEM_AUDIO_WARN_SECONDS"):
        resolve_no_system_audio_seconds()
