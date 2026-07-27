import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator

import pytest
import uvicorn
import websockets

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import TalkTimeConfig, WebSocketConfig
from sales_copilot.modules.talk_time.publisher import TalkTimePublisher
from sales_copilot.modules.talk_time.tracker import TalkTimeTracker
from sales_copilot.websocket import hub, hub_core
from tests.ws_helpers import ws_url


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


async def _wait_for_phase(client: websockets.ClientConnection, phase: str) -> dict:
    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        raw = await asyncio.wait_for(client.recv(), timeout=1)
        payload = json.loads(raw)
        if payload.get("phase") == phase:
            return payload
    raise TimeoutError(f"phase {phase} not received")


@pytest.fixture()
async def running_hub(pro_feature_policy: FeaturePolicy) -> AsyncIterator[int]:
    port = _free_port()
    config = uvicorn.Config(hub.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)

    hub_core._feature_policy = pro_feature_policy
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port("127.0.0.1", port)

    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=2)
        hub._subscribers.clear()
        hub_core._feature_policy = None


@pytest.mark.asyncio
async def test_module1_e2e_pipeline(running_hub: int) -> None:
    config = TalkTimeConfig(
        monologue_warning_seconds=80,
        coaching_update_interval_ms=100,
        ratio_amber_threshold=1.0,
        ratio_red_threshold=2.0,
    )
    tracker = TalkTimeTracker(config)
    tracker.record_speech("self", 0, 10000)
    tracker.record_speech("prospect", 10000, 20000)
    tracker.record_speech("self", 20000, 30000)

    current_time = {"ms": 30000}

    def now_ms() -> int:
        return current_time["ms"]
    ws_config = WebSocketConfig(host="127.0.0.1", port=running_hub)

    talk_time_uri = ws_url("127.0.0.1", running_hub, "talk-time")
    coaching_uri = ws_url("127.0.0.1", running_hub, "coaching")
    phase_uri = ws_url("127.0.0.1", running_hub, "phase")

    async with (
        websockets.connect(talk_time_uri) as talk_client,
        websockets.connect(coaching_uri) as coaching_client,
        websockets.connect(phase_uri) as phase_client,
        TalkTimePublisher(tracker, config=config, ws_config=ws_config, now_ms=now_ms) as publisher,
    ):
        task = asyncio.create_task(publisher.run())
        try:
            raw = await asyncio.wait_for(talk_client.recv(), timeout=1)
            payload = json.loads(raw)
            expected_keys = {
                "type",
                "rolling_self_pct",
                "rolling_prospect_pct",
                "cumulative_self_pct",
                "cumulative_prospect_pct",
                "current_monologue_ms",
                "monologue_speaker",
                "call_duration_ms",
                "phase",
                "status",
            }
            assert expected_keys.issubset(payload.keys())
            assert payload["type"] == "talk_time"
            assert isinstance(payload["rolling_self_pct"], float)
            assert isinstance(payload["call_duration_ms"], int)

            await phase_client.send(json.dumps({"type": "phase_change", "phase": "pitch"}))
            current_time["ms"] = 35000
            payload = await _wait_for_phase(talk_client, "pitch")
            assert payload["phase"] == "pitch"

            tracker.record_speech("self", 0, 80000)
            current_time["ms"] = 80000
            coaching_raw = await asyncio.wait_for(coaching_client.recv(), timeout=1)
            coaching_payload = json.loads(coaching_raw)
            assert coaching_payload["type"] == "coaching_alert"
            assert coaching_payload["alert_type"] == "monologue_warning"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
