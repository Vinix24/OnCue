"""Tests for the post-call, opt-in dossier auto-save (modules/reports/__main__.py, PR-D4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot.core import context_docs
from sales_copilot.modules.reports import __main__ as reports_main
from sales_copilot.modules.reports.generator import CallReport, TranscriptEntry


def _report(**overrides: object) -> CallReport:
    base = dict(
        session_id="session-1",
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
            TranscriptEntry(speaker="self", text="  ", start_ms=1000, end_ms=1200),
        ],
    )
    base.update(overrides)
    return CallReport(**base)


def test_dossier_transcript_markdown_includes_speaker_lines() -> None:
    markdown = reports_main._dossier_transcript_markdown(_report())

    assert "self: Hallo" in markdown
    assert "prospect: Budget is krap" in markdown


def test_dossier_transcript_markdown_skips_blank_entries() -> None:
    markdown = reports_main._dossier_transcript_markdown(_report())

    assert markdown.count("self:") == 1


def test_dossier_transcript_markdown_includes_company_and_contact() -> None:
    markdown = reports_main._dossier_transcript_markdown(_report())

    assert "Acme Corp" in markdown
    assert "Jan" in markdown


def test_maybe_save_noop_without_client_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("DOSSIER_AUTO_SAVE", "true")

    reports_main._maybe_save_dossier_transcript(_report(), None)

    assert list(tmp_path.iterdir()) == []


def test_maybe_save_noop_when_auto_save_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("DOSSIER_AUTO_SAVE", "false")

    reports_main._maybe_save_dossier_transcript(_report(), "acme-corp")

    assert list(tmp_path.iterdir()) == []


def test_maybe_save_writes_transcript_when_opted_in_and_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("DOSSIER_AUTO_SAVE", "true")

    reports_main._maybe_save_dossier_transcript(_report(), "acme-corp")

    saved = list((tmp_path / "acme-corp" / "dossier").glob("*.md"))
    assert len(saved) == 1
    assert "Budget is krap" in saved[0].read_text(encoding="utf-8")


def test_maybe_save_never_raises_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("DOSSIER_AUTO_SAVE", "true")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(reports_main, "write_dossier_transcript", _boom)

    # Must not raise.
    reports_main._maybe_save_dossier_transcript(_report(), "acme-corp")
