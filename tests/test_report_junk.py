"""Junk decision and skipped delivery (belapp-junkfilter-rapportschema D2+D3).

junk = the report model said ``gesprek_gevoerd == False``. The prospect-word count only
confirms it in ``junk_reason``; it never decides. These tests run the real ``decide_junk``,
the real ``_finish_report`` and the real generator; only the LLM call (``enrich_report``)
and the websocket are replaced.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from sales_copilot.modules.reports import __main__ as reports_main
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.enrichment import ReportEnrichment
from sales_copilot.modules.reports.generator import CallReport, TranscriptEntry
from sales_copilot.modules.reports.junk import (
    JunkConfig,
    count_prospect_words,
    decide_junk,
    load_junk_config,
)


def _entry(speaker: str, text: str) -> TranscriptEntry:
    return TranscriptEntry(speaker=speaker, text=text, start_ms=0, end_ms=1000)


def _words(count: int) -> str:
    return " ".join(f"woord{index}" for index in range(count))


def _model(gesprek_gevoerd: bool) -> ReportEnrichment:
    return ReportEnrichment(
        gesprek_gevoerd=gesprek_gevoerd, short_summary="", overview="", keywords=[], action_items=[]
    )


# ---------------------------------------------------------------- D2: the edge-case table


@pytest.mark.parametrize(
    ("prospect_words", "expected_reason_part"),
    [(11, "onder drempel 12"), (12, "boven drempel 12"), (13, "boven drempel 12")],
)
def test_the_word_count_never_decides_only_explains(prospect_words: int, expected_reason_part: str) -> None:
    transcript = [_entry("prospect", _words(prospect_words))]

    junk = decide_junk(_model(False), transcript)
    kept = decide_junk(_model(True), transcript)

    assert junk.junk is True
    assert junk.prospect_words == prospect_words
    assert expected_reason_part in (junk.reason or "")
    assert kept.junk is False
    assert kept.reason is None


@pytest.mark.parametrize(
    "transcript",
    [
        [],
        [_entry("prospect", "")],
        [_entry("prospect", "... ?! , - ")],
        [_entry("prospect", "Ondertiteling ingediend door de Amara.org gemeenschap")],
        [_entry("prospect", "Thanks for watching! Subtitles by the Amara.org community")],
        [_entry("prospect", "Bedankt voor het kijken")],
    ],
    ids=["empty", "blank-text", "punctuation-only", "nl-hallucination", "en-hallucination", "nl-thanks"],
)
def test_no_prospect_words_after_cleanup_counts_zero_and_is_junk_when_the_model_says_so(
    transcript: list[TranscriptEntry],
) -> None:
    assert count_prospect_words(transcript) == 0
    verdict = decide_junk(_model(False), transcript)
    assert verdict.junk is True
    assert "prospect-woorden: 0" in (verdict.reason or "")


def test_the_seller_leaving_a_40_word_voicemail_adds_no_prospect_words() -> None:
    transcript = [_entry("self", _words(40))]

    assert count_prospect_words(transcript) == 0
    assert decide_junk(_model(False), transcript).junk is True


def test_a_network_announcement_over_12_words_on_the_prospect_track_is_junk_when_the_model_says_false() -> None:
    announcement = (
        "De persoon die u probeert te bereiken is momenteel niet beschikbaar. "
        "Laat een bericht achter na de piep of probeer het later nog eens."
    )
    transcript = [_entry("prospect", announcement)]

    verdict = decide_junk(_model(False), transcript)

    assert count_prospect_words(transcript) > 12
    assert verdict.junk is True
    assert "boven drempel 12" in (verdict.reason or "")


def test_a_short_real_conversation_the_model_confirms_is_not_junk_even_under_12_words() -> None:
    transcript = [_entry("self", "Goedemiddag, spreek ik met Jan?"), _entry("prospect", "Ja, maar nu even niet.")]

    verdict = decide_junk(_model(True), transcript)

    assert verdict.junk is False
    assert verdict.prospect_words < 12


def test_speaker_unknown_counts_as_prospect_track() -> None:
    transcript = [_entry("unknown", _words(20))]

    assert count_prospect_words(transcript) == 20
    assert "boven drempel 12" in (decide_junk(_model(False), transcript).reason or "")


def test_hallucination_phrases_are_removed_before_counting_and_matched_case_insensitively() -> None:
    transcript = [_entry("prospect", f"THANKS FOR WATCHING {_words(3)} bedankt voor het kijken")]

    assert count_prospect_words(transcript) == 3


def test_the_reason_is_a_fixed_category_and_a_count_never_transcript_text() -> None:
    secret = "Jan Jansen jan@acme.nl 06 12345678"
    verdict = decide_junk(_model(False), [_entry("prospect", secret)])

    assert "Jansen" not in (verdict.reason or "")
    assert "acme" not in (verdict.reason or "")
    assert verdict.reason == ("model: geen gesprek (voicemail/IVR/geen gehoor); prospect-woorden: 7 (onder drempel 12)")


def test_the_config_holds_dutch_and_english_phrases_and_no_customer_names() -> None:
    config = load_junk_config()

    phrases = " | ".join(config.hallucination_phrases).lower()
    assert config.min_prospect_words == 12
    assert "ondertiteling ingediend door" in phrases
    assert "bedankt voor het kijken" in phrases
    assert "thanks for watching" in phrases
    assert "subtitles by" in phrases


def test_a_custom_threshold_only_changes_the_confirmation() -> None:
    config = JunkConfig(min_prospect_words=3, hallucination_phrases=())

    verdict = decide_junk(_model(False), [_entry("prospect", "een twee drie vier")], config)

    assert verdict.junk is True
    assert "boven drempel 3" in (verdict.reason or "")


# ---------------------------------------------------------------- D3: schema and delivery


@pytest.fixture()
def reports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "reports"
    monkeypatch.setattr(generator, "REPORTS_DIR", directory)
    monkeypatch.delenv("REPORT_REDACT_PII", raising=False)
    return directory


def _session(transcript: list[Any]) -> generator.SessionData:
    return generator.SessionData(
        session_id="session-junk",
        call_start_ms=0,
        call_end_ms=60000,
        prospect_name=None,
        prospect_company=None,
        context_docs=[],
        speech_events=[],
        phase_events=[],
        pain_points=[],
        monologues=[],
        transcript=transcript,
    )


_VOICEMAIL = [generator.TranscriptSegment("prospect", "De persoon is niet bereikbaar.", 0, 3000)]
_CONVERSATION = [
    generator.TranscriptSegment("self", "Goedemiddag.", 0, 1000),
    generator.TranscriptSegment("prospect", "Dag, zeg het maar.", 1000, 3000),
]


class _FakeWs:
    def __init__(self, sent: list[dict[str, Any]]) -> None:
        self._sent = sent

    async def __aenter__(self) -> _FakeWs:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, raw: str) -> None:
        self._sent.append(json.loads(raw))


class _Harness:
    def __init__(self) -> None:
        self.delivered: list[bytes] = []
        self.sent: list[dict[str, Any]] = []


@pytest.fixture()
def harness(monkeypatch: pytest.MonkeyPatch) -> _Harness:
    state = _Harness()
    monkeypatch.setattr(
        generator.delivery,
        "deliver_report_in_background",
        lambda payload, filename, config, **kwargs: state.delivered.append(payload),
    )
    monkeypatch.setattr(reports_main.websockets, "connect", lambda url: _FakeWs(state.sent))
    return state


def _model_says(monkeypatch: pytest.MonkeyPatch, enrichment: ReportEnrichment) -> None:
    async def _enrich(session: Any, detector_config: Any = None, *, terms: Any = ()) -> ReportEnrichment:
        return enrichment

    monkeypatch.setattr(reports_main, "enrich_report", _enrich)


async def _finish(session: generator.SessionData, **kwargs: Any) -> generator.WrittenReport:
    written = generator.write_report(session)
    await reports_main._finish_report(
        written,
        session,
        detector_config=None,
        aflevering=kwargs.pop("aflevering", None),
        client_slug=kwargs.pop("client_slug", None),
        coaching_url="ws://localhost:8760/ws/coaching",
    )
    return written


async def test_a_junk_call_is_marked_locally_and_never_reaches_the_sinks(
    reports_dir: Path, harness: _Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _model_says(monkeypatch, _model(False))
    caplog.set_level("INFO")

    written = await _finish(_session(_VOICEMAIL))

    data = json.loads(written.path.read_text(encoding="utf-8"))
    assert data["junk"] is True
    assert data["gesprek_gevoerd"] is False
    assert "voicemail/IVR/geen gehoor" in data["junk_reason"]
    assert harness.delivered == []
    assert "delivery skipped for session=session-junk: junk" in caplog.text


async def test_a_normal_call_is_delivered_and_not_marked(
    reports_dir: Path, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(monkeypatch, _model(True))

    written = await _finish(_session(_CONVERSATION))

    data = json.loads(written.path.read_text(encoding="utf-8"))
    assert (data["junk"], data["junk_reason"]) == (False, None)
    assert harness.delivered == [written.path.read_bytes()]


async def test_a_failed_enrichment_is_never_junk_and_is_delivered(
    reports_dir: Path, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(monkeypatch, ReportEnrichment.fail_open())

    written = await _finish(_session(_VOICEMAIL))

    assert json.loads(written.path.read_text(encoding="utf-8"))["junk"] is False
    assert len(harness.delivered) == 1


async def test_aflevering_lokaal_still_skips_the_sinks_for_a_normal_call(
    reports_dir: Path, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(monkeypatch, _model(True))

    written = await _finish(_session(_CONVERSATION), aflevering="lokaal")

    assert harness.delivered == []
    assert json.loads(written.path.read_text(encoding="utf-8"))["junk"] is False


async def test_report_enriched_carries_the_decision_and_no_transcript_or_names(
    reports_dir: Path, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(
        monkeypatch,
        ReportEnrichment(
            gesprek_gevoerd=False, short_summary="Jan Jansen belde", overview="", keywords=[], action_items=[]
        ),
    )
    session = _session([generator.TranscriptSegment("prospect", "Ik ben Jan Jansen, mail jan@acme.nl", 0, 2000)])
    session = dataclasses.replace(session, prospect_name="Jan Jansen")

    await _finish(session)

    [event] = harness.sent
    # The term-correction keys are counts (termenlijst-in-uitwerking D2), never text.
    assert set(event) == {
        "type",
        "session_id",
        "gesprek_gevoerd",
        "junk",
        "junk_reason",
        "term_correction_count",
        "term_corrections_pii_limited",
    }
    assert (event["term_correction_count"], event["term_corrections_pii_limited"]) == (0, 0)
    assert event["type"] == "report_enriched"
    assert event["session_id"] == "session-junk"
    assert event["junk"] is True
    assert event["gesprek_gevoerd"] is False
    assert "Jansen" not in json.dumps(event)
    assert "acme" not in json.dumps(event)


async def test_report_enriched_for_a_normal_call_says_not_junk(
    reports_dir: Path, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(monkeypatch, _model(True))

    await _finish(_session(_CONVERSATION))

    [event] = harness.sent
    assert (event["junk"], event["junk_reason"], event["gesprek_gevoerd"]) == (False, None, True)


async def test_a_dashboard_that_is_down_costs_neither_the_rewrite_nor_the_delivery(
    reports_dir: Path, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(url: str) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(reports_main.websockets, "connect", _refuse)
    _model_says(monkeypatch, _model(True))

    written = await _finish(_session(_CONVERSATION))

    assert len(harness.delivered) == 1
    assert json.loads(written.path.read_text(encoding="utf-8"))["gesprek_gevoerd"] is True


async def test_a_junk_call_is_still_archived_in_the_klantmap(
    reports_dir: Path, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    archived: list[CallReport] = []
    monkeypatch.setattr(
        reports_main,
        "_archive_session_to_klantmap",
        lambda report, client_slug, path: archived.append(report),
    )
    _model_says(monkeypatch, _model(False))

    await _finish(_session(_VOICEMAIL), client_slug="acme")

    assert [report.junk for report in archived] == [True]
    assert harness.delivered == []


def test_reports_written_before_the_junk_keys_still_load_and_deliver(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_copilot.modules.reports import delivery

    written = generator.write_report(_session(_CONVERSATION))
    data = json.loads(written.path.read_text(encoding="utf-8"))
    for key in ("junk", "junk_reason", "gesprek_gevoerd", "short_summary", "overview", "keywords", "action_items"):
        data.pop(key)
    old = json.dumps(data).encode("utf-8")
    written.path.write_bytes(old)

    # The consumers read plain JSON: none of the new keys is required.
    assert data.get("junk", False) is False
    assert reports_main._dossier_transcript_markdown(written.report).startswith("# Sessie session-junk")
    sent: list[bytes] = []
    monkeypatch.setattr(delivery, "deliver_report_in_background", lambda payload, *a, **kw: sent.append(payload))
    generator.deliver_report(generator.WrittenReport(written.report, written.path, old.decode("utf-8")))
    assert sent == [old]


def test_call_report_defaults_are_not_junk() -> None:
    report = generator.build_report(_session(_CONVERSATION))

    assert (report.junk, report.junk_reason) == (False, None)


def test_redact_pii_leaves_junk_reason_alone(reports_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_REDACT_PII", "true")
    written = generator.write_report(_session(_CONVERSATION))
    reason = decide_junk(_model(False), [_entry("prospect", "Jan jan@acme.nl")]).reason

    rewritten = generator.rewrite_with_enrichment(written, _model(False), junk=True, junk_reason=reason)

    on_disk = json.loads(rewritten.path.read_text(encoding="utf-8"))
    assert on_disk["junk"] is True
    assert on_disk["junk_reason"] == reason
    assert json.loads(rewritten.payload)["junk_reason"] == reason


async def test_tracked_task_path_runs_end_to_end(
    reports_dir: Path, harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _model_says(monkeypatch, _model(False))
    session = _session(_VOICEMAIL)
    written = generator.write_report(session)

    reports_main._schedule_report_finish(
        written,
        session,
        detector_config=None,
        aflevering=None,
        client_slug=None,
        coaching_url="ws://localhost:8760/ws/coaching",
    )
    await asyncio.wait_for(reports_main.wait_for_pending_reports(), 5)

    assert harness.delivered == []
    assert [event["junk"] for event in harness.sent] == [True]
