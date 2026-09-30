from __future__ import annotations

import asyncio
import bisect
import datetime
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
import websockets
from websockets.exceptions import ConnectionClosed

from sales_copilot.audio.tap_health import TRANSCRIPT_MARKER_TYPE
from sales_copilot.core.config import SlidesConfig, WebSocketConfig, env, env_bool, env_int, load_env
from sales_copilot.core.session_store import DetectionRecord, SessionStore
from sales_copilot.modules.slides.case_db import SQLiteCaseDB
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode_payload(raw: str | bytes) -> object:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


@dataclass(frozen=True)
class SessionData:
    session_id: str
    started_at: str
    ended_at: str | None
    prospect_name: str | None
    prospect_company: str | None
    context_docs: list[str]
    transcript: list[dict[str, object]]
    pain_points: list[dict[str, object]]
    talk_time_snapshots: list[dict[str, object]]
    phase_transitions: list[dict[str, object]]
    coaching_alerts: list[dict[str, object]]
    summaries: list[dict[str, object]]
    # Track 3 (deep insight lane): every `insight` payload published on the
    # `insights` channel during the session (PR-D2). Excludes `ask` messages
    # (input, not output) and `insight_budget_exhausted` notices (not an
    # insight) -- both filtered in `_handle_insight`.
    insights: list[dict[str, object]] = field(default_factory=list)
    # Phase 3 Free post-call scorecard (design doc section 9). `script_coverage`
    # is the persisted-checkpoint reduction ({covered, total, missing_required})
    # or None when no checkpoint exists for the session; the counts come from
    # the objections/buying-signals channels. Defaults keep older constructors
    # (tests, replay tooling) valid without the new fields.
    script_coverage: dict[str, object] | None = None
    objection_count: int = 0
    objection_responded_count: int = 0
    opportunity_count: int = 0


def _count_responded_objections(
    objections: list[dict[str, object]],
    transcript: list[dict[str, object]],
    window_ms: int,
) -> int:
    """Count objections the rep verbally responded to (deterministic heuristic).

    An objection counts as responded when a `self` transcript segment starts
    within [objection_ts, objection_ts + window_ms]. Objection timestamps and
    transcript start_ms share the same call-relative millisecond base, so the
    comparison is direct. No LLM, no adequacy judgment -- the Free scorecard
    shows the count and the gap only.
    """
    self_starts = sorted(
        int(entry["start_ms"])
        for entry in transcript
        if entry.get("speaker") == "self" and isinstance(entry.get("start_ms"), (int, float))
    )
    responded = 0
    for objection in objections:
        timestamp_ms = objection.get("timestamp_ms")
        if not isinstance(timestamp_ms, (int, float)):
            continue
        idx = bisect.bisect_left(self_starts, int(timestamp_ms))
        if idx < len(self_starts) and self_starts[idx] <= int(timestamp_ms) + window_ms:
            responded += 1
    return responded


class ScriptCoverageCheckpointStore:
    """SQLite checkpoint store for `ScriptTracker` coverage state.

    Salesprep design doc, section 7 finding #2 / section 9 Phase 2: a crash
    mid-call must not produce a dishonest Free gap report, so the coverage
    map is checkpointed to SQLite on every confirmation cycle and rehydrated
    on restart. This reuses the exact local SQLite file `SessionTracker`
    already checkpoints session data into (`case_db_sqlite_path`) -- no new
    datastore, no new service. Rows are keyed by `session_id` so rehydration
    never cross-contaminates between sessions, and each checkpoint write is a
    single atomic UPSERT statement: a crash mid-write leaves either the
    previous committed row or the fully-written new one, never a partial one.

    This is local session state (like `SessionStore`), not a central-DB
    table -- ADR-007's composite-key-over-project_id note does not apply
    (design doc section 9). Coverage checkpoints never leave this machine.
    """

    _SCHEMA = (
        "CREATE TABLE IF NOT EXISTS script_coverage_checkpoints ("
        "session_id TEXT PRIMARY KEY, "
        "coverage_json TEXT NOT NULL, "
        "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        ")"
    )

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = SlidesConfig.from_env().case_db_sqlite_path
        self.db_path = Path(db_path)

    async def save(self, session_id: str, covered: dict[str, dict[str, object]]) -> None:
        """Atomically upsert the coverage map for `session_id`.

        `covered` is the full round-trippable per-point state (status,
        confidence, hint, timestamp_ms, and the point metadata) -- whatever
        `ScriptTracker` passes in is persisted verbatim as JSON, no lossy
        projection.
        """
        if not session_id:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(self._SCHEMA)
            await conn.commit()
            await conn.execute(
                """
                INSERT INTO script_coverage_checkpoints (session_id, coverage_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(session_id) DO UPDATE SET
                    coverage_json = excluded.coverage_json,
                    updated_at = excluded.updated_at
                """,
                (session_id, json.dumps(covered)),
            )
            await conn.commit()

    async def load(self, session_id: str) -> dict[str, dict[str, object]] | None:
        """Return the last-checkpointed coverage map for `session_id`, or None.

        This is the read path both crash-restart rehydration (same phase)
        and the post-call Free scorecard (a later phase) use as the source
        of truth -- the scorecard reads the persisted snapshot, never live
        in-process memory.
        """
        if not session_id or not self.db_path.exists():
            return None
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute(self._SCHEMA)
            await conn.commit()
            cursor = await conn.execute(
                "SELECT coverage_json FROM script_coverage_checkpoints WHERE session_id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            logger.warning("Corrupt script coverage checkpoint for session %s", session_id)
            return None
        if not isinstance(data, dict):
            logger.warning("Unexpected script coverage checkpoint shape for session %s", session_id)
            return None
        return data


class SessionTracker:
    def __init__(
        self,
        *,
        ws_config: WebSocketConfig | None = None,
        db_path: str | Path | None = None,
        prospect_name: str | None = None,
        prospect_company: str | None = None,
        context_docs: list[str] | None = None,
        now_iso: Callable[[], str] | None = None,
        now_ms: Callable[[], int] | None = None,
        checkpoint_enabled: bool | None = None,
        checkpoint_interval_seconds: int | None = None,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
        client_slug: str | None = None,
        script_config_path: str | None = None,
        objection_response_window_ms: int | None = None,
    ) -> None:
        load_env()
        self._ws_config = ws_config or WebSocketConfig.from_env()
        slides_config = SlidesConfig.from_env()
        self._db_path = Path(db_path or slides_config.case_db_sqlite_path)
        self._prospect_industry = slides_config.prospect_industry
        self._prospect_name = prospect_name
        self._prospect_company = prospect_company
        self._context_docs = list(context_docs or [])
        self._now_iso = now_iso or _now_iso
        self._now_ms = now_ms or _now_ms
        self._checkpoint_enabled = (
            checkpoint_enabled
            if checkpoint_enabled is not None
            else bool(env_bool("SESSION_CHECKPOINT_ENABLED", True))
        )
        self._checkpoint_interval_seconds = (
            checkpoint_interval_seconds
            if checkpoint_interval_seconds is not None
            else int(env_int("SESSION_CHECKPOINT_INTERVAL_SECONDS", 60) or 60)
        )
        if self._checkpoint_interval_seconds < 1:
            self._checkpoint_interval_seconds = 1

        self._session_store = session_store
        self._requested_session_id = session_id
        # klantmap-als-eenheid D3: the client this session is linked to (or
        # None, "geen klant"), already resolved server-side by D2's
        # hub_core._apply_klant_config -- recorded on the sessions row here so
        # purge_session/retention can find it without re-resolving klant.yaml.
        self._client_slug = client_slug
        # Phase 3: same script-point config the detector's ScriptTracker uses,
        # so the scorecard's total/missing-required match what was tracked.
        self._script_config_path = (
            script_config_path
            if script_config_path is not None
            else (env("SCRIPT_TRACKING_CONFIG", "config/scripts/default.yaml") or "config/scripts/default.yaml")
        )
        self._objection_response_window_ms = (
            objection_response_window_ms
            if objection_response_window_ms is not None
            else int(env_int("OBJECTION_RESPONSE_WINDOW_MS", 60000) or 60000)
        )
        if self._objection_response_window_ms < 0:
            self._objection_response_window_ms = 0
        self._lock = asyncio.Lock()
        self._stop_event: asyncio.Event | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._connections: list[websockets.ClientConnection] = []

        self._session_id: str | None = None
        self._started_at: str | None = None
        self._ended_at: str | None = None
        self._transcript: list[dict[str, object]] = []
        self._pain_points: list[dict[str, object]] = []
        self._talk_time_snapshots: list[dict[str, object]] = []
        self._phase_transitions: list[dict[str, object]] = []
        self._coaching_alerts: list[dict[str, object]] = []
        self._summaries: list[dict[str, object]] = []
        self._objections: list[dict[str, object]] = []
        self._opportunities: list[dict[str, object]] = []
        self._insights: list[dict[str, object]] = []
        self._last_phase: str | None = None

    async def start_session(self) -> None:
        if self._session_id is not None:
            raise RuntimeError("Session already started")

        self._start_state()
        self._stop_event = asyncio.Event()

        channels = {
            "transcript": self._handle_transcript,
            "pain-points": self._handle_pain_points,
            "talk-time": self._handle_talk_time,
            "coaching": self._handle_coaching,
            "summary": self._handle_summary,
            "objections": self._handle_objection,
            "buying-signals": self._handle_buying_signal,
            "insights": self._handle_insight,
        }

        for channel, handler in channels.items():
            # Localhost hub: bound the connect so a stalled hub does not hang
            # session startup. The library default is 10s; 5s is plenty locally.
            ws = await websockets.connect(self._channel_url(channel), open_timeout=5)
            self._connections.append(ws)
            self._tasks.append(asyncio.create_task(self._consume_channel(ws, handler)))
        if self._checkpoint_enabled:
            self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())

    async def end_session(self) -> SessionData:
        if self._session_id is None:
            raise RuntimeError("Session not started")

        self._ended_at = self._now_iso()
        if self._stop_event is not None:
            self._stop_event.set()

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._checkpoint_task is not None:
            self._checkpoint_task.cancel()
            try:
                await self._checkpoint_task
            except asyncio.CancelledError:
                pass
            self._checkpoint_task = None

        for ws in self._connections:
            try:
                await ws.close()
            except Exception:
                pass

        if self._session_store is not None and self._session_id is not None:
            try:
                self._session_store.end_session(self._session_id)
            except Exception:
                logger.exception("session_store.end_session failed for %s", self._session_id)
        session = await self.get_session_data()
        await self._persist_session(session)
        return session

    async def get_session_data(self) -> SessionData:
        # Read the persisted checkpoint outside the lock: it is SQLite IO and
        # the scorecard's agreed source of truth (Phase 3).
        script_coverage = await self._load_script_coverage()
        async with self._lock:
            transcript = list(self._transcript)
            objections = list(self._objections)
            opportunities = list(self._opportunities)
            return SessionData(
                session_id=self._session_id or "",
                started_at=self._started_at or "",
                ended_at=self._ended_at,
                prospect_name=self._prospect_name,
                prospect_company=self._prospect_company,
                context_docs=list(self._context_docs),
                transcript=transcript,
                pain_points=list(self._pain_points),
                talk_time_snapshots=list(self._talk_time_snapshots),
                phase_transitions=list(self._phase_transitions),
                coaching_alerts=list(self._coaching_alerts),
                summaries=list(self._summaries),
                insights=list(self._insights),
                script_coverage=script_coverage,
                objection_count=len(objections),
                objection_responded_count=_count_responded_objections(
                    objections, transcript, self._objection_response_window_ms
                ),
                opportunity_count=len(opportunities),
            )

    async def _load_script_coverage(self) -> dict[str, object] | None:
        """Free scorecard coverage, read from the PERSISTED checkpoint.

        Phase 3 (design doc section 9): the checkpoint `ScriptTracker` wrote
        during the call is the scorecard's source of truth -- never live
        in-process memory (the tracker lives in the detector process, so
        there is nothing live to read here anyway). Returns None when no
        checkpoint exists for this session (script tracking disabled, or the
        call ended before the first checkpoint) so the scorecard omits the
        script line instead of showing a dishonest 0-of-N.
        """
        if not self._session_id:
            return None
        store = ScriptCoverageCheckpointStore(self._db_path)
        try:
            stored = await store.load(self._session_id)
        except Exception:
            logger.warning(
                "Script coverage checkpoint read failed for session %s",
                self._session_id,
                exc_info=True,
            )
            return None
        if stored is None:
            return None
        # Lazy import: script_tracker imports this module for the checkpoint
        # store, so a module-level import would be circular.
        from sales_copilot.modules.coaching.script_tracker import (
            build_coverage_summary,
            load_script_points,
        )

        try:
            points = load_script_points(self._script_config_path)
        except Exception:
            logger.warning(
                "Script point config load failed: %s", self._script_config_path, exc_info=True
            )
            return None
        return build_coverage_summary(points, stored)

    def _start_state(self) -> None:
        self._session_id = self._requested_session_id or str(uuid.uuid4())
        self._started_at = self._now_iso()
        self._ended_at = None
        self._transcript = []
        self._pain_points = []
        self._talk_time_snapshots = []
        self._phase_transitions = []
        self._coaching_alerts = []
        self._summaries = []
        self._objections = []
        self._opportunities = []
        self._insights = []
        self._last_phase = None
        if self._session_store is not None:
            try:
                self._session_store.create_session(
                    self._session_id, client_slug=self._client_slug
                )
            except Exception:
                logger.exception("session_store.create_session failed for %s", self._session_id)

    def _channel_url(self, channel: str) -> str:
        return channel_ws_url(self._ws_config, channel)

    async def _consume_channel(
        self,
        ws: websockets.ClientConnection,
        handler: Callable[[object], Awaitable[None]],
    ) -> None:
        if self._stop_event is None:
            return
        while not self._stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
            except TimeoutError:
                continue
            except ConnectionClosed:
                break
            payload = _decode_payload(raw)
            await handler(payload)

    async def _handle_transcript(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") == TRANSCRIPT_MARKER_TYPE:
            await self._handle_transcript_marker(payload)
            return
        if payload.get("type") != "transcript":
            return
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        raw_speaker = payload.get("speaker")
        # The TranscriptEvent dataclass allows speaker=None (e.g. when
        # diarization cannot map a stream to a speaker). Earlier the filter
        # required speaker to be a string and silently dropped these
        # transcripts, leaving post-call reports empty even when the
        # dashboard rendered live transcripts. Default to "unknown" so the
        # transcript is captured; downstream consumers preserve the label.
        if isinstance(raw_speaker, str) and raw_speaker.strip():
            speaker = raw_speaker
        else:
            speaker = "unknown"
        entry = {
            "text": text,
            "speaker": speaker,
            "start_ms": payload.get("start_ms"),
            "end_ms": payload.get("end_ms"),
            "is_final": payload.get("is_final"),
        }
        async with self._lock:
            self._transcript.append(entry)
            captured = len(self._transcript)
        logger.info(
            "Transcript captured: %d chars from %s (total=%d)",
            len(text),
            speaker,
            captured,
        )

    async def _handle_transcript_marker(self, payload: dict[str, object]) -> None:
        """Record "the audio stopped here" as a transcript line of its own.

        Speaker ``system`` on purpose: the talk-time totals count only ``self``
        and ``prospect``, so the marker reads in the transcript without shifting
        a single percentage point in the report.
        """

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        start_ms = payload.get("start_ms")
        if not isinstance(start_ms, (int, float)):
            start_ms = 0
        entry = {
            "text": text,
            "speaker": "system",
            "start_ms": int(start_ms),
            "end_ms": int(start_ms),
            "is_final": True,
        }
        async with self._lock:
            self._transcript.append(entry)
        logger.warning("Transcript marker recorded at %sms: %s", int(start_ms), text)

    async def _handle_pain_points(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "pain_point":
            return
        async with self._lock:
            self._pain_points.append(dict(payload))
        await self._persist_detection(payload, "pain_point")

    async def _persist_detection(self, payload: dict[str, object], detection_type: str) -> None:
        if self._session_store is None or self._session_id is None:
            return
        try:
            record = DetectionRecord(
                session_id=self._session_id,
                ts=time.time(),
                type=detection_type,
                subcat=(payload.get("category") or payload.get("subcat") or None),
                confidence=float(payload.get("confidence") or 0.0),
                text=str(payload.get("trigger_phrase") or payload.get("text") or ""),
            )
            await asyncio.to_thread(self._session_store.append_detection, record)
        except Exception:
            logger.exception("session_store.append_detection failed")

    async def _handle_objection(self, payload: object) -> None:
        """Objection events (Phase 3 scorecard): counted, never adequacy-judged."""
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "objection":
            return
        async with self._lock:
            self._objections.append(dict(payload))
        await self._persist_detection(payload, "objection")

    async def _handle_buying_signal(self, payload: object) -> None:
        """Opportunity events (Phase 3 scorecard): counted for the report only."""
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "buying_signal":
            return
        async with self._lock:
            self._opportunities.append(dict(payload))
        await self._persist_detection(payload, "buying_signal")

    async def _handle_talk_time(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "talk_time":
            return
        async with self._lock:
            self._talk_time_snapshots.append(dict(payload))
            phase = payload.get("phase")
            if isinstance(phase, str) and phase != self._last_phase:
                timestamp_ms = payload.get("call_duration_ms")
                if not isinstance(timestamp_ms, (int, float)):
                    timestamp_ms = self._now_ms()
                self._phase_transitions.append(
                    {"phase": phase, "timestamp_ms": int(timestamp_ms)}
                )
                self._last_phase = phase

    async def _handle_coaching(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "coaching_alert":
            return
        async with self._lock:
            self._coaching_alerts.append(dict(payload))

    async def _handle_summary(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "summary":
            return
        async with self._lock:
            self._summaries.append(dict(payload))

    async def _handle_insight(self, payload: object) -> None:
        """Track 3 (deep insight lane): only `insight` payloads land in the report.

        `ask` messages are the vraag-box's input (already echoed back as an
        `insight` of type `antwoord` once `InsightEngine` answers), and
        `insight_budget_exhausted` is a management notice, not an insight -- both
        excluded here so the post-call report section is insights only.
        """
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "insight":
            return
        async with self._lock:
            self._insights.append(dict(payload))

    async def _persist_session(self, session: SessionData) -> None:
        db = SQLiteCaseDB(self._db_path)
        await db.initialize()
        async with aiosqlite.connect(self._db_path) as conn:
            await conn.execute(
                """
                INSERT INTO call_sessions (
                    id,
                    started_at,
                    ended_at,
                    prospect_name,
                    prospect_industry,
                    phase_transitions,
                    pain_points_detected,
                    talk_time_data,
                    transcript
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    prospect_name = excluded.prospect_name,
                    prospect_industry = excluded.prospect_industry,
                    phase_transitions = excluded.phase_transitions,
                    pain_points_detected = excluded.pain_points_detected,
                    talk_time_data = excluded.talk_time_data,
                    transcript = excluded.transcript
                """,
                (
                    session.session_id,
                    session.started_at,
                    session.ended_at,
                    session.prospect_name,
                    self._prospect_industry,
                    json.dumps(session.phase_transitions),
                    json.dumps(session.pain_points),
                    json.dumps(
                        {
                            "snapshots": session.talk_time_snapshots,
                            "coaching_alerts": session.coaching_alerts,
                            "summaries": session.summaries,
                            "insights": session.insights,
                            # Phase 3: the scorecard data persists with the
                            # session even when the report UI section is
                            # hidden (the phase's rollback invariant).
                            "script_coverage": session.script_coverage,
                            "objection_count": session.objection_count,
                            "objection_responded_count": session.objection_responded_count,
                            "opportunity_count": session.opportunity_count,
                        }
                    ),
                    json.dumps(session.transcript),
                ),
            )
            await conn.commit()

    async def _checkpoint_loop(self) -> None:
        if self._stop_event is None:
            return
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=float(self._checkpoint_interval_seconds),
                )
                break
            except TimeoutError:
                session = await self.get_session_data()
                await self._persist_session(session)
