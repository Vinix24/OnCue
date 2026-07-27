"""Configurable data-retention policy (AVG storage limitation).

``DATA_RETENTION_DAYS`` controls how long session data is kept. ``0`` (the
default) disables auto-purge. The actual deletion is performed by
``purge_before_date`` (``scripts/purge_session.py``); this module is the pure,
testable policy layer plus a thin :func:`run_retention_sweep` that the
orchestrator runs once at startup so retention is enforced at the app level and
not only by the ``scripts/retention_purge.py`` cron.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sales_copilot.core.paths import resolve_app_path

logger = logging.getLogger(__name__)

_RETENTION_ENV = "DATA_RETENTION_DAYS"

# Defaults mirror ``scripts/purge_session.py`` so the startup sweep operates on the
# same locations as the cron purge.
_DEFAULT_REPORTS_DIR = resolve_app_path("data/reports")
_DEFAULT_SESSIONS_DIR = resolve_app_path("data/sessions")


def resolve_retention_days() -> int:
    """Configured retention window in days; ``0`` means retention is disabled."""
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
    """The cutoff before which data should be purged, or ``None`` if disabled."""
    days = resolve_retention_days()
    if days <= 0:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=days)


def _dir_has_files(path: Path) -> bool:
    return path.exists() and any(p.is_file() for p in path.rglob("*"))


def _load_purge_before_date() -> Callable[..., dict[str, int]] | None:
    """Import the deletion routine from ``scripts/purge_session.py``.

    The deletion logic lives in the cron script rather than the package, so this
    resolves it via the repo's ``scripts`` directory. Returns ``None`` (and the
    caller degrades to a no-op) if it cannot be imported.
    """
    try:
        from purge_session import purge_before_date  # type: ignore[import-not-found]

        return purge_before_date
    except ImportError:
        scripts_dir = Path(__file__).resolve().parents[3] / "scripts"
        if scripts_dir.is_dir() and str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        try:
            from purge_session import purge_before_date  # type: ignore[import-not-found]

            return purge_before_date
        except ImportError:
            logger.warning(
                "Retention sweep: could not import purge_before_date; skipping."
            )
            return None


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
    """Purge session data older than the retention window.

    Returns the per-resource purge totals when a sweep ran, or ``None`` when
    retention is disabled (``DATA_RETENTION_DAYS=0``). When retention is disabled
    but persisted session data still exists, logs a single WARNING so an operator
    on a default install learns that nothing is being auto-purged.

    Never raises — retention enforcement is best-effort and must never block a
    call from starting. The orchestrator calls this off the event loop at
    startup; only data older than the cutoff is removed, so a freshly started
    call (current timestamp) is never touched.
    """
    cutoff = retention_cutoff(now)
    if cutoff is None:
        if _dir_has_files(sessions_dir) or _dir_has_files(reports_dir):
            logger.warning(
                "Data retention is disabled (%s=0) but persisted session data "
                "exists under %s / %s. Set %s>0 to auto-purge, or run "
                "scripts/retention_purge.py --confirm manually.",
                _RETENTION_ENV,
                sessions_dir,
                reports_dir,
                _RETENTION_ENV,
            )
        return None

    purge_before_date = _load_purge_before_date()
    if purge_before_date is None:
        return None

    kwargs: dict[str, Any] = {
        "dry_run": dry_run,
        "data_dir": sessions_dir,
        "reports_dir": reports_dir,
    }
    if db_path is not None:
        kwargs["db_path"] = db_path
    if audit_dir is not None:
        kwargs["audit_dir"] = audit_dir
    if case_db_path is not None:
        kwargs["case_db_path"] = case_db_path
    if uploads_dir is not None:
        kwargs["uploads_dir"] = uploads_dir

    try:
        totals = purge_before_date(cutoff, **kwargs)
    except Exception:
        logger.exception("Retention sweep failed (non-blocking); data unchanged.")
        return None

    purged = totals.get("sessions", 0) if isinstance(totals, dict) else 0
    if purged:
        logger.info(
            "Retention sweep purged %d session(s) started before %s.",
            purged,
            cutoff.date().isoformat(),
        )
    return totals
