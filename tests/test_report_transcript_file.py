"""A readable transcript next to every report: ``<prefix>_transcript.md``.

Runs the real ``reports_main.main`` end to end (the tracker, the websocket and the report
model are replaced, nothing else), plus the pure pieces: the label resolution, the writer and
the start-call validation of the call preset's ``ui_labels``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from sales_copilot.core.config import CallConfig, DetectorConfig, validate_ui_labels
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.modules.reports import __main__ as reports_main
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.enrichment import ReportEnrichment, wait_for_pending_reports
from sales_copilot.modules.reports.session import SessionData as TrackerSessionData
from sales_copilot.websocket import hub_core

_SECRET_LINE = "Het budget is vijftigduizend euro"


@pytest.fixture()
def reports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "reports"
    monkeypatch.setattr(generator, "REPORTS_DIR", directory)
    return directory


@pytest.fixture(autouse=True)
def _language_nl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGUAGE", "nl")
    monkeypatch.delenv("REPORT_REDACT_PII", raising=False)


class _FakeTracker:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def start_session(self) -> None:
        return None

    async def end_session(self) -> TrackerSessionData:
        return TrackerSessionData(
            session_id="session-tr",
            started_at="2026-10-22T10:00:00+00:00",
            ended_at="2026-10-22T10:01:00+00:00",
            prospect_name=None,
            prospect_company=None,
            context_docs=[],
            transcript=[
                {"speaker": "prospect", "text": _SECRET_LINE, "start_ms": 2000, "end_ms": 4000},
                {"speaker": "self", "text": "Dat is helder.", "start_ms": 0, "end_ms": 2000},
                {"speaker": "self", "text": "   ", "start_ms": 4000, "end_ms": 4500},
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


async def _run_call(
    monkeypatch: pytest.MonkeyPatch, *, preset_name: str | None, ui_labels: dict[str, str] | None
) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-token")
    monkeypatch.setattr(reports_main, "load_env", lambda: None)
    monkeypatch.setattr(reports_main, "configure_logging", lambda: None)
    monkeypatch.setattr(reports_main, "SessionTracker", _FakeTracker)
    monkeypatch.setattr(reports_main, "SessionStore", lambda: None)
    monkeypatch.setattr(reports_main, "ConversionCounterStore", _NoConversionStore)
    monkeypatch.setattr(reports_main, "get_active_recorder", lambda: None)
    monkeypatch.setattr(reports_main, "enrich_report", _enrich)
    monkeypatch.setattr(reports_main.websockets, "connect", lambda url: _FakeWs())
    stop_event = asyncio.Event()
    stop_event.set()
    detector_config = DetectorConfig(preset_name=preset_name) if preset_name else None
    await reports_main.main(
        stop_event=stop_event, register_signals=False, detector_config=detector_config, ui_labels=ui_labels
    )
    await wait_for_pending_reports()


def _only_transcript(directory: Path) -> Path:
    [path] = directory.glob("*_transcript.md")
    return path


# ------------------------------------------------------------------ the file itself


async def test_every_report_gets_a_transcript_beside_it_with_owner_only_rights(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_call(monkeypatch, preset_name="sales", ui_labels=None)

    [report_path] = reports_dir.glob("*_report.json")
    transcript = _only_transcript(reports_dir)
    assert transcript.name == report_path.name.removesuffix("_report.json") + "_transcript.md"
    assert transcript.parent == report_path.parent
    assert stat.S_IMODE(transcript.stat().st_mode) == 0o600
    assert stat.S_IMODE(reports_dir.stat().st_mode) == 0o700
    assert sorted(path.name for path in reports_dir.iterdir()) == sorted([report_path.name, transcript.name])
    # Chronological, blank segments skipped, one line per segment.
    assert transcript.read_text(encoding="utf-8").splitlines() == [
        "# Sessie session-tr",
        "",
        "verkoper: Dat is helder.",
        f"prospect: {_SECRET_LINE}",
    ]


async def test_the_transcript_survives_the_enrichment_rewrite_unchanged(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_call(monkeypatch, preset_name="sales", ui_labels=None)

    stored = json.loads(next(reports_dir.glob("*_report.json")).read_text(encoding="utf-8"))
    assert stored["speaker_labels"] == {"self": "verkoper", "prospect": "prospect"}
    assert _SECRET_LINE in _only_transcript(reports_dir).read_text(encoding="utf-8")


async def test_no_transcript_text_reaches_the_logs(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        await _run_call(monkeypatch, preset_name="sales", ui_labels=None)

    assert "_transcript.md" in caplog.text
    assert _SECRET_LINE not in caplog.text
    assert "Dat is helder" not in caplog.text


def test_a_write_failure_is_logged_without_text_and_does_not_raise(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    written = generator.write_report(_generator_session())

    def _refuse(*args: Any, **kwargs: Any) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(generator, "write_transcript_copy", _refuse)
    with caplog.at_level(logging.WARNING):
        reports_main._write_transcript_next_to_report(written)

    assert "Could not write the readable transcript" in caplog.text
    assert _SECRET_LINE not in caplog.text
    assert list(reports_dir.glob("*_transcript.md")) == []


def test_the_transcript_follows_the_report_redaction_rule(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_REDACT_PII", "true")
    session = _generator_session("Mail me op jan@acme.nl graag")
    written = generator.write_report(session)

    path = generator.write_transcript_copy(written)

    assert "jan@acme.nl" not in path.read_text(encoding="utf-8")


def _generator_session(prospect_text: str = _SECRET_LINE) -> generator.SessionData:
    return generator.SessionData(
        session_id="session-gen",
        call_start_ms=0,
        call_end_ms=60000,
        prospect_name=None,
        prospect_company=None,
        context_docs=[],
        speech_events=[],
        phase_events=[],
        pain_points=[],
        conversation_summary=None,
        key_moments=[],
        monologues=[],
        transcript=[generator.TranscriptSegment("prospect", prospect_text, 0, 2000)],
    )


# ------------------------------------------------------------------ the labels


async def test_the_labels_follow_the_domain_preset(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_call(monkeypatch, preset_name="sales", ui_labels=None)

    text = _only_transcript(reports_dir).read_text(encoding="utf-8")
    assert "verkoper: Dat is helder." in text
    assert "Ik:" not in text


async def test_the_call_presets_own_labels_win_over_the_domain_preset(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _run_call(monkeypatch, preset_name="sales", ui_labels={"self": "Ik", "prospect": "Anna"})

    text = _only_transcript(reports_dir).read_text(encoding="utf-8")
    assert f"Anna: {_SECRET_LINE}" in text
    assert "Ik: Dat is helder." in text


async def test_the_notetaker_preset_gets_neutral_labels_in_dutch(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    presets = yaml.safe_load(resolve_app_path("config/presets.yaml").read_text(encoding="utf-8"))["presets"]
    notetaker = next(entry for entry in presets if entry["name"] == "notetaker")

    await _run_call(monkeypatch, preset_name="sales", ui_labels=notetaker["ui_labels"])

    text = _only_transcript(reports_dir).read_text(encoding="utf-8")
    assert "Ik: Dat is helder." in text
    assert f"Gesprekspartner: {_SECRET_LINE}" in text
    assert "verkoper" not in text


async def test_the_notetaker_preset_gets_neutral_labels_in_english(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGUAGE", "en")

    await _run_call(monkeypatch, preset_name="sales", ui_labels={})

    text = _only_transcript(reports_dir).read_text(encoding="utf-8")
    assert "Me: Dat is helder." in text
    assert f"Other: {_SECRET_LINE}" in text


def test_a_preset_that_cannot_be_read_falls_back_to_neutral_labels() -> None:
    labels = reports_main._speaker_labels(None, "does-not-exist")

    assert labels == {"self": "Ik", "prospect": "Gesprekspartner"}


def test_a_role_the_preset_leaves_out_is_neutral() -> None:
    labels = reports_main._speaker_labels({"self": "Consultant"}, None)

    assert labels == {"self": "Consultant", "prospect": "Gesprekspartner"}


# ------------------------------------------------------------------ start-call validation


def test_validate_ui_labels_accepts_none_empty_and_the_two_roles() -> None:
    assert validate_ui_labels(None) is None
    assert validate_ui_labels({}) == {}
    assert validate_ui_labels({"self": " Ik ", "prospect": "Gast"}) == {"self": "Ik", "prospect": "Gast"}


@pytest.mark.parametrize(
    "bad",
    ["Ik", ["Ik"], {"other": "x"}, {"self": 3}, {"self": "a\nb"}, {"self": "x" * 41}],
    ids=["string", "list", "unknown-role", "non-string", "newline", "too-long"],
)
def test_validate_ui_labels_refuses_malformed_values(bad: object) -> None:
    with pytest.raises(ValueError, match="ui_labels"):
        validate_ui_labels(bad)


def test_the_start_call_carries_the_labels_and_refuses_bad_ones() -> None:
    config = hub_core.extract_start_call_config({"config": {"ui_labels": {}}})
    assert config["ui_labels"] == {}
    assert "ui_labels" not in hub_core.extract_start_call_config({"config": {}})
    with pytest.raises(ValueError, match="ui_labels"):
        hub_core.extract_start_call_config({"config": {"ui_labels": "Ik"}})


def test_call_config_reads_the_labels_from_the_start_call_payload() -> None:
    from sales_copilot.__main__ import _parse_call_config

    assert _parse_call_config({"ui_labels": {}}).ui_labels == {}
    assert _parse_call_config({}).ui_labels is None
    with pytest.raises(ValueError, match="ui_labels"):
        CallConfig(ui_labels={"self": 1})  # type: ignore[dict-item]


def test_the_notetaker_preset_in_presets_yaml_names_no_roles() -> None:
    presets = yaml.safe_load(resolve_app_path("config/presets.yaml").read_text(encoding="utf-8"))["presets"]
    labelled = {entry["name"]: entry["ui_labels"] for entry in presets if "ui_labels" in entry}

    assert labelled == {"notetaker": {}}
