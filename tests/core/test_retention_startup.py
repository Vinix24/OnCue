"""Startup retention sweep (#13) — app-level enforcement of DATA_RETENTION_DAYS.

Covers: deletes only data older than the cutoff, no-op + single WARNING when
retention is disabled while old data exists, silence when disabled and empty, and
that the sweep never raises (so it can never block call start).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sales_copilot.core import retention
from sales_copilot.core.session_store import SessionStore


def _seed_session(db_path: Path, session_id: str, start_ts: float) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO sessions (id, start_ts, end_ts, profile, transcript_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, start_ts, start_ts + 1.0, "sales", None),
        )
        conn.commit()
    finally:
        conn.close()


def _make_dirs(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "db_path": tmp_path / "sessions.db",
        "reports_dir": tmp_path / "reports",
        "sessions_dir": tmp_path / "sessions",
        "audit_dir": tmp_path / "audit",
        "uploads_dir": tmp_path / "uploads",
        "case_db_path": tmp_path / "cases.db",
    }
    for key in ("reports_dir", "sessions_dir", "audit_dir", "uploads_dir"):
        paths[key].mkdir()
    return paths


def test_startup_sweep_deletes_only_files_older_than_cutoff(tmp_path, monkeypatch) -> None:
    paths = _make_dirs(tmp_path)
    SessionStore(paths["db_path"])  # initialise schema

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=100)).timestamp()
    new_ts = (now - timedelta(days=1)).timestamp()
    _seed_session(paths["db_path"], "old-sess", old_ts)
    _seed_session(paths["db_path"], "new-sess", new_ts)

    old_report = paths["reports_dir"] / "20260101_old-sess_report.json"
    new_report = paths["reports_dir"] / "20260101_new-sess_report.json"
    old_report.write_text("{}", encoding="utf-8")
    new_report.write_text("{}", encoding="utf-8")
    old_tx = paths["sessions_dir"] / "old-sess.json"
    new_tx = paths["sessions_dir"] / "new-sess.json"
    old_tx.write_text("{}", encoding="utf-8")
    new_tx.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("DATA_RETENTION_DAYS", "30")
    totals = retention.run_retention_sweep(now=now, **paths)

    assert totals is not None
    assert totals["sessions"] == 1  # only the old session purged
    assert not old_report.exists()
    assert not old_tx.exists()
    assert new_report.exists()
    assert new_tx.exists()


def test_sweep_noop_and_warns_when_disabled_with_old_data(
    tmp_path, monkeypatch, caplog
) -> None:
    monkeypatch.delenv("DATA_RETENTION_DAYS", raising=False)
    sessions_dir = tmp_path / "sessions"
    reports_dir = tmp_path / "reports"
    sessions_dir.mkdir()
    reports_dir.mkdir()
    (sessions_dir / "leftover.json").write_text("{}", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = retention.run_retention_sweep(
            reports_dir=reports_dir, sessions_dir=sessions_dir
        )

    assert result is None  # disabled → no purge ran
    assert "retention is disabled" in caplog.text.lower()


def test_sweep_silent_when_disabled_and_empty(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.delenv("DATA_RETENTION_DAYS", raising=False)
    sessions_dir = tmp_path / "sessions"
    reports_dir = tmp_path / "reports"
    sessions_dir.mkdir()
    reports_dir.mkdir()

    with caplog.at_level(logging.WARNING):
        result = retention.run_retention_sweep(
            reports_dir=reports_dir, sessions_dir=sessions_dir
        )

    assert result is None
    assert "retention is disabled" not in caplog.text.lower()


def test_sweep_never_raises_on_purge_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_RETENTION_DAYS", "30")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated purge failure")

    monkeypatch.setattr(retention, "_load_purge_before_date", lambda: _boom)

    # Must degrade to None, never propagate — the sweep can never block call start.
    result = retention.run_retention_sweep(
        reports_dir=tmp_path / "reports", sessions_dir=tmp_path / "sessions"
    )
    assert result is None
