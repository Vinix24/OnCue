from __future__ import annotations

import numpy as np

from sales_copilot.audio.levels import (
    SILENCE_FLOOR_DB,
    LevelTracker,
    render_meter_bar,
    rms_dbfs,
)


def test_rms_dbfs_empty_and_silent_return_floor() -> None:
    assert rms_dbfs(None) == SILENCE_FLOOR_DB
    assert rms_dbfs(np.zeros(0, dtype=np.float32)) == SILENCE_FLOOR_DB
    assert rms_dbfs(np.zeros(1600, dtype=np.float32)) == SILENCE_FLOOR_DB


def test_rms_dbfs_full_scale_is_zero_db() -> None:
    full = np.ones(1600, dtype=np.float32)
    assert abs(rms_dbfs(full) - 0.0) < 0.01


def test_rms_dbfs_half_amplitude_is_minus_six_db() -> None:
    half = np.full(1600, 0.5, dtype=np.float32)
    assert abs(rms_dbfs(half) - (-6.02)) < 0.1


def test_render_meter_bar_endpoints_and_width() -> None:
    assert render_meter_bar(-60.0, width=10, floor_db=-60.0) == "░" * 10
    assert render_meter_bar(0.0, width=10, floor_db=-60.0) == "█" * 10
    mid = render_meter_bar(-30.0, width=10, floor_db=-60.0)
    assert len(mid) == 10
    assert mid.count("█") == 5
    # below the floor clamps to empty, above 0 clamps to full
    assert render_meter_bar(-200.0, width=8) == "░" * 8
    assert render_meter_bar(12.0, width=8) == "█" * 8


def test_level_tracker_loud_then_silent() -> None:
    t = LevelTracker(silence_db=-50.0, silence_seconds=4.0)
    t.observe(-10.0, now=0.0)
    assert not t.is_silent(0.0)
    assert not t.is_silent(3.9)
    assert t.is_silent(4.0)


def test_level_tracker_dead_stream_goes_silent_after_grace() -> None:
    t = LevelTracker(silence_db=-50.0, silence_seconds=4.0)
    t.observe(SILENCE_FLOOR_DB, now=0.0)  # never any loud frame
    assert not t.is_silent(1.0)
    assert t.is_silent(5.0)


def test_level_tracker_peak_hold_and_decay() -> None:
    t = LevelTracker(decay_db_per_s=80.0)
    t.observe(-10.0, now=0.0)
    assert t.peak_db == -10.0
    # a quieter frame one second later decays the display but not the peak
    t.observe(-120.0, now=1.0)
    assert t.peak_db == -10.0
    assert abs(t.display_db - (-90.0)) < 0.001
