import numpy as np
import pytest

from sales_copilot.core.config import TalkTimeConfig
from sales_copilot.modules.talk_time import tracker as tracker_module
from sales_copilot.modules.talk_time.tracker import TalkTimeTracker
from sales_copilot.modules.talk_time.vad import VADProcessor


class _ScoreSequence:
    def __init__(self, scores: list[float]) -> None:
        self._scores = iter(scores)

    def __call__(self, audio, sample_rate=None):
        return next(self._scores)


def test_record_speech_rejects_invalid_speaker() -> None:
    tracker = TalkTimeTracker()
    with pytest.raises(ValueError):
        tracker.record_speech("agent", 0, 100)


def test_record_speech_rejects_invalid_range() -> None:
    tracker = TalkTimeTracker()
    with pytest.raises(ValueError):
        tracker.record_speech("self", 200, 100)


def test_get_state_empty_defaults() -> None:
    tracker = TalkTimeTracker()

    state = tracker.get_state(1000)

    assert state.rolling_self_pct == 0.0
    assert state.cumulative_self_pct == 0.0
    assert state.current_monologue_ms == 0
    assert state.monologue_speaker is None
    assert state.call_duration_ms == 0
    assert state.status == "green"


def test_call_duration_uses_first_start() -> None:
    tracker = TalkTimeTracker()

    tracker.record_speech("self", 5000, 7000)
    tracker.record_speech("prospect", 8000, 9000)

    assert tracker.get_state(10000).call_duration_ms == 5000


def test_call_duration_increments_after_start_call() -> None:
    tracker = TalkTimeTracker()

    tracker.mark_call_started(0)

    assert tracker.get_state(5000).call_duration_ms == 5000


def test_rolling_window_partial_overlap() -> None:
    config = TalkTimeConfig(rolling_window_seconds=1)
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 800)
    tracker.record_speech("prospect", 900, 1300)

    state = tracker.get_state(1500)

    assert state.rolling_self_pct == pytest.approx(0.4286, rel=1e-3)
    assert state.rolling_prospect_pct == pytest.approx(0.5714, rel=1e-3)


def test_cumulative_with_single_speaker() -> None:
    tracker = TalkTimeTracker()

    tracker.record_speech("self", 0, 1000)
    tracker.record_speech("self", 1000, 2000)

    state = tracker.get_state(2000)

    assert state.cumulative_self_pct == 1.0
    assert state.cumulative_prospect_pct == 0.0


def test_monologue_accumulates_for_contiguous_speech() -> None:
    tracker = TalkTimeTracker()

    tracker.record_speech("self", 0, 500)
    tracker.record_speech("self", 500, 900)

    state = tracker.get_state(900)

    assert state.current_monologue_ms == 900
    assert state.monologue_speaker == "self"


def test_monologue_resets_after_gap() -> None:
    tracker = TalkTimeTracker()

    tracker.record_speech("self", 0, 500)
    tracker.record_speech("self", 700, 900)

    state = tracker.get_state(900)

    assert state.current_monologue_ms == 200


def test_monologue_clears_after_end() -> None:
    tracker = TalkTimeTracker()

    tracker.record_speech("self", 0, 500)

    state = tracker.get_state(700)

    assert state.current_monologue_ms == 0
    assert state.monologue_speaker is None


def test_status_turns_red_on_monologue_threshold() -> None:
    config = TalkTimeConfig(monologue_warning_seconds=1, ratio_amber_threshold=1.0, ratio_red_threshold=2.0)
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 1500)

    assert tracker.get_state(1500).status == "red"


def test_ratio_threshold_boundaries() -> None:
    config = TalkTimeConfig(
        rolling_window_seconds=1,
        discovery_target_self=0.5,
        ratio_amber_threshold=0.1,
        ratio_red_threshold=0.2,
    )
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 610)
    tracker.record_speech("prospect", 610, 1000)
    assert tracker.get_state(1000).status == "amber"

    tracker.record_speech("self", 1000, 1710)
    tracker.record_speech("prospect", 1710, 2000)
    assert tracker.get_state(2000).status == "red"


def test_phase_switch_updates_target_ratio() -> None:
    config = TalkTimeConfig(discovery_target_self=0.3, pitch_target_self=0.7)
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 700)
    tracker.record_speech("prospect", 700, 1000)

    assert tracker.get_state(1000).status == "red"

    tracker.set_phase("pitch")
    assert tracker.get_state(1000).status == "green"


def test_set_phase_rejects_invalid() -> None:
    tracker = TalkTimeTracker()
    with pytest.raises(ValueError):
        tracker.set_phase("wrap")


def test_ratio_alert_interval_respected() -> None:
    config = TalkTimeConfig(
        rolling_window_seconds=10,
        discovery_target_self=0.5,
        ratio_amber_threshold=0.1,
        ratio_red_threshold=0.2,
        coaching_update_interval_ms=5000,
    )
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 700)
    tracker.record_speech("prospect", 700, 1000)

    assert tracker.check_alerts(1000) is not None
    assert tracker.check_alerts(4000) is None
    assert tracker.check_alerts(6000) is not None


def test_monologue_alert_interval_and_reset() -> None:
    config = TalkTimeConfig(
        monologue_warning_seconds=1,
        ratio_amber_threshold=1.0,
        ratio_red_threshold=2.0,
    )
    tracker = TalkTimeTracker(config)

    tracker.record_speech("self", 0, 22000)
    assert tracker.check_alerts(1200) is not None
    assert tracker.check_alerts(3000) is None
    assert tracker.check_alerts(21200) is not None

    tracker.record_speech("prospect", 25000, 25500)
    assert tracker.check_alerts(26000) is None

    tracker.record_speech("self", 27000, 28200)
    assert tracker.check_alerts(28200) is not None


def test_post_call_data_contains_durations() -> None:
    tracker = TalkTimeTracker()

    tracker.record_speech("self", 0, 100)
    tracker.record_speech("prospect", 200, 350)

    data = tracker.get_post_call_data()

    assert data == [
        {"speaker": "self", "start_ms": 0, "end_ms": 100, "duration_ms": 100},
        {"speaker": "prospect", "start_ms": 200, "end_ms": 350, "duration_ms": 150},
    ]


def test_reset_clears_rolling_and_cumulative_state() -> None:
    tracker = TalkTimeTracker()
    tracker.record_speech("self", 0, 100)
    tracker.record_speech("prospect", 100, 300)

    tracker.reset()
    state = tracker.get_state(1000)

    assert state.rolling_self_pct == 0.0
    assert state.rolling_prospect_pct == 0.0
    assert state.cumulative_self_pct == 0.0
    assert state.cumulative_prospect_pct == 0.0
    assert state.call_duration_ms == 0
    assert tracker.get_post_call_data() == []


def test_mark_call_ended_clears_state() -> None:
    tracker = TalkTimeTracker()
    tracker.mark_call_started(0)
    tracker.record_speech("self", 0, 100)

    tracker.mark_call_ended()
    state = tracker.get_state(1000)

    assert state.call_duration_ms == 0
    assert state.cumulative_self_pct == 0.0
    assert tracker.get_post_call_data() == []


def test_vad_emits_event_after_debounce() -> None:
    model = _ScoreSequence([0.6, 0.6, 0.1, 0.1])
    processor = VADProcessor(
        model,
        "self",
        sample_rate=16000,
        chunk_duration_ms=100,
        positive_threshold=0.5,
        negative_threshold=0.3,
        debounce_ms=200,
    )
    chunk = np.zeros(1600, dtype=np.float32)

    assert processor.process_chunk(chunk) is None
    assert processor.process_chunk(chunk) is None
    assert processor.process_chunk(chunk) is None
    event = processor.process_chunk(chunk)

    assert isinstance(event, tracker_module.SpeechEvent)
    assert event.start_ms == 0
    assert event.end_ms == 200


def test_vad_rejects_invalid_thresholds() -> None:
    model = _ScoreSequence([0.6])
    with pytest.raises(ValueError):
        VADProcessor(model, "self", positive_threshold=0.3, negative_threshold=0.3)
    with pytest.raises(ValueError):
        VADProcessor(model, "self", debounce_ms=-1)
