import pytest

from sales_copilot.core.config import TalkTimeConfig
from sales_copilot.modules.talk_time.tracker import TalkTimeTracker


def test_rolling_window_percentages() -> None:
    config = TalkTimeConfig(rolling_window_seconds=120)
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 60000)
    tracker.record_speech("prospect", 100000, 130000)
    tracker.record_speech("self", 150000, 170000)

    state = tracker.get_state(200000)

    assert state.rolling_self_pct == pytest.approx(0.4, rel=1e-3)
    assert state.rolling_prospect_pct == pytest.approx(0.6, rel=1e-3)
    assert state.cumulative_self_pct == pytest.approx(0.7273, rel=1e-3)
    assert state.cumulative_prospect_pct == pytest.approx(0.2727, rel=1e-3)


def test_phase_switch_updates_status() -> None:
    config = TalkTimeConfig(
        rolling_window_seconds=120,
        ratio_amber_threshold=0.05,
        ratio_red_threshold=0.10,
    )
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 42000)
    tracker.record_speech("prospect", 42000, 100000)

    state = tracker.get_state(100000)
    assert state.status == "amber"

    tracker.set_phase("pitch")
    state = tracker.get_state(100000)
    assert state.status == "red"


def test_monologue_alert_emits() -> None:
    config = TalkTimeConfig(monologue_warning_seconds=1)
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 1500)
    alert = tracker.check_alerts(1500)

    assert alert is not None
    assert alert.alert_type == "monologue_warning"
    assert alert.severity == "red"
