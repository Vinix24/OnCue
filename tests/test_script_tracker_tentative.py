"""Phase 5 tests: Q2 tentative-vs-confirmed coverage model + Pro live tick-off gate.

Covers (salesprep-pro design doc section 7 finding #5, section 9 Phase 5):

(a) a fast-track (ScriptRouter) match sets a point TENTATIVE (mogelijk
    geraakt), never confirmed -- the fast matcher over-matches (measured
    precision 0.590, below the 0.80 advertise gate), so a fast-track hit must
    not present as a hard "bevestigd";
(b) the slow-track LLM confirm PROMOTES tentative -> confirmed;
(c) the slow-track confirm can DOWNGRADE/REVOKE a tentative match it
    disagrees with (today confirm_coverage was upgrade-only);
(d) confirmed points stay sticky -- never re-confirmed, never revoked, never
    re-matched by the fast track;
(e) `_publish_payload` (the Phase 1 choke point) only pushes when `.live` is
    entitled;
(f) `resolve_response_suggestion` rides the `coaching.response_playbook`
    (Pro) entitlement specifically.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sales_copilot.auth.feature_policy import (
    FEATURE_RESPONSE_PLAYBOOK,
    RESPONSE_LOCKED_TEASER,
    FeaturePolicy,
    resolve_response_suggestion,
)
from sales_copilot.auth.license_format import PRO_FEATURES
from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.coaching.script_tracker import (
    ScriptPoint,
    ScriptTracker,
    ScriptTrackerEngine,
    _CoverageConfirmResponse,
)
from sales_copilot.modules.detector.router import RouteMatch


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
    """A config that keeps tests fast: thresholds and no real LLM."""
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


class _StubConfirmLLM:
    """Test-only slow-track client returning a queued confirmation response."""

    def __init__(self, response: _CoverageConfirmResponse | None) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def confirm(
        self,
        points: list[ScriptPoint],
        transcript_lines: list[str],
        tentative: list[ScriptPoint] | None = None,
    ) -> _CoverageConfirmResponse | None:
        self.calls.append(
            {
                "points": list(points),
                "transcript_lines": list(transcript_lines),
                "tentative": list(tentative or []),
            }
        )
        return self._response


class _GrantPolicy(FeaturePolicy):
    """Test-only policy with an explicit capability grant set."""

    def __init__(self, grants: frozenset[str]) -> None:
        self._grants = grants

    def allows(self, feature_id: str) -> bool:  # type: ignore[override]
        return feature_id in self._grants


class _RecordingWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


async def _async_return(value: object) -> object:
    return value


# ---------------------------------------------------------------------------
# (a) fast-track match sets tentative, not confirmed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_track_match_sets_tentative_not_confirmed(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    router = _StaticRouter(
        {"we hebben een budget van tienduizend euro": RouteMatch(category="budget", confidence=0.95, tier="high")}
    )
    tracker = ScriptTracker(detector_config, points=sample_points, router=router)

    result = await tracker.process_utterance("we hebben een budget van tienduizend euro", "self", 1000)

    assert result is not None
    assert result.status == "tentative"
    assert result.status != "confirmed"
    snapshot = {c.point_id: c for c in tracker.build_full_snapshot()}
    assert snapshot["budget"].status == "tentative"


# ---------------------------------------------------------------------------
# (b) slow-track confirm promotes tentative -> confirmed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_track_confirm_promotes_tentative_to_confirmed(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    router = _StaticRouter(
        {"we hebben een budget van tienduizend euro": RouteMatch(category="budget", confidence=0.92, tier="high")}
    )
    llm = _StubConfirmLLM(_CoverageConfirmResponse(covered_point_ids=["budget"]))
    tracker = ScriptTracker(detector_config, points=sample_points, router=router, llm_client=llm)

    await tracker.process_utterance("we hebben een budget van tienduizend euro", "self", 1000)
    state = await tracker.confirm_coverage(["self: we hebben een budget van tienduizend euro"])

    assert state["budget"].status == "confirmed"
    assert state["budget"].confidence == pytest.approx(1.0)
    # The slow track was TOLD which points were tentative, so it can judge them.
    assert [point.id for point in llm.calls[0]["tentative"]] == ["budget"]


@pytest.mark.asyncio
async def test_slow_track_confirm_still_confirms_points_fast_track_missed(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    llm = _StubConfirmLLM(_CoverageConfirmResponse(covered_point_ids=["tijdlijn"]))
    tracker = ScriptTracker(detector_config, points=sample_points, llm_client=llm)

    state = await tracker.confirm_coverage(["prospect: wanneer moet dit opgeleverd zijn"])

    assert state["tijdlijn"].status == "confirmed"


# ---------------------------------------------------------------------------
# (c) slow-track confirm can downgrade/revoke a tentative match
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slow_track_confirm_revokes_tentative_match_it_disagrees_with(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    """A tentative fast-track false positive (the 0.590-precision failure mode)
    is correctable: the LLM revokes it and the point becomes re-matchable."""
    router = _StaticRouter(
        {
            "we hebben een budget van tienduizend euro": RouteMatch(
                category="budget", confidence=0.92, tier="high"
            ),
            "later echt over budget": RouteMatch(category="budget", confidence=0.97, tier="high"),
        }
    )
    llm = _StubConfirmLLM(_CoverageConfirmResponse(revoked_point_ids=["budget"]))
    tracker = ScriptTracker(detector_config, points=sample_points, router=router, llm_client=llm)

    await tracker.process_utterance("we hebben een budget van tienduizend euro", "self", 1000)
    state = await tracker.confirm_coverage(["self: we hebben een budget van tienduizend euro"])

    # Revoked: no longer tentative, and not silently flipped to confirmed.
    assert "budget" not in state
    snapshot = {c.point_id: c for c in tracker.build_full_snapshot()}
    assert snapshot["budget"].status == "missing"

    # Correctable: the fast track may legitimately cover the point again later.
    rematch = await tracker.process_utterance("later echt over budget", "self", 5000)
    assert rematch is not None
    assert rematch.status == "tentative"


@pytest.mark.asyncio
async def test_slow_track_confirm_revokes_partial_match(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    """`partial` (the lower-tier fast-track state) is tentative too and equally
    correctable by the slow track."""
    router = _StaticRouter(
        {"budget vraag": RouteMatch(category="budget", confidence=0.60, tier="uncertain")}
    )
    llm = _StubConfirmLLM(_CoverageConfirmResponse(revoked_point_ids=["budget"]))
    tracker = ScriptTracker(detector_config, points=sample_points, router=router, llm_client=llm)

    await tracker.process_utterance("budget vraag", "self", 1000)
    state = await tracker.confirm_coverage(["self: budget vraag"])

    assert "budget" not in state


@pytest.mark.asyncio
async def test_slow_track_revoke_of_unknown_point_is_a_noop(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    llm = _StubConfirmLLM(_CoverageConfirmResponse(revoked_point_ids=["budget", "ghost"]))
    tracker = ScriptTracker(detector_config, points=sample_points, llm_client=llm)

    state = await tracker.confirm_coverage(["prospect: niets over budget gezegd"])

    assert "budget" not in state
    assert "ghost" not in state


# ---------------------------------------------------------------------------
# (d) confirmed points stay sticky
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmed_point_is_sticky_against_revoke_and_reconfirm(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    """Once the slow track confirms a point it stays confirmed: a later cycle
    that omits it, revokes it, or re-lists it must not change the state, and
    the fast track must not treat a re-match as new coverage."""
    router = _StaticRouter(
        {"we hebben een budget van tienduizend euro": RouteMatch(category="budget", confidence=0.92, tier="high")}
    )
    tracker = ScriptTracker(detector_config, points=sample_points, router=router)

    # Cycle 1: the LLM confirms the point outright.
    tracker.llm_client = _StubConfirmLLM(_CoverageConfirmResponse(covered_point_ids=["budget"]))
    state = await tracker.confirm_coverage(["self: we hebben een budget van tienduizend euro"])
    assert state["budget"].status == "confirmed"
    confirmed_ts = state["budget"].timestamp_ms

    # Cycle 2: the LLM now disagrees and tries to revoke it -- too late.
    tracker.llm_client = _StubConfirmLLM(_CoverageConfirmResponse(revoked_point_ids=["budget"]))
    state = await tracker.confirm_coverage(["prospect: dat was eigenlijk iets anders"])
    assert state["budget"].status == "confirmed"
    assert state["budget"].timestamp_ms == confirmed_ts

    # Fast-track re-match of a confirmed point is not new coverage.
    rematch = await tracker.process_utterance("we hebben een budget van tienduizend euro", "self", 9000)
    assert rematch is None
    state = await tracker.get_coverage_state()
    assert state["budget"].status == "confirmed"


# ---------------------------------------------------------------------------
# (e) _publish_payload only pushes when `.live` is entitled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_payload_sends_tentative_state_only_when_live_entitled(
    monkeypatch: pytest.MonkeyPatch,
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    free_feature_policy: FeaturePolicy,
    pro_feature_policy: FeaturePolicy,
) -> None:
    """The Phase 1 choke point gates the live push on `.live`; what Pro sees is
    the tentative state (not a hard confirmed) from the fast track."""
    from sales_copilot.auth.license_format import FEATURE_SCRIPT_TRACKING_LIVE

    router = _StaticRouter(
        {"we hebben een budget van tienduizend euro": RouteMatch(category="budget", confidence=0.92, tier="high")}
    )

    # Free: compute runs, but the push no-ops.
    free_tracker = ScriptTracker(detector_config, points=sample_points, router=router)
    free_engine = ScriptTrackerEngine(free_tracker, WebSocketConfig(), feature_policy=free_feature_policy)
    await free_tracker.process_utterance("we hebben een budget van tienduizend euro", "self", 1000)
    free_ws = _RecordingWs()
    monkeypatch.setattr(free_engine, "_ensure_script_ws", lambda: _async_return(free_ws))
    await free_engine._publish_coverage()
    assert free_ws.sent == []

    # Pro: the push goes out, carrying the tentative (not confirmed) state.
    pro_tracker = ScriptTracker(detector_config, points=sample_points, router=router)
    pro_engine = ScriptTrackerEngine(pro_tracker, WebSocketConfig(), feature_policy=pro_feature_policy)
    assert pro_feature_policy.allows(FEATURE_SCRIPT_TRACKING_LIVE) is True
    await pro_tracker.process_utterance("we hebben een budget van tienduizend euro", "self", 1000)
    pro_ws = _RecordingWs()
    monkeypatch.setattr(pro_engine, "_ensure_script_ws", lambda: _async_return(pro_ws))
    await pro_engine._publish_coverage()
    assert len(pro_ws.sent) == 1
    payload = json.loads(pro_ws.sent[0])
    assert payload["type"] == "script_coverage"
    row = next(item for item in payload["coverage"] if item["point_id"] == "budget")
    assert row["status"] == "tentative"


# ---------------------------------------------------------------------------
# (f) resolve_response_suggestion rides the response_playbook entitlement
# ---------------------------------------------------------------------------


def test_resolve_response_suggestion_granted_by_response_playbook_alone() -> None:
    """A session whose ONLY capability is `coaching.response_playbook` still
    gets the curated text -- the gate rides that entitlement, not Pro-ness in
    general."""
    curated = "Bel de klant terug binnen 24 uur."
    policy = _GrantPolicy(frozenset({FEATURE_RESPONSE_PLAYBOOK}))

    assert resolve_response_suggestion(curated, feature_policy=policy) == curated


def test_resolve_response_suggestion_teaser_when_only_playbook_missing() -> None:
    """A session with every Pro capability EXCEPT `coaching.response_playbook`
    gets the teaser -- it is the playbook capability specifically that unlocks
    the curated remedy."""
    curated = "Bel de klant terug binnen 24 uur."
    policy = _GrantPolicy(frozenset(PRO_FEATURES - {FEATURE_RESPONSE_PLAYBOOK}))

    assert resolve_response_suggestion(curated, feature_policy=policy) == RESPONSE_LOCKED_TEASER


# ---------------------------------------------------------------------------
# Legacy checkpoint compatibility: pre-Phase-5 "discussed" rows rehydrate as
# tentative
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_discussed_checkpoint_restores_as_tentative(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
    tmp_path: Any,
) -> None:
    from sales_copilot.modules.reports.session import ScriptCoverageCheckpointStore

    store = ScriptCoverageCheckpointStore(tmp_path / "cases.db")
    await store.save(
        "session-legacy",
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
        },
    )

    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        session_id="session-legacy",
        checkpoint_store=store,
    )
    assert await tracker.restore_checkpoint() is True

    state = await tracker.get_coverage_state()
    assert state["budget"].status == "tentative"
