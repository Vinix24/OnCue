from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
import requests
import uvicorn

import sales_copilot.__main__ as app_main
from sales_copilot.core.config import build_module_configs
from sales_copilot.websocket import hub


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 3.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Timed out waiting for WebSocket hub")


@pytest.fixture()
async def running_hub() -> AsyncIterator[tuple[str, int]]:
    hub.reset_config_state()
    port = _free_port()
    config = uvicorn.Config(hub.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port("127.0.0.1", port)

    try:
        yield "127.0.0.1", port
    finally:
        hub.reset_config_state()
        server.should_exit = True
        thread.join(timeout=2)
        hub._subscribers.clear()


async def _idle_module(record: list[str], name: str, **kwargs: Any) -> None:
    record.append(name)
    stop_event = kwargs["stop_event"]
    while not stop_event.is_set():
        await asyncio.sleep(0.05)


async def _wait_for_http_state(base_url: str, expected: str, timeout: float = 2.0) -> str | None:
    deadline = time.time() + timeout
    state = None
    while time.time() < deadline:
        state = requests.get(f"{base_url}/api/status", timeout=1.0).json()["state"]
        if state == expected:
            return state
        await asyncio.sleep(0.05)
    return state


@pytest.mark.asyncio
async def test_start_call_e2e_starts_expected_modules(
    running_hub: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, port = running_hub
    monkeypatch.setenv("WS_HUB_HOST", host)
    monkeypatch.setenv("WS_HUB_PORT", str(port))
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-e2e-token")
    auth_headers = {"X-Sales-Copilot-Token": "test-e2e-token"}

    called: list[str] = []

    async def talk_stub(**kwargs: Any) -> None:
        await _idle_module(called, "talk_time", **kwargs)

    async def transcriber_stub(**kwargs: Any) -> None:
        await _idle_module(called, "transcriber", **kwargs)

    async def detector_stub(**kwargs: Any) -> None:
        await _idle_module(called, "detector", **kwargs)

    async def reports_stub(**kwargs: Any) -> None:
        await _idle_module(called, "reports", **kwargs)

    monkeypatch.setattr(app_main, "talk_time_main", talk_stub)
    monkeypatch.setattr(app_main, "transcriber_main", transcriber_stub)

    # detector_main and reports_main are lazy imports inside _run_call() (PR #149,
    # Windows install-friction fix). Patch the source modules instead of app_main.
    import sales_copilot.modules.detector.__main__ as detector_mod
    import sales_copilot.modules.reports.__main__ as reports_mod

    monkeypatch.setattr(detector_mod, "main", detector_stub)
    monkeypatch.setattr(reports_mod, "main", reports_stub)

    stop_event = asyncio.Event()

    async def orchestrate_once() -> None:
        payload = None
        while payload is None:
            payload = await hub.wait_for_start_call(timeout=0.25)
        call_config = app_main._parse_call_config(payload)
        configs = build_module_configs(call_config)
        await app_main._run_call(stop_event, call_config, configs)
        hub.reset_config_state()

    orchestrator_task = asyncio.create_task(orchestrate_once())
    base_url = f"http://{host}:{port}"

    try:
        response = requests.post(
            f"{base_url}/api/start-call",
            json={
                "config": {
                    "screen_mode": "single",
                    "modules": {
                        "talk_time": True,
                        "transcript": True,
                        "pain_points": True,
                        "presentation": False,
                        "post_call_report": True,
                    }
                }
            },
            headers=auth_headers,
            timeout=3.0,
        )
        assert response.status_code == 200

        state = await _wait_for_http_state(base_url, "call_active")
        assert state == "call_active"

        deadline = time.time() + 2.0
        while time.time() < deadline:
            if set(called) >= {"talk_time", "transcriber", "detector", "reports"}:
                break
            await asyncio.sleep(0.05)
        assert set(called) >= {"talk_time", "transcriber", "detector", "reports"}

        response = requests.post(
            f"{base_url}/api/end-call",
            headers=auth_headers,
            timeout=3.0,
        )
        assert response.status_code == 200

        state = await _wait_for_http_state(base_url, "waiting_for_config")
        assert state == "waiting_for_config"
    finally:
        stop_event.set()
        await orchestrator_task
