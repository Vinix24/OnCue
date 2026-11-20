"""Auto-stop on silence: both tracks quiet for the window, measured in dBFS."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from sales_copilot.audio.levels import rms_dbfs
from sales_copilot.audio.recorder_engine import RecorderEngine
from sales_copilot.core.config import AutoStopConfig
from tests.test_recorder_engine import FakeStream, _metadata, _stop_at

PROSPECT_DB = -50.0
SELF_DB = -40.0
WINDOW = 10.0
CONFIG = AutoStopConfig(silence_seconds=WINDOW, prospect_db=PROSPECT_DB, self_db=SELF_DB)


def _frame(db: float) -> np.ndarray:
    return np.full(1024, 10 ** (db / 20.0), dtype=np.float32)


def _engine(tmp_path, config: AutoStopConfig = CONFIG) -> RecorderEngine:
    engine = RecorderEngine(
        out_dir=tmp_path,
        streams={"prospect": FakeStream([]), "self": FakeStream([])},
        auto_stop=config,
    )
    engine.start(run_loop=False)
    return engine


def _tick(engine: RecorderEngine, t: float, prospect: float | None, mic: float | None) -> None:
    """One pump at ``t`` seconds after the start with one frame per track at the given dBFS."""
    t0 = engine._start_monotonic
    for label, db in (("prospect", prospect), ("self", mic)):
        if db is not None:
            dict(engine._streams)[label]._frames.append(_frame(db))
    engine.pump(t0 + t)


def test_frame_helper_matches_rms_dbfs() -> None:
    assert math.isclose(rms_dbfs(_frame(-42.0)), -42.0, abs_tol=0.01)


@pytest.mark.parametrize(
    ("track", "threshold"),
    [("prospect", PROSPECT_DB), ("self", SELF_DB)],
)
def test_just_above_the_threshold_keeps_recording(tmp_path, track, threshold) -> None:
    engine = _engine(tmp_path)
    _tick(engine, 0.0, -10.0, -10.0)  # arms the clock
    for t in range(1, 40):
        db = threshold + 1.0
        _tick(engine, float(t), db if track == "prospect" else -90.0, db if track == "self" else -90.0)
    assert engine.auto_stop_s is None


@pytest.mark.parametrize(
    ("track", "threshold"),
    [("prospect", PROSPECT_DB), ("self", SELF_DB)],
)
def test_just_below_the_threshold_stops_after_the_window(tmp_path, track, threshold) -> None:
    engine = _engine(tmp_path)
    _tick(engine, 0.0, -10.0, -10.0)
    for t in range(1, int(WINDOW)):
        db = threshold - 1.0
        _tick(engine, float(t), db if track == "prospect" else -90.0, db if track == "self" else -90.0)
        assert engine.auto_stop_s is None
    _tick(engine, WINDOW, -90.0, -90.0)
    assert engine.auto_stop_s == int(WINDOW)
    assert engine.snapshot().auto_stop_s == int(WINDOW)


def test_silence_before_the_first_speech_never_stops(tmp_path) -> None:
    engine = _engine(tmp_path)
    for t in range(0, 60):
        _tick(engine, float(t), -90.0, -90.0)
    assert engine.auto_stop_s is None
    _tick(engine, 60.0, -10.0, None)  # first speech arms the clock at 60 s
    _tick(engine, 60.0 + WINDOW - 1, None, None)
    assert engine.auto_stop_s is None
    _tick(engine, 60.0 + WINDOW, None, None)
    assert engine.auto_stop_s == int(WINDOW)


def test_one_track_silent_while_the_other_talks_does_not_stop(tmp_path) -> None:
    engine = _engine(tmp_path)
    for t in range(0, 60):
        _tick(engine, float(t), -90.0, -20.0)  # only the microphone talks
    assert engine.auto_stop_s is None
    for t in range(60, 120):
        _tick(engine, float(t), -20.0, -90.0)  # only the other side talks
    assert engine.auto_stop_s is None


def test_pause_suspends_the_silence_clock(tmp_path) -> None:
    engine = _engine(tmp_path)
    t0 = engine._start_monotonic
    _tick(engine, 0.0, -10.0, -10.0)
    _tick(engine, 6.0, -90.0, -90.0)  # 6 s of quiet counted
    assert engine.pause(t0 + 6.0)
    for t in range(7, 600, 5):
        _tick(engine, float(t), -90.0, -90.0)  # a long pause does not count
    assert engine.auto_stop_s is None
    assert engine.resume(t0 + 600.0)
    _tick(engine, 603.0, -90.0, -90.0)  # 6 + 3 s of quiet
    assert engine.auto_stop_s is None
    _tick(engine, 604.0, -90.0, -90.0)  # 6 + 4 s reaches the window
    assert engine.auto_stop_s == int(WINDOW)


def test_loud_audio_during_a_pause_does_not_reset_the_clock(tmp_path) -> None:
    engine = _engine(tmp_path)
    t0 = engine._start_monotonic
    _tick(engine, 0.0, -10.0, -10.0)
    _tick(engine, 5.0, -90.0, -90.0)
    assert engine.pause(t0 + 5.0)
    _tick(engine, 6.0, -10.0, -10.0)  # discarded audio, must not count as speech
    assert engine.resume(t0 + 7.0)
    _tick(engine, 12.0, -90.0, -90.0)  # 5 + 5 s of quiet recording time
    assert engine.auto_stop_s == int(WINDOW)


def test_disabled_never_stops(tmp_path) -> None:
    engine = _engine(tmp_path, AutoStopConfig(silence_seconds=0.0))
    _tick(engine, 0.0, -10.0, -10.0)
    for t in range(1, 1000, 10):
        _tick(engine, float(t), -90.0, -90.0)
    assert engine.auto_stop_s is None


def test_marker_lands_in_metadata_and_the_transcript(tmp_path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    _tick(engine, 0.0, -10.0, -10.0)
    _tick(engine, WINDOW + 2.0, -90.0, -90.0)
    assert engine.auto_stop_s == int(WINDOW)
    directory = _stop_at(monkeypatch, engine, WINDOW + 2.0)
    stop = _metadata(directory)["auto_stop"]
    assert stop == {"reason": "silence", "silence_s": int(WINDOW), "offset_ms": int((WINDOW + 2.0) * 1000)}

    from tests.test_record_server import _load_module

    line = _load_module()._auto_stop_line(directory, "en")
    assert line == "[00:12] (Recording stopped automatically after 10 s of silence)\n"
    assert "stilte" in _load_module()._auto_stop_line(directory, "nl")


def test_a_manual_stop_has_no_auto_stop_marker(tmp_path, monkeypatch) -> None:
    from tests.test_record_server import _load_module

    engine = _engine(tmp_path)
    _tick(engine, 0.0, -10.0, -10.0)
    directory = _stop_at(monkeypatch, engine, 2.0)
    assert "auto_stop" not in _metadata(directory)
    assert _load_module()._auto_stop_line(directory, "en") == ""


def test_a_corrupt_auto_stop_entry_is_not_a_marker(tmp_path) -> None:
    from tests.test_record_server import _load_module

    (tmp_path / "metadata.json").write_text(json.dumps({"auto_stop": {"silence_s": "x"}}), encoding="utf-8")
    assert _load_module()._auto_stop_line(tmp_path, "en") == ""


def test_replay_of_a_call_with_a_hang_up(tmp_path) -> None:
    """Synthetic call: talk for 60 s, hang up, then room noise on the mic and nothing on the system tap."""
    engine = _engine(tmp_path, AutoStopConfig(silence_seconds=30.0, prospect_db=PROSPECT_DB, self_db=SELF_DB))
    hang_up = 60
    stopped_at = None
    for t in range(0, 600):
        talking = t < hang_up
        prospect = -25.0 if talking and (t // 5) % 2 == 0 else -80.0
        mic = -22.0 if talking and (t // 5) % 2 == 1 else -48.0  # room noise sits under the mic threshold
        _tick(engine, float(t), prospect, mic)
        if engine.auto_stop_s is not None:
            stopped_at = t
            break
    assert stopped_at is not None
    last_speech = hang_up - 1
    assert stopped_at - last_speech >= 30  # not before the window ended
    assert stopped_at - last_speech <= 30 + 5  # and within a tick or two of it


@pytest.mark.parametrize("value", ["", "  "])
def test_config_blank_values_use_the_defaults(monkeypatch, value) -> None:
    for name in ("RECORDER_AUTOSTOP_SILENCE_SECONDS", "RECORDER_AUTOSTOP_PROSPECT_DB", "RECORDER_AUTOSTOP_SELF_DB"):
        monkeypatch.setenv(name, value)
    assert AutoStopConfig.from_env(default_seconds=120.0) == AutoStopConfig(120.0, -50.0, -40.0)
    assert not AutoStopConfig.from_env().enabled


def test_config_reads_env_and_zero_means_off(monkeypatch) -> None:
    monkeypatch.setenv("RECORDER_AUTOSTOP_SILENCE_SECONDS", "0")
    monkeypatch.setenv("RECORDER_AUTOSTOP_PROSPECT_DB", "-55")
    monkeypatch.setenv("RECORDER_AUTOSTOP_SELF_DB", "-35.5")
    config = AutoStopConfig.from_env(default_seconds=120.0)
    assert config == AutoStopConfig(0.0, -55.0, -35.5)
    assert not config.enabled
    monkeypatch.setenv("RECORDER_AUTOSTOP_SILENCE_SECONDS", "90")
    assert AutoStopConfig.from_env().silence_seconds == 90.0


def test_config_rejects_a_malformed_value(monkeypatch) -> None:
    monkeypatch.setenv("RECORDER_AUTOSTOP_SELF_DB", "loud")
    with pytest.raises(ValueError, match="RECORDER_AUTOSTOP_SELF_DB"):
        AutoStopConfig.from_env()
