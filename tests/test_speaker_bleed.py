"""Echo filter: microphone lines that repeat what the system side says at the same moment."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.speaker_bleed import (
    BleedSegment,
    drop_speaker_bleed,
    normalise_words,
)
from sales_copilot.modules.reports import __main__ as reports_main
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.enrichment import ReportEnrichment, wait_for_pending_reports
from sales_copilot.modules.reports.session import SessionData as TrackerSessionData
from sales_copilot.modules.transcriber.postcall import merge_labeled_segments

SENTENCE = "Wij hebben een probleem met de facturatie van onze klanten"


def seg(speaker: str, start_s: float, end_s: float, text: str) -> BleedSegment:
    return BleedSegment(speaker, round(start_s * 1000), round(end_s * 1000), text)


def run(segments: list[BleedSegment], **kw: Any):
    return drop_speaker_bleed(segments, "self", "prospect", enabled=kw.pop("enabled", True), **kw)


def test_normalise_words_ignores_case_and_punctuation_and_keeps_apostrophes() -> None:
    assert normalise_words("It's THE, tram!") == ["it's", "the", "tram"]
    assert normalise_words("Ja.") == ["ja"]
    assert normalise_words("") == []


def test_echo_with_other_punctuation_and_a_missing_word_is_removed() -> None:
    prospect = seg("prospect", 10.0, 13.0, SENTENCE)
    echo = seg("self", 10.3, 13.4, "wij hebben een probleem, met de facturatie van klanten.")
    result = run([prospect, echo])
    assert result.kept == [prospect]
    assert result.removed == [echo]
    assert (result.removed_long, result.removed_short) == (1, 0)


def test_echo_across_two_system_segments_is_removed() -> None:
    a = seg("prospect", 10.0, 11.5, "Wij hebben een probleem")
    b = seg("prospect", 11.5, 13.0, "met de facturatie van onze klanten")
    echo = seg("self", 10.2, 13.2, SENTENCE)
    assert run([a, b, echo]).kept == [a, b]


def test_mirroring_after_the_prospect_finished_is_kept() -> None:
    prospect = seg("prospect", 2.0, 5.0, "de facturen komen altijd te laat binnen")
    mirror = seg("self", 6.2, 8.0, "dus de facturen komen altijd te laat binnen?")
    result = run([prospect, mirror])
    assert result.kept == [prospect, mirror]
    assert result.removed == []


def test_real_overlapping_speech_with_a_different_sentence_is_kept() -> None:
    prospect = seg("prospect", 10.0, 13.0, SENTENCE)
    own = seg("self", 10.5, 12.5, "ik wil graag even iets anders vragen over de planning")
    assert run([prospect, own]).removed == []


def test_headset_call_keeps_a_short_ja_inside_prospect_speech() -> None:
    prospect = seg("prospect", 10.0, 14.0, SENTENCE)
    ja = seg("self", 11.0, 11.4, "Ja.")
    assert run([prospect, ja]).kept == [prospect, ja]


def test_short_line_identical_to_a_system_segment_is_removed_even_without_long_echo() -> None:
    prospect = seg("prospect", 10.0, 10.6, "Precies.")
    echo = seg("self", 10.1, 10.8, "precies")
    result = run([prospect, echo])
    assert result.removed == [echo]
    assert (result.removed_long, result.removed_short) == (0, 1)


def _echo_heavy_call() -> list[BleedSegment]:
    return [
        seg("prospect", 0.0, 3.0, SENTENCE),
        seg("self", 0.2, 3.2, SENTENCE),
        seg("prospect", 20.0, 23.0, "Het is lastig om van bus op tram over te stappen"),
        seg("self", 20.1, 23.1, "het is lastig om van bus op tram over te stappen"),
    ]


def test_echo_heavy_call_removes_a_clipped_word_at_the_edge() -> None:
    base = _echo_heavy_call()
    prospect = seg("prospect", 30.0, 33.0, "dat ging van bus op tram")
    clipped = seg("self", 33.1, 33.5, "tram")
    result = run([*base, prospect, clipped])
    assert clipped in result.removed
    assert result.removed_short == 1


def test_echo_heavy_call_removes_a_short_line_inside_system_speech() -> None:
    base = _echo_heavy_call()
    prospect = seg("prospect", 40.0, 46.0, "daar zit een heel verhaal achter vertelde hij")
    inside = seg("self", 42.0, 43.0, "mmm oké")
    long_short = seg("self", 41.0, 44.5, "oké")
    result = run([*base, prospect, inside, long_short])
    assert inside in result.removed
    assert long_short not in result.removed  # lasts more than 1.5 s


def test_echo_heavy_call_keeps_a_short_line_outside_system_speech() -> None:
    base = _echo_heavy_call()
    own = seg("self", 60.0, 60.5, "oké")
    assert own in run([*base, own]).kept


def test_only_microphone_lines_are_ever_removed() -> None:
    marker = BleedSegment("system", 10_000, 13_000, SENTENCE)
    unknown = BleedSegment("unknown", 10_000, 13_000, SENTENCE)
    prospect = seg("prospect", 10.0, 13.0, SENTENCE)
    assert run([marker, unknown, prospect]).removed == []


def test_filter_disabled_removes_nothing() -> None:
    segments = [seg("prospect", 10.0, 13.0, SENTENCE), seg("self", 10.3, 13.4, SENTENCE)]
    result = run(segments, enabled=False)
    assert result.kept == segments and result.removed == []


def test_setting_off_in_the_environment_disables_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [seg("prospect", 10.0, 13.0, SENTENCE), seg("self", 10.3, 13.4, SENTENCE)]
    monkeypatch.setenv("TRANSCRIPT_ECHO_FILTER", "false")
    assert drop_speaker_bleed(segments, "self", "prospect").removed == []
    monkeypatch.delenv("TRANSCRIPT_ECHO_FILTER")
    assert len(drop_speaker_bleed(segments, "self", "prospect").removed) == 1


def test_no_system_segments_returns_the_input_unchanged() -> None:
    segments = [seg("self", 1.0, 3.0, SENTENCE), seg("self", 5.0, 7.0, "ja")]
    result = run(segments)
    assert result.kept == segments and result.removed == []


def test_removal_is_logged_per_call_without_text(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="sales_copilot.core.speaker_bleed"):
        run([seg("prospect", 10.0, 13.0, SENTENCE), seg("self", 10.3, 13.4, SENTENCE)])
    assert any("1 microphone lines (1 long, 0 short)" in r.getMessage() for r in caplog.records)
    assert "facturatie" not in caplog.text


# ---------------------------------------------------------------- report integration


class _EchoTracker:
    def __init__(self, **kwargs: Any) -> None:
        pass

    async def start_session(self) -> None:
        return None

    async def end_session(self) -> TrackerSessionData:
        return TrackerSessionData(
            session_id="session-echo",
            started_at="2026-10-22T10:00:00+00:00",
            ended_at="2026-10-22T10:01:00+00:00",
            prospect_name=None,
            prospect_company=None,
            context_docs=[],
            transcript=[
                {"speaker": "prospect", "text": SENTENCE, "start_ms": 10000, "end_ms": 13000},
                {"speaker": "self", "text": SENTENCE.lower() + ".", "start_ms": 10300, "end_ms": 13400},
                {"speaker": "self", "text": "Dat begrijp ik goed.", "start_ms": 20000, "end_ms": 22000},
            ],
            pain_points=[],
            talk_time_snapshots=[],
            phase_transitions=[],
            coaching_alerts=[],
            summaries=[],
        )


class _NoConversionStore:
    def note_tier(self, tier: str) -> None:
        return None

    def record_gap_shown(self, session_id: str) -> None:
        return None


class _FakeWs:
    async def __aenter__(self) -> _FakeWs:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, raw: str) -> None:
        return None


async def _enrich(*args: Any, **kwargs: Any) -> ReportEnrichment:
    return ReportEnrichment(gesprek_gevoerd=True, short_summary="", overview="", keywords=[], action_items=[])


async def test_report_drops_echo_from_transcript_and_talk_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_dir = tmp_path / "reports"
    monkeypatch.setattr(generator, "REPORTS_DIR", reports_dir)
    monkeypatch.setenv("LANGUAGE", "nl")
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-token")
    monkeypatch.delenv("TRANSCRIPT_ECHO_FILTER", raising=False)
    monkeypatch.setattr(reports_main, "load_env", lambda: None)
    monkeypatch.setattr(reports_main, "configure_logging", lambda: None)
    monkeypatch.setattr(reports_main, "SessionTracker", _EchoTracker)
    monkeypatch.setattr(reports_main, "SessionStore", lambda: None)
    monkeypatch.setattr(reports_main, "ConversionCounterStore", _NoConversionStore)
    monkeypatch.setattr(reports_main, "get_active_recorder", lambda: None)
    monkeypatch.setattr(reports_main, "enrich_report", _enrich)
    monkeypatch.setattr(reports_main.websockets, "connect", lambda url: _FakeWs())
    stop_event = asyncio.Event()
    stop_event.set()
    await reports_main.main(
        stop_event=stop_event,
        register_signals=False,
        detector_config=DetectorConfig(preset_name="sales"),
    )
    await wait_for_pending_reports()

    [path] = reports_dir.glob("*_transcript.md")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.count(f"verkoper: {SENTENCE.lower()}.") == 0
    assert f"prospect: {SENTENCE}" in lines
    assert "verkoper: Dat begrijp ik goed." in lines
    stored = json.loads(next(reports_dir.glob("*_report.json")).read_text(encoding="utf-8"))
    # Only the 2 s own line remains as self speech: 2 s of 5 s total speech.
    assert stored["total_self_pct"] == pytest.approx(0.4)


def test_drop_echo_lines_keeps_entries_without_timing() -> None:
    session = asyncio.run(_EchoTracker().end_session())
    session.transcript.append({"speaker": "self", "text": SENTENCE})
    filtered = reports_main._drop_echo_lines(session)
    assert len(filtered.transcript) == 3
    assert {"speaker": "self", "text": SENTENCE} in filtered.transcript


# ---------------------------------------------------------------- postcall integration


@pytest.mark.parametrize(("mic", "system"), [("self", "prospect"), ("mic", "system")])
def test_postcall_merge_drops_the_echo_line(mic: str, system: str) -> None:
    channels = {
        system: [{"start": 10.0, "end": 13.0, "text": SENTENCE}],
        mic: [
            {"start": 10.3, "end": 13.4, "text": SENTENCE.lower() + "."},
            {"start": 20.0, "end": 22.0, "text": "dat begrijp ik goed"},
        ],
    }
    merged = merge_labeled_segments(channels)
    assert [(m["label"], m["start"]) for m in merged] == [(system, 10.0), (mic, 20.0)]


def test_postcall_merge_keeps_everything_when_only_one_channel_exists() -> None:
    merged = merge_labeled_segments({"self": [{"start": 1.0, "end": 2.0, "text": SENTENCE}]})
    assert len(merged) == 1
