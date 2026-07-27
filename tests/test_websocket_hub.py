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
from sales_copilot.auth.license_format import TIER_FEATURES
from sales_copilot.websocket import hub, hub_core
from sales_copilot.websocket.hub_auth import authenticated_ws_url


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


def _ws_url(port: int, channel: str) -> str:
    return authenticated_ws_url(f"ws://127.0.0.1:{port}/ws/{channel}")


@pytest.fixture()
async def running_hub() -> AsyncIterator[int]:
    port = _free_port()
    config = uvicorn.Config(hub.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port("127.0.0.1", port)

    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=2)
        hub._subscribers.clear()


@pytest.mark.asyncio
async def test_broadcasts_to_same_channel(running_hub: int) -> None:
    uri = _ws_url(running_hub, "talk-time")
    async with websockets.connect(uri) as ws_sender, websockets.connect(uri) as ws_receiver:
        await ws_sender.send(json.dumps({"hello": "world"}))
        payload = await asyncio.wait_for(ws_receiver.recv(), timeout=1)
        assert json.loads(payload) == {"hello": "world"}

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws_sender.recv(), timeout=0.2)


@pytest.mark.asyncio
async def test_channel_isolation(running_hub: int) -> None:
    uri_alpha = _ws_url(running_hub, "talk-time")
    uri_beta = _ws_url(running_hub, "coaching")
    async with (
        websockets.connect(uri_alpha) as ws_alpha_1,
        websockets.connect(uri_alpha) as ws_alpha_2,
        websockets.connect(uri_beta) as ws_beta,
    ):
        await ws_alpha_1.send(json.dumps({"type": "alpha"}))
        alpha_payload = await asyncio.wait_for(ws_alpha_2.recv(), timeout=1)
        assert json.loads(alpha_payload) == {"type": "alpha"}

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws_beta.recv(), timeout=0.2)


@pytest.mark.asyncio
async def test_disconnect_cleanup(running_hub: int) -> None:
    uri = _ws_url(running_hub, "objections")
    ws = await websockets.connect(uri)
    await ws.send(json.dumps({"ready": True}))
    await ws.close()

    for _ in range(20):
        if "objections" not in hub._subscribers:
            break
        await asyncio.sleep(0.05)

    assert "objections" not in hub._subscribers


@pytest.mark.asyncio
async def test_sender_connection_stays_open_for_multiple_messages(running_hub: int) -> None:
    uri = _ws_url(running_hub, "transcript")
    async with websockets.connect(uri) as ws_sender, websockets.connect(uri) as ws_receiver:
        await ws_sender.send(json.dumps({"text": "one"}))
        payload1 = await asyncio.wait_for(ws_receiver.recv(), timeout=1)
        assert json.loads(payload1)["text"] == "one"

        assert ws_sender.close_code is None
        await ws_sender.send(json.dumps({"text": "two"}))
        payload2 = await asyncio.wait_for(ws_receiver.recv(), timeout=1)
        assert json.loads(payload2)["text"] == "two"
        assert ws_sender.close_code is None


@pytest.mark.asyncio
async def test_config_subscribers_receive_active_call_replay(running_hub: int) -> None:
    from sales_copilot.websocket import hub_core

    hub_core.reset_config_state()
    try:
        async with hub_core._config_lock:
            hub_core.apply_start_call({"preset_name": "demo"})

        uri = _ws_url(running_hub, "config")
        async with websockets.connect(uri) as ws:
            payload = await asyncio.wait_for(ws.recv(), timeout=1)
            decoded = json.loads(payload)
            assert decoded["type"] == "call_started"
            assert decoded["config"] == {"preset_name": "demo"}
    finally:
        hub_core.reset_config_state()


@pytest.mark.asyncio
async def test_config_subscribers_no_replay_when_idle(running_hub: int) -> None:
    from sales_copilot.websocket import hub_core

    hub_core.reset_config_state()
    uri = _ws_url(running_hub, "config")
    async with websockets.connect(uri) as ws:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), timeout=0.2)


@pytest.mark.asyncio
async def test_coaching_broadcast_gated_in_free_tier(running_hub: int, monkeypatch) -> None:
    """Free tier must not deliver coaching alerts; Pro tier must allow them."""

    class _FreePolicy(FeaturePolicy):
        def current_tier(self) -> str:  # type: ignore[override]
            return "free"

        def allows(self, feature_id: str) -> bool:
            return feature_id in TIER_FEATURES.get("free", set())

    class _ProPolicy(FeaturePolicy):
        def current_tier(self) -> str:  # type: ignore[override]
            return "pro"

        def allows(self, feature_id: str) -> bool:
            return feature_id in TIER_FEATURES.get("pro", set())

    uri = _ws_url(running_hub, "coaching")

    monkeypatch.setattr(hub_core, "get_feature_policy", lambda: _FreePolicy())
    hub_core._feature_policy = None

    async with websockets.connect(uri) as ws_sender, websockets.connect(uri) as ws_receiver:
        await ws_sender.send(json.dumps({"type": "coaching_alert", "message": "free-test"}))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws_receiver.recv(), timeout=0.3)

    monkeypatch.setattr(hub_core, "get_feature_policy", lambda: _ProPolicy())
    hub_core._feature_policy = None

    async with websockets.connect(uri) as ws_sender, websockets.connect(uri) as ws_receiver:
        await ws_sender.send(json.dumps({"type": "coaching_alert", "message": "pro-test"}))
        payload = await asyncio.wait_for(ws_receiver.recv(), timeout=1)
        assert json.loads(payload)["message"] == "pro-test"


@pytest.mark.asyncio
async def test_script_tracking_broadcast_gated_in_free_tier(running_hub: int, monkeypatch) -> None:
    """Free tier must not deliver script-tracking events; Pro tier must allow them."""

    class _FreePolicy(FeaturePolicy):
        def current_tier(self) -> str:  # type: ignore[override]
            return "free"

        def allows(self, feature_id: str) -> bool:
            return feature_id in TIER_FEATURES.get("free", set())

    class _ProPolicy(FeaturePolicy):
        def current_tier(self) -> str:  # type: ignore[override]
            return "pro"

        def allows(self, feature_id: str) -> bool:
            return feature_id in TIER_FEATURES.get("pro", set())

    uri = _ws_url(running_hub, "script-tracking")

    monkeypatch.setattr(hub_core, "get_feature_policy", lambda: _FreePolicy())
    hub_core._feature_policy = None

    async with websockets.connect(uri) as ws_sender, websockets.connect(uri) as ws_receiver:
        await ws_sender.send(json.dumps({"type": "script_coverage", "coverage": []}))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws_receiver.recv(), timeout=0.3)

    monkeypatch.setattr(hub_core, "get_feature_policy", lambda: _ProPolicy())
    hub_core._feature_policy = None

    async with websockets.connect(uri) as ws_sender, websockets.connect(uri) as ws_receiver:
        await ws_sender.send(json.dumps({"type": "script_coverage", "coverage": []}))
        payload = await asyncio.wait_for(ws_receiver.recv(), timeout=1)
        assert json.loads(payload)["type"] == "script_coverage"


@pytest.mark.asyncio
async def test_transcript_subscribers_receive_buffered_replay(running_hub: int) -> None:
    from sales_copilot.websocket import hub_core

    hub_core.reset_config_state()
    try:
        async with hub_core._config_lock:
            hub_core.apply_start_call({"preset_name": "demo"})

        uri = _ws_url(running_hub, "transcript")
        async with websockets.connect(uri) as ws_sender:
            await ws_sender.send(
                json.dumps({"type": "transcript", "text": "hallo wereld", "speaker": "self"})
            )
            await ws_sender.send(
                json.dumps({"type": "transcript", "text": "tweede zin", "speaker": "prospect"})
            )

            for _ in range(40):
                if len(hub_core._transcript_buffer) >= 2:
                    break
                await asyncio.sleep(0.05)

            async with websockets.connect(uri) as late_subscriber:
                first = json.loads(
                    await asyncio.wait_for(late_subscriber.recv(), timeout=1)
                )
                second = json.loads(
                    await asyncio.wait_for(late_subscriber.recv(), timeout=1)
                )
                assert first["text"] == "hallo wereld"
                assert first["speaker"] == "self"
                assert second["text"] == "tweede zin"
                assert second["speaker"] == "prospect"
    finally:
        hub_core.reset_config_state()


@pytest.mark.asyncio
async def test_transcript_buffer_clears_on_end_call(running_hub: int) -> None:
    from sales_copilot.websocket import hub_core

    hub_core.reset_config_state()
    try:
        async with hub_core._config_lock:
            hub_core.apply_start_call({"preset_name": "demo"})

        uri = _ws_url(running_hub, "transcript")
        async with websockets.connect(uri) as ws_sender:
            await ws_sender.send(
                json.dumps({"type": "transcript", "text": "voor end_call", "speaker": "self"})
            )
            for _ in range(40):
                if hub_core._transcript_buffer:
                    break
                await asyncio.sleep(0.05)
            assert hub_core._transcript_buffer

        async with hub_core._config_lock:
            hub_core.apply_end_call()
        assert hub_core._transcript_buffer == []
    finally:
        hub_core.reset_config_state()


@pytest.mark.asyncio
async def test_close_all_connections_drops_active_subscribers(running_hub: int) -> None:
    uri = _ws_url(running_hub, "transcript")
    ws_sender = await websockets.connect(uri)
    ws_receiver = await websockets.connect(uri)
    try:
        await hub.close_all_connections()
        await asyncio.sleep(0.1)
        assert ws_sender.close_code is not None
        assert ws_receiver.close_code is not None
    finally:
        if ws_sender.close_code is None:
            await ws_sender.close()
        if ws_receiver.close_code is None:
            await ws_receiver.close()


@pytest.mark.asyncio
async def test_rejects_unknown_channel(running_hub: int) -> None:
    with pytest.raises(websockets.exceptions.InvalidStatus):
        async with websockets.connect(_ws_url(running_hub, "arbitrary")):
            pass


@pytest.mark.asyncio
async def test_broadcast_drops_back_pressured_subscriber(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subscriber whose send never completes must be timed out and discarded.

    A back-pressured socket stalls on send without raising, so the _safe_send
    try/except alone cannot recover. The per-message wait_for converts the stall
    into a TimeoutError so the broadcast still reaches healthy subscribers.
    """
    monkeypatch.setattr(hub_core, "_SEND_TIMEOUT_S", 0.05)

    class _HangingWs:
        async def send_json(self, _data: object) -> None:
            await asyncio.sleep(10)

        async def send_text(self, _data: object) -> None:
            await asyncio.sleep(10)

    class _FastWs:
        def __init__(self) -> None:
            self.received: list[object] = []

        async def send_json(self, data: object) -> None:
            self.received.append(data)

        async def send_text(self, data: object) -> None:
            self.received.append(data)

    hanging = _HangingWs()
    fast = _FastWs()
    async with hub_core._subscribers_lock:  # noqa: SLF001
        hub_core._subscribers["system"].add(hanging)  # noqa: SLF001
        hub_core._subscribers["system"].add(fast)  # noqa: SLF001

    try:
        # Bounded overall: the hung subscriber must not stall the whole broadcast.
        await asyncio.wait_for(hub_core.broadcast("system", {"type": "ping"}), timeout=2.0)

        assert fast.received == [{"type": "ping"}]
        async with hub_core._subscribers_lock:  # noqa: SLF001
            remaining = hub_core._subscribers.get("system", set())  # noqa: SLF001
            assert hanging not in remaining
            assert fast in remaining
    finally:
        async with hub_core._subscribers_lock:  # noqa: SLF001
            hub_core._subscribers.pop("system", None)  # noqa: SLF001
