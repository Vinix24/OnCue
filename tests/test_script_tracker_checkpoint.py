"""Tests for script-tracking coverage checkpoint/restore.

Salesprep design doc (`claudedocs/2026-07-22-salesprep-pro-design.md`),
section 7 finding #2 / section 9 Phase 2: a crash mid-call must not produce
a dishonest Free gap report, so `ScriptTracker` checkpoints its coverage map
to SQLite on every confirmation cycle and rehydrates it on restart, keyed by
session id, reusing the local SQLite file `SessionTracker` already
checkpoints session data into.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.coaching.script_tracker import (
    ScriptCoverageLLMClient,
    ScriptPoint,
    ScriptTracker,
    ScriptTrackerEngine,
    _CoverageConfirmResponse,
)
from sales_copilot.modules.detector.router import RouteMatch
from sales_copilot.modules.reports.session import ScriptCoverageCheckpointStore


@pytest.fixture()
def sample_points() -> list[ScriptPoint]:
    return [
        ScriptPoint(
            id="budget",
            title="Budget",
            phase="discovery",
            required=True,
            example_phrases=["wat is het budget voor dit project"],
        ),
        ScriptPoint(
            id="tijdlijn",
            title="Tijdlijn",
            phase="discovery",
            required=True,
            example_phrases=["wanneer moet dit opgeleverd zijn"],
        ),
    ]


@pytest.fixture()
def detector_config() -> DetectorConfig:
    """A config that keeps tests fast: no real LLM, deterministic thresholds."""
    return DetectorConfig(
        llm_provider="none",
        confidence_threshold_high=0.85,
        confidence_threshold_low=0.50,
    )


class _StaticRouter:
    """Test-only router that returns deterministic matches without embeddings."""

    def __init__(self, matches: dict[str, RouteMatch]) -> None:
        self._matches = matches

    async def classify_async(self, text: str) -> RouteMatch | None:
        return self._matches.get(text)


BUDGET_UTTERANCE = "we hebben een budget van tienduizend euro"
BUDGET_MATCH = {BUDGET_UTTERANCE: RouteMatch(category="budget", confidence=0.92, tier="high")}
TIJDLIJN_UTTERANCE = "wanneer moet dit opgeleverd zijn"
TIJDLIJN_MATCH = {TIJDLIJN_UTTERANCE: RouteMatch(category="tijdlijn", confidence=0.90, tier="high")}


# ---------------------------------------------------------------------------
# (a) coverage is checkpointed to SQLite during a call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_persists_coverage_to_sqlite(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")
    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter(BUDGET_MATCH),
        session_id="session-a",
        checkpoint_store=store,
    )

    await tracker.process_utterance(BUDGET_UTTERANCE, "self", 1000)
    await tracker.checkpoint()

    persisted = await store.load("session-a")
    assert persisted is not None
    assert persisted["budget"]["status"] == "tentative"
    assert persisted["budget"]["point_id"] == "budget"
    assert persisted["budget"]["confidence"] == pytest.approx(0.92)
    # Points never touched are absent, not a placeholder "missing" row --
    # that would incorrectly satisfy `process_utterance`'s idempotency check
    # (`if match.category in self._covered`) on rehydrate.
    assert "tijdlijn" not in persisted


@pytest.mark.asyncio
async def test_confirmation_cycle_checkpoints_coverage(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    """Coverage is checkpointed as part of the normal in-call flow: the
    engine's periodic confirmation cycle, not just a manual checkpoint()."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")

    class _FastLLM(ScriptCoverageLLMClient):
        async def confirm(self, points: Any, transcript_lines: Any, tentative: Any = None) -> Any:
            return _CoverageConfirmResponse(covered_point_ids=["tijdlijn"], hints={})

    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        llm_client=_FastLLM(detector_config),
        session_id="session-confirm",
        checkpoint_store=store,
    )
    engine = ScriptTrackerEngine(tracker, WebSocketConfig())
    engine._transcript_lines = ["prospect: wanneer moet dit opgeleverd zijn"]

    await engine._run_confirmation_cycle()

    persisted = await store.load("session-confirm")
    assert persisted is not None
    assert persisted["tijdlijn"]["status"] == "confirmed"


# ---------------------------------------------------------------------------
# (b) fresh tracker for the SAME session id rehydrates persisted coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_rehydrates_coverage_for_same_session(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")

    before_crash = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter(BUDGET_MATCH),
        session_id="session-restart",
        checkpoint_store=store,
    )
    await before_crash.process_utterance(BUDGET_UTTERANCE, "self", 1000)
    await before_crash.checkpoint()

    # Simulate crash + restart: a brand-new tracker instance for the SAME
    # session id, with a router that would not re-detect the point on its
    # own -- coverage can only come from the checkpoint.
    after_restart = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter({}),
        session_id="session-restart",
        checkpoint_store=store,
    )
    restored = await after_restart.restore_checkpoint()

    assert restored is True
    state = await after_restart.get_coverage_state()
    assert state["budget"].status == "tentative"
    assert state["budget"].confidence == pytest.approx(0.92)

    # Idempotency survives the restart: re-matching the same point again
    # must not treat it as new.
    duplicate = await after_restart.process_utterance(BUDGET_UTTERANCE, "self", 5000)
    assert duplicate is None


@pytest.mark.asyncio
async def test_engine_run_restores_checkpoint_before_consuming_channels(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ScriptTrackerEngine.run()` rehydrates the tracker before consuming
    any channel -- restoring BEFORE resuming, not racing with live updates."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")
    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter({}),
        session_id="session-engine",
        checkpoint_store=store,
    )
    engine = ScriptTrackerEngine(tracker, WebSocketConfig())

    call_order: list[str] = []

    async def _fake_restore() -> bool:
        call_order.append("restore")
        return True

    async def _fake_confirmation_loop(stop_event: asyncio.Event) -> None:
        call_order.append("confirm")

    async def _fake_heartbeat_loop(stop_event: asyncio.Event) -> None:
        # Phase 4 added a real `_heartbeat_loop` task to `run()`. Like the
        # confirmation/consume loops, it must be stubbed to an instant no-op
        # here: this test drives `run()` with a never-set Event, so the real
        # heartbeat loop would spin forever and deadlock `asyncio.gather()`.
        call_order.append("heartbeat")

    async def _fake_consume_channel(channel: str, handler: Any, stop_event: asyncio.Event) -> None:
        call_order.append(f"consume:{channel}")

    monkeypatch.setattr(tracker, "restore_checkpoint", _fake_restore)
    monkeypatch.setattr(engine, "_confirmation_loop", _fake_confirmation_loop)
    monkeypatch.setattr(engine, "_heartbeat_loop", _fake_heartbeat_loop)
    monkeypatch.setattr(engine, "_consume_channel", _fake_consume_channel)

    await engine.run(asyncio.Event())

    assert call_order[0] == "restore"
    assert "consume:transcript" in call_order
    assert "consume:phase" in call_order


# ---------------------------------------------------------------------------
# (c) two different session ids do not cross-contaminate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_sessions_do_not_cross_contaminate(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")

    tracker_x = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter(BUDGET_MATCH),
        session_id="session-x",
        checkpoint_store=store,
    )
    tracker_y = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter(TIJDLIJN_MATCH),
        session_id="session-y",
        checkpoint_store=store,
    )

    await tracker_x.process_utterance(BUDGET_UTTERANCE, "self", 1000)
    await tracker_x.checkpoint()
    await tracker_y.process_utterance(TIJDLIJN_UTTERANCE, "self", 1000)
    await tracker_y.checkpoint()

    persisted_x = await store.load("session-x")
    persisted_y = await store.load("session-y")

    assert persisted_x is not None and persisted_y is not None
    assert set(persisted_x.keys()) == {"budget"}
    assert set(persisted_y.keys()) == {"tijdlijn"}

    fresh_x = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter({}),
        session_id="session-x",
        checkpoint_store=store,
    )
    assert await fresh_x.restore_checkpoint() is True
    state_x = await fresh_x.get_coverage_state()
    assert "budget" in state_x
    assert "tijdlijn" not in state_x

    fresh_y = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter({}),
        session_id="session-y",
        checkpoint_store=store,
    )
    assert await fresh_y.restore_checkpoint() is True
    state_y = await fresh_y.get_coverage_state()
    assert "tijdlijn" in state_y
    assert "budget" not in state_y


# ---------------------------------------------------------------------------
# (d) checkpoint flag disabled falls back to in-memory behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_disabled_falls_back_to_in_memory(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    """Phase 2 rollback path: disabling the checkpoint flag reproduces
    today's in-memory-only behaviour exactly -- no row is written and
    rehydration is a no-op even for the same session id."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")

    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter(BUDGET_MATCH),
        session_id="session-disabled",
        checkpoint_enabled=False,
        checkpoint_store=store,
    )
    await tracker.process_utterance(BUDGET_UTTERANCE, "self", 1000)
    await tracker.checkpoint()

    assert await store.load("session-disabled") is None

    fresh = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter({}),
        session_id="session-disabled",
        checkpoint_enabled=False,
        checkpoint_store=store,
    )
    restored = await fresh.restore_checkpoint()

    assert restored is False
    state = await fresh.get_coverage_state()
    assert state == {}


@pytest.mark.asyncio
async def test_checkpoint_without_session_id_is_a_noop(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    """Callers that do not pass `session_id` (today's only call site) are
    unaffected by Phase 2: checkpointing silently no-ops."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")
    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter(BUDGET_MATCH),
        checkpoint_store=store,
    )

    await tracker.process_utterance(BUDGET_UTTERANCE, "self", 1000)
    await tracker.checkpoint()

    assert await store.load("") is None
    assert await tracker.restore_checkpoint() is False


# ---------------------------------------------------------------------------
# Data-integrity: full round-trip + corrupted-row resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_coverage_missing_with_hint_round_trips(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    """`confirm_coverage` can leave a point `status="missing"` with a hint
    attached (script_tracker.py confirm_coverage, hint-only branch). That
    state must round-trip through a checkpoint exactly, not just the
    discussed/confirmed happy path."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")

    class _HintOnlyLLM(ScriptCoverageLLMClient):
        async def confirm(self, points: Any, transcript_lines: Any, tentative: Any = None) -> Any:
            return _CoverageConfirmResponse(
                covered_point_ids=[], hints={"budget": "vraag naar het beschikbare budget"}
            )

    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        llm_client=_HintOnlyLLM(detector_config),
        session_id="session-hint",
        checkpoint_store=store,
    )
    await tracker.confirm_coverage(["prospect: geen idee wat het gaat kosten"])
    await tracker.checkpoint()

    fresh = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter({}),
        session_id="session-hint",
        checkpoint_store=store,
    )
    assert await fresh.restore_checkpoint() is True
    state = await fresh.get_coverage_state()
    assert state["budget"].status == "missing"
    assert state["budget"].hint == "vraag naar het beschikbare budget"


@pytest.mark.asyncio
async def test_restore_skips_row_with_unknown_point_id(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    """A checkpoint written under a different script config (extra point no
    longer present) must not crash rehydration -- the unknown row is
    skipped, known rows still restore."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")
    await store.save(
        "session-stale-point",
        {
            "budget": {
                "point_id": "budget",
                "title": "Budget",
                "phase": "discovery",
                "required": True,
                "status": "discussed",
                "confidence": 0.92,
                "hint": "",
                "timestamp_ms": 1000,
            },
            "beslisser": {
                "point_id": "beslisser",
                "title": "Beslisser",
                "phase": "discovery",
                "required": True,
                "status": "discussed",
                "confidence": 0.9,
                "hint": "",
                "timestamp_ms": 1200,
            },
        },
    )

    tracker = ScriptTracker(
        detector_config,
        points=sample_points,  # only budget + tijdlijn -- no "beslisser"
        router=_StaticRouter({}),
        session_id="session-stale-point",
        checkpoint_store=store,
    )
    restored = await tracker.restore_checkpoint()

    assert restored is True
    state = await tracker.get_coverage_state()
    assert "budget" in state
    assert "beslisser" not in state


@pytest.mark.asyncio
async def test_restore_skips_row_with_invalid_status(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    """A corrupted checkpoint row (invalid status enum) is skipped rather
    than crashing rehydration or silently trusting bad data."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")
    await store.save(
        "session-corrupt",
        {
            "budget": {
                "point_id": "budget",
                "title": "Budget",
                "phase": "discovery",
                "required": True,
                "status": "not-a-real-status",
                "confidence": 0.92,
                "hint": "",
                "timestamp_ms": 1000,
            },
            "tijdlijn": {
                "point_id": "tijdlijn",
                "title": "Tijdlijn",
                "phase": "discovery",
                "required": True,
                "status": "confirmed",
                "confidence": 1.0,
                "hint": "",
                "timestamp_ms": 1500,
            },
        },
    )

    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        router=_StaticRouter({}),
        session_id="session-corrupt",
        checkpoint_store=store,
    )
    restored = await tracker.restore_checkpoint()

    assert restored is True
    state = await tracker.get_coverage_state()
    assert "budget" not in state
    assert state["tijdlijn"].status == "confirmed"


@pytest.mark.asyncio
async def test_store_load_returns_none_for_unknown_session(tmp_path: Any) -> None:
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")
    assert await store.load("never-checkpointed") is None


@pytest.mark.asyncio
async def test_store_save_upserts_same_session_atomically(tmp_path: Any) -> None:
    """A second checkpoint for the same session id replaces the row (UPSERT),
    it does not accumulate duplicate rows or leave a stale prior snapshot
    readable alongside the new one."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")

    await store.save("session-upsert", {"budget": {"status": "partial"}})
    first = await store.load("session-upsert")
    assert first == {"budget": {"status": "partial"}}

    await store.save("session-upsert", {"budget": {"status": "confirmed"}, "tijdlijn": {"status": "discussed"}})
    second = await store.load("session-upsert")

    assert second == {"budget": {"status": "confirmed"}, "tijdlijn": {"status": "discussed"}}


@pytest.mark.asyncio
async def test_store_reused_across_multiple_sessions_same_db_file(tmp_path: Any) -> None:
    """One store instance backed by one db file safely holds many sessions
    at once -- the same file `SessionTracker` checkpoints session data into,
    no new datastore."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")

    await store.save("s1", {"budget": {"status": "discussed"}})
    await store.save("s2", {"tijdlijn": {"status": "confirmed"}})
    await store.save("s3", {"beslisser": {"status": "partial"}})

    assert await store.load("s1") == {"budget": {"status": "discussed"}}
    assert await store.load("s2") == {"tijdlijn": {"status": "confirmed"}}
    assert await store.load("s3") == {"beslisser": {"status": "partial"}}
