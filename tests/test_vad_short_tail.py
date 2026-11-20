"""OI-043: a short final chunk must not crash Silero scoring."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import numpy as np
import pytest
import uvicorn
import websockets

from sales_copilot.__main__ import _publish_module_failure
from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import WebSocketConfig
from sales_copilot.modules.talk_time.vad import VADProcessor
from sales_copilot.websocket import hub, hub_core
from sales_copilot.websocket.hub_auth import authenticated_ws_url


class StrictSileroLike:
    """Raises like Silero v5 on a window shorter than 512 samples at 16 kHz."""

    def __init__(self) -> None:
        self.lengths: list[int] = []

    def __call__(self, audio, sample_rate=16000) -> float:
        n = len(audio)
        self.lengths.append(n)
        if sample_rate / n > 31.25:
            raise ValueError("Input audio chunk is too short")
        return 0.9 if float(np.abs(np.asarray(audio)).max()) > 0.1 else 0.0


def _stream(total_samples: int) -> np.ndarray:
    # 1 s loud, then silence, so the VAD emits one speech event.
    audio = np.zeros(total_samples, dtype=np.float32)
    audio[:16000] = 0.5
    return audio


def _run(audio: np.ndarray, model: StrictSileroLike) -> list[tuple[int, int]]:
    vad = VADProcessor(model, "self", sample_rate=16000, debounce_ms=300)
    events = []
    for start in range(0, len(audio), 512):
        event = vad.process_chunk(audio[start : start + 512])
        if event is not None:
            events.append((event.start_ms, event.end_ms))
    return events


def test_stream_not_multiple_of_512_runs_to_end() -> None:
    model = StrictSileroLike()
    events = _run(_stream(512 * 60 + 100), model)
    assert events
    assert model.lengths[-1] == 512  # tail was padded to the window


def test_full_chunk_result_equals_result_without_tail() -> None:
    with_tail = _run(_stream(512 * 60 + 100), StrictSileroLike())
    without_tail = _run(_stream(512 * 60), StrictSileroLike())
    assert with_tail == without_tail


def test_short_chunk_at_8khz_padded_to_256() -> None:
    model = StrictSileroLike()
    vad = VADProcessor(lambda a, sr=8000: model(a, sr), "self", sample_rate=8000)
    vad.process_chunk(np.full(100, 0.5, dtype=np.float32))
    assert model.lengths == [256]


def test_failure_inside_model_still_raises() -> None:
    def broken(audio, sr=16000):
        raise RuntimeError("model broke")

    vad = VADProcessor(broken, "self")
    with pytest.raises(RuntimeError, match="model broke"):
        vad.process_chunk(np.full(512, 0.5, dtype=np.float32))


# OI-046: module_failure reaches a free-tier dashboard (coaching.live is free since #370).


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_module_failure_reaches_subscriber_on_free_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "free")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
    policy = FeaturePolicy()
    assert policy.current_tier() == "free"
    monkeypatch.setattr(hub_core, "_get_feature_policy", lambda: policy)

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(hub.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
            break
        except OSError:
            time.sleep(0.05)

    ws_config = WebSocketConfig(host="127.0.0.1", port=port)

    async def scenario() -> dict:
        url = authenticated_ws_url(f"ws://127.0.0.1:{port}/ws/coaching")
        async with websockets.connect(url) as dashboard:
            await _publish_module_failure("talk_time", "boom", ws_config)
            for _ in range(5):
                msg = json.loads(await asyncio.wait_for(dashboard.recv(), timeout=5))
                if msg.get("type") == "module_failure":
                    return msg
        raise AssertionError("no module_failure received")

    try:
        msg = asyncio.run(scenario())
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        hub._subscribers.clear()
    assert msg["module"] == "talk_time"
    assert msg["error"] == "boom"
