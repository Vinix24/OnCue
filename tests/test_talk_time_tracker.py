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


def test_prospect_monologue_does_not_alert() -> None:
    config = TalkTimeConfig(monologue_warning_seconds=1)
    tracker = TalkTimeTracker(config)

    tracker.record_speech("prospect", 0, 1500)
    state = tracker.get_state(1500)
    alert = tracker.check_alerts(1500)

    assert state.monologue_speaker == "prospect"
    # Status may still be red through the ratio rule; the monologue rule must not fire.
    assert alert is None or alert.alert_type != "monologue_warning"


def test_seller_monologue_after_prospect_monologue_alerts_once() -> None:
    config = TalkTimeConfig(monologue_warning_seconds=1)
    tracker = TalkTimeTracker(config)

    tracker.record_speech("prospect", 0, 1500)
    first = tracker.check_alerts(1500)
    tracker.record_speech("self", 1500, 3000)
    second = tracker.check_alerts(3000)
    third = tracker.check_alerts(3100)

    assert first is None or first.alert_type != "monologue_warning"
    assert second is not None and second.alert_type == "monologue_warning"
    assert third is None or third.alert_type != "monologue_warning"


def test_seller_alert_rearms_after_prospect_turn() -> None:
    config = TalkTimeConfig(monologue_warning_seconds=1)
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 1500)
    assert tracker.check_alerts(1500) is not None
    tracker.record_speech("prospect", 1600, 1700)
    tracker.check_alerts(1700)
    tracker.record_speech("self", 1800, 3300)
    again = tracker.check_alerts(3300)

    assert again is not None and again.alert_type == "monologue_warning"
