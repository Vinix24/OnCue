from __future__ import annotations

import sqlite3
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    industry TEXT,
    pain_point TEXT NOT NULL,
    description TEXT,
    slide_html TEXT NOT NULL,
    image_url TEXT,
    metrics TEXT,
    priority INTEGER DEFAULT 0,
    active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cases_pain_point ON cases(pain_point);
CREATE INDEX IF NOT EXISTS idx_cases_industry ON cases(industry);

CREATE TABLE IF NOT EXISTS call_sessions (
    id TEXT PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    prospect_name TEXT,
    prospect_industry TEXT,
    phase_transitions TEXT,
    pain_points_detected TEXT,
    talk_time_data TEXT,
    transcript TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    industry: str | None
    pain_point: str
    description: str | None
    slide_html: str
    metrics: str | None
    priority: int = 0


class CaseDB(Protocol):
    async def find_case(self, pain_point: str, industry: str | None = None) -> Case | None:
        ...

    async def list_cases(self) -> list[Case]:
        ...


class SQLiteCaseDB:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    async def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db_path.parent.chmod(stat.S_IRWXU)
        except OSError:
            pass
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executescript(SCHEMA)
            await conn.commit()
        try:
            self.db_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    async def upsert_cases(self, cases: Iterable[Case]) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.executemany(
                """
                INSERT OR REPLACE INTO cases (
                    id,
                    title,
                    industry,
                    pain_point,
                    description,
                    slide_html,
                    metrics,
                    priority
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        case.id,
                        case.title,
                        case.industry,
                        case.pain_point,
                        case.description,
                        case.slide_html,
                        case.metrics,
                        case.priority,
                    )
                    for case in cases
                ],
            )
            await conn.commit()

    async def find_case(self, pain_point: str, industry: str | None = None) -> Case | None:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            if industry:
                cursor = await conn.execute(
                    """
                    SELECT id, title, industry, pain_point, description, slide_html, metrics, priority
                    FROM cases
                    WHERE pain_point = ? AND industry = ? AND active = 1
                    ORDER BY priority DESC
                    LIMIT 1
                    """,
                    (pain_point, industry),
                )
                row = await cursor.fetchone()
                if row:
                    return Case(**dict(row))

            cursor = await conn.execute(
                """
                SELECT id, title, industry, pain_point, description, slide_html, metrics, priority
                FROM cases
                WHERE pain_point = ? AND active = 1
                ORDER BY priority DESC
                LIMIT 1
                """,
                (pain_point,),
            )
            row = await cursor.fetchone()
            if row:
                return Case(**dict(row))

        return None

    async def list_cases(self) -> list[Case]:
        async with aiosqlite.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = await conn.execute(
                """
                SELECT id, title, industry, pain_point, description, slide_html, metrics, priority
                FROM cases
                WHERE active = 1
                ORDER BY priority DESC, title ASC
                """
            )
            rows = await cursor.fetchall()
            return [Case(**dict(row)) for row in rows]
