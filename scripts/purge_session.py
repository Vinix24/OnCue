"""AVG Art. 17 purge-session CLI — verwijdert alle sporen van één sessie.

Usage:
    .venv/bin/python scripts/purge_session.py --session-id <UUID> [--confirm]
    .venv/bin/python scripts/purge_session.py --before-date YYYY-MM-DD [--confirm]

Default is dry-run. Pass --confirm to actually delete.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from sales_copilot.core.audit_ledger import get_audit_writer
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.core.session_store import SessionStore

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_DB = resolve_app_path(".vnx-data/sessions.db")
_DEFAULT_AUDIT_DIR = resolve_app_path(".vnx-data/audit")
_DEFAULT_DATA_DIR = resolve_app_path("data/sessions")
_DEFAULT_CASE_DB = resolve_app_path("data/cases.db")
_DEFAULT_REPORTS_DIR = resolve_app_path("data/reports")
_DEFAULT_UPLOADS_DIR = resolve_app_path("data/clients")
_DEFAULT_LEADS_FILE = resolve_app_path(".vnx-data/leads.ndjson")
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validate_session_id(session_id: str) -> str:
    if not _SAFE_SESSION_ID.fullmatch(session_id) or session_id in {".", ".."}:
        raise ValueError("session_id must be a safe filename segment")
    return session_id


def _count_sqlite_resources(db_path: Path, session_id: str) -> dict[str, int]:
    if not db_path.exists():
        return {"sessions": 0, "detections": 0}
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            ses = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id=?", (session_id,)
            ).fetchone()[0]
            det = conn.execute(
                "SELECT COUNT(*) FROM detections WHERE session_id=?", (session_id,)
            ).fetchone()[0]
            return {"sessions": ses, "detections": det}
        except sqlite3.OperationalError:
            return {"sessions": 0, "detections": 0}
        finally:
            conn.close()
    except sqlite3.Error:
        return {"sessions": 0, "detections": 0}


def _purge_ndjson(path: Path, session_id: str, *, dry_run: bool) -> int:
    """Filter session_id lines from NDJSON. Returns count of removed lines."""
    if not path.exists():
        return 0

    removed = 0
    kept_lines: list[str] = []

    with path.open("r+b") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        content = lock_f.read().decode("utf-8")

        for line in content.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
                if record.get("session_id") == session_id:
                    removed += 1
                    continue
            except json.JSONDecodeError:
                pass
            kept_lines.append(line)

        if not dry_run and removed > 0:
            tmp_path = path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as tmp_f:
                tmp_f.writelines(kept_lines)
                tmp_f.flush()
                os.fsync(tmp_f.fileno())
            tmp_path.chmod(0o600)
            os.replace(tmp_path, path)

    return removed


def purge_lead(email: str, *, dry_run: bool = True, leads_file: Path = _DEFAULT_LEADS_FILE) -> int:
    """Remove lead-capture records for one email address."""

    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        raise ValueError("email must be a valid non-empty address")
    if not leads_file.exists():
        return 0

    removed = 0
    kept_lines: list[str] = []
    with leads_file.open("r+b") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        content = lock_f.read().decode("utf-8")
        for line in content.splitlines(keepends=True):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                kept_lines.append(line)
                continue
            if str(record.get("email", "")).strip().lower() == normalized:
                removed += 1
            else:
                kept_lines.append(line)
        if not dry_run and removed:
            tmp_path = leads_file.with_suffix(".ndjson.tmp")
            with tmp_path.open("w", encoding="utf-8") as tmp_f:
                tmp_f.writelines(kept_lines)
                tmp_f.flush()
                os.fsync(tmp_f.fileno())
            tmp_path.chmod(0o600)
            os.replace(tmp_path, leads_file)
    return removed


def _purge_transcript_files(
    data_dir: Path, session_id: str, *, dry_run: bool
) -> int:
    """Unlink transcript files for session_id. Returns count removed."""
    candidates = [
        data_dir / f"{session_id}.json",
        data_dir / f"{session_id}.txt",
        data_dir / session_id / "transcript.json",
    ]
    removed = 0
    for path in candidates:
        if path.exists():
            if not dry_run:
                path.unlink()
            removed += 1
    session_dir = data_dir / session_id
    if session_dir.is_dir():
        session_files = sum(1 for path in session_dir.rglob("*") if path.is_file())
        if not dry_run:
            shutil.rmtree(session_dir)
        removed += session_files
    return removed


def _purge_case_session(case_db_path: Path, session_id: str, *, dry_run: bool) -> int:
    if not case_db_path.exists():
        return 0
    try:
        with sqlite3.connect(str(case_db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM call_sessions WHERE id = ?", (session_id,)
            ).fetchone()[0]
            if not dry_run and count:
                conn.execute("DELETE FROM call_sessions WHERE id = ?", (session_id,))
            return int(count)
    except sqlite3.Error:
        return 0


def _purge_reports(reports_dir: Path, session_id: str, *, dry_run: bool) -> int:
    removed = 0
    if not reports_dir.exists():
        return removed
    for path in reports_dir.glob(f"*_{session_id}_report.json"):
        if not dry_run:
            path.unlink()
        removed += 1
    return removed


def _purge_context_uploads(
    data_dir: Path,
    uploads_dir: Path,
    session_id: str,
    *,
    dry_run: bool,
) -> int:
    manifest = data_dir / session_id / "context_docs.json"
    if not manifest.exists():
        return 0
    try:
        raw_paths = json.loads(manifest.read_text(encoding="utf-8")).get("paths", [])
    except (OSError, json.JSONDecodeError, AttributeError):
        return 0
    root = uploads_dir.resolve()
    removed = 0
    for raw_path in raw_paths:
        if not isinstance(raw_path, str):
            continue
        path = Path(raw_path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file():
            if not dry_run:
                path.unlink()
            removed += 1
    return removed


def purge_session(
    session_id: str,
    *,
    dry_run: bool = True,
    db_path: Path = _DEFAULT_DB,
    audit_dir: Path = _DEFAULT_AUDIT_DIR,
    data_dir: Path = _DEFAULT_DATA_DIR,
    case_db_path: Path = _DEFAULT_CASE_DB,
    reports_dir: Path = _DEFAULT_REPORTS_DIR,
    uploads_dir: Path = _DEFAULT_UPLOADS_DIR,
) -> dict[str, int]:
    """Purge all data for session_id across SQLite, transcripts, NDJSON, and Supabase.

    Returns counts per resource type. In dry_run mode nothing is deleted.
    """
    session_id = _validate_session_id(session_id)
    counts: dict[str, int] = {
        "sessions": 0,
        "detections": 0,
        "transcript_files": 0,
        "audit_lines": 0,
        "call_sessions": 0,
        "reports": 0,
        "context_uploads": 0,
        "supabase_rows": 0,
    }

    # SQLite
    if dry_run:
        sqlite_counts = _count_sqlite_resources(db_path, session_id)
        counts["sessions"] = sqlite_counts["sessions"]
        counts["detections"] = sqlite_counts["detections"]
    elif db_path.exists():
        store = SessionStore(db_path)
        result = store.delete_session(session_id)
        counts["sessions"] = result["sessions"]
        counts["detections"] = result["detections"]

    counts["call_sessions"] = _purge_case_session(
        case_db_path, session_id, dry_run=dry_run
    )
    counts["reports"] = _purge_reports(reports_dir, session_id, dry_run=dry_run)
    counts["context_uploads"] = _purge_context_uploads(
        data_dir, uploads_dir, session_id, dry_run=dry_run
    )

    # Transcript, audio, metadata, and context manifest files
    counts["transcript_files"] = _purge_transcript_files(
        data_dir, session_id, dry_run=dry_run
    )

    # NDJSON audit files
    if audit_dir.exists():
        for ndjson_path in sorted(audit_dir.glob("*.ndjson")):
            counts["audit_lines"] += _purge_ndjson(
                ndjson_path, session_id, dry_run=dry_run
            )

    # Supabase (only when configured)
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    audit_backend = os.environ.get("AUDIT_BACKEND", "ndjson").lower()
    if audit_backend == "supabase" and supabase_url and supabase_key and not dry_run:
        try:
            from supabase import create_client

            client = create_client(supabase_url, supabase_key)
            result = (
                client.table("recruitment_audit")
                .delete()
                .eq("session_id", session_id)
                .execute()
            )
            counts["supabase_rows"] = len(result.data) if result.data else 0
        except Exception as exc:
            logger.error("Supabase purge failed: %s", exc)

    # Audit trail: record the purge action itself (not the deleted content).
    if not dry_run:
        try:
            get_audit_writer().write(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "session_id": session_id,
                    "event_type": "purge",
                    "purged": {k: v for k, v in counts.items() if v > 0},
                }
            )
        except Exception as exc:
            logger.error("purge audit write failed (non-blocking): %s", exc)

    return counts


def _get_sessions_before(db_path: Path, cutoff_ts: float) -> list[str]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE start_ts < ?", (cutoff_ts,)
            ).fetchall()
            return [row[0] for row in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def purge_before_date(
    cutoff: datetime,
    *,
    dry_run: bool = True,
    db_path: Path = _DEFAULT_DB,
    audit_dir: Path = _DEFAULT_AUDIT_DIR,
    data_dir: Path = _DEFAULT_DATA_DIR,
    case_db_path: Path = _DEFAULT_CASE_DB,
    reports_dir: Path = _DEFAULT_REPORTS_DIR,
    uploads_dir: Path = _DEFAULT_UPLOADS_DIR,
) -> dict[str, int]:
    """Purge all sessions with start_ts before cutoff datetime."""
    totals: dict[str, int] = {
        "sessions": 0,
        "detections": 0,
        "transcript_files": 0,
        "audit_lines": 0,
        "call_sessions": 0,
        "reports": 0,
        "context_uploads": 0,
        "supabase_rows": 0,
    }
    session_ids = _get_sessions_before(db_path, cutoff.timestamp())
    for sid in session_ids:
        counts = purge_session(
            sid,
            dry_run=dry_run,
            db_path=db_path,
            audit_dir=audit_dir,
            data_dir=data_dir,
            case_db_path=case_db_path,
            reports_dir=reports_dir,
            uploads_dir=uploads_dir,
        )
        for key in totals:
            totals[key] += counts.get(key, 0)
    return totals


def _print_counts(counts: dict[str, int], dry_run: bool, session_id: str) -> None:
    mode = "[DRY-RUN]" if dry_run else "[CONFIRMED]"
    print(f"\n{mode} Purge report for session: {session_id}")
    print(f"  SQLite sessions deleted   : {counts['sessions']}")
    print(f"  SQLite detections deleted : {counts['detections']}")
    print(f"  Transcript files removed  : {counts['transcript_files']}")
    print(f"  Audit NDJSON lines removed: {counts['audit_lines']}")
    print(f"  Case DB sessions deleted  : {counts['call_sessions']}")
    print(f"  Reports removed            : {counts['reports']}")
    print(f"  Context uploads removed    : {counts['context_uploads']}")
    if counts["supabase_rows"]:
        print(f"  Supabase rows deleted     : {counts['supabase_rows']}")
    if dry_run:
        print("\n  Nothing was changed. Pass --confirm to apply.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AVG Art. 17 purge-session CLI for OnCue"
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--session-id", metavar="UUID", help="Single session to purge")
    target.add_argument(
        "--before-date",
        metavar="YYYY-MM-DD",
        help="Bulk purge all sessions started before this date (UTC)",
    )
    target.add_argument("--email", help="Remove lead-capture records for one email address")
    parser.add_argument(
        "--confirm",
        action="store_true",
        default=False,
        help="Actually delete. Default is dry-run.",
    )
    parser.add_argument("--db-path", type=Path, default=_DEFAULT_DB)
    parser.add_argument("--audit-dir", type=Path, default=_DEFAULT_AUDIT_DIR)
    parser.add_argument("--data-dir", type=Path, default=_DEFAULT_DATA_DIR)
    parser.add_argument("--case-db-path", type=Path, default=_DEFAULT_CASE_DB)
    parser.add_argument("--reports-dir", type=Path, default=_DEFAULT_REPORTS_DIR)
    parser.add_argument("--uploads-dir", type=Path, default=_DEFAULT_UPLOADS_DIR)
    parser.add_argument("--leads-file", type=Path, default=_DEFAULT_LEADS_FILE)

    args = parser.parse_args()
    dry_run = not args.confirm

    if args.email:
        try:
            removed = purge_lead(
                args.email,
                dry_run=dry_run,
                leads_file=args.leads_file,
            )
        except ValueError as exc:
            print(f"[!] {exc}", file=sys.stderr)
            return 2
        mode = "[DRY-RUN]" if dry_run else "[CONFIRMED]"
        print(f"\n{mode} Lead purge for {args.email}: {removed} record(s)")
        if dry_run:
            print("\n  Nothing was changed. Pass --confirm to apply.")
    elif args.session_id:
        try:
            counts = purge_session(
                args.session_id,
                dry_run=dry_run,
                db_path=args.db_path,
                audit_dir=args.audit_dir,
                data_dir=args.data_dir,
                case_db_path=args.case_db_path,
                reports_dir=args.reports_dir,
                uploads_dir=args.uploads_dir,
            )
        except ValueError as exc:
            print(f"[!] {exc}", file=sys.stderr)
            return 2
        _print_counts(counts, dry_run, args.session_id)
        if not dry_run and counts["sessions"] == 0:
            print(
                f"\n[!] Session '{args.session_id}' not found in database.",
                file=sys.stderr,
            )
            return 1
    else:
        try:
            cutoff = datetime.strptime(args.before_date, "%Y-%m-%d").replace(
                tzinfo=UTC
            )
        except ValueError:
            print(
                f"[!] Invalid date format: {args.before_date}. Use YYYY-MM-DD.",
                file=sys.stderr,
            )
            return 1
        totals = purge_before_date(
            cutoff,
            dry_run=dry_run,
            db_path=args.db_path,
            audit_dir=args.audit_dir,
            data_dir=args.data_dir,
            case_db_path=args.case_db_path,
            reports_dir=args.reports_dir,
            uploads_dir=args.uploads_dir,
        )
        mode = "[DRY-RUN]" if dry_run else "[CONFIRMED]"
        print(f"\n{mode} Bulk purge before {args.before_date}:")
        for key, val in totals.items():
            print(f"  {key}: {val}")
        if dry_run:
            print("\n  Nothing was changed. Pass --confirm to apply.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
