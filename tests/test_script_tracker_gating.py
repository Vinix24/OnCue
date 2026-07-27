"""Pro-gating tests for script tracking.

Covers the Phase 1 two-layer capability gate (salesprep-pro design doc
section 3, finding #1): `coaching.script_tracking.compute` runs for every
tier (Free included) so coverage is always computed; only
`coaching.script_tracking.live` -- the live WS push -- is Pro/Enterprise.
The legacy, pre-split `coaching.script_tracking` flag is kept as a real
capability (still granted to Pro/Enterprise) and, via
`LEGACY_FEATURE_EXPANSIONS`, expands to grant both new capabilities even to
a grant set that only carries the legacy id.
"""

from __future__ import annotations

import json

import pytest

from sales_copilot.auth.feature_policy import (
    FEATURE_SCRIPT_TRACKING,
    FEATURE_SCRIPT_TRACKING_COMPUTE,
    FEATURE_SCRIPT_TRACKING_LIVE,
    FeaturePolicy,
)
from sales_copilot.auth.license_format import TIER_FEATURES
from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.coaching.script_tracker import (
    ScriptPoint,
    ScriptTracker,
    ScriptTrackerEngine,
)
from sales_copilot.modules.detector.router import RouteMatch


class _TierPolicy(FeaturePolicy):
    """Test-only policy that resolves from TIER_FEATURES directly."""

    def __init__(self, tier: str) -> None:
        self._tier = tier

    def current_tier(self) -> str:  # type: ignore[override]
        return self._tier

    def allows(self, feature_id: str) -> bool:  # type: ignore[override]
        return feature_id in TIER_FEATURES.get(self._tier, frozenset())


def test_script_tracking_denied_in_free_tier() -> None:
    policy = _TierPolicy("free")
    assert policy.allows(FEATURE_SCRIPT_TRACKING) is False


def test_script_tracking_allowed_in_pro_tier() -> None:
    policy = _TierPolicy("pro")
    assert policy.allows(FEATURE_SCRIPT_TRACKING) is True


def test_script_tracking_allowed_in_enterprise_tier() -> None:
    policy = _TierPolicy("enterprise")
    assert policy.allows(FEATURE_SCRIPT_TRACKING) is True


def test_free_features_do_not_include_script_tracking() -> None:
    assert FEATURE_SCRIPT_TRACKING not in TIER_FEATURES["free"]


def test_pro_features_include_script_tracking() -> None:
    assert FEATURE_SCRIPT_TRACKING in TIER_FEATURES["pro"]


# ---------------------------------------------------------------------------
# Phase 1 two-layer gate: `.compute` (Free) vs `.live` (Pro)
# ---------------------------------------------------------------------------


def test_compute_capability_allowed_on_every_tier() -> None:
    """`.compute` is the ungated layer -- Free, Pro, and Enterprise all get it,
    which is what lets `ScriptTracker` run and produce the post-call scorecard
    for Free."""
    for tier in ("free", "pro", "enterprise"):
        assert _TierPolicy(tier).allows(FEATURE_SCRIPT_TRACKING_COMPUTE) is True


def test_live_capability_denied_on_free_allowed_on_pro_and_enterprise() -> None:
    assert _TierPolicy("free").allows(FEATURE_SCRIPT_TRACKING_LIVE) is False
    assert _TierPolicy("pro").allows(FEATURE_SCRIPT_TRACKING_LIVE) is True
    assert _TierPolicy("enterprise").allows(FEATURE_SCRIPT_TRACKING_LIVE) is True


def test_legacy_script_tracking_flag_expands_to_both_new_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant set that only carries the pre-split legacy flag (simulating an
    older license/capability source that has not been migrated to the two
    named capabilities) must still resolve both `.compute` and `.live` as
    granted via the policy-layer alias expansion."""
    monkeypatch.setitem(TIER_FEATURES, "legacy_pro_test", frozenset({FEATURE_SCRIPT_TRACKING}))

    class _LegacyOnlyPolicy(FeaturePolicy):
        def current_tier(self) -> str:
            return "legacy_pro_test"

    policy = _LegacyOnlyPolicy()
    assert policy.allows(FEATURE_SCRIPT_TRACKING_COMPUTE) is True
    assert policy.allows(FEATURE_SCRIPT_TRACKING_LIVE) is True


def test_legacy_expansion_does_not_leak_to_a_tier_without_the_legacy_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tier that has neither the legacy flag nor the new capabilities gets
    neither -- the expansion only fires when the legacy id is actually
    granted."""
    monkeypatch.setitem(TIER_FEATURES, "no_script_tracking_test", frozenset())

    class _EmptyPolicy(FeaturePolicy):
        def current_tier(self) -> str:
            return "no_script_tracking_test"

    policy = _EmptyPolicy()
    assert policy.allows(FEATURE_SCRIPT_TRACKING_COMPUTE) is False
    assert policy.allows(FEATURE_SCRIPT_TRACKING_LIVE) is False


# ---------------------------------------------------------------------------
# ScriptTrackerEngine choke point: compute always runs, `.live` gates the push
# ---------------------------------------------------------------------------


class _StaticRouter:
    """Test-only router that returns a deterministic match without embeddings."""

    def __init__(self, matches: dict[str, RouteMatch]) -> None:
        self._matches = matches

    async def classify_async(self, text: str) -> RouteMatch | None:
        return self._matches.get(text)


def _build_tracker(config: DetectorConfig) -> ScriptTracker:
    points = [
        ScriptPoint(
            id="budget",
            title="Budget",
            phase="discovery",
            required=True,
            example_phrases=["wat is het budget voor dit project"],
        ),
    ]
    router = _StaticRouter(
        {"we hebben een budget van tienduizend euro": RouteMatch(category="budget", confidence=0.92, tier="high")}
    )
    return ScriptTracker(config, points=points, router=router)


class _RecordingWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_free_compute_runs_but_publish_payload_does_not_push(
    monkeypatch: pytest.MonkeyPatch,
    free_feature_policy: FeaturePolicy,
) -> None:
    """Free (only `.compute`): ScriptTracker computes coverage normally, but
    `_publish_payload` -- the single Free/Pro choke point -- must no-op."""
    config = DetectorConfig(llm_provider="none")
    tracker = _build_tracker(config)
    engine = ScriptTrackerEngine(tracker, WebSocketConfig(), feature_policy=free_feature_policy)

    result = await tracker.process_utterance(
        "we hebben een budget van tienduizend euro", "self", 1000
    )
    assert result is not None
    assert result.status == "tentative"
    snapshot = tracker.build_full_snapshot()
    assert any(c.point_id == "budget" and c.status == "tentative" for c in snapshot)

    ws = _RecordingWs()
    monkeypatch.setattr(engine, "_ensure_script_ws", lambda: _async_return(ws))

    await engine._publish_coverage()
    await engine._publish_nudge("discovery")

    assert ws.sent == []


@pytest.mark.asyncio
async def test_pro_live_publish_payload_pushes(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    """Pro (`.live` entitled): the same publish path DOES push to the widget."""
    config = DetectorConfig(llm_provider="none")
    tracker = _build_tracker(config)
    engine = ScriptTrackerEngine(tracker, WebSocketConfig(), feature_policy=pro_feature_policy)

    await tracker.process_utterance("we hebben een budget van tienduizend euro", "self", 1000)

    ws = _RecordingWs()
    monkeypatch.setattr(engine, "_ensure_script_ws", lambda: _async_return(ws))

    await engine._publish_coverage()

    assert len(ws.sent) == 1
    payload = json.loads(ws.sent[0])
    assert payload["type"] == "script_coverage"
    assert any(item["point_id"] == "budget" and item["status"] == "tentative" for item in payload["coverage"])


async def _async_return(value: object) -> object:
    return value
