from __future__ import annotations

import pytest

from sales_copilot.modules.reports import __main__ as reports_main
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.__main__ import (
    _build_generator_session,
    _insights_from_session,
    _phase_events_from_transitions,
    _report_payload,
    _speech_events_from_transcript,
)
from sales_copilot.modules.reports.delivery import ReportDeliveryConfigError
from sales_copilot.modules.reports.session import SessionData


def _session_stub() -> SessionData:
    return SessionData(
        session_id="session-1",
        started_at="2026-04-13T00:00:00+00:00",
        ended_at="2026-04-13T00:01:30+00:00",
        prospect_name="Alex",
        prospect_company="Acme",
        context_docs=["/tmp/context.md"],
        transcript=[
            {"speaker": "self", "text": "Hallo", "start_ms": 0, "end_ms": 2000},
            {"speaker": "prospect", "text": "Dag", "start_ms": 2000, "end_ms": 4000},
        ],
        pain_points=[{"category": "offerteproces", "timestamp_ms": 45000, "case_id": "case-001"}],
        talk_time_snapshots=[],
        phase_transitions=[
            {"phase": "discovery", "timestamp_ms": 0},
            {"phase": "pitch", "timestamp_ms": 60000},
        ],
        coaching_alerts=[],
        summaries=[],
        insights=[
            {
                "type": "insight",
                "insight_type": "doorvraag",
                "text": "Wat weet de klant al over Lime CRM?",
                "grounding": "prospect noemde Lime CRM",
                "speculation": "hoog",
                "ttl_s": 120,
                "timestamp_ms": 12000,
            },
            {
                "type": "insight",
                "insight_type": "antwoord",
                "text": "Lime CRM mist een native koppeling.",
                "grounding": "vraag van de verkoper",
                "speculation": "laag",
                "ttl_s": 60,
                "question": "zwaktes van Lime CRM?",
                "timestamp_ms": 30000,
            },
            # Malformed entry (no timestamp_ms) -- must be skipped, not raise.
            {"insight_type": "risico", "text": "incompleet"},
        ],
    )


def test_speech_events_from_transcript() -> None:
    session = _session_stub()

    events = _speech_events_from_transcript(session)

    assert len(events) == 2
    assert events[0].speaker == "self"
    assert events[1].end_ms == 4000


def test_insights_from_session_skips_malformed_entries() -> None:
    session = _session_stub()

    events = _insights_from_session(session)

    assert len(events) == 2
    assert events[0].insight_type == "doorvraag"
    assert events[0].question is None
    assert events[1].insight_type == "antwoord"
    assert events[1].question == "zwaktes van Lime CRM?"


def test_phase_events_from_transitions() -> None:
    session = _session_stub()

    events = _phase_events_from_transitions(session, call_duration_ms=90000)

    assert len(events) == 2
    assert events[0].phase == "discovery"
    assert events[0].end_ms == 60000
    assert events[1].end_ms == 90000


def test_report_payload_contains_summary() -> None:
    report = generator.CallReport(
        session_id="session-1",
        call_duration_ms=90000,
        prospect_name=None,
        prospect_company=None,
        context_docs=[],
        phase_timeline=[],
        per_minute_talk_time=[],
        pain_points_detected=[],
        conversation_summary=None,
        key_moments=[],
        monologue_count=2,
        total_self_pct=0.4,
        total_prospect_pct=0.6,
        full_transcript=[],
        insights=[
            generator.InsightEntry(
                insight_type="doorvraag",
                text="Wat weet de klant al?",
                grounding="",
                speculation="laag",
                timestamp_ms=1000,
            )
        ],
    )

    payload = _report_payload(report, None)

    assert payload["type"] == "report_ready"
    assert payload["session_id"] == "session-1"
    assert payload["monologue_count"] == 2
    assert payload["insight_count"] == 1


def test_build_generator_session_maps_data() -> None:
    session = _session_stub()

    mapped = _build_generator_session(session)

    assert mapped.session_id == "session-1"
    assert mapped.prospect_name == "Alex"
    assert len(mapped.speech_events) == 2
    assert len(mapped.insights) == 2
    assert mapped.started_at == "2026-04-13T00:00:00+00:00"
    assert mapped.ended_at == "2026-04-13T00:01:30+00:00"


async def test_main_fails_fast_on_unusable_delivery_directory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured-but-missing REPORT_DELIVERY_DIR must fail at startup -- before
    any WebSocket connection is attempted -- not at the end of the first call."""
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv("REPORT_DELIVERY_DIR", str(missing))
    monkeypatch.delenv("REPORT_DELIVERY_ENDPOINT", raising=False)

    with pytest.raises(ReportDeliveryConfigError, match=str(missing)):
        await reports_main.main(register_signals=False)
