"""Tests for scripts/eval_report_junk.py: reading sessions and the shape of the output.

Synthetic sessions in a temporary SQLite, the report model replaced by a local coroutine:
no network, no real data.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from sales_copilot.modules.reports.enrichment import ReportEnrichment
from scripts import eval_report_junk as erj

SECRET = "GEHEIMEKLANTNAAM"

REAL_ID = "aaaaaaaa-1111-2222-3333-444444444444"
VOICEMAIL_ID = "bbbbbbbb-1111-2222-3333-444444444444"
EMPTY_ID = "cccccccc-1111-2222-3333-444444444444"


def _entry(speaker: str, text: str, start_ms: int, end_ms: int) -> dict[str, object]:
    return {"speaker": speaker, "text": text, "start_ms": start_ms, "end_ms": end_ms, "is_final": True}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "cases.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE call_sessions (id TEXT PRIMARY KEY, started_at TIMESTAMP NOT NULL, "
        "ended_at TIMESTAMP, transcript TEXT)"
    )
    real = [
        _entry("prospect", f"Goedemiddag met {SECRET}, ik bel over de offerte", 0, 4000),
        _entry("self", "Dag, ik wil graag even afstemmen over de planning", 4000, 9000),
    ]
    voicemail = [_entry("prospect", "U bent verbonden met de voicemail van deze abonnee", 0, 5000)]
    conn.executemany(
        "INSERT INTO call_sessions (id, started_at, ended_at, transcript) VALUES (?, ?, ?, ?)",
        [
            (REAL_ID, "2026-09-01T10:00:00", "2026-09-01T10:05:00", json.dumps(real)),
            (VOICEMAIL_ID, "2026-09-01T11:00:00", "2026-09-01T11:00:20", json.dumps(voicemail)),
            (EMPTY_ID, "2026-09-01T12:00:00", None, "[]"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_load_sessions_reads_all_rows(db_path: Path) -> None:
    sessions = erj.load_sessions(db_path)

    assert [s.session_id for s in sessions] == [REAL_ID, VOICEMAIL_ID, EMPTY_ID]
    assert len(sessions[0].transcript) == 2
    assert sessions[2].transcript == []


def test_load_sessions_missing_db_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        erj.load_sessions(tmp_path / "nope.db")


def test_load_sessions_ignores_malformed_transcript(tmp_path: Path) -> None:
    path = tmp_path / "bad.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE call_sessions (id TEXT, started_at TEXT, ended_at TEXT, transcript TEXT)")
    conn.execute("INSERT INTO call_sessions VALUES ('x', '2026-09-01T10:00:00', NULL, '{not json')")
    conn.commit()
    conn.close()

    assert erj.load_sessions(path)[0].transcript == []


def test_select_sessions_by_prefix_in_wanted_order(db_path: Path) -> None:
    sessions = erj.load_sessions(db_path)

    chosen = erj.select_sessions(sessions, ["bbbbbbbb", "aaaaaaaa"])

    assert [s.session_id for s in chosen] == [VOICEMAIL_ID, REAL_ID]


def test_select_sessions_unknown_and_ambiguous(db_path: Path) -> None:
    sessions = erj.load_sessions(db_path)

    with pytest.raises(LookupError, match="no session"):
        erj.select_sessions(sessions, ["zzzz"])
    with pytest.raises(LookupError, match="matches 3"):
        erj.select_sessions(sessions, [""])


def test_duration_from_timestamps_or_transcript(db_path: Path) -> None:
    real, voicemail, empty = erj.load_sessions(db_path)

    assert erj.duration_seconds(real) == 300
    assert erj.duration_seconds(voicemail) == 20
    assert erj.duration_seconds(empty) == 0
    no_end = erj.StoredSession("d", "2026-09-01T10:00:00", None, real.transcript)
    assert erj.duration_seconds(no_end) == 9


def test_scan_counts_without_llm(db_path: Path) -> None:
    rows = erj.scan(erj.load_sessions(db_path))

    assert [r.session for r in rows] == ["aaaaaaaa", "bbbbbbbb", "cccccccc"]
    assert rows[0].prospect_words == 8
    assert rows[2].prospect_words == 0


def test_evaluate_runs_chain_and_reports_fail_open(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_enrich(session: object, detector_config: object = None) -> ReportEnrichment:
        session_id = session.session_id  # type: ignore[attr-defined]
        if session_id == VOICEMAIL_ID:
            return ReportEnrichment(
                gesprek_gevoerd=False, short_summary="", overview="", keywords=[], action_items=[]
            )
        if session_id == EMPTY_ID:
            return ReportEnrichment.fail_open()
        return ReportEnrichment(
            gesprek_gevoerd=True, short_summary=f"samenvatting {SECRET}", overview="o", keywords=[], action_items=[]
        )

    monkeypatch.setattr(erj, "enrich_report", fake_enrich)
    rows = asyncio.run(erj.evaluate(erj.load_sessions(db_path), None))
    by_session = {r.session: r for r in rows}

    real, voicemail, empty = by_session["aaaaaaaa"], by_session["bbbbbbbb"], by_session["cccccccc"]
    assert (real.gesprek_gevoerd, real.junk, real.fail_open) == (True, False, False)
    assert (voicemail.gesprek_gevoerd, voicemail.junk, voicemail.fail_open) == (False, True, False)
    assert voicemail.junk_reason is not None and "prospect-woorden" in voicemail.junk_reason
    assert (empty.junk, empty.fail_open) == (False, True)
    assert real.duration_s == 300
    assert real.latency_s >= 0


def test_output_contains_no_transcript_text(
    db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_enrich(session: object, detector_config: object = None) -> ReportEnrichment:
        return ReportEnrichment(
            gesprek_gevoerd=False, short_summary=f"over {SECRET}", overview=SECRET, keywords=[SECRET], action_items=[]
        )

    monkeypatch.setattr(erj, "enrich_report", fake_enrich)
    monkeypatch.setattr(erj, "load_env", lambda: None)
    out = tmp_path / "result.json"

    assert erj.main(["--db", str(db_path), "--sessions", "aaaaaaaa,bbbbbbbb", "--out", str(out)]) == 0

    printed = capsys.readouterr().out
    written = out.read_text(encoding="utf-8")
    for text in (printed, written):
        assert SECRET not in text
        assert "voicemail van deze abonnee" not in text
        assert REAL_ID not in text
    assert set(json.loads(written)[0]) == {
        "session",
        "duration_s",
        "prospect_words",
        "gesprek_gevoerd",
        "junk",
        "junk_reason",
        "latency_s",
        "fail_open",
    }


def test_scan_output_contains_no_transcript_text(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert erj.main(["--db", str(db_path), "--scan"]) == 0

    printed = capsys.readouterr().out
    assert SECRET not in printed
    assert "aaaaaaaa" in printed
    assert REAL_ID not in printed


def test_main_errors_return_2(db_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert erj.main(["--db", str(tmp_path / "missing.db"), "--scan"]) == 2
    assert erj.main(["--db", str(db_path), "--sessions", "zzzz"]) == 2
    assert "no session" in capsys.readouterr().err
