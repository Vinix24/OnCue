"""Tests for the hub-side auto-arm wiring (``sales_copilot.websocket.hub``).

Exercises the ``on_consent_required``/``on_session_arm`` closures built by
``hub._make_autostart_monitor()`` directly -- these are the glue between the
pure ``RuntimeAutostartMonitor`` state machine (see test_autostart_monitor.py)
and the real WebSocket hub broadcast/start-call machinery. Fully hermetic: no
real process detection, no real filesystem/consent-arm check.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sales_copilot.core import route_runtime
from sales_copilot.core.autostart_monitor import AutostartMonitorConfig, RuntimeAutostartMonitor, set_active_monitor
from sales_copilot.websocket import hub, hub_core
from sales_copilot.wizard.steps import StepResult


@pytest.fixture(autouse=True)
def _reset_hub_state():
    hub_core.reset_config_state()
    yield
    hub_core.reset_config_state()


def test_build_autostart_call_config_uses_default_preset_and_fresh_consent() -> None:
    config = hub._build_autostart_call_config()

    assert config["preset_name"] == "sales"
    assert config["consent"] == {"asked": True, "given": True}


def test_make_autostart_monitor_returns_configured_monitor() -> None:
    monitor = hub._make_autostart_monitor()

    assert isinstance(monitor, RuntimeAutostartMonitor)
    assert monitor.on_consent_required is not None
    assert monitor.on_session_arm is not None


async def test_on_consent_required_broadcasts_system_event(monkeypatch: pytest.MonkeyPatch) -> None:
    broadcasts: list[tuple[str, Any]] = []

    async def _fake_broadcast(channel: str, payload: Any) -> None:
        broadcasts.append((channel, payload))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)
    monitor = hub._make_autostart_monitor()

    monitor.on_consent_required("p1", "Google Chrome", "video")
    await asyncio.sleep(0)

    assert broadcasts == [
        (
            "system",
            {"type": "autostart_consent_required", "prompt_id": "p1", "process": "Google Chrome", "source": "video"},
        )
    ]


async def test_on_consent_resolved_broadcasts_system_event(monkeypatch: pytest.MonkeyPatch) -> None:
    broadcasts: list[tuple[str, Any]] = []

    async def _fake_broadcast(channel: str, payload: Any) -> None:
        broadcasts.append((channel, payload))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)
    monitor = hub._make_autostart_monitor()

    monitor.on_consent_resolved("p1", "expired")
    await asyncio.sleep(0)

    assert broadcasts == [("system", {"type": "consent_resolved", "prompt_id": "p1", "outcome": "expired"})]


async def test_yes_while_a_call_is_active_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    broadcasts: list[tuple[str, Any]] = []

    async def _fake_broadcast(channel: str, payload: Any) -> None:
        broadcasts.append((channel, payload))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)
    hub_core.apply_start_call({"preset_name": "manual"})
    monitor = hub._make_autostart_monitor()

    monitor.on_session_arm("Google Chrome")
    await asyncio.sleep(0)

    assert hub_core.get_latest_config() == {"preset_name": "manual"}
    assert broadcasts == []


async def test_on_session_arm_applies_start_call_and_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    broadcasts: list[tuple[str, Any]] = []

    async def _fake_broadcast(channel: str, payload: Any) -> None:
        broadcasts.append((channel, payload))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)
    monitor = hub._make_autostart_monitor()

    monitor.on_session_arm("Google Chrome")
    await asyncio.sleep(0)

    config = hub_core.get_latest_config()
    assert config is not None
    assert config["preset_name"] == "sales"
    assert config["consent"] == {"asked": True, "given": True}
    assert hub_core._call_active is True
    assert broadcasts == [("config", {"type": "start_call", "config": config})]


async def test_on_session_arm_blocked_by_missing_entitlements(monkeypatch: pytest.MonkeyPatch) -> None:
    broadcasts: list[tuple[str, Any]] = []

    async def _fake_broadcast(channel: str, payload: Any) -> None:
        broadcasts.append((channel, payload))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)
    monkeypatch.setattr(hub_core, "missing_entitlements", lambda _config: ["some.feature"])
    monitor = hub._make_autostart_monitor()

    monitor.on_session_arm("Google Chrome")
    await asyncio.sleep(0)

    assert hub_core.get_latest_config() is None
    assert hub_core._call_active is False
    assert broadcasts == []


class _AllowAll:
    def allows(self, _feature_id: str) -> bool:
        return True


def _snapshot_on_connect(client: TestClient) -> dict[str, Any]:
    with client.websocket_connect("/ws/system", headers={"origin": "http://localhost:8760"}) as ws:
        return ws.receive_json()


def test_system_channel_sends_the_open_prompt_on_connect_and_null_after_expiry(authed_client: TestClient) -> None:
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=1),
        feature_policy=_AllowAll(),
        detect_fn=lambda _c: ("Google Chrome", 1, ["Google Chrome"]),
        telephony_detect_fn=lambda _c: (None, None, []),
        consent_arm_check=lambda: StepResult(ok=True, message="ok"),
    )
    monitor.tick()
    prompt_id = monitor._prompt_id
    set_active_monitor(monitor)
    try:
        first = _snapshot_on_connect(authed_client)
        second = _snapshot_on_connect(authed_client)
        monitor.withdraw_pending()
        after_expiry = _snapshot_on_connect(authed_client)
    finally:
        set_active_monitor(None)

    expected = {"prompt_id": prompt_id, "process": "Google Chrome", "source": "video"}
    assert first == second == {"type": "consent_snapshot", "pending": expected}
    assert after_expiry == {"type": "consent_snapshot", "pending": None}


def test_system_channel_snapshot_is_null_without_a_monitor(authed_client: TestClient) -> None:
    set_active_monitor(None)

    assert _snapshot_on_connect(authed_client) == {"type": "consent_snapshot", "pending": None}


async def test_both_broadcasts_and_the_snapshot_carry_the_same_prompt_id(
    monkeypatch: pytest.MonkeyPatch, authed_client: TestClient
) -> None:
    broadcasts: list[tuple[str, Any]] = []

    async def _fake_broadcast(channel: str, payload: Any) -> None:
        broadcasts.append((channel, payload))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)
    monitor = hub._make_autostart_monitor()
    monitor.config = AutostartMonitorConfig(debounce_arm_polls=1)
    monitor.feature_policy = _AllowAll()  # type: ignore[assignment]
    monitor.detect_fn = lambda _c: ("Google Chrome", 1, ["Google Chrome"])
    monitor.telephony_detect_fn = lambda _c: (None, None, [])
    monitor.consent_arm_check = lambda: StepResult(ok=True, message="ok")
    monitor.tick()
    prompt_id = monitor._prompt_id
    set_active_monitor(monitor)
    try:
        snapshot = _snapshot_on_connect(authed_client)
    finally:
        set_active_monitor(None)
    monitor.decline_consent(prompt_id or "")
    await asyncio.sleep(0)

    assert prompt_id
    assert snapshot["pending"]["prompt_id"] == prompt_id
    assert [p["prompt_id"] for _c, p in broadcasts] == [prompt_id, prompt_id]
    assert [p["type"] for _c, p in broadcasts] == ["autostart_consent_required", "consent_resolved"]


def test_make_autostart_monitor_wires_the_check_to_the_copilot_route(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def _fake(route: str) -> bool:
        asked.append(route)
        return True

    monkeypatch.setattr(route_runtime, "other_route_running", _fake)
    monitor = hub._make_autostart_monitor()

    assert monitor.other_route_running() is True
    assert asked == [route_runtime.COPILOT]


async def test_on_session_arm_starts_nothing_when_the_other_route_starts_after_the_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broadcasts: list[tuple[str, Any]] = []

    async def _fake_broadcast(channel: str, payload: Any) -> None:
        broadcasts.append((channel, payload))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)
    monkeypatch.setattr(route_runtime, "other_route_running", lambda _route: False)
    monitor = hub._make_autostart_monitor()
    monitor.on_session_arm("Google Chrome")
    # the other route starts between the Yes and the start
    monkeypatch.setattr(route_runtime, "other_route_running", lambda _route: True)
    await asyncio.sleep(0)

    assert hub_core.get_latest_config() is None
    assert hub_core._call_active is False
    assert broadcasts == []
