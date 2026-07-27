from __future__ import annotations

import json
from pathlib import Path

from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.generator import CallReport, SessionData


def _build_session() -> SessionData:
    return SessionData(
        session_id="session-123",
        call_start_ms=0,
        call_end_ms=90000,
        prospect_name="Robin",
        prospect_company="Example BV",
        context_docs=["/tmp/context.md"],
        speech_events=[
            generator.SpeechEvent("self", 0, 30000),
            generator.SpeechEvent("prospect", 30000, 60000),
            generator.SpeechEvent("self", 60000, 90000),
        ],
        phase_events=[
            generator.PhaseEvent("discovery", 0, 60000),
            generator.PhaseEvent("pitch", 60000, 90000),
        ],
        pain_points=[
            generator.PainPointEvent("offerteproces", 45000, "case-001"),
        ],
        conversation_summary="Prospect benoemde lange offertedoorlooptijd.",
        key_moments=[
            generator.KeyMoment(
                type="pain_point",
                timestamp_ms=45000,
                description="Eerste pain point gedetecteerd: offerteproces.",
            )
        ],
        monologues=[{"start_ms": 0, "end_ms": 12000}],
        transcript=[
            generator.TranscriptSegment("self", "Hallo", 0, 2000),
            generator.TranscriptSegment("prospect", "Dag", 2000, 4000),
        ],
    )


def test_generate_report_outputs_json(tmp_path) -> None:
    generator.REPORTS_DIR = Path(tmp_path) / "reports"

    report = generator.generate_report(_build_session())

    assert isinstance(report, CallReport)
    assert report.session_id == "session-123"
    assert report.call_duration_ms == 90000
    assert report.prospect_name == "Robin"
    assert report.prospect_company == "Example BV"
    assert report.monologue_count == 1
    assert report.pain_points_detected[0].case_id == "case-001"
    assert report.conversation_summary == "Prospect benoemde lange offertedoorlooptijd."
    assert report.key_moments[0].type == "pain_point"
    assert report.phase_timeline[0].duration_ms == 60000
    assert report.per_minute_talk_time[0].self_pct == 0.5
    assert report.per_minute_talk_time[0].prospect_pct == 0.5
    assert report.per_minute_talk_time[1].self_pct == 1.0
    assert report.total_self_pct == 2 / 3
    assert report.total_prospect_pct == 1 / 3
    assert report.context_docs == ["context.md"]

    output_files = list(generator.REPORTS_DIR.glob("*_report.json"))
    assert len(output_files) == 1
    assert output_files[0].read_text(encoding="utf-8")


def _build_session_with_pii() -> SessionData:
    """Session whose prospect name and transcript carry redactable PII."""
    return SessionData(
        session_id="session-pii",
        call_start_ms=0,
        call_end_ms=60000,
        prospect_name="Sophie",
        prospect_company="Example BV",
        context_docs=["/tmp/context.md"],
        speech_events=[generator.SpeechEvent("prospect", 0, 60000)],
        phase_events=[generator.PhaseEvent("discovery", 0, 60000)],
        pain_points=[],
        conversation_summary=None,
        key_moments=[],
        monologues=[],
        transcript=[
            generator.TranscriptSegment("prospect", "Bel me op 06-12345678", 0, 4000),
        ],
    )


def _written_report(reports_dir: Path) -> dict:
    output_files = list(reports_dir.glob("*_report.json"))
    assert len(output_files) == 1
    return json.loads(output_files[0].read_text(encoding="utf-8"))


def test_report_keeps_pii_verbatim_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("REPORT_REDACT_PII", raising=False)
    generator.REPORTS_DIR = Path(tmp_path) / "reports"

    report = generator.generate_report(_build_session_with_pii())

    assert report.prospect_name == "Sophie"
    written = _written_report(generator.REPORTS_DIR)
    assert written["prospect_name"] == "Sophie"
    assert written["full_transcript"][0]["text"] == "Bel me op 06-12345678"


def test_report_redacts_pii_when_flag_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REPORT_REDACT_PII", "true")
    generator.REPORTS_DIR = Path(tmp_path) / "reports"

    report = generator.generate_report(_build_session_with_pii())

    # In-memory report (live coaching payload) stays verbatim.
    assert report.prospect_name == "Sophie"
    assert report.full_transcript[0].text == "Bel me op 06-12345678"

    # On-disk artifact is redacted.
    written = _written_report(generator.REPORTS_DIR)
    assert written["prospect_name"] == "[NAAM]"
    assert "Sophie" not in json.dumps(written)
    assert written["full_transcript"][0]["text"] == "Bel me op [TELEFOON]"
    assert "06-12345678" not in json.dumps(written)
