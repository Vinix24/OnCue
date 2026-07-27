"""Q3 honest conversion instrumentation (design doc open question 5).

A LOCAL-ONLY count of how often the Free scorecard's missed-point list
appeared vs actual upgrade events. No nagging, no growth-hack prompts, no
network: the events live in the same local SQLite file `SessionStore` uses,
are queryable via `counts()`, and are never surfaced to the user as a
prompt. Vincent's decision 2026-07-23: teller vanaf Phase 3, geen nag.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sales_copilot.core.session_store import DEFAULT_DB as _DEFAULT_DB

logger = logging.getLogger(__name__)

METRIC_GAP_SHOWN = "scorecard_gap_shown"
METRIC_TIER_OBSERVED = "tier_observed"
METRIC_UPGRADE = "pro_upgrade"

_PRO_TIERS = frozenset({"pro", "enterprise"})


class ConversionCounterStore:
    """Append-only local event counter for the Q3 conversion question.

    One row per event; counts are derived with COUNT queries so the store
    stays trivially auditable with the sqlite3 CLI. Rows never
    leave this machine and are never read back into any user-facing surface.
    """

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS conversion_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "metric TEXT NOT NULL, "
        "detail TEXT, "
        "ts REAL NOT NULL"
        ")"
    )

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else _DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(self._SCHEMA)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversion_events_metric "
                "ON conversion_events(metric)"
            )

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_event(self, metric: str, detail: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO conversion_events (metric, detail, ts) VALUES (?, ?, ?)",
                (metric, detail, time.time()),
            )

    def record_gap_shown(self, session_id: str | None = None) -> None:
        """Count one appearance of the scorecard's missed-point list."""
        self.record_event(METRIC_GAP_SHOWN, detail=session_id)

    def note_tier(self, tier: str) -> bool:
        """Record a tier observation; count an upgrade on a free -> pro transition.

        Called once per call by the reports module with the currently resolved
        tier. When the previously observed tier was free and the new one is a
        paid tier, one `pro_upgrade` event is recorded -- the honest local
        proxy for "the user upgraded" (license activation itself is an offline
        .env change, so there is no activation callback to hook). Returns True
        exactly when an upgrade event was recorded.
        """
        normalized = (tier or "free").strip().lower() or "free"
        previous = self._last_observed_tier()
        self.record_event(METRIC_TIER_OBSERVED, detail=normalized)
        if previous == "free" and normalized in _PRO_TIERS:
            self.record_event(METRIC_UPGRADE, detail=normalized)
            logger.info("Conversion counter: tier transition free -> %s recorded", normalized)
            return True
        return False

    def count(self, metric: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM conversion_events WHERE metric = ?",
                (metric,),
            ).fetchone()
        return int(row["n"])

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT metric, COUNT(*) AS n FROM conversion_events GROUP BY metric"
            ).fetchall()
        return {str(row["metric"]): int(row["n"]) for row in rows}

    def _last_observed_tier(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT detail FROM conversion_events WHERE metric = ? ORDER BY id DESC LIMIT 1",
                (METRIC_TIER_OBSERVED,),
            ).fetchone()
        if row is None or not row["detail"]:
            return None
        return str(row["detail"])
