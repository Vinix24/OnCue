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

from sales_copilot.core.autostart_monitor import RuntimeAutostartMonitor
from sales_copilot.websocket import hub, hub_core


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

    monitor.on_consent_required("Google Chrome")
    await asyncio.sleep(0)

    assert broadcasts == [
        ("system", {"type": "autostart_consent_required", "process": "Google Chrome"})
    ]


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
