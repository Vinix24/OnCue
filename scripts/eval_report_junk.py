#!/usr/bin/env python3
"""Meting van het junkfilter op echte opnames (objective belapp-junkfilter-rapportschema).

Leest sessies uit een SQLite-bestand met een ``call_sessions``-tabel en draait op de gekozen
sessies de ECHTE keten: ``enrich_report`` (via de resolver, taak ``report``) en ``decide_junk``.

    # Kandidaten zoeken: alleen de goedkope telling, geen LLM
    python scripts/eval_report_junk.py --db data/cases.db --scan

    # De echte keten op gekozen sessies (sleutels via de omgeving, nooit geprint)
    python scripts/eval_report_junk.py --db data/cases.db --sessions ab12cd34,ef56gh78 --out result.json

``--sessions`` accepteert volledige id's of een unieke prefix. De uitvoer bevat per sessie
alleen id (eerste 8 tekens), duur, aantal prospect-woorden, ``gesprek_gevoerd``, ``junk``,
``junk_reason``, latentie en of de verrijking fail-open ging. Nooit transcripttekst, namen
of samenvatting: ``junk_reason`` is een vaste categorie plus een telling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sales_copilot.core.config import DetectorConfig, load_env
from sales_copilot.modules.reports.enrichment import ReportEnrichment, enrich_report
from sales_copilot.modules.reports.generator import SessionData, TranscriptSegment
from sales_copilot.modules.reports.junk import count_prospect_words, decide_junk

ID_PREFIX_LEN = 8


@dataclass(frozen=True)
class StoredSession:
    session_id: str
    started_at: str | None
    ended_at: str | None
    transcript: list[TranscriptSegment]


@dataclass(frozen=True)
class ScanRow:
    session: str
    duration_s: int
    segments: int
    prospect_words: int


@dataclass(frozen=True)
class EvalRow:
    session: str
    duration_s: int
    prospect_words: int
    gesprek_gevoerd: bool
    junk: bool
    junk_reason: str | None
    latency_s: float
    fail_open: bool


def _segments(raw: Any) -> list[TranscriptSegment]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    segments: list[TranscriptSegment] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        speaker = entry.get("speaker")
        text = entry.get("text")
        if not isinstance(speaker, str) or not isinstance(text, str):
            continue
        start_ms = entry.get("start_ms")
        end_ms = entry.get("end_ms")
        start = int(start_ms) if isinstance(start_ms, (int, float)) else 0
        end = int(end_ms) if isinstance(end_ms, (int, float)) else start
        segments.append(TranscriptSegment(speaker=speaker, text=text, start_ms=start, end_ms=end))
    segments.sort(key=lambda s: (s.start_ms, s.end_ms))
    return segments


def load_sessions(db_path: Path) -> list[StoredSession]:
    """All sessions of ``db_path``, read-only. Raises ``FileNotFoundError`` for a missing file."""
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, started_at, ended_at, transcript FROM call_sessions ORDER BY started_at"
        ).fetchall()
    finally:
        conn.close()
    return [
        StoredSession(
            session_id=str(row[0]),
            started_at=row[1],
            ended_at=row[2],
            transcript=_segments(row[3]),
        )
        for row in rows
    ]


def select_sessions(sessions: Sequence[StoredSession], wanted: Sequence[str]) -> list[StoredSession]:
    """The sessions whose id equals or starts with each wanted id, in the wanted order."""
    chosen: list[StoredSession] = []
    for prefix in wanted:
        matches = [s for s in sessions if s.session_id.startswith(prefix)]
        if not matches:
            raise LookupError(f"no session matches {prefix!r}")
        if len(matches) > 1:
            raise LookupError(f"{prefix!r} matches {len(matches)} sessions; use a longer prefix")
        chosen.append(matches[0])
    return chosen


def duration_seconds(session: StoredSession) -> int:
    """Wall-clock duration from the timestamps, else the end of the last transcript segment."""
    if session.started_at and session.ended_at:
        try:
            delta = datetime.fromisoformat(str(session.ended_at)) - datetime.fromisoformat(str(session.started_at))
            return max(0, int(delta.total_seconds()))
        except ValueError:
            pass
    if session.transcript:
        return max(0, max(s.end_ms for s in session.transcript) // 1000)
    return 0


def scan(sessions: Sequence[StoredSession]) -> list[ScanRow]:
    """The cheap layer-1 count for every session: no LLM, no network."""
    return [
        ScanRow(
            session=s.session_id[:ID_PREFIX_LEN],
            duration_s=duration_seconds(s),
            segments=len(s.transcript),
            prospect_words=count_prospect_words(s.transcript),
        )
        for s in sessions
    ]


def _generator_session(session: StoredSession) -> SessionData:
    return SessionData(
        session_id=session.session_id,
        call_start_ms=0,
        call_end_ms=duration_seconds(session) * 1000,
        prospect_name=None,
        prospect_company=None,
        context_docs=[],
        speech_events=[],
        phase_events=[],
        pain_points=[],
        monologues=[],
        transcript=session.transcript,
        started_at=session.started_at,
        ended_at=session.ended_at,
    )


async def evaluate_session(session: StoredSession, detector_config: DetectorConfig | None) -> EvalRow:
    """The real chain for one session: ``enrich_report`` then ``decide_junk``."""
    started = time.monotonic()
    enrichment = await enrich_report(_generator_session(session), detector_config)
    latency = time.monotonic() - started
    verdict = decide_junk(enrichment, session.transcript)
    return EvalRow(
        session=session.session_id[:ID_PREFIX_LEN],
        duration_s=duration_seconds(session),
        prospect_words=verdict.prospect_words,
        gesprek_gevoerd=enrichment.gesprek_gevoerd,
        junk=verdict.junk,
        junk_reason=verdict.reason,
        latency_s=round(latency, 2),
        fail_open=enrichment == ReportEnrichment.fail_open(),
    )


async def evaluate(sessions: Sequence[StoredSession], detector_config: DetectorConfig | None) -> list[EvalRow]:
    return [await evaluate_session(s, detector_config) for s in sessions]


def format_table(rows: Sequence[ScanRow] | Sequence[EvalRow]) -> str:
    if not rows:
        return "(no sessions)"
    dicts = [asdict(r) for r in rows]
    headers = list(dicts[0])
    cells = [[str(d[h]) for h in headers] for d in dicts]
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, h in enumerate(headers)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(c, widths, strict=True)) for c in cells]
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, type=Path, help="SQLite file with a call_sessions table")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sessions", help="comma-separated session ids (or unique prefixes)")
    mode.add_argument("--scan", action="store_true", help="layer-1 count for all sessions, no LLM")
    parser.add_argument("--out", type=Path, help="write the result rows as JSON")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        sessions = load_sessions(args.db)
    except (FileNotFoundError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rows: Sequence[ScanRow] | Sequence[EvalRow]
    if args.scan:
        rows = scan(sessions)
    else:
        wanted = [w.strip() for w in args.sessions.split(",") if w.strip()]
        try:
            chosen = select_sessions(sessions, wanted)
        except LookupError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        load_env()
        rows = asyncio.run(evaluate(chosen, DetectorConfig.from_env()))

    print(format_table(rows))
    if args.out:
        args.out.write_text(json.dumps([asdict(r) for r in rows], indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
