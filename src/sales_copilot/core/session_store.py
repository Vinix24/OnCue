"""SQLite-backed session store voor crash-recovery.

Tables: sessions(id, start_ts, end_ts, profile, transcript_path)
         detections(session_id, ts, type, subcat, confidence, text)

On startup: scan unfinished sessions <24u, publish 'session_resume_available' event.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sales_copilot.core.paths import resolve_app_path

logger = logging.getLogger(__name__)

_DEFAULT_DB = resolve_app_path(".vnx-data/sessions.db")
# Public alias so sibling local stores (e.g. the Phase 3 conversion counter)
# reuse this exact file without introducing a new `.vnx-data` path literal
# (architecture boundary: vnx-data-isolation).
DEFAULT_DB = _DEFAULT_DB
_GC_THRESHOLD_SECONDS = 24 * 3600  # 24u


@dataclass(frozen=True)
class SessionRecord:
    id: str
    start_ts: float
    end_ts: float | None
    profile: str
    transcript_path: str | None
    # klantmap-als-eenheid D3: the client this session is linked to (or None,
    # "geen klant"). Nullable and additive -- rows created before this column
    # existed read back as None, unchanged.
    client_slug: str | None = None


@dataclass(frozen=True)
class DetectionRecord:
    session_id: str
    ts: float
    type: str
    subcat: str | None
    confidence: float
    text: str


class SessionStore:
    def __init__(self, db_path: Path | str = _DEFAULT_DB) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db_path.parent.chmod(stat.S_IRWXU)
        except OSError:
            pass
        self._init_schema()
        try:
            os.chmod(self.db_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                start_ts REAL NOT NULL,
                end_ts REAL,
                profile TEXT NOT NULL DEFAULT 'sales',
                transcript_path TEXT
            );
            CREATE TABLE IF NOT EXISTS detections (
                session_id TEXT NOT NULL,
                ts REAL NOT NULL,
                type TEXT NOT NULL,
                subcat TEXT,
                confidence REAL NOT NULL,
                text TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_detections_session ON detections(session_id);
            """)
            self._migrate_client_slug_column(conn)

    def _migrate_client_slug_column(self, conn: sqlite3.Connection) -> None:
        """Additive migration: add ``client_slug`` to a pre-D3 ``sessions`` table.

        ``CREATE TABLE IF NOT EXISTS`` above never alters an existing table, so a
        database created before klantmap-als-eenheid D3 needs this explicit
        ``ALTER TABLE`` once. Existing rows read back with ``client_slug IS NULL``
        (no client, unchanged from before this column existed).
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "client_slug" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN client_slug TEXT")

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_session(
        self,
        session_id: str,
        profile: str = "sales",
        transcript_path: str | None = None,
        client_slug: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (id, start_ts, profile, transcript_path, client_slug) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, time.time(), profile, transcript_path, client_slug),
            )

    def get_client_slug(self, session_id: str) -> str | None:
        """The client this session is linked to, or ``None`` (no client, or unknown session)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT client_slug FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return row["client_slug"] if row and row["client_slug"] else None

    def end_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET end_ts = ? WHERE id = ?",
                (time.time(), session_id),
            )

    def append_detection(self, record: DetectionRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO detections (session_id, ts, type, subcat, confidence, text) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    record.session_id,
                    record.ts,
                    record.type,
                    record.subcat,
                    record.confidence,
                    record.text,
                ),
            )

    def find_unfinished_sessions(self) -> list[SessionRecord]:
        """Return sessions met end_ts IS NULL en start_ts < 24u geleden."""
        cutoff = time.time() - _GC_THRESHOLD_SECONDS
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE end_ts IS NULL AND start_ts >= ?",
                (cutoff,),
            ).fetchall()
        return [
            SessionRecord(
                id=r["id"],
                start_ts=r["start_ts"],
                end_ts=r["end_ts"],
                profile=r["profile"],
                transcript_path=r["transcript_path"],
                client_slug=r["client_slug"],
            )
            for r in rows
        ]

    def get_session_detections(self, session_id: str) -> list[DetectionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM detections WHERE session_id = ? ORDER BY ts",
                (session_id,),
            ).fetchall()
        return [
            DetectionRecord(
                session_id=r["session_id"],
                ts=r["ts"],
                type=r["type"],
                subcat=r["subcat"],
                confidence=r["confidence"],
                text=r["text"],
            )
            for r in rows
        ]

    def delete_session(self, session_id: str) -> dict[str, int]:
        """AVG Art. 17: delete all rows for session_id. Returns counts per table."""
        with self._connect() as conn:
            det_result = conn.execute(
                "DELETE FROM detections WHERE session_id = ?", (session_id,)
            )
            ses_result = conn.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            return {
                "sessions": ses_result.rowcount,
                "detections": det_result.rowcount,
            }

    def gc_old_sessions(self) -> int:
        """Delete sessions ouder dan 24u (incl. detections)."""
        cutoff = time.time() - _GC_THRESHOLD_SECONDS
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM detections WHERE session_id IN "
                "(SELECT id FROM sessions WHERE start_ts < ?)",
                (cutoff,),
            )
            result = conn.execute(
                "DELETE FROM sessions WHERE start_ts < ?",
                (cutoff,),
            )
            return result.rowcount
