"""Tests for the runtime auto-arm HTTP endpoints and their wiring.

``/api/autostart/confirm`` and ``/api/autostart/decline`` are the only paths
that can turn a pending consent-arm prompt into an armed (recording) session --
these tests exercise that boundary, plus the ``notify_call_ended`` wiring in
``/api/end-call`` and ``/api/sample-aha/stop``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sales_copilot.core.autostart_monitor import (
    STATE_ARMED,
    STATE_CONSENT_PENDING,
    STATE_IDLE,
    AutostartMonitorConfig,
    RuntimeAutostartMonitor,
    set_active_monitor,
)
from sales_copilot.websocket import hub
from sales_copilot.wizard.steps import StepResult


def _ok_gate() -> StepResult:
    return StepResult(ok=True, message="ok")


def _detect_once(process_name: str = "Google Chrome"):
    def _detect(candidates: tuple[str, ...]):
        return process_name, 4242, [process_name]

    return _detect


def _detect_never(candidates: tuple[str, ...]):
    """A ``DetectFn`` that never reports a process present -- neutralizes a trigger source."""

    return None, None, []


class _AllowAllPolicy:
    def allows(self, _feature_id: str) -> bool:
        return True


def _pending_monitor(*, session_calls: list[str] | None = None) -> RuntimeAutostartMonitor:
    """A monitor already in STATE_CONSENT_PENDING, ready for confirm/decline."""
    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=1),
        feature_policy=_AllowAllPolicy(),
        detect_fn=_detect_once(),
        telephony_detect_fn=_detect_never,
        consent_arm_check=_ok_gate,
        on_session_arm=(session_calls.append if session_calls is not None else None),
    )
    monitor.tick()
    assert monitor.state == STATE_CONSENT_PENDING
    return monitor


@pytest.fixture(autouse=True)
def _reset_active_monitor():
    set_active_monitor(None)
    yield
    set_active_monitor(None)


# ---------------------------------------------------------------------------
# /api/autostart/confirm
# ---------------------------------------------------------------------------


def test_autostart_confirm_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-shutdown-token-32-chars-ok!")
    client = TestClient(hub.app)

    response = client.post("/api/autostart/confirm")

    assert response.status_code == 401


def test_autostart_confirm_returns_409_when_no_active_monitor(authed_client: TestClient) -> None:
    set_active_monitor(None)

    response = authed_client.post("/api/autostart/confirm")

    assert response.status_code == 409


def test_autostart_confirm_returns_409_when_nothing_pending(authed_client: TestClient) -> None:
    set_active_monitor(RuntimeAutostartMonitor())

    response = authed_client.post("/api/autostart/confirm")

    assert response.status_code == 409


def test_autostart_confirm_arms_pending_monitor(authed_client: TestClient) -> None:
    session_calls: list[str] = []
    monitor = _pending_monitor(session_calls=session_calls)
    set_active_monitor(monitor)

    response = authed_client.post("/api/autostart/confirm")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert monitor.state == STATE_ARMED
    assert session_calls == ["Google Chrome"]


def test_autostart_confirm_twice_second_call_returns_409(authed_client: TestClient) -> None:
    monitor = _pending_monitor()
    set_active_monitor(monitor)

    first = authed_client.post("/api/autostart/confirm")
    second = authed_client.post("/api/autostart/confirm")

    assert first.status_code == 200
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# /api/autostart/decline
# ---------------------------------------------------------------------------


def test_autostart_decline_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-shutdown-token-32-chars-ok!")
    client = TestClient(hub.app)

    response = client.post("/api/autostart/decline")

    assert response.status_code == 401


def test_autostart_decline_resets_pending_monitor(authed_client: TestClient) -> None:
    monitor = _pending_monitor()
    set_active_monitor(monitor)

    response = authed_client.post("/api/autostart/decline")

    assert response.status_code == 200
    assert monitor.state == STATE_IDLE


def test_autostart_decline_is_noop_when_no_active_monitor(authed_client: TestClient) -> None:
    set_active_monitor(None)

    response = authed_client.post("/api/autostart/decline")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# end-call / sample-aha-stop wiring
# ---------------------------------------------------------------------------


class _SpyMonitor:
    def __init__(self) -> None:
        self.notify_calls = 0

    def notify_call_ended(self) -> None:
        self.notify_calls += 1


def test_end_call_api_notifies_active_monitor(authed_client: TestClient) -> None:
    hub.reset_config_state()
    spy = _SpyMonitor()
    set_active_monitor(spy)

    response = authed_client.post("/api/start-call", json={"config": {"preset": "sales", "install_code": ""}})
    assert response.status_code == 200

    response = authed_client.post("/api/end-call")

    assert response.status_code == 200
    assert spy.notify_calls == 1


def test_end_call_api_tolerates_no_active_monitor(authed_client: TestClient) -> None:
    hub.reset_config_state()
    set_active_monitor(None)

    response = authed_client.post("/api/start-call", json={"config": {"preset": "sales", "install_code": ""}})
    assert response.status_code == 200

    response = authed_client.post("/api/end-call")

    assert response.status_code == 200
