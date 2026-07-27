"""Tests for Phase 4 reliability fixes on `ScriptTrackerEngine`.

Salesprep design doc (`claudedocs/2026-07-22-salesprep-pro-design.md`),
section 7 findings #3 and #4 / section 9 Phase 4:

- Finding #3: `_run_confirmation_cycle` used to clear the pending transcript
  buffer *before* calling `confirm_coverage`, then swallow any exception with
  only `logger.warning`. A failed confirm silently dropped those lines
  forever. Phase 4 keeps the buffer intact until confirm succeeds, re-queues
  it (bounded at `MAX_TRANSCRIPT_LINES`) on failure, and marks the engine
  degraded instead of swallowing silently -- while leaving the Phase 2
  `tracker.checkpoint()` call in place on both paths (coverage state itself
  is unchanged by a failed confirm, so re-checkpointing it is a safe no-op
  re-save, not a corruption).

- Finding #4: `_publish_payload` used to catch every WS send failure with
  only `logger.warning`, leaving a Pro user's live widget no way to tell a
  frozen tick-off from a current one. Phase 4 adds a periodic
  `script_tracking_health` heartbeat and stamps a `degraded` flag on every
  outgoing script-tracking message, driven by the last publish outcome and
  the last confirmation-cycle outcome.

These tests target the engine's internal state and the `_ensure_script_ws`
seam directly, following the same monkeypatch conventions already used in
`tests/test_script_tracker_gating.py` and `tests/test_script_tracker_checkpoint.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.coaching.script_tracker import (
    ScriptPoint,
    ScriptTracker,
    ScriptTrackerEngine,
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


class _RecordingWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


class _FailingWs:
    async def send(self, data: str) -> None:
        raise ConnectionError("ws down")


async def _async_return(value: object) -> object:
    return value


# ---------------------------------------------------------------------------
# Finding #3 (a): confirm failure does not lose transcript lines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_failure_requeues_lines_without_loss(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = ScriptTracker(detector_config, points=sample_points)
    engine = ScriptTrackerEngine(tracker, WebSocketConfig())

    checkpoint_calls = 0

    async def _fail_confirm(transcript_lines: list[str]) -> dict[str, Any]:
        raise RuntimeError("LLM provider unavailable")

    async def _spy_checkpoint() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1

    monkeypatch.setattr(tracker, "confirm_coverage", _fail_confirm)
    monkeypatch.setattr(tracker, "checkpoint", _spy_checkpoint)

    engine._transcript_lines = ["prospect: wat kost dit ongeveer"]

    await engine._run_confirmation_cycle()

    # The line is NOT lost -- it is re-queued for the next cycle to retry.
    assert engine._transcript_lines == ["prospect: wat kost dit ongeveer"]
    # Phase 2's checkpoint still runs on failure: coverage state itself is
    # unchanged by a failed confirm, so re-saving it here is not a corruption.
    assert checkpoint_calls == 1
    # The failure is surfaced as engine health, not swallowed silently.
    assert engine._confirm_degraded is True


@pytest.mark.asyncio
async def test_requeued_lines_are_retried_and_merged_with_new_lines(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = ScriptTracker(detector_config, points=sample_points)
    engine = ScriptTrackerEngine(tracker, WebSocketConfig())

    calls: list[list[str]] = []
    attempt = {"n": 0}

    async def _confirm(transcript_lines: list[str]) -> dict[str, Any]:
        calls.append(list(transcript_lines))
        attempt["n"] += 1
        if attempt["n"] == 1:
            raise RuntimeError("LLM timeout")
        return {}

    monkeypatch.setattr(tracker, "confirm_coverage", _confirm)
    monkeypatch.setattr(tracker, "checkpoint", lambda: _async_return(None))

    engine._transcript_lines = ["self: hoi", "prospect: wat kost dit"]
    await engine._run_confirmation_cycle()  # fails -> both lines re-queued

    assert engine._transcript_lines == ["self: hoi", "prospect: wat kost dit"]
    assert engine._confirm_degraded is True

    # A new line arrives on the live channel before the retry fires.
    engine._transcript_lines.append("self: en de tijdlijn?")

    await engine._run_confirmation_cycle()  # succeeds this time

    # Retry order stays chronological: the failed lines come before the new one.
    assert calls[1] == ["self: hoi", "prospect: wat kost dit", "self: en de tijdlijn?"]
    assert engine._transcript_lines == []
    assert engine._confirm_degraded is False


# ---------------------------------------------------------------------------
# Finding #3 (b): re-queue is bounded at MAX_TRANSCRIPT_LINES
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requeue_is_bounded_at_max_transcript_lines(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A persistently failing LLM cannot grow the re-queue unbounded --
    beyond `MAX_TRANSCRIPT_LINES` the oldest lines are dropped, newest kept,
    and the drop is logged."""
    tracker = ScriptTracker(detector_config, points=sample_points)
    engine = ScriptTrackerEngine(tracker, WebSocketConfig())

    async def _fail_confirm(transcript_lines: list[str]) -> dict[str, Any]:
        raise RuntimeError("LLM provider unavailable")

    monkeypatch.setattr(tracker, "confirm_coverage", _fail_confirm)
    monkeypatch.setattr(tracker, "checkpoint", lambda: _async_return(None))

    max_lines = ScriptTrackerEngine.MAX_TRANSCRIPT_LINES
    failed_lines = [f"self: regel {i}" for i in range(max_lines)]
    engine._transcript_lines = list(failed_lines)

    await engine._run_confirmation_cycle()  # pops max_lines, fails, re-queues -> still max_lines

    assert engine._transcript_lines == failed_lines
    assert len(engine._transcript_lines) == max_lines

    # More lines arrive on the live channel while the LLM keeps failing.
    overflow_lines = [f"prospect: extra {i}" for i in range(5)]
    engine._transcript_lines.extend(overflow_lines)
    assert len(engine._transcript_lines) == max_lines + 5

    with caplog.at_level(logging.WARNING):
        await engine._run_confirmation_cycle()  # pops everything, fails again, must re-cap

    assert len(engine._transcript_lines) == max_lines
    # Newest lines survive; oldest unretried lines are dropped first.
    assert engine._transcript_lines[-5:] == overflow_lines
    assert any(
        "MAX_TRANSCRIPT_LINES" in record.message and "dropped" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_checkpoint_persists_pre_failure_state_when_confirm_fails(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """'checkpoint the current coverage even when confirm fails -- coverage
    state is unchanged, not corrupted' (Phase 4 spec), proven against a real
    SQLite-backed checkpoint store rather than a spy."""
    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")
    router = _StaticRouter(
        {"we hebben een budget van tienduizend euro": RouteMatch(category="budget", confidence=0.92, tier="high")}
    )
    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        router=router,
        session_id="session-confirm-fail",
        checkpoint_store=store,
    )
    await tracker.process_utterance("we hebben een budget van tienduizend euro", "self", 1000)

    engine = ScriptTrackerEngine(tracker, WebSocketConfig())

    async def _fail_confirm(transcript_lines: list[str]) -> dict[str, Any]:
        raise RuntimeError("LLM provider unavailable")

    monkeypatch.setattr(tracker, "confirm_coverage", _fail_confirm)
    engine._transcript_lines = ["prospect: wanneer moet dit opgeleverd zijn"]

    await engine._run_confirmation_cycle()

    persisted = await store.load("session-confirm-fail")
    assert persisted is not None
    assert persisted["budget"]["status"] == "tentative"
    assert "tijdlijn" not in persisted
    # The pending line is still queued for retry, not lost.
    assert engine._transcript_lines == ["prospect: wanneer moet dit opgeleverd zijn"]


# ---------------------------------------------------------------------------
# Finding #4 (c): a failed publish sets `degraded`, heartbeat reflects it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_publish_sets_degraded_and_heartbeat_reflects_it(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    tracker = ScriptTracker(detector_config, points=sample_points)
    engine = ScriptTrackerEngine(tracker, WebSocketConfig(), feature_policy=pro_feature_policy)

    monkeypatch.setattr(engine, "_ensure_script_ws", lambda: _async_return(_FailingWs()))
    await engine._publish_coverage()

    assert engine._publish_degraded is True

    ws = _RecordingWs()
    monkeypatch.setattr(engine, "_ensure_script_ws", lambda: _async_return(ws))
    await engine._publish_health()

    assert len(ws.sent) == 1
    heartbeat = json.loads(ws.sent[0])
    assert heartbeat["type"] == "script_tracking_health"
    # Reflects the state as of BEFORE this send -- the prior publish failure.
    assert heartbeat["degraded"] is True

    # This send succeeded, so the publish-failure state clears for the next message.
    assert engine._publish_degraded is False
    await engine._publish_health()
    healthy = json.loads(ws.sent[1])
    assert healthy["degraded"] is False


@pytest.mark.asyncio
async def test_confirmation_cycle_failure_reflected_in_heartbeat(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    """The heartbeat also reflects a degraded confirmation cycle (finding
    #3), not only a degraded WS publish -- both feed the same `degraded` flag."""
    tracker = ScriptTracker(detector_config, points=sample_points)
    engine = ScriptTrackerEngine(tracker, WebSocketConfig(), feature_policy=pro_feature_policy)

    async def _fail_confirm(transcript_lines: list[str]) -> dict[str, Any]:
        raise RuntimeError("LLM provider unavailable")

    monkeypatch.setattr(tracker, "confirm_coverage", _fail_confirm)
    monkeypatch.setattr(tracker, "checkpoint", lambda: _async_return(None))
    engine._transcript_lines = ["prospect: wat kost dit"]

    await engine._run_confirmation_cycle()

    ws = _RecordingWs()
    monkeypatch.setattr(engine, "_ensure_script_ws", lambda: _async_return(ws))
    await engine._publish_health()

    heartbeat = json.loads(ws.sent[0])
    assert heartbeat["degraded"] is True

    async def _succeed_confirm(transcript_lines: list[str]) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(tracker, "confirm_coverage", _succeed_confirm)
    engine._transcript_lines = ["prospect: en de tijdlijn?"]
    await engine._run_confirmation_cycle()

    await engine._publish_health()
    healthy = json.loads(ws.sent[1])
    assert healthy["degraded"] is False


# ---------------------------------------------------------------------------
# Finding #4 (d): the heartbeat respects the `.live` gate (Phase 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_respects_live_gate(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    monkeypatch: pytest.MonkeyPatch,
    free_feature_policy: FeaturePolicy,
) -> None:
    """Free (`.compute` only, no `.live`): the heartbeat must no-op through
    the same single choke point (`_publish_payload`) as every other message
    on this channel -- it does not get its own tier check."""
    tracker = ScriptTracker(detector_config, points=sample_points)
    engine = ScriptTrackerEngine(tracker, WebSocketConfig(), feature_policy=free_feature_policy)

    ws = _RecordingWs()
    monkeypatch.setattr(engine, "_ensure_script_ws", lambda: _async_return(ws))

    await engine._publish_health()

    assert ws.sent == []


@pytest.mark.asyncio
async def test_heartbeat_loop_runs_as_a_task_in_engine_run(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run()` starts the heartbeat loop alongside confirmation and channel
    consumption, so a call without any transcript/phase activity still gets
    a periodic liveness signal."""
    tracker = ScriptTracker(detector_config, points=sample_points)
    engine = ScriptTrackerEngine(tracker, WebSocketConfig())

    call_order: list[str] = []

    async def _fake_restore() -> bool:
        return False

    async def _fake_confirmation_loop(stop_event: asyncio.Event) -> None:
        call_order.append("confirm")

    async def _fake_heartbeat_loop(stop_event: asyncio.Event) -> None:
        call_order.append("heartbeat")

    async def _fake_consume_channel(channel: str, handler: Any, stop_event: asyncio.Event) -> None:
        call_order.append(f"consume:{channel}")

    monkeypatch.setattr(tracker, "restore_checkpoint", _fake_restore)
    monkeypatch.setattr(engine, "_confirmation_loop", _fake_confirmation_loop)
    monkeypatch.setattr(engine, "_heartbeat_loop", _fake_heartbeat_loop)
    monkeypatch.setattr(engine, "_consume_channel", _fake_consume_channel)

    await engine.run(asyncio.Event())

    assert "heartbeat" in call_order
    assert "confirm" in call_order
    assert "consume:transcript" in call_order
    assert "consume:phase" in call_order
