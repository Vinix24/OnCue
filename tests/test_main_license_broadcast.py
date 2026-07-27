"""Startup license-status broadcast (#7) actually fires now.

The old ``_run_license_check`` ran synchronously in ``main()`` before
``asyncio.run(_orchestrate())`` and guarded every emit with
``loop.is_running()`` — always False there — so ``license_grace`` /
``license_expired`` were never broadcast. ``_broadcast_license_status`` now runs
inside the live orchestrator loop and reaches ``/ws/system`` subscribers.
"""

from __future__ import annotations

import asyncio
import json

import websockets

from sales_copilot import __main__ as main_mod
from sales_copilot.auth.startup_check import LicenseStatus
from sales_copilot.core.config import WebSocketConfig
from tests.ws_helpers import ws_url

# URL-safe (no chars that get percent-encoded in WS Basic-auth userinfo).
_TEST_TOKEN = "test-shutdown-token-32-chars-safe"


async def test_broadcast_emits_grace_event(monkeypatch) -> None:
    """A GRACE status emits a license_grace event with parsed days_remaining."""
    monkeypatch.setattr(
        main_mod,
        "check_license_at_startup",
        lambda: (
            LicenseStatus.GRACE,
            "No license key found. Running in grace period: 9 day(s) remaining.",
        ),
    )
    captured: list[dict] = []
    audit_records: list[dict] = []

    async def _capture(_ws_config, payload):
        captured.append(payload)

    monkeypatch.setattr(main_mod, "_emit_system_event", _capture)
    monkeypatch.setattr(
        main_mod,
        "_write_audit_event",
        lambda event_type, payload: audit_records.append({"event_type": event_type, **payload}),
    )

    await main_mod._broadcast_license_status(WebSocketConfig())

    assert len(captured) == 1
    assert captured[0]["event"] == "license_grace"
    assert captured[0]["days_remaining"] == 9
    assert len(audit_records) == 1
    assert audit_records[0]["event_type"] == "license_verify"
    assert audit_records[0]["license_status"] == "grace"


async def test_broadcast_emits_expired_for_missing(monkeypatch) -> None:
    """MISSING (and EXPIRED) map to a single license_expired dashboard event."""
    monkeypatch.setattr(
        main_mod,
        "check_license_at_startup",
        lambda: (LicenseStatus.MISSING, "License key is invalid (bad signature or format)."),
    )
    captured: list[dict] = []

    async def _capture(_ws_config, payload):
        captured.append(payload)

    monkeypatch.setattr(main_mod, "_emit_system_event", _capture)

    await main_mod._broadcast_license_status(WebSocketConfig())

    assert len(captured) == 1
    assert captured[0]["event"] == "license_expired"


async def test_broadcast_stays_silent_when_valid(monkeypatch) -> None:
    """A VALID license logs but emits nothing to the dashboard."""
    monkeypatch.setattr(
        main_mod,
        "check_license_at_startup",
        lambda: (LicenseStatus.VALID, "License valid — tier=pro, expires=2099-01-01."),
    )
    captured: list[dict] = []

    async def _capture(_ws_config, payload):
        captured.append(payload)

    monkeypatch.setattr(main_mod, "_emit_system_event", _capture)

    await main_mod._broadcast_license_status(WebSocketConfig())

    assert captured == []


async def test_broadcast_reaches_system_subscriber(running_hub, monkeypatch) -> None:
    """End-to-end: the event is delivered to a live /ws/system subscriber.

    This is the exact path the old guarded code never executed — proof that
    moving the check onto the running loop makes the broadcast fire.
    """
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TEST_TOKEN)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "not-a-real-license-key")  # -> MISSING
    port = running_hub
    ws_config = WebSocketConfig(host="127.0.0.1", port=port)

    async with websockets.connect(ws_url("127.0.0.1", port, "system")) as subscriber:
        await asyncio.sleep(0.1)  # let the hub register the subscriber
        await main_mod._broadcast_license_status(ws_config)
        raw = await asyncio.wait_for(subscriber.recv(), timeout=2.0)

    event = json.loads(raw)
    assert event["event"] == "license_expired"
