import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, cast

import websockets
from pydantic import BaseModel, Field
from semantic_router import Route
from websockets.exceptions import ConnectionClosed

from sales_copilot.auth.feature_policy import (
    FEATURE_SCRIPT_TRACKING_LIVE,
    FeaturePolicy,
    get_feature_policy,
)
from sales_copilot.core.config import DetectorConfig, WebSocketConfig, env_bool, env_int, load_yaml
from sales_copilot.core.llm_client import LLMClient
from sales_copilot.modules.detector.router import (
    RouteMatch,
    _build_router,
    _normalize_model_name,
)
from sales_copilot.modules.reports.session import ScriptCoverageCheckpointStore
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)

ScriptStatus = Literal["missing", "partial", "tentative", "confirmed"]
_VALID_STATUSES: frozenset[str] = frozenset({"missing", "partial", "tentative", "confirmed"})

# Legacy alias (Phase 5, design finding #5): pre-Phase-5 checkpoints stored the
# fast-track state as "discussed" -- that state IS today's "tentative"
# (mogelijk geraakt). Normalized on read so old checkpoints stay loadable.
_LEGACY_STATUS_ALIASES: dict[str, str] = {"discussed": "tentative"}


def _normalize_status(status: object) -> ScriptStatus | None:
    """Map a raw persisted status onto the current enum, or None when invalid."""
    if not isinstance(status, str):
        return None
    normalized = _LEGACY_STATUS_ALIASES.get(status, status)
    if normalized in _VALID_STATUSES:
        return cast(ScriptStatus, normalized)
    return None


@dataclass(frozen=True)
class ScriptPoint:
    """A single point the salesperson wants to cover."""

    id: str
    title: str
    phase: str
    required: bool
    example_phrases: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScriptCoverage:
    """Coverage state for one script point."""

    point_id: str
    title: str
    phase: str
    required: bool
    status: ScriptStatus
    confidence: float = 0.0
    hint: str = ""
    timestamp_ms: int | None = None


def load_script_points(path: str | Path) -> list[ScriptPoint]:
    """Load script point definitions from a YAML config file.

    Shared by `ScriptTracker` (live tracking) and the post-call Free
    scorecard (Phase 3), which re-derives the full point list -- total count
    and required flags -- when reducing the persisted checkpoint snapshot.
    Relative paths resolve against the repo root, same as before.
    """
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path(__file__).parents[4] / resolved
    points: list[ScriptPoint] = []
    data = load_yaml(resolved)
    for entry in data.get("script", []):
        if not isinstance(entry, dict):
            continue
        point_id = entry.get("id")
        if not isinstance(point_id, str) or not point_id:
            continue
        points.append(
            ScriptPoint(
                id=point_id,
                title=str(entry.get("title", point_id)),
                phase=str(entry.get("phase", "discovery")).lower(),
                required=bool(entry.get("required", True)),
                example_phrases=[str(u) for u in entry.get("example_phrases", []) if u],
                keywords=[str(k) for k in entry.get("keywords", []) if k],
            )
        )
    return points


def build_coverage_summary(
    points: Sequence[ScriptPoint],
    stored: Mapping[str, object] | None,
) -> dict[str, object] | None:
    """Reduce a persisted checkpoint snapshot to the Free scorecard numbers.

    Phase 3 (design doc section 9): the scorecard reads the PERSISTED
    checkpoint -- never live in-process memory -- and shows the count and the
    gap only: how many points were covered (status tentative/confirmed; a
    legacy pre-Phase-5 "discussed" row normalizes to "tentative"), the
    total, and which required points are still missing (missing/partial).
    Returns None when there is no persisted snapshot at all (e.g. script
    tracking disabled for the call) so the caller omits the section instead
    of showing a dishonest 0-of-N.
    """
    if stored is None:
        return None
    covered = 0
    missing_required: list[str] = []
    for point in points:
        row = stored.get(point.id)
        status = _normalize_status(row.get("status")) if isinstance(row, dict) else None
        if status is None:
            status = "missing"
        if status in {"tentative", "confirmed"}:
            covered += 1
        elif point.required:
            missing_required.append(point.title)
    return {
        "covered": covered,
        "total": len(points),
        "missing_required": missing_required,
    }


class _CoverageConfirmResponse(BaseModel):
    """Structured LLM confirmation output."""

    covered_point_ids: list[str] = Field(
        default_factory=list,
        description="IDs van scriptpunten die duidelijk aan bod zijn gekomen.",
    )
    revoked_point_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs van scriptpunten die als 'mogelijk geraakt' gemarkeerd waren "
            "maar volgens het transcript toch NIET aan bod zijn gekomen."
        ),
    )
    hints: dict[str, str] = Field(
        default_factory=dict,
        description="Korte terugkom-hint per nog ontbrekend punt.",
    )


class ScriptRouter:
    """Fast local matcher between transcript text and script-point example phrases.

    Reuses the same sentence-transformer embedding router as the pain-point
    detector so the behaviour is consistent and model downloads are shared.
    """

    _CLASSIFY_TIMEOUT_S: float = 30.0

    def __init__(
        self,
        config: DetectorConfig,
        points: Sequence[ScriptPoint],
    ) -> None:
        self.config = config
        self.points = list(points)
        self.routes = [
            Route(
                name=point.id,
                utterances=point.example_phrases,
                metadata={
                    "title": point.title,
                    "phase": point.phase,
                    "required": point.required,
                },
            )
            for point in self.points
            if point.example_phrases
        ]
        self._router: object | None = None
        self._router_lock = threading.Lock()
        self._route_utterances: dict[str, list[str]] = {
            point.id: [utterance.strip().lower() for utterance in point.example_phrases if utterance.strip()]
            for point in self.points
        }

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) >= 4}

    def _ensure_router(self) -> object:
        if self._router is not None:
            return self._router
        with self._router_lock:
            if self._router is None:
                logger.debug(
                    "Loading script-tracking embedding model: %s",
                    _normalize_model_name(self.config.embedding_model),
                )
                self._router = _build_router(self.routes, self.config.embedding_model)
        return self._router

    def _keyword_match(self, text: str) -> RouteMatch | None:
        best_point_id: str | None = None
        best_score = 0
        text_tokens = self._tokens(text)
        for point_id, utterances in self._route_utterances.items():
            for utterance in utterances:
                if utterance in text or text in utterance:
                    return RouteMatch(category=point_id, confidence=1.0, tier="high", source="keyword")
                utterance_tokens = self._tokens(utterance)
                overlap = utterance_tokens & text_tokens
                if not overlap:
                    continue
                long_token_overlap = any(len(token) >= 7 for token in overlap)
                score = len(overlap) + (1 if long_token_overlap else 0)
                if score > best_score:
                    best_point_id = point_id
                    best_score = score
        if best_point_id is None or best_score < 2:
            return None
        return RouteMatch(category=best_point_id, confidence=0.95, tier="high", source="keyword")

    def classify(self, text: str) -> RouteMatch | None:
        cleaned = text.strip()
        if not cleaned:
            return None

        keyword_match = self._keyword_match(cleaned.lower())
        if keyword_match is not None:
            return keyword_match

        if not self.routes:
            return None

        choice = self._ensure_router()(cleaned)
        if isinstance(choice, list):
            choice = choice[0] if choice else None
        if choice is None or choice.name is None or choice.similarity_score is None:
            return None
        score = float(choice.similarity_score)
        if score < self.config.confidence_threshold_low:
            return None
        tier: Literal["high", "uncertain", "none"]
        if score >= self.config.confidence_threshold_high:
            tier = "high"
        else:
            tier = "uncertain"
        return RouteMatch(category=choice.name, confidence=score, tier=tier)

    async def classify_async(self, text: str) -> RouteMatch | None:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.classify, text),
                timeout=self._CLASSIFY_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "Script router classify timed out after %.1fs — skipping classification",
                self._CLASSIFY_TIMEOUT_S,
            )
            return None


class ScriptCoverageLLMClient:
    """Periodic LLM confirmation: which script points are covered + comeback hints."""

    _SYSTEM_PROMPT = (
        "Je bent een salescoach. Je krijgt een gesprekstranscript en een checklist "
        "met scriptpunten. Bepaal welke punten duidelijk aan bod zijn gekomen. "
        "Controleer ook de punten die de snelle matcher als 'mogelijk geraakt' "
        "markeerde: bevestig ze alleen als ze echt aan bod kwamen, en trek ze "
        "anders in. Voor elk nog ontbrekend verplicht punt geef je een korte, "
        "concrete terugkom-hint die de verkoper kan gebruiken."
    )

    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        self.provider = config.llm_provider.lower()
        self._llm = LLMClient(self.provider, timeout_ms=config.llm_timeout_ms)

    async def confirm(
        self,
        points: Sequence[ScriptPoint],
        transcript_lines: Sequence[str],
        tentative: Sequence[ScriptPoint] = (),
    ) -> _CoverageConfirmResponse | None:
        if self._llm is None or self._llm._create is None:
            logger.debug("Script coverage LLM skipped: provider is '%s'", self.provider)
            return None
        cleaned_lines = [line.strip() for line in transcript_lines if isinstance(line, str) and line.strip()]
        if not cleaned_lines:
            return None
        try:
            return await self._llm.acreate(
                model=self.config.llm_model,
                system_prompt=self._SYSTEM_PROMPT,
                user_text=self._user_prompt(points, cleaned_lines, tentative),
                response_model=_CoverageConfirmResponse,
                temperature=self.config.llm_temperature,
                allow_local=True,
            )
        except TimeoutError:
            logger.warning("Script coverage LLM confirmation timed out")
            return None
        except Exception:
            logger.exception("Script coverage LLM confirmation failed")
            return None

    def _user_prompt(
        self,
        points: Sequence[ScriptPoint],
        transcript_lines: Sequence[str],
        tentative: Sequence[ScriptPoint] = (),
    ) -> str:
        script_block = "\n".join(
            f"- {point.id}: {point.title} (fase={point.phase}, verplicht={point.required})"
            for point in points
        )
        tentative_block = (
            "\n".join(f"- {point.id}: {point.title}" for point in tentative)
            if tentative
            else "- (geen)"
        )
        transcript_block = "\n".join(f"- {line}" for line in transcript_lines)
        return (
            "Scriptpunten om te checken:\n"
            f"{script_block}\n\n"
            "Deze punten zijn door de snelle matcher als 'mogelijk geraakt' gemarkeerd:\n"
            f"{tentative_block}\n\n"
            "Transcript van het gesprek tot nu toe:\n"
            f"{transcript_block}\n\n"
            "Geef terug:\n"
            "1. covered_point_ids: IDs van punten die duidelijk aan bod kwamen.\n"
            "2. revoked_point_ids: IDs van 'mogelijk geraakt' punten die volgens "
            "het transcript toch NIET aan bod kwamen.\n"
            "3. hints: per nog ontbrekend verplicht punt een korte terugkom-hint.\n"
            "Hints moeten concreet zijn, in het Nederlands, en maximaal 1 zin."
        )


class ScriptTracker:
    """Tracks script coverage for a single call.

    Two-track coverage model (Phase 5, Q2 + design finding #5):

    - FAST track: an embedding/keyword match on an utterance marks a point
      "tentative" (mogelijk geraakt) -- never a hard confirmation, because
      the fast matcher over-matches (measured precision 0.590, below the
      0.80 advertise gate). Tentative state is visible immediately but is
      correctable.
    - SLOW track: `confirm_coverage()` runs an async LLM pass off the live
      path. It PROMOTES a tentative fast-track match to "confirmed"
      (bevestigd), can confirm additional points the fast track missed, can
      REVOKE a tentative match it disagrees with (the point becomes
      re-matchable), and generates hints for missing ones.
    - Confirmed is sticky: once the slow track confirms a point it stays
      confirmed. Tentative is the only state the slow track may downgrade.
    """

    def __init__(
        self,
        config: DetectorConfig,
        *,
        points: Sequence[ScriptPoint] | None = None,
        router: ScriptRouter | None = None,
        llm_client: ScriptCoverageLLMClient | None = None,
        now_ms: Callable[[], int] | None = None,
        session_id: str | None = None,
        checkpoint_enabled: bool | None = None,
        checkpoint_store: ScriptCoverageCheckpointStore | None = None,
    ) -> None:
        self.config = config
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._points: dict[str, ScriptPoint] = {}
        if points is not None:
            self._points = {point.id: point for point in points}
        else:
            self._load_points(config.script_tracking_config)
        self.router = router or ScriptRouter(config, list(self._points.values()))
        self.llm_client = llm_client or ScriptCoverageLLMClient(config)
        self._covered: dict[str, ScriptCoverage] = {}
        self._lock = asyncio.Lock()

        # Crash/restart recovery (design finding #2, Phase 2). Gated behind a
        # checkpoint flag so it can be disabled without a data-model break:
        # disabled (or no session_id) falls back to today's in-memory-only
        # behaviour exactly -- the rollback path from the design doc's
        # phased-rollout section. `session_id` must come from the caller (the
        # same id `SessionTracker` uses for the call) so checkpoint rows are
        # keyed consistently across the two independent trackers.
        self._session_id = session_id
        self._checkpoint_enabled = (
            checkpoint_enabled
            if checkpoint_enabled is not None
            else bool(env_bool("SCRIPT_COVERAGE_CHECKPOINT_ENABLED", True))
        )
        self._checkpoint_store: ScriptCoverageCheckpointStore | None = checkpoint_store
        if self._checkpoint_store is None and self._checkpoint_enabled:
            self._checkpoint_store = ScriptCoverageCheckpointStore()

    def _load_points(self, path: str) -> None:
        for point in load_script_points(path):
            self._points[point.id] = point

    @property
    def points(self) -> dict[str, ScriptPoint]:
        return self._points

    async def process_utterance(self, text: str, speaker: str, timestamp_ms: int) -> ScriptCoverage | None:
        """Fast local match. Returns the coverage update if a new point was covered.

        A fast-track hit is TENTATIVE (mogelijk geraakt), never confirmed --
        only the slow-track LLM confirm may promote it (Phase 5, Q2).
        """
        cleaned = text.strip()
        if not cleaned:
            return None

        match = await self.router.classify_async(cleaned)
        if match is None:
            return None

        async with self._lock:
            if match.category in self._covered:
                return None
            point = self._points.get(match.category)
            if point is None:
                return None
            coverage = ScriptCoverage(
                point_id=point.id,
                title=point.title,
                phase=point.phase,
                required=point.required,
                status="tentative" if match.tier == "high" else "partial",
                confidence=match.confidence,
                hint="",
                timestamp_ms=timestamp_ms,
            )
            self._covered[point.id] = coverage
        logger.debug("Script point covered: %s (%s)", point.id, match.tier)
        return coverage

    async def confirm_coverage(
        self,
        transcript_lines: Sequence[str],
    ) -> dict[str, ScriptCoverage]:
        """Async LLM confirmation off the live path. Returns the updated coverage map.

        Phase 5 (Q2, design finding #5): the slow track both PROMOTES and
        CORRECTS the fast track. A point the LLM lists as covered is promoted
        to "confirmed" (from tentative/partial, or confirmed outright when
        the fast track missed it). A tentative/partial point the LLM revokes
        is removed from the coverage map so the fast track may match it
        again later -- tentative is correctable. "confirmed" is sticky in
        both directions: never re-confirmed, never revoked.
        """
        async with self._lock:
            points = list(self._points.values())
            tentative_points = [
                self._points[coverage.point_id]
                for coverage in self._covered.values()
                if coverage.status in {"tentative", "partial"}
                and coverage.point_id in self._points
            ]

        confirmation = await self.llm_client.confirm(points, transcript_lines, tentative_points)
        if confirmation is None:
            return dict(self._covered)

        async with self._lock:
            for point_id in confirmation.covered_point_ids:
                point = self._points.get(point_id)
                if point is None:
                    continue
                existing = self._covered.get(point_id)
                if existing is not None and existing.status == "confirmed":
                    continue  # confirmed is sticky
                self._covered[point_id] = ScriptCoverage(
                    point_id=point.id,
                    title=point.title,
                    phase=point.phase,
                    required=point.required,
                    status="confirmed",
                    confidence=1.0,
                    hint="",
                    timestamp_ms=self._now_ms(),
                )
            for point_id in confirmation.revoked_point_ids:
                existing = self._covered.get(point_id)
                if existing is None or existing.status == "confirmed":
                    continue  # nothing to revoke / confirmed is sticky
                if existing.status in {"tentative", "partial"}:
                    # Correctable: drop the row entirely so a later fast-track
                    # match can cover the point again (the idempotency check
                    # is `if match.category in self._covered`).
                    del self._covered[point_id]
            for point_id, hint in confirmation.hints.items():
                point = self._points.get(point_id)
                if point is None:
                    continue
                if point_id not in self._covered:
                    self._covered[point_id] = ScriptCoverage(
                        point_id=point.id,
                        title=point.title,
                        phase=point.phase,
                        required=point.required,
                        status="missing",
                        confidence=0.0,
                        hint=hint.strip(),
                        timestamp_ms=self._now_ms(),
                    )
                else:
                    existing = self._covered[point_id]
                    self._covered[point_id] = ScriptCoverage(
                        point_id=existing.point_id,
                        title=existing.title,
                        phase=existing.phase,
                        required=existing.required,
                        status=existing.status,
                        confidence=existing.confidence,
                        hint=hint.strip() or existing.hint,
                        timestamp_ms=existing.timestamp_ms,
                    )
            return dict(self._covered)

    async def get_coverage_state(self) -> dict[str, ScriptCoverage]:
        async with self._lock:
            return dict(self._covered)

    def build_full_snapshot(self) -> list[ScriptCoverage]:
        """Return a coverage row for every script point (covered or missing)."""
        covered = dict(self._covered)
        snapshot: list[ScriptCoverage] = []
        for point in self._points.values():
            if point.id in covered:
                snapshot.append(covered[point.id])
            else:
                snapshot.append(
                    ScriptCoverage(
                        point_id=point.id,
                        title=point.title,
                        phase=point.phase,
                        required=point.required,
                        status="missing",
                        confidence=0.0,
                        hint="",
                        timestamp_ms=None,
                    )
                )
        return snapshot

    def missing_required_points(self) -> list[ScriptCoverage]:
        return [c for c in self.build_full_snapshot() if c.required and c.status in {"missing", "partial"}]

    async def checkpoint(self) -> None:
        """Persist the current coverage map to SQLite (design finding #2).

        Called on every confirmation cycle by `ScriptTrackerEngine`. Persists
        `self._covered` -- the sparse map of points that have actually been
        touched -- not the dense `build_full_snapshot()` output, so points
        that were never matched stay absent from the checkpoint and rehydrate
        as "never touched" rather than as an inert placeholder row that would
        block future coverage (`process_utterance`'s idempotency check is
        `if match.category in self._covered`).

        No-op when checkpointing is disabled or no `session_id` was supplied:
        that is the Phase 2 rollback path -- disable the flag (or omit
        `session_id`) and behaviour is in-memory-only, exactly as before this
        phase shipped.
        """
        if not self._checkpoint_enabled or not self._session_id or self._checkpoint_store is None:
            return
        async with self._lock:
            covered = {point_id: asdict(coverage) for point_id, coverage in self._covered.items()}
        try:
            await self._checkpoint_store.save(self._session_id, covered)
        except Exception:
            logger.warning(
                "Script coverage checkpoint failed for session %s", self._session_id, exc_info=True
            )

    async def restore_checkpoint(self) -> bool:
        """Rehydrate `_covered` from the last persisted checkpoint, if any.

        Call this before resuming work on a session id that may have crashed
        mid-call (`ScriptTrackerEngine.run` does this before consuming any
        channel). Reads are keyed on `session_id`, so a fresh tracker for a
        *different* session id never sees another session's rows. Returns
        True when a checkpoint was found and applied.
        """
        if not self._checkpoint_enabled or not self._session_id or self._checkpoint_store is None:
            return False
        try:
            stored = await self._checkpoint_store.load(self._session_id)
        except Exception:
            logger.warning(
                "Script coverage checkpoint load failed for session %s", self._session_id, exc_info=True
            )
            return False
        if not stored:
            return False

        restored: dict[str, ScriptCoverage] = {}
        for point_id, payload in stored.items():
            if point_id not in self._points or not isinstance(payload, dict):
                continue
            status = _normalize_status(payload.get("status"))
            if status is None:
                logger.warning(
                    "Skipping checkpoint row with invalid status for point %s (session %s)",
                    point_id,
                    self._session_id,
                )
                continue
            try:
                restored[point_id] = ScriptCoverage(
                    point_id=str(payload["point_id"]),
                    title=str(payload["title"]),
                    phase=str(payload["phase"]),
                    required=bool(payload["required"]),
                    status=status,
                    confidence=float(payload.get("confidence", 0.0)),
                    hint=str(payload.get("hint", "")),
                    timestamp_ms=payload.get("timestamp_ms"),
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Skipping malformed checkpoint row for point %s (session %s)",
                    point_id,
                    self._session_id,
                )
                continue

        if not restored:
            return False

        async with self._lock:
            self._covered = restored
        logger.info(
            "Script coverage restored from checkpoint: %d point(s) for session %s",
            len(restored),
            self._session_id,
        )
        return True


class ScriptTrackerEngine:
    """Background engine for periodic LLM confirmation and dashboard publishing.

    Runs off the live detector path: it consumes transcript/phase WebSocket
    channels and emits `script_coverage` / `script_nudge` events, plus a
    periodic `script_tracking_health` heartbeat (design finding #4).
    """

    DEFAULT_INTERVAL_SECONDS = 60
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 20
    MAX_TRANSCRIPT_LINES = 40

    def __init__(
        self,
        tracker: ScriptTracker,
        ws_config: WebSocketConfig,
        *,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        heartbeat_interval_seconds: int | None = None,
        now_ms: Callable[[], int] | None = None,
        feature_policy: FeaturePolicy | None = None,
    ) -> None:
        self.tracker = tracker
        self.ws_config = ws_config
        self._interval_seconds = max(5, int(interval_seconds))
        # Heartbeat cadence (finding #4). Follows the same override-then-env
        # convention as `ScriptTracker`'s checkpoint flag: an explicit
        # constructor arg wins, otherwise fall back to an env knob so the
        # cadence can be tuned per-deployment without a code change.
        self._heartbeat_interval_seconds = max(
            5,
            int(
                heartbeat_interval_seconds
                if heartbeat_interval_seconds is not None
                else (
                    env_int(
                        "SCRIPT_TRACKING_HEARTBEAT_INTERVAL_S",
                        self.DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
                    )
                    or self.DEFAULT_HEARTBEAT_INTERVAL_SECONDS
                )
            ),
        )
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._transcript_lines: list[str] = []
        self._lock = asyncio.Lock()
        self._phase: str = "discovery"
        self._script_ws: websockets.ClientConnection | None = None
        self._feature_policy = feature_policy or get_feature_policy()

        # Liveness state (finding #4). `_publish_degraded` reflects the
        # outcome of the last WS send attempt; `_confirm_degraded` reflects
        # the outcome of the last confirmation cycle (finding #3). Combined,
        # they drive the `degraded` flag `_publish_payload` stamps on every
        # outgoing message -- including the heartbeat itself.
        self._health_lock = asyncio.Lock()
        self._publish_degraded = False
        self._confirm_degraded = False

    async def run(self, stop_event: asyncio.Event) -> None:
        # Crash/restart recovery (design finding #2): rehydrate coverage for
        # this session id from the last checkpoint BEFORE any channel is
        # consumed, so a restart on the same session never regresses covered
        # points back to "missing". No-op when the tracker has no session_id
        # or checkpointing is disabled (Phase 2 rollback path).
        await self.tracker.restore_checkpoint()
        tasks = [
            asyncio.create_task(
                self._confirmation_loop(stop_event), name="script-tracker-confirm"
            ),
            asyncio.create_task(
                self._heartbeat_loop(stop_event), name="script-tracker-heartbeat"
            ),
            asyncio.create_task(
                self._consume_channel("transcript", self._handle_transcript, stop_event),
                name="script-tracker-transcript",
            ),
            asyncio.create_task(
                self._consume_channel("phase", self._handle_phase, stop_event),
                name="script-tracker-phase",
            ),
        ]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results):
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, BaseException):
                    logger.warning(
                        "Script tracker engine task %s exited with error",
                        task.get_name(),
                        exc_info=result,
                    )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await self.close()

    async def close(self) -> None:
        if self._script_ws is None:
            return
        try:
            await self._script_ws.close()
        except Exception:
            pass
        self._script_ws = None

    async def _confirmation_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
                break
            except TimeoutError:
                await self._run_confirmation_cycle()

    async def _heartbeat_loop(self, stop_event: asyncio.Event) -> None:
        """Finding #4: periodic liveness signal, independent of confirmation
        cadence. Runs even when nothing else is due to publish, so a stalled
        confirmation cycle or a silently-dropped WS connection still reaches
        the widget instead of leaving it to guess from a frozen last update."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._heartbeat_interval_seconds)
                break
            except TimeoutError:
                await self._publish_health()

    async def _run_confirmation_cycle(self) -> None:
        """Pop the pending transcript lines and run the slow LLM confirm pass.

        Finding #3: the buffer is only cleared once `confirm_coverage`
        SUCCEEDS. On failure the popped lines are re-queued (prepended ahead
        of whatever arrived on the live channel meanwhile) so the next cycle
        retries them instead of the lines being silently dropped forever --
        this is also the future home of adequacy scoring per the design doc,
        so losing lines here would silently drop that too.
        """
        async with self._lock:
            transcript_lines = self._transcript_lines
            self._transcript_lines = []

        if not transcript_lines:
            return

        try:
            await self.tracker.confirm_coverage(transcript_lines)
        except Exception:
            logger.warning(
                "Script tracker confirmation cycle failed; re-queuing %d transcript line(s) for retry",
                len(transcript_lines),
                exc_info=True,
            )
            await self._requeue_transcript_lines(transcript_lines)
            await self._set_confirm_degraded(True)
            # Coverage state itself is unchanged by a failed confirm --
            # `ScriptTracker.confirm_coverage` only mutates `_covered` after
            # the LLM call resolves, so a failure here leaves the map exactly
            # as it was before this cycle. Checkpointing it again is a no-op
            # re-save of the last-known-good state, not a corruption of it,
            # which is why Phase 2's checkpoint call stays on this path too.
            await self.tracker.checkpoint()
            return

        await self._set_confirm_degraded(False)
        # Checkpoint runs for every tier (Free included) -- it is the
        # compute-side persistence finding #2 requires, not the live push.
        # Keep it outside `_publish_payload`'s `.live` choke point.
        await self.tracker.checkpoint()
        await self._publish_coverage()

    async def _requeue_transcript_lines(self, failed_lines: list[str]) -> None:
        """Re-queue transcript lines from a confirmation cycle that failed.

        Prepends `failed_lines` ahead of anything `_handle_transcript`
        appended while the cycle was running, so retry order stays
        chronological, then applies the same bound `_handle_transcript`
        already uses (keep newest `MAX_TRANSCRIPT_LINES`, drop oldest) so a
        persistently failing LLM cannot grow the buffer unbounded.
        """
        async with self._lock:
            merged = failed_lines + self._transcript_lines
            overflow = len(merged) - self.MAX_TRANSCRIPT_LINES
            if overflow > 0:
                merged = merged[-self.MAX_TRANSCRIPT_LINES :]
                logger.warning(
                    "Script tracker re-queue exceeded MAX_TRANSCRIPT_LINES (%d); "
                    "dropped %d oldest unretried line(s)",
                    self.MAX_TRANSCRIPT_LINES,
                    overflow,
                )
            self._transcript_lines = merged

    async def _set_confirm_degraded(self, value: bool) -> None:
        async with self._health_lock:
            self._confirm_degraded = value

    async def _publish_coverage(self) -> None:
        snapshot = self.tracker.build_full_snapshot()
        payload = {
            "type": "script_coverage",
            "coverage": [
                {
                    "point_id": item.point_id,
                    "title": item.title,
                    "phase": item.phase,
                    "required": item.required,
                    "status": item.status,
                    "confidence": round(item.confidence, 3),
                    "hint": item.hint,
                    "timestamp_ms": item.timestamp_ms,
                }
                for item in snapshot
            ],
            "missing_required": [c.point_id for c in self.tracker.missing_required_points()],
            "phase": self._phase,
            "timestamp_ms": self._now_ms(),
        }
        await self._publish_payload(payload)

    async def _publish_nudge(self, phase: str) -> None:
        missing = self.tracker.missing_required_points()
        if not missing:
            return
        payload = {
            "type": "script_nudge",
            "phase": phase,
            "missing": [c.point_id for c in missing],
            "top_hint": missing[0].hint or f"Kom terug op: {missing[0].title}.",
            "timestamp_ms": self._now_ms(),
        }
        await self._publish_payload(payload)

    async def _publish_health(self) -> None:
        """Finding #4 heartbeat message.

        Carries no state of its own beyond `type`/`timestamp_ms` --
        `_publish_payload` stamps the current `degraded` flag on every
        message it sends, this one included, so a stalled confirmation cycle
        or a broken WS surfaces here even when no coverage/nudge update was
        otherwise due.
        """
        await self._publish_payload({"type": "script_tracking_health", "timestamp_ms": self._now_ms()})

    async def _ensure_script_ws(self) -> websockets.ClientConnection:
        if self._script_ws is None or getattr(self._script_ws, "close_code", None) is not None:
            self._script_ws = await websockets.connect(
                channel_ws_url(self.ws_config, "script-tracking")
            )
        return self._script_ws

    async def _publish_payload(self, payload: dict[str, object]) -> None:
        """The single Free/Pro choke point for this engine (Phase 1, finding #1).

        `ScriptTracker` computation runs for every tier that has `.compute`
        (Free included) so coverage is always tracked and can be persisted for
        the post-call scorecard. Only the live WS push -- this method -- is
        gated on `.live`. No other method in this engine checks tier; keep it
        that way so the boundary stays in exactly one place.

        Also the single point that stamps a `degraded` flag (finding #4) onto
        every outgoing message: True when the last WS send attempt failed or
        the last confirmation cycle failed (finding #3). The flag reflects
        state as of *before* this send -- the message that would first report
        a brand-new publish failure cannot itself be the one that failed to
        arrive, so a fresh failure surfaces on the next successful send or
        heartbeat tick instead.
        """
        if not self._feature_policy.allows(FEATURE_SCRIPT_TRACKING_LIVE):
            return
        async with self._health_lock:
            degraded = self._publish_degraded or self._confirm_degraded
        outgoing = {**payload, "degraded": degraded}
        try:
            ws = await self._ensure_script_ws()
            await ws.send(json.dumps(outgoing))
        except Exception:
            logger.warning("Failed to publish script-tracking payload (type=%s)", payload.get("type"))
            async with self._health_lock:
                self._publish_degraded = True
            return
        async with self._health_lock:
            self._publish_degraded = False

    async def _consume_channel(
        self,
        channel: str,
        handler: Callable[[object], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        url = channel_ws_url(self.ws_config, channel)
        backoff = 1.0
        while not stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:
                    backoff = 1.0
                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
                        except TimeoutError:
                            continue
                        except ConnectionClosed:
                            break
                        payload = self._decode_payload(raw)
                        try:
                            await handler(payload)
                        except Exception:
                            logger.warning("Script tracker %s handler failed.", channel, exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if stop_event.is_set():
                    break
                logger.warning("Script tracker stream disconnected for %s: %s", channel, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle_transcript(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "transcript":
            return
        text = payload.get("text")
        speaker = payload.get("speaker")
        if not isinstance(text, str) or not isinstance(speaker, str):
            return
        cleaned = " ".join(text.split()).strip()
        if not cleaned:
            return
        line = f"{speaker}: {cleaned}"
        async with self._lock:
            self._transcript_lines.append(line)
            self._transcript_lines = self._transcript_lines[-self.MAX_TRANSCRIPT_LINES:]

    async def _handle_phase(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "phase_change":
            return
        phase = payload.get("phase")
        if not isinstance(phase, str) or not phase.strip():
            return
        if phase == self._phase:
            return
        self._phase = phase
        await self._publish_nudge(phase)

    @staticmethod
    def _decode_payload(raw: str | bytes) -> object:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
