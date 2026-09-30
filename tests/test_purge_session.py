"""Tests for scripts/purge_session.py — AVG Art. 17 purge CLI."""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import purge_session as purge_module
from purge_session import (
    purge_before_date,
    purge_lead,
    purge_session,
)

from sales_copilot.core import context_docs
from sales_copilot.core.session_store import DetectionRecord, SessionStore


def test_default_uploads_dir_is_context_docs_upload_root() -> None:
    """The klantenmap comes exclusively from context_docs.UPLOAD_ROOT (honours
    the KLANTEN_ROOT env override) -- never a second, independent default that
    could silently diverge and miss the gesprekken/ archive on purge."""
    assert purge_module._DEFAULT_UPLOADS_DIR is context_docs.UPLOAD_ROOT


class _CaptureWriter:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


def _make_audit_line(session_id: str) -> str:
    return json.dumps({"session_id": session_id, "event_type": "detection", "ts": "2026-01-01T00:00:00+00:00"})


# ---------------------------------------------------------------------------
# Dry-run: no changes
# ---------------------------------------------------------------------------


def test_dry_run_no_changes(tmp_path):
    db_path = tmp_path / "sessions.db"
    audit_dir = tmp_path / "audit"
    data_dir = tmp_path / "data"
    audit_dir.mkdir()
    data_dir.mkdir()

    store = SessionStore(db_path)
    store.create_session("dry-uuid-001", profile="sales")

    ndjson_path = audit_dir / "recruitment.ndjson"
    ndjson_path.write_text(_make_audit_line("dry-uuid-001") + "\n")

    transcript = data_dir / "dry-uuid-001.json"
    transcript.write_text('{"ok": true}')

    original_ndjson = ndjson_path.read_text()

    counts = purge_session(
        "dry-uuid-001",
        dry_run=True,
        db_path=db_path,
        audit_dir=audit_dir,
        data_dir=data_dir,
    )

    # Nothing must have changed
    assert ndjson_path.read_text() == original_ndjson
    assert transcript.exists()

    # But counts must reflect what would be deleted
    assert counts["sessions"] == 1
    assert counts["audit_lines"] == 1
    assert counts["transcript_files"] == 1


# ---------------------------------------------------------------------------
# Confirm: SQLite rows deleted
# ---------------------------------------------------------------------------


def test_confirm_deletes_sqlite_rows(tmp_path):
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)
    store.create_session("purge-uuid-001", profile="sales")
    store.append_detection(
        DetectionRecord(
            session_id="purge-uuid-001",
            ts=time.time(),
            type="pain_point",
            subcat="cost",
            confidence=0.9,
            text="too expensive",
        )
    )
    store.append_detection(
        DetectionRecord(
            session_id="purge-uuid-001",
            ts=time.time(),
            type="objection",
            subcat=None,
            confidence=0.8,
            text="not sure",
        )
    )

    counts = purge_session(
        "purge-uuid-001",
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=tmp_path / "data",
    )

    assert counts["sessions"] == 1
    assert counts["detections"] == 2
    assert store.get_session_detections("purge-uuid-001") == []


# ---------------------------------------------------------------------------
# Confirm: transcript file unlinked
# ---------------------------------------------------------------------------


def test_confirm_unlinks_transcript_file(tmp_path):
    db_path = tmp_path / "sessions.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    store = SessionStore(db_path)
    store.create_session("file-uuid-001", profile="sales")

    transcript_json = data_dir / "file-uuid-001.json"
    transcript_txt = data_dir / "file-uuid-001.txt"
    transcript_json.write_text('{"transcript": "hello"}')
    transcript_txt.write_text("hello world")

    counts = purge_session(
        "file-uuid-001",
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=data_dir,
    )

    assert counts["transcript_files"] == 2
    assert not transcript_json.exists()
    assert not transcript_txt.exists()


# ---------------------------------------------------------------------------
# Confirm: NDJSON audit lines filtered
# ---------------------------------------------------------------------------


def test_confirm_filters_ndjson_audit(tmp_path):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    ndjson_path = audit_dir / "recruitment.ndjson"
    keep_line = _make_audit_line("other-session-999")
    remove_line = _make_audit_line("target-uuid-001")
    ndjson_path.write_text(keep_line + "\n" + remove_line + "\n")

    counts = purge_session(
        "target-uuid-001",
        dry_run=False,
        db_path=tmp_path / "sessions.db",
        audit_dir=audit_dir,
        data_dir=tmp_path / "data",
    )

    assert counts["audit_lines"] == 1

    remaining = [
        line for line in ndjson_path.read_text().splitlines() if line.strip()
    ]
    assert len(remaining) == 1
    record = json.loads(remaining[0])
    assert record["session_id"] == "other-session-999"


def test_confirm_purges_lead_by_email(tmp_path):
    leads_file = tmp_path / "leads.ndjson"
    leads_file.write_text(
        json.dumps({"email": "remove@example.com"}) + "\n"
        + json.dumps({"email": "keep@example.com"}) + "\n",
        encoding="utf-8",
    )

    removed = purge_lead(
        "REMOVE@example.com",
        dry_run=False,
        leads_file=leads_file,
    )

    assert removed == 1
    assert "remove@example.com" not in leads_file.read_text(encoding="utf-8")
    assert "keep@example.com" in leads_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Invalid session ID — no rows deleted
# ---------------------------------------------------------------------------


def test_invalid_session_id_returns_error(tmp_path):
    db_path = tmp_path / "sessions.db"
    SessionStore(db_path)  # creates empty DB

    counts = purge_session(
        "non-existent-uuid-0000",
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=tmp_path / "data",
    )

    assert counts["sessions"] == 0
    assert counts["detections"] == 0
    assert counts["transcript_files"] == 0
    assert counts["audit_lines"] == 0


def test_unsafe_session_id_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="safe filename segment"):
        purge_session(
            "../outside",
            dry_run=False,
            db_path=tmp_path / "sessions.db",
            audit_dir=tmp_path / "audit",
            data_dir=tmp_path / "sessions",
        )

    assert marker.exists()


def test_confirm_writes_purge_audit_event(monkeypatch, tmp_path):
    """A confirmed purge emits a non-PII purge audit event via the tiered writer."""
    db_path = tmp_path / "sessions.db"
    SessionStore(db_path).create_session("audit-uuid-001", profile="sales")

    capture = _CaptureWriter()
    monkeypatch.setattr(purge_module, "get_audit_writer", lambda: capture)

    counts = purge_session(
        "audit-uuid-001",
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=tmp_path / "data",
    )

    assert counts["sessions"] == 1
    assert len(capture.records) == 1
    record = capture.records[0]
    assert record["event_type"] == "purge"
    assert record["session_id"] == "audit-uuid-001"
    assert record["purged"]["sessions"] == 1


def test_dry_run_does_not_write_purge_audit_event(monkeypatch, tmp_path):
    """Dry-run purges must not emit a purge audit event."""
    db_path = tmp_path / "sessions.db"
    SessionStore(db_path).create_session("dry-audit-uuid-001", profile="sales")

    capture = _CaptureWriter()
    monkeypatch.setattr(purge_module, "get_audit_writer", lambda: capture)

    purge_session(
        "dry-audit-uuid-001",
        dry_run=True,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=tmp_path / "data",
    )

    assert capture.records == []


def test_confirm_purges_all_session_resources(tmp_path):
    session_id = "all-resources-001"
    db_path = tmp_path / "sessions.db"
    case_db_path = tmp_path / "cases.db"
    data_dir = tmp_path / "sessions"
    reports_dir = tmp_path / "reports"
    uploads_dir = tmp_path / "clients"

    SessionStore(db_path).create_session(session_id, profile="sales")
    with sqlite3.connect(case_db_path) as conn:
        conn.execute("CREATE TABLE call_sessions (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO call_sessions (id) VALUES (?)", (session_id,))

    session_dir = data_dir / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "self.wav").write_bytes(b"audio")
    upload = uploads_dir / "acme" / "context.md"
    upload.parent.mkdir(parents=True)
    upload.write_text("context", encoding="utf-8")
    (session_dir / "context_docs.json").write_text(
        json.dumps({"paths": [str(upload)]}),
        encoding="utf-8",
    )
    reports_dir.mkdir()
    report = reports_dir / f"2026-06-11_{session_id}_report.json"
    report.write_text("{}", encoding="utf-8")

    counts = purge_session(
        session_id,
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=data_dir,
        case_db_path=case_db_path,
        reports_dir=reports_dir,
        uploads_dir=uploads_dir,
    )

    assert counts["sessions"] == 1
    assert counts["call_sessions"] == 1
    assert counts["reports"] == 1
    assert counts["context_uploads"] == 1
    assert counts["transcript_files"] == 2
    assert not session_dir.exists()
    assert not upload.exists()
    assert not report.exists()
    with sqlite3.connect(case_db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM call_sessions WHERE id = ?", (session_id,)
        ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Bulk purge --before-date
# ---------------------------------------------------------------------------


def test_bulk_purge_before_date(tmp_path):
    db_path = tmp_path / "sessions.db"
    store = SessionStore(db_path)

    # Create two old sessions and one recent session
    store.create_session("old-uuid-001", profile="sales")
    store.create_session("old-uuid-002", profile="recruitment")

    # Manually set start_ts to past for old sessions
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    past_ts = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
    conn.execute("UPDATE sessions SET start_ts=? WHERE id IN (?,?)", (past_ts, "old-uuid-001", "old-uuid-002"))
    conn.commit()
    conn.close()

    store.create_session("recent-uuid-001", profile="sales")  # current time

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)

    totals = purge_before_date(
        cutoff,
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=tmp_path / "data",
    )

    assert totals["sessions"] == 2

    # Recent session must still exist
    conn2 = sqlite3.connect(str(db_path))
    remaining = conn2.execute("SELECT id FROM sessions").fetchall()
    conn2.close()
    remaining_ids = [r[0] for r in remaining]
    assert "recent-uuid-001" in remaining_ids
    assert "old-uuid-001" not in remaining_ids
    assert "old-uuid-002" not in remaining_ids


# ---------------------------------------------------------------------------
# klantmap-als-eenheid D3: klant archief + dossier auto-save + manual dossier
# ---------------------------------------------------------------------------


def test_purge_session_removes_klant_archief_for_that_session_only(tmp_path):
    session_id = "archief-sess-001"
    db_path = tmp_path / "sessions.db"
    uploads_dir = tmp_path / "clients"

    SessionStore(db_path).create_session(session_id, client_slug="acme")

    archive_root = uploads_dir / "acme" / "gesprekken"
    this_session_dir = archive_root / f"2026-09-28-{session_id}"
    this_session_dir.mkdir(parents=True)
    (this_session_dir / "transcript.md").write_text("call transcript", encoding="utf-8")
    (this_session_dir / "rapport.json").write_text("{}", encoding="utf-8")

    other_session_dir = archive_root / "2026-09-01-other-sess-002"
    other_session_dir.mkdir(parents=True)
    (other_session_dir / "transcript.md").write_text("other call", encoding="utf-8")

    counts = purge_session(
        session_id,
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=tmp_path / "data",
        uploads_dir=uploads_dir,
    )

    assert counts["klant_archief"] == 2
    assert not this_session_dir.exists()
    assert other_session_dir.is_dir()
    assert (other_session_dir / "transcript.md").read_text(encoding="utf-8") == "other call"


def test_purge_session_removes_own_dossier_auto_save_only(tmp_path):
    session_id = "dossier-sess-001"
    db_path = tmp_path / "sessions.db"
    uploads_dir = tmp_path / "clients"

    SessionStore(db_path).create_session(session_id, client_slug="acme")

    dossier_dir = uploads_dir / "acme" / "dossier"
    dossier_dir.mkdir(parents=True)
    auto_save = dossier_dir / f"2026-09-28T10-00-00_{session_id}_transcript.md"
    auto_save.write_text("auto-saved transcript", encoding="utf-8")
    manual_note = dossier_dir / "manual-note.md"
    manual_note.write_text("hand-curated dossier note", encoding="utf-8")
    other_auto_save = dossier_dir / "2026-09-01T09-00-00_other-sess-002_transcript.md"
    other_auto_save.write_text("other session's auto-save", encoding="utf-8")

    counts = purge_session(
        session_id,
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=tmp_path / "data",
        uploads_dir=uploads_dir,
    )

    assert counts["klant_dossier_auto_save"] == 1
    assert not auto_save.exists()
    assert manual_note.is_file()
    assert other_auto_save.is_file()


def test_purge_session_context_upload_never_deletes_manual_dossier_file(tmp_path):
    """A session's context-doc manifest can reference an existing file anywhere
    under the uploads root (resolve_context_doc_ids accepts any existing file,
    e.g. a reused prior dossier note) -- but purging that ONE session must never
    delete a client's hand-curated dossier material as a side effect."""
    session_id = "manifest-sess-001"
    db_path = tmp_path / "sessions.db"
    data_dir = tmp_path / "data"
    uploads_dir = tmp_path / "clients"

    SessionStore(db_path).create_session(session_id, client_slug="acme")

    manual_dossier_file = uploads_dir / "acme" / "dossier" / "manual-note.md"
    manual_dossier_file.parent.mkdir(parents=True)
    manual_dossier_file.write_text("hand-curated dossier note", encoding="utf-8")

    session_dir = data_dir / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "context_docs.json").write_text(
        json.dumps({"paths": [str(manual_dossier_file)]}),
        encoding="utf-8",
    )

    counts = purge_session(
        session_id,
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=data_dir,
        uploads_dir=uploads_dir,
    )

    assert counts["context_uploads"] == 0
    assert manual_dossier_file.is_file()
    assert manual_dossier_file.read_text(encoding="utf-8") == "hand-curated dossier note"


def test_purge_session_context_upload_never_deletes_other_sessions_archief(tmp_path):
    """Same guard as above, for a manifest that reused another session's
    gesprekken/ archive file instead of a dossier note."""
    session_id = "manifest-sess-002"
    db_path = tmp_path / "sessions.db"
    data_dir = tmp_path / "data"
    uploads_dir = tmp_path / "clients"

    SessionStore(db_path).create_session(session_id, client_slug="acme")

    archived_transcript = (
        uploads_dir / "acme" / "gesprekken" / "2026-09-01-other-sess-002" / "transcript.md"
    )
    archived_transcript.parent.mkdir(parents=True)
    archived_transcript.write_text("archived call content", encoding="utf-8")

    session_dir = data_dir / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "context_docs.json").write_text(
        json.dumps({"paths": [str(archived_transcript)]}),
        encoding="utf-8",
    )

    counts = purge_session(
        session_id,
        dry_run=False,
        db_path=db_path,
        audit_dir=tmp_path / "audit",
        data_dir=data_dir,
        uploads_dir=uploads_dir,
    )

    assert counts["context_uploads"] == 0
    assert archived_transcript.is_file()
