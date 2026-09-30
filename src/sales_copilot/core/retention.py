"""Configurable data-retention policy (AVG storage limitation).

``DATA_RETENTION_DAYS`` controls how long session data is kept globally. ``0``
(the default) disables the global auto-purge. The actual deletion is performed
by ``purge_before_date``/``purge_session`` (``scripts/purge_session.py``); this
module is the pure, testable policy layer plus a thin :func:`run_retention_sweep`
that the orchestrator runs once at startup so retention is enforced at the app
level and not only by the ``scripts/retention_purge.py`` cron.

klantmap-als-eenheid D3 adds a second, independent retention layer: a client's
own ``klant.yaml`` ``bewaren_dagen`` governs that client's linked sessions,
and fires regardless of the global ``DATA_RETENTION_DAYS`` setting -- see
:func:`_run_per_client_sweep`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sales_copilot.core import context_docs
from sales_copilot.core.klant_config import KlantConfigError, load_klant_config
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.core.session_store import DEFAULT_DB as _DEFAULT_SESSION_DB

logger = logging.getLogger(__name__)

_RETENTION_ENV = "DATA_RETENTION_DAYS"

# Defaults mirror ``scripts/purge_session.py`` so the startup sweep operates on the
# same locations as the cron purge.
_DEFAULT_REPORTS_DIR = resolve_app_path("data/reports")
_DEFAULT_SESSIONS_DIR = resolve_app_path("data/sessions")


def resolve_retention_days() -> int:
    """Configured GLOBAL retention window in days; ``0`` means the global sweep is disabled.

    A client's own ``bewaren_dagen`` (klant.yaml) is a separate, independent
    window -- see :func:`_run_per_client_sweep` -- and is unaffected by this
    value being ``0``.
    """
    raw = os.environ.get(_RETENTION_ENV, "0").strip()
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r; treating retention as disabled", _RETENTION_ENV, raw
        )
        return 0
    return days if days > 0 else 0


def retention_cutoff(now: datetime | None = None) -> datetime | None:
    """The GLOBAL cutoff before which data should be purged, or ``None`` if disabled."""
    days = resolve_retention_days()
    if days <= 0:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=days)


def _dir_has_files(path: Path) -> bool:
    return path.exists() and any(p.is_file() for p in path.rglob("*"))


def _import_purge_session_symbol(name: str) -> Any | None:
    """Import ``name`` from ``scripts/purge_session.py``.

    The deletion logic lives in the cron script rather than the package, so this
    resolves it via the repo's ``scripts`` directory. Returns ``None`` (and the
    caller degrades to a no-op) if it cannot be imported.
    """
    try:
        module = __import__("purge_session", fromlist=[name])
        return getattr(module, name)
    except ImportError:
        scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
        if scripts_dir.is_dir() and str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        try:
            module = __import__("purge_session", fromlist=[name])
            return getattr(module, name)
        except ImportError:
            logger.warning(
                "Retention sweep: could not import purge_session.%s; skipping.", name
            )
            return None


def _load_purge_before_date() -> Callable[..., dict[str, int]] | None:
    return _import_purge_session_symbol("purge_before_date")


def _load_purge_session() -> Callable[..., dict[str, int]] | None:
    return _import_purge_session_symbol("purge_session")


def _distinct_client_slugs(db_path: Path) -> list[str]:
    """Every non-null ``client_slug`` with at least one session row."""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT DISTINCT client_slug FROM sessions WHERE client_slug IS NOT NULL"
            ).fetchall()
            return [row[0] for row in rows if row[0]]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _session_ids_before_for_client(db_path: Path, client_slug: str, cutoff_ts: float) -> list[str]:
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE client_slug = ? AND start_ts < ?",
                (client_slug, cutoff_ts),
            ).fetchall()
            return [row[0] for row in rows]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _merge_totals(target: dict[str, int], addition: dict[str, int] | None) -> None:
    for key, value in (addition or {}).items():
        target[key] = target.get(key, 0) + value


def _run_per_client_sweep(
    *,
    now: datetime,
    db_path: Path,
    dry_run: bool,
    klanten_root: Path,
    purge_session_fn: Callable[..., dict[str, int]],
    purge_kwargs: dict[str, Any],
) -> tuple[dict[str, int], set[str]]:
    """Purge sessions whose linked client sets its own ``bewaren_dagen``.

    Runs independent of the global ``DATA_RETENTION_DAYS`` cutoff: a client's
    own retention window must fire even when the global sweep is disabled
    (``DATA_RETENTION_DAYS=0``) -- klantmap-als-eenheid D3's mandatory
    tiebreaker. It must also NOT be re-capped by a shorter global window, nor
    left unenforced by a longer one, so every client with an explicit (non-
    None) ``bewaren_dagen`` is governed EXCLUSIVELY here. Returns the merged
    purge totals plus the set of client slugs with such an override -- the
    caller's global sweep excludes exactly those slugs.
    """
    totals: dict[str, int] = {}
    overridden_slugs: set[str] = set()
    for slug in _distinct_client_slugs(db_path):
        try:
            klant = load_klant_config(slug, root=klanten_root)
        except KlantConfigError:
            logger.warning(
                "Retention sweep: klant.yaml for '%s' is invalid; skipping its "
                "per-client retention override for this sweep.",
                slug,
            )
            continue
        if klant is None or klant.bewaren_dagen is None:
            continue
        overridden_slugs.add(slug)
        if klant.bewaren_dagen == 0:
            # 0 = "voor altijd" (same sentinel meaning as the global
            # DATA_RETENTION_DAYS=0) -- never auto-purged for this client.
            continue
        per_cutoff_ts = (now - timedelta(days=klant.bewaren_dagen)).timestamp()
        for session_id in _session_ids_before_for_client(db_path, slug, per_cutoff_ts):
            try:
                counts = purge_session_fn(
                    session_id, dry_run=dry_run, db_path=db_path, **purge_kwargs
                )
            except Exception:
                logger.exception(
                    "Per-client retention purge failed for session=%s client=%s",
                    session_id,
                    slug,
                )
                continue
            _merge_totals(totals, counts)
    return totals, overridden_slugs


def run_retention_sweep(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    reports_dir: Path = _DEFAULT_REPORTS_DIR,
    sessions_dir: Path = _DEFAULT_SESSIONS_DIR,
    db_path: Path | None = None,
    audit_dir: Path | None = None,
    case_db_path: Path | None = None,
    uploads_dir: Path | None = None,
) -> dict[str, int] | None:
    """Purge session data older than the retention window(s).

    Two independent layers, both evaluated on every call:

    1. Per-client: a linked client's own ``klant.yaml`` ``bewaren_dagen``
       (see :func:`_run_per_client_sweep`) -- runs even when the global
       sweep below is disabled.
    2. Global: ``DATA_RETENTION_DAYS``, excluding any client slug already
       fully governed by layer 1.

    Returns the merged per-resource purge totals when either layer ran, or
    ``None`` when neither did (global disabled AND no client has its own
    ``bewaren_dagen``). When the global layer is disabled but persisted
    session data still exists, logs a single WARNING so an operator on a
    default install learns that nothing is being globally auto-purged (a
    per-client sweep having run does not suppress this warning -- the two are
    independent facts).

    Never raises — retention enforcement is best-effort and must never block a
    call from starting. The orchestrator calls this off the event loop at
    startup; only data older than a cutoff is removed, so a freshly started
    call (current timestamp) is never touched.
    """
    resolved_now = now or datetime.now(UTC)
    resolved_db_path = db_path or _DEFAULT_SESSION_DB
    resolved_klanten_root = uploads_dir or context_docs.UPLOAD_ROOT

    purge_kwargs: dict[str, Any] = {
        "data_dir": sessions_dir,
        "reports_dir": reports_dir,
    }
    if audit_dir is not None:
        purge_kwargs["audit_dir"] = audit_dir
    if case_db_path is not None:
        purge_kwargs["case_db_path"] = case_db_path
    if uploads_dir is not None:
        purge_kwargs["uploads_dir"] = uploads_dir

    totals: dict[str, int] = {}
    overridden_slugs: set[str] = set()
    purge_session_fn = _load_purge_session()
    if purge_session_fn is not None:
        try:
            client_totals, overridden_slugs = _run_per_client_sweep(
                now=resolved_now,
                db_path=resolved_db_path,
                dry_run=dry_run,
                klanten_root=resolved_klanten_root,
                purge_session_fn=purge_session_fn,
                purge_kwargs=purge_kwargs,
            )
            _merge_totals(totals, client_totals)
        except Exception:
            logger.exception("Per-client retention sweep failed (non-blocking); data unchanged.")

    ran_per_client_sweep = bool(totals) or bool(overridden_slugs)

    cutoff = retention_cutoff(resolved_now)
    if cutoff is None:
        if _dir_has_files(sessions_dir) or _dir_has_files(reports_dir):
            logger.warning(
                "Global data retention is disabled (%s=0) but persisted session "
                "data exists under %s / %s. Set %s>0 to auto-purge globally, set "
                "a client's own bewaren_dagen in klant.yaml, or run "
                "scripts/retention_purge.py --confirm manually.",
                _RETENTION_ENV,
                sessions_dir,
                reports_dir,
                _RETENTION_ENV,
            )
        return totals if ran_per_client_sweep else None

    purge_before_date = _load_purge_before_date()
    if purge_before_date is None:
        return totals if ran_per_client_sweep else None

    kwargs: dict[str, Any] = dict(purge_kwargs)
    kwargs["dry_run"] = dry_run
    kwargs["db_path"] = resolved_db_path
    kwargs["exclude_client_slugs"] = overridden_slugs

    try:
        global_totals = purge_before_date(cutoff, **kwargs)
    except Exception:
        logger.exception("Retention sweep failed (non-blocking); data unchanged.")
        return totals if ran_per_client_sweep else None

    _merge_totals(totals, global_totals)

    purged = totals.get("sessions", 0)
    if purged:
        logger.info(
            "Retention sweep purged %d session(s) (global cutoff %s, plus any "
            "per-client bewaren_dagen overrides).",
            purged,
            cutoff.date().isoformat(),
        )
    return totals
