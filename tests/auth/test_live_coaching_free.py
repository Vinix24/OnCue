"""Live coaching (coaching.live) is part of the free tier (decision 2026-11-18)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.auth.license_format import (
    ENTERPRISE_FEATURES,
    FEATURE_AUTOSTART,
    FEATURE_CALLTAP,
    FEATURE_CENTRAL_AUDIT,
    FEATURE_DEEP_INSIGHTS,
    FEATURE_DYNAMIC_SLIDES,
    FEATURE_LIVE_COACHING,
    FEATURE_MCP_BRIDGE,
    FEATURE_REPORT_DELIVERY_ENDPOINT,
    FEATURE_RESPONSE_PLAYBOOK,
    FEATURE_SCRIPT_TRACKING,
    FEATURE_SCRIPT_TRACKING_LIVE,
    FREE_FEATURES,
    PRO_FEATURES,
)
from sales_copilot.websocket import hub_core

STILL_PRO = [
    FEATURE_CALLTAP,
    FEATURE_CENTRAL_AUDIT,
    FEATURE_DYNAMIC_SLIDES,
    FEATURE_AUTOSTART,
    FEATURE_SCRIPT_TRACKING,
    FEATURE_SCRIPT_TRACKING_LIVE,
    FEATURE_RESPONSE_PLAYBOOK,
    FEATURE_DEEP_INSIGHTS,
    FEATURE_MCP_BRIDGE,
    FEATURE_REPORT_DELIVERY_ENDPOINT,
]


@pytest.fixture
def free_policy(monkeypatch: pytest.MonkeyPatch, tmp_path) -> FeaturePolicy:
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "free")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
    monkeypatch.setattr(hub_core, "_feature_policy", None, raising=False)
    return FeaturePolicy()


def test_tier_sets_include_live_coaching() -> None:
    assert FEATURE_LIVE_COACHING in FREE_FEATURES
    assert FEATURE_LIVE_COACHING in PRO_FEATURES
    assert FEATURE_LIVE_COACHING in ENTERPRISE_FEATURES


def test_free_allows_live_coaching(free_policy: FeaturePolicy) -> None:
    assert free_policy.current_tier() == "free"
    assert free_policy.allows(FEATURE_LIVE_COACHING) is True


@pytest.mark.parametrize("feature", STILL_PRO)
def test_free_still_refuses_pro_feature(free_policy: FeaturePolicy, feature: str) -> None:
    assert free_policy.allows(feature) is False
    assert feature not in FREE_FEATURES


def test_unknown_feature_is_refused_on_free(free_policy: FeaturePolicy) -> None:
    assert free_policy.allows("coaching.not_a_feature") is False


def test_grace_expired_keeps_live_coaching(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the grace period the tool degrades to free; live coaching stays on."""
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "free")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
    policy = FeaturePolicy()
    assert policy.current_tier() == "free"
    assert policy.allows(FEATURE_LIVE_COACHING) is True
    assert policy.allows(FEATURE_CALLTAP) is False


def test_coaching_broadcast_reaches_subscriber_without_license(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "free")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
    policy = FeaturePolicy()
    assert policy.current_tier() == "free"
    monkeypatch.setattr(hub_core, "_get_feature_policy", lambda: policy)

    ws = AsyncMock()
    hub_core._subscribers["coaching"] = {ws}
    try:
        asyncio.run(hub_core._broadcast_to_channel("coaching", {"type": "coaching", "text": "x"}))
    finally:
        hub_core._subscribers.pop("coaching", None)
    ws.send_json.assert_awaited_once()


def test_other_gated_channel_still_suppressed_on_free(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "free")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
    policy = FeaturePolicy()
    monkeypatch.setattr(hub_core, "_get_feature_policy", lambda: policy)

    ws = AsyncMock()
    hub_core._subscribers["script-tracking"] = {ws}
    try:
        asyncio.run(hub_core._broadcast_to_channel("script-tracking", {"type": "x"}))
    finally:
        hub_core._subscribers.pop("script-tracking", None)
    ws.send_json.assert_not_awaited()
