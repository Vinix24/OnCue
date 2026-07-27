from sales_copilot.modules.talk_time.publisher import TalkTimePublisher
from sales_copilot.modules.talk_time.tracker import TalkTimeTracker


def test_phase_change_payload_updates_tracker() -> None:
    tracker = TalkTimeTracker()
    publisher = TalkTimePublisher(tracker)

    publisher._handle_phase_payload({"type": "phase_change", "phase": "pitch"})
    state = tracker.get_state(0)

    assert state.phase == "pitch"


def test_invalid_phase_payload_ignored() -> None:
    tracker = TalkTimeTracker()
    publisher = TalkTimePublisher(tracker)

    publisher._handle_phase_payload({"type": "phase_change", "phase": "invalid"})
    state = tracker.get_state(0)

    assert state.phase == "discovery"
