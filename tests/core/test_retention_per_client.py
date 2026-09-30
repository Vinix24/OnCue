"""Per-client retention sweep (klantmap-als-eenheid D3).

A client's own `klant.yaml` `bewaren_dagen` is a second, independent retention
window on top of the global `DATA_RETENTION_DAYS` sweep tested in
`test_retention_startup.py`. Mandatory tiebreaker (plan-gate ronde 3): a
client's own window must fire even when the global sweep is disabled.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sales_copilot.core import retention
from sales_copilot.core.session_store import SessionStore


def _write_klant_yaml(klanten_root: Path, slug: str, *, bewaren_dagen: int | None) -> None:
    client_dir = klanten_root / slug
    client_dir.mkdir(parents=True, exist_ok=True)
    lines = ["bedrijf: Acme BV"]
    if bewaren_dagen is not None:
        lines.append(f"bewaren_dagen: {bewaren_dagen}")
    (client_dir / "klant.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_dirs(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "db_path": tmp_path / "sessions.db",
        "reports_dir": tmp_path / "reports",
        "sessions_dir": tmp_path / "sessions",
        "audit_dir": tmp_path / "audit",
        "uploads_dir": tmp_path / "clients",
        "case_db_path": tmp_path / "cases.db",
    }
    for key in ("reports_dir", "sessions_dir", "audit_dir", "uploads_dir"):
        paths[key].mkdir()
    return paths


def _session_exists(db_path: Path, session_id: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return bool(row[0])
    finally:
        conn.close()


def test_per_client_bewaren_dagen_fires_even_when_global_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    """Mandatory tiebreaker: DATA_RETENTION_DAYS=0 (global disabled) must not
    suppress a client's own bewaren_dagen cutoff."""
    monkeypatch.delenv("DATA_RETENTION_DAYS", raising=False)
    paths = _make_dirs(tmp_path)
    store = SessionStore(paths["db_path"])

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=10)).timestamp()
    store.create_session("acme-old-sess", client_slug="acme")
    store.create_session("unlinked-old-sess")
    with sqlite3.connect(str(paths["db_path"])) as conn:
        conn.execute(
            "UPDATE sessions SET start_ts = ? WHERE id IN (?, ?)",
            (old_ts, "acme-old-sess", "unlinked-old-sess"),
        )
        conn.commit()

    _write_klant_yaml(paths["uploads_dir"], "acme", bewaren_dagen=7)

    totals = retention.run_retention_sweep(now=now, **paths)

    assert totals is not None
    assert totals["sessions"] == 1
    assert not _session_exists(paths["db_path"], "acme-old-sess")
    # No client link and global disabled: must survive.
    assert _session_exists(paths["db_path"], "unlinked-old-sess")


def test_per_client_bewaren_dagen_zero_means_forever(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DATA_RETENTION_DAYS", raising=False)
    paths = _make_dirs(tmp_path)
    store = SessionStore(paths["db_path"])

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=3650)).timestamp()
    store.create_session("acme-ancient-sess", client_slug="acme")
    with sqlite3.connect(str(paths["db_path"])) as conn:
        conn.execute(
            "UPDATE sessions SET start_ts = ? WHERE id = ?", (old_ts, "acme-ancient-sess")
        )
        conn.commit()

    _write_klant_yaml(paths["uploads_dir"], "acme", bewaren_dagen=0)

    result = retention.run_retention_sweep(now=now, **paths)

    # The per-client policy is evaluated (that's the point of this test) but
    # purges nothing: 0 = "voor altijd", so the session survives.
    assert result is not None
    assert result.get("sessions", 0) == 0
    assert _session_exists(paths["db_path"], "acme-ancient-sess")


def test_per_client_override_is_not_recapped_by_shorter_global_cutoff(
    tmp_path: Path, monkeypatch
) -> None:
    """A client with a LONGER own window than the global one must keep its own
    sessions past the global cutoff -- the global sweep must not additionally
    catch them."""
    monkeypatch.setenv("DATA_RETENTION_DAYS", "5")
    paths = _make_dirs(tmp_path)
    store = SessionStore(paths["db_path"])

    now = datetime.now(UTC)
    ten_days_ago = (now - timedelta(days=10)).timestamp()
    store.create_session("acme-recent-ish-sess", client_slug="acme")
    with sqlite3.connect(str(paths["db_path"])) as conn:
        conn.execute(
            "UPDATE sessions SET start_ts = ? WHERE id = ?",
            (ten_days_ago, "acme-recent-ish-sess"),
        )
        conn.commit()

    _write_klant_yaml(paths["uploads_dir"], "acme", bewaren_dagen=365)

    totals = retention.run_retention_sweep(now=now, **paths)

    # Something ran (the global sweep, on an otherwise-empty DB) but this
    # client's own 365-day window is nowhere near firing at 10 days old, and
    # the shorter 5-day global cutoff must not have caught it either.
    assert totals is not None
    assert _session_exists(paths["db_path"], "acme-recent-ish-sess")


def test_global_sweep_still_covers_sessions_without_a_client_override(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DATA_RETENTION_DAYS", "5")
    paths = _make_dirs(tmp_path)
    store = SessionStore(paths["db_path"])

    now = datetime.now(UTC)
    old_ts = (now - timedelta(days=10)).timestamp()
    store.create_session("plain-old-sess")
    with sqlite3.connect(str(paths["db_path"])) as conn:
        conn.execute(
            "UPDATE sessions SET start_ts = ? WHERE id = ?", (old_ts, "plain-old-sess")
        )
        conn.commit()

    totals = retention.run_retention_sweep(now=now, **paths)

    assert totals is not None
    assert totals["sessions"] == 1
    assert not _session_exists(paths["db_path"], "plain-old-sess")
