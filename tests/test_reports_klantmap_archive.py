"""Tests for the post-call klantmap archive (modules/reports/__main__.py, klantmap-als-eenheid D3).

Unlike the dossier auto-save (PR-D4, opt-in via DOSSIER_AUTO_SAVE), archiving a
client-linked call into <klantmap>/gesprekken/ is unconditional: the plan's
"Zonder klant gebeurt er niets extra" only gates on whether a client is linked
at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot.core import context_docs
from sales_copilot.modules.reports import __main__ as reports_main
from sales_copilot.modules.reports.generator import CallReport, TranscriptEntry


def _report(**overrides: object) -> CallReport:
    base = dict(
        session_id="session-9",
        call_duration_ms=1000,
        prospect_name="Jan",
        prospect_company="Acme Corp",
        context_docs=[],
        phase_timeline=[],
        per_minute_talk_time=[],
        pain_points_detected=[],
        conversation_summary=None,
        key_moments=[],
        monologue_count=0,
        total_self_pct=0.5,
        total_prospect_pct=0.5,
        full_transcript=[
            TranscriptEntry(speaker="self", text="Hallo", start_ms=0, end_ms=500),
            TranscriptEntry(speaker="prospect", text="Budget is krap", start_ms=500, end_ms=1000),
        ],
    )
    base.update(overrides)
    return CallReport(**base)


def _archive_dir(tmp_path: Path, session_id: str) -> Path:
    matches = list((tmp_path / "acme-corp" / "gesprekken").glob(f"*-{session_id}"))
    assert len(matches) == 1, matches
    return matches[0]


def test_archive_noop_without_client_slug(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)

    reports_main._archive_session_to_klantmap(_report(), None, None)

    assert list(tmp_path.iterdir()) == []


def test_archive_writes_transcript_when_client_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)

    reports_main._archive_session_to_klantmap(_report(), "acme-corp", None)

    archive_dir = _archive_dir(tmp_path, "session-9")
    transcript = archive_dir / "transcript.md"
    assert transcript.is_file()
    assert "Budget is krap" in transcript.read_text(encoding="utf-8")
    assert not (archive_dir / "rapport.json").exists()


def test_archive_copies_report_json_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    report_path = tmp_path / "2026-09-28T10-00-00_session-9_report.json"
    report_path.write_text('{"session_id": "session-9"}', encoding="utf-8")

    reports_main._archive_session_to_klantmap(_report(), "acme-corp", report_path)

    archive_dir = _archive_dir(tmp_path, "session-9")
    rapport = archive_dir / "rapport.json"
    assert rapport.is_file()
    assert rapport.read_text(encoding="utf-8") == report_path.read_text(encoding="utf-8")


def test_archive_never_raises_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)

    def _boom(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(reports_main, "resolve_client_dir", _boom)

    # Must not raise.
    reports_main._archive_session_to_klantmap(_report(), "acme-corp", None)
