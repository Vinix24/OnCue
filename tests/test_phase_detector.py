import json

import pytest

from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.detector.phase_detector import (
    AutomaticPhaseDetector,
    TriggerBasedPhaseDetector,
    _extract_transcript_line,
)


class _FakePhaseClient:
    def __init__(self, phases: list[str | None]) -> None:
        self._phases = list(phases)

    async def aclassify_phase(self, transcript_lines: list[str]) -> str | None:
        assert transcript_lines
        if not self._phases:
            return None
        return self._phases.pop(0)


class _FakePhaseWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def test_extract_transcript_line_includes_speaker() -> None:
    payload = {"type": "transcript", "text": "Vraag over prijs", "speaker": "prospect"}

    line = _extract_transcript_line(payload)

    assert line == "prospect: Vraag over prijs"


def test_extract_transcript_line_rejects_invalid_payload() -> None:
    assert _extract_transcript_line({"type": "coaching"}) is None
    assert _extract_transcript_line({"type": "transcript", "text": " ", "speaker": "self"}) is None
    assert _extract_transcript_line({"type": "transcript", "text": "Hallo"}) == "Hallo"


@pytest.mark.asyncio
async def test_phase_detector_keeps_last_five_lines() -> None:
    config = DetectorConfig(auto_phase_detection=True)
    detector = AutomaticPhaseDetector(
        config,
        WebSocketConfig(),
        llm_client=_FakePhaseClient(["discovery"]),
        interval_seconds=30,
        line_limit=5,
    )

    for idx in range(7):
        detector._recent_lines.append(f"line-{idx}")  # noqa: SLF001

    ws = _FakePhaseWS()
    await detector._classify_and_emit(ws)  # noqa: SLF001

    assert list(detector._recent_lines) == ["line-2", "line-3", "line-4", "line-5", "line-6"]  # noqa: SLF001
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_phase_detector_emits_only_on_phase_change() -> None:
    config = DetectorConfig(auto_phase_detection=True)
    detector = AutomaticPhaseDetector(
        config,
        WebSocketConfig(),
        llm_client=_FakePhaseClient(["discovery", "discovery", "pitch", "closing"]),
        interval_seconds=30,
    )
    detector._recent_lines.extend(["self: welkom", "prospect: laten we starten"])  # noqa: SLF001
    ws = _FakePhaseWS()

    await detector._classify_and_emit(ws)  # noqa: SLF001
    await detector._classify_and_emit(ws)  # noqa: SLF001
    await detector._classify_and_emit(ws)  # noqa: SLF001
    await detector._classify_and_emit(ws)  # noqa: SLF001

    events = [json.loads(raw) for raw in ws.sent]
    assert [event["phase"] for event in events] == ["discovery", "pitch", "closing"]
    assert all(event["type"] == "phase_change" for event in events)
    assert all(event["source"] == "auto" for event in events)


# ---------------------------------------------------------------------------
# TriggerBasedPhaseDetector — 4 new scenarios
# ---------------------------------------------------------------------------


def test_open_to_discovery_after_3_vragen() -> None:
    detector = TriggerBasedPhaseDetector()
    assert detector.current_phase == "open"

    result_1 = detector.process_event("vraag")
    result_2 = detector.process_event("vraag")
    assert result_1 is None
    assert result_2 is None

    result_3 = detector.process_event("vraag")
    assert result_3 is not None
    assert result_3["type"] == "phase_change"
    assert result_3["from"] == "open"
    assert result_3["phase"] == "discovery"
    assert result_3["source"] == "trigger"
    assert detector.current_phase == "discovery"


def test_discovery_to_demo_on_buying_signal() -> None:
    detector = TriggerBasedPhaseDetector()
    # Manually advance to discovery
    detector._state.phase = "discovery"  # noqa: SLF001

    result = detector.process_event("buying_signal")
    assert result is not None
    assert result["type"] == "phase_change"
    assert result["from"] == "discovery"
    assert result["phase"] == "demo"
    assert result["source"] == "trigger"
    assert detector.current_phase == "demo"


def test_demo_to_close_on_objection_resolved() -> None:
    detector = TriggerBasedPhaseDetector()
    # Manually advance to demo
    detector._state.phase = "demo"  # noqa: SLF001

    objection_result = detector.process_event("objection")
    assert objection_result is None  # objection alone does not trigger close

    resolved_result = detector.process_event("objection_resolved")
    assert resolved_result is not None
    assert resolved_result["type"] == "phase_change"
    assert resolved_result["from"] == "demo"
    assert resolved_result["phase"] == "close"
    assert resolved_result["source"] == "trigger"
    assert detector.current_phase == "close"


@pytest.mark.asyncio
async def test_trigger_based_run_emits_phase_change() -> None:
    """Three vraag events trigger open→discovery; phase_change published to ws_out."""
    detector = TriggerBasedPhaseDetector()

    class _FakeWSIn:
        def __init__(self, messages: list[str]) -> None:
            self._it = iter(messages)

        def __aiter__(self):
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._it)
            except StopIteration:
                raise StopAsyncIteration

    class _FakeWSOut:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    ws_in = _FakeWSIn([
        json.dumps({"type": "detection", "subcat": "vraag", "confidence": 0.9}),
        json.dumps({"type": "detection", "subcat": "vraag", "confidence": 0.8}),
        json.dumps({"type": "detection", "subcat": "vraag", "confidence": 0.85}),
    ])
    ws_out = _FakeWSOut()

    await detector.run(ws_in, ws_out)

    assert len(ws_out.sent) == 1
    event = json.loads(ws_out.sent[0])
    assert event["type"] == "phase_change"
    assert event["from"] == "open"
    assert event["phase"] == "discovery"
    assert event["channel"] == "coaching"


def test_phase_no_backwards() -> None:
    detector = TriggerBasedPhaseDetector()
    # Manually advance to close
    detector._state.phase = "close"  # noqa: SLF001

    # No event type should move backwards
    for event_type in ("vraag", "buying_signal", "objection", "objection_resolved"):
        result = detector.process_event(event_type)
        # phase_change back to an earlier phase must not occur
        if result is not None:
            assert result.get("type") != "phase_change"
    assert detector.current_phase == "close"
