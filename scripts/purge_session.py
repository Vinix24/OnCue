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

from sales_copilot.core import context_docs
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
# klantmap-als-eenheid: the klantenmap comes exclusively from
# context_docs.UPLOAD_ROOT (honours the KLANTEN_ROOT env override, D1) --
# NOT a second, independent resolve_app_path("data/clients") call, which
# would silently point at the wrong folder (and miss the gesprekken/ archive
# and dossier saves entirely) on an install with KLANTEN_ROOT set.
_DEFAULT_UPLOADS_DIR = context_docs.UPLOAD_ROOT
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


def _sessions_table_has_client_slug(conn: sqlite3.Connection) -> bool:
    """Whether the ``sessions`` table has the klantmap-als-eenheid D3 column.

    A database opened only through this raw ``sqlite3.connect`` (never through
    ``SessionStore``, which runs the additive migration) may predate the
    column -- checked explicitly rather than assumed, so a stale database
    degrades to "no client link known" instead of raising.
    """
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    except sqlite3.OperationalError:
        return False
    return "client_slug" in columns


def _get_session_client_slug(db_path: Path, session_id: str) -> str | None:
    """The client linked to ``session_id``, read BEFORE the session row is deleted."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            if not _sessions_table_has_client_slug(conn):
                return None
            row = conn.execute(
                "SELECT client_slug FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return row[0] if row and row[0] else None
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _resolve_klant_dir_under_root(uploads_dir: Path, client_slug: str) -> Path | None:
    """Resolve ``<uploads_dir>/<client_slug>``, refusing any escape of ``uploads_dir``.

    Defense in depth: ``client_slug`` here always originates from our own
    ``slugify_client_name``/``parse_client_slug_from_payload`` at write time, but a
    hand-edited or legacy database row is not re-validated on the way in, so this
    purge-side path is checked again before ever building a glob or a delete off it.
    """
    root = uploads_dir.resolve()
    candidate = (root / client_slug).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _purge_klant_archief(
    uploads_dir: Path, client_slug: str | None, session_id: str, *, dry_run: bool
) -> int:
    """Remove this session's own ``gesprekken/<datum>-<session_id>/`` archive.

    Only the archive directory named for THIS session is touched -- another
    session's (or the client's manually-curated dossier material's) files in the
    same client folder are never in scope here.
    """
    if not client_slug:
        return 0
    klant_dir = _resolve_klant_dir_under_root(uploads_dir, client_slug)
    if klant_dir is None:
        return 0
    archive_root = klant_dir / "gesprekken"
    if not archive_root.is_dir():
        return 0
    removed = 0
    for entry in archive_root.glob(f"*-{session_id}"):
        if not entry.is_dir():
            continue
        removed += sum(1 for path in entry.rglob("*") if path.is_file())
        if not dry_run:
            shutil.rmtree(entry)
    return removed


def _purge_klant_dossier_auto_save(
    uploads_dir: Path, client_slug: str | None, session_id: str, *, dry_run: bool
) -> int:
    """Remove THIS session's own auto-saved dossier transcript, never a manual one.

    ``write_dossier_transcript`` (core/context_docs.py) names its file
    ``<timestamp>_<session_id>_transcript.md`` under ``<klant>/dossier/`` -- the
    glob below matches only that exact, session-scoped pattern, so a client note
    someone dropped into ``dossier/`` by hand (any other filename) is never a
    candidate here, regardless of what a session's context-doc manifest may
    separately reference (see ``_purge_context_uploads``'s own dossier/gesprekken
    exclusion for that case).
    """
    if not client_slug:
        return 0
    klant_dir = _resolve_klant_dir_under_root(uploads_dir, client_slug)
    if klant_dir is None:
        return 0
    dossier_dir = klant_dir / "dossier"
    if not dossier_dir.is_dir():
        return 0
    removed = 0
    for entry in dossier_dir.glob(f"*_{session_id}_transcript.md"):
        if entry.is_file():
            if not dry_run:
                entry.unlink()
            removed += 1
    return removed


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


#: Subfolders of a client folder that a session's context-doc manifest must
#: never be allowed to delete through, even when a manifest path happens to
#: resolve inside one. A manifest records whatever `document_id`/`context_doc_ids`
#: a start-call payload named; `resolve_context_doc_ids` accepts ANY existing
#: file under the upload root, including one a caller reused from a client's
#: dossier or a previous call's archive rather than something freshly uploaded
#: for this session -- so without this guard, erasing one session could delete
#: another session's archived transcript, or a client's hand-curated dossier note.
_CLIENT_SUBFOLDERS_EXEMPT_FROM_CONTEXT_PURGE = frozenset({"dossier", "gesprekken"})


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
            relative = path.relative_to(root)
        except ValueError:
            continue
        if (
            len(relative.parts) >= 2
            and relative.parts[1] in _CLIENT_SUBFOLDERS_EXEMPT_FROM_CONTEXT_PURGE
        ):
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
        "klant_archief": 0,
        "klant_dossier_auto_save": 0,
    }
    # Read the client link BEFORE the sessions row is deleted below -- once
    # gone, there is no other record of which klantmap this session belonged to.
    client_slug = _get_session_client_slug(db_path, session_id)

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
    counts["klant_archief"] = _purge_klant_archief(
        uploads_dir, client_slug, session_id, dry_run=dry_run
    )
    counts["klant_dossier_auto_save"] = _purge_klant_dossier_auto_save(
        uploads_dir, client_slug, session_id, dry_run=dry_run
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


def _get_sessions_before(
    db_path: Path, cutoff_ts: float, *, exclude_client_slugs: set[str] | None = None
) -> list[str]:
    """Session ids with ``start_ts`` before ``cutoff_ts``.

    ``exclude_client_slugs`` lets a caller (the retention sweep) keep a
    client's own ``bewaren_dagen`` window fully in charge of that client's
    sessions -- excluded here so this global cutoff can never additionally
    purge (or double-purge) a session already governed by its own client's
    window, regardless of whether that window is shorter or longer than this
    one.
    """
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            if _sessions_table_has_client_slug(conn):
                rows = conn.execute(
                    "SELECT id, client_slug FROM sessions WHERE start_ts < ?", (cutoff_ts,)
                ).fetchall()
            else:
                rows = [
                    (row[0], None)
                    for row in conn.execute(
                        "SELECT id FROM sessions WHERE start_ts < ?", (cutoff_ts,)
                    ).fetchall()
                ]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    excluded = exclude_client_slugs or set()
    return [row[0] for row in rows if not (row[1] and row[1] in excluded)]


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
    exclude_client_slugs: set[str] | None = None,
) -> dict[str, int]:
    """Purge all sessions with start_ts before cutoff datetime.

    ``exclude_client_slugs``: see ``_get_sessions_before``.
    """
    totals: dict[str, int] = {
        "sessions": 0,
        "detections": 0,
        "transcript_files": 0,
        "audit_lines": 0,
        "call_sessions": 0,
        "reports": 0,
        "context_uploads": 0,
        "supabase_rows": 0,
        "klant_archief": 0,
        "klant_dossier_auto_save": 0,
    }
    session_ids = _get_sessions_before(
        db_path, cutoff.timestamp(), exclude_client_slugs=exclude_client_slugs
    )
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
    print(f"  Klant archief bestanden    : {counts['klant_archief']}")
    print(f"  Klant dossier auto-save    : {counts['klant_dossier_auto_save']}")
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
