from __future__ import annotations

import json
from pathlib import Path

import pytest

from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.generator import CallReport, SessionData


def _build_session() -> SessionData:
    return SessionData(
        session_id="session-123",
        call_start_ms=0,
        call_end_ms=90000,
        started_at="2026-09-06T10-00-00+00:00",
        ended_at="2026-09-06T10-01-30+00:00",
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
        insights=[
            generator.InsightEvent(
                insight_type="doorvraag",
                text="Wat weet de klant al over Lime CRM?",
                grounding="prospect noemde Lime CRM",
                speculation="hoog",
                timestamp_ms=50000,
            ),
            generator.InsightEvent(
                insight_type="antwoord",
                text="Lime CRM mist een native facturatiekoppeling.",
                grounding="vraag van de verkoper",
                speculation="laag",
                timestamp_ms=70000,
                question="zwaktes van Lime CRM?",
            ),
        ],
    )


def test_generate_report_outputs_json(tmp_path) -> None:
    generator.REPORTS_DIR = Path(tmp_path) / "reports"

    report = generator.generate_report(_build_session())

    assert isinstance(report, CallReport)
    assert report.session_id == "session-123"
    assert report.call_duration_ms == 90000
    assert report.call_started_at == "2026-09-06T10-00-00+00:00"
    assert report.call_ended_at == "2026-09-06T10-01-30+00:00"
    assert report.prospect_name == "Robin"
    assert report.prospect_company == "Example BV"
    assert report.monologue_count == 1
    assert report.pain_points_detected[0].case_id == "case-001"
    assert report.conversation_summary == "Prospect benoemde lange offertedoorlooptijd."
    assert report.key_moments[0].type == "pain_point"
    assert len(report.insights) == 2
    assert report.insights[0].insight_type == "doorvraag"
    assert report.insights[0].question is None
    assert report.insights[1].insight_type == "antwoord"
    assert report.insights[1].question == "zwaktes van Lime CRM?"
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
        insights=[
            generator.InsightEvent(
                insight_type="antwoord",
                text="Sophie noemde eerder haar directe nummer.",
                grounding="Bel me op 06-12345678",
                speculation="laag",
                timestamp_ms=4500,
                question="wat is haar directe nummer?",
            ),
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
    assert written["insights"][0]["grounding"] == "Bel me op [TELEFOON]"
    assert "06-12345678" not in json.dumps(written["insights"])


def test_generate_report_skips_delivery_when_unconfigured(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sinks default off: generate_report() still calls the delivery seam
    (so the wiring is exercised), but it must be a true no-op -- no thread
    spawned, nothing attempted."""
    monkeypatch.delenv("REPORT_DELIVERY_DIR", raising=False)
    monkeypatch.delenv("REPORT_DELIVERY_ENDPOINT", raising=False)
    generator.REPORTS_DIR = Path(tmp_path) / "reports"

    real_deliver = generator.delivery.deliver_report_in_background
    results: list[object] = []
    monkeypatch.setattr(
        generator.delivery,
        "deliver_report_in_background",
        lambda *args, **kwargs: results.append(real_deliver(*args, **kwargs)),
    )

    generator.generate_report(_build_session())

    assert results == [None]


def test_generate_report_delivers_exact_disk_payload_when_configured(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivery sink receives the SAME bytes as the local file -- same shape,
    same redaction state -- never a second, differently-built payload."""
    delivery_dir = tmp_path / "delivery"
    delivery_dir.mkdir()
    monkeypatch.setenv("REPORT_DELIVERY_DIR", str(delivery_dir))
    monkeypatch.delenv("REPORT_DELIVERY_ENDPOINT", raising=False)
    generator.REPORTS_DIR = Path(tmp_path) / "reports"

    generator.generate_report(_build_session())

    local_files = list(generator.REPORTS_DIR.glob("*_report.json"))
    assert len(local_files) == 1

    delivered = delivery_dir / local_files[0].name
    # Background delivery: give the daemon thread a moment to land the file.
    import time as _time

    for _ in range(50):
        if delivered.exists():
            break
        _time.sleep(0.05)

    assert delivered.read_bytes() == local_files[0].read_bytes()


def test_generate_report_skips_delivery_when_aflevering_is_lokaal(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """klantmap-als-eenheid D2: aflevering="lokaal" opts a client OUT of the global
    report-delivery sinks entirely -- even when they ARE configured, unlike the
    unconfigured-sinks no-op case above."""
    delivery_dir = tmp_path / "delivery"
    delivery_dir.mkdir()
    monkeypatch.setenv("REPORT_DELIVERY_DIR", str(delivery_dir))
    monkeypatch.delenv("REPORT_DELIVERY_ENDPOINT", raising=False)
    generator.REPORTS_DIR = Path(tmp_path) / "reports"

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("deliver_report_in_background must not run when aflevering=lokaal")

    monkeypatch.setattr(generator.delivery, "deliver_report_in_background", _must_not_be_called)

    generator.generate_report(_build_session(), aflevering="lokaal")

    # The local disk write still happens unconditionally.
    local_files = list(generator.REPORTS_DIR.glob("*_report.json"))
    assert len(local_files) == 1


def test_generate_report_aflevering_none_keeps_global_sinks(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No client (or a client without aflevering set) keeps the existing global
    behaviour -- the delivery seam is still called."""
    monkeypatch.delenv("REPORT_DELIVERY_DIR", raising=False)
    monkeypatch.delenv("REPORT_DELIVERY_ENDPOINT", raising=False)
    generator.REPORTS_DIR = Path(tmp_path) / "reports"

    real_deliver = generator.delivery.deliver_report_in_background
    results: list[object] = []
    monkeypatch.setattr(
        generator.delivery,
        "deliver_report_in_background",
        lambda *args, **kwargs: results.append(real_deliver(*args, **kwargs)),
    )

    generator.generate_report(_build_session(), aflevering=None)

    assert results == [None]
