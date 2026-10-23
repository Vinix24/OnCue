import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets

from sales_copilot.__main__ import _parse_call_config, _run_call
from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import WebSocketConfig, build_module_configs
from sales_copilot.modules.detector.sliding_window import TranscriptChunk
from sales_copilot.modules.detector.window_classifier import (
    WindowAnalysis,
    WindowClassifier,
    WindowDetection,
)
from sales_copilot.modules.reports.enrichment import wait_for_pending_reports
from sales_copilot.modules.slides.case_db import Case, SQLiteCaseDB
from sales_copilot.websocket import hub
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


#: Upper bound for an event the test EXPECTS. An event that arrives returns at once, so the
#: size of this bound costs nothing when green; it only absorbs a slow shared runner.
EVENT_TIMEOUT_S = 10.0

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


async def _recv_json(ws: websockets.ClientConnection, timeout: float = EVENT_TIMEOUT_S) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


def _forbid_outbound_http(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make any non-loopback httpx request fail loudly; returns the blocked URLs.

    Every LLM SDK the project uses (openai, instructor, google-genai) sends through httpx, so
    guarding ``send`` covers a client that a future change forgets to switch off.
    """
    blocked: list[str] = []

    def _check(request: httpx.Request) -> None:
        if request.url.host not in _LOOPBACK_HOSTS:
            blocked.append(str(request.url))
            raise AssertionError(f"outbound HTTP request in a hermetic test: {request.url}")

    original_send = httpx.Client.send
    original_async_send = httpx.AsyncClient.send

    def _send(self: httpx.Client, request: httpx.Request, **kwargs):
        _check(request)
        return original_send(self, request, **kwargs)

    async def _async_send(self: httpx.AsyncClient, request: httpx.Request, **kwargs):
        _check(request)
        return await original_async_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", _send)
    monkeypatch.setattr(httpx.AsyncClient, "send", _async_send)
    return blocked


async def _wait_for_subscribers(channel: str, minimum: int, timeout: float = 3.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        if len(hub._subscribers.get(channel, set())) >= minimum:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {minimum} subscribers on {channel}")


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
        hub.reset_config_state()


async def _orchestrate_call(stop_event: asyncio.Event) -> None:
    config_payload = None
    while config_payload is None and not stop_event.is_set():
        config_payload = await hub.wait_for_start_call(timeout=0.25)
    if stop_event.is_set():
        return
    call_config = _parse_call_config(config_payload)
    configs = build_module_configs(call_config)
    ws_config = WebSocketConfig.from_env()
    await _run_call(stop_event, call_config, configs, ws_config)


async def _idle_module(**kwargs) -> None:
    stop_event = kwargs["stop_event"]
    while not stop_event.is_set():
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_config_flow_e2e(
    running_hub: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    db_path = tmp_path / "cases.db"
    case_db = SQLiteCaseDB(db_path)
    await case_db.initialize()
    await case_db.upsert_cases(
        [
            Case(
                id="case-017",
                title="Case 017",
                industry=None,
                pain_point="offerteproces",
                description="Offerteproces case",
                slide_html="<section>Case</section>",
                metrics="70% sneller",
                priority=1,
            )
        ]
    )

    monkeypatch.setenv("WS_HUB_HOST", "127.0.0.1")
    monkeypatch.setenv("WS_HUB_PORT", str(running_hub))
    monkeypatch.setenv("CASE_DB_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("CONFIDENCE_THRESHOLD_HIGH", "0.01")
    monkeypatch.setenv("CONFIDENCE_THRESHOLD_LOW", "0.005")
    monkeypatch.setenv("ONLY_CLASSIFY_PROSPECT", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    # The real reports module runs here: switch its post-call LLM step off so the dummy
    # OpenAI key never reaches the network (the step itself: tests/test_report_enrichment.py).
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "none")
    # Every other LLM lane the call runs is switched off the same way ("none" builds no client
    # and sends nothing). The test asserts on none of them: the pain point comes from the
    # mocked WindowClassifier.classify, the slide from the case DB.
    for task in (
        "DETECTOR_CONFIRM",
        "WINDOW_CLASSIFIER",
        "PHASE",
        "SUGGESTIONS",
        "SUMMARY",
        "SCRIPT_TRACKING",
        "SLIDES",
        "REPORT_TERMS",
    ):
        monkeypatch.setenv(f"{task}_LLM_PROVIDER", "none")
    # litellm downloads its model price map from GitHub when first imported; the bundled copy
    # is enough here.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    blocked_requests = _forbid_outbound_http(monkeypatch)
    monkeypatch.setenv("DETECTOR_MIN_CHUNKS", "1")
    monkeypatch.setenv("DETECTOR_DEBOUNCE_S", "0")
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-config-e2e-token")
    monkeypatch.setattr("sales_copilot.__main__.transcriber_main", _idle_module)

    async def _mock_classify(
        self: WindowClassifier,
        window_text: str,
        latest_chunk: TranscriptChunk | None,
        phase: str = "discovery",
    ) -> WindowAnalysis:
        return WindowAnalysis(
            detections=[
                WindowDetection(
                    category="pain_point",
                    subcategory="offerteproces",
                    confidence=0.95,
                    evidence_quote=window_text[:60] if window_text else "offerteproces",
                    reasoning="Test: deterministic pain_point for e2e config flow",
                )
            ]
        )

    monkeypatch.setattr(WindowClassifier, "classify", _mock_classify)
    monkeypatch.setattr(
        "sales_copilot.modules.copilot.injector.get_feature_policy",
        lambda: pro_feature_policy,
    )
    monkeypatch.setattr(
        "sales_copilot.websocket.hub_core.get_feature_policy",
        lambda: pro_feature_policy,
    )
    from sales_copilot.websocket import hub_core

    hub_core._feature_policy = None

    ws_config = WebSocketConfig(host="127.0.0.1", port=running_hub)
    stop_event = asyncio.Event()
    orchestrator = asyncio.create_task(_orchestrate_call(stop_event))

    config_uri = ws_url(ws_config.host, ws_config.port, "config")
    talk_time_uri = ws_url(ws_config.host, ws_config.port, "talk-time")
    transcript_uri = ws_url(ws_config.host, ws_config.port, "transcript")
    pain_points_uri = ws_url(ws_config.host, ws_config.port, "pain-points")
    slide_control_uri = ws_url(ws_config.host, ws_config.port, "slide-control")
    coaching_uri = ws_url(ws_config.host, ws_config.port, "coaching")

    async with (
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{running_hub}") as http_client,
        websockets.connect(config_uri),
        websockets.connect(talk_time_uri) as talk_time_ws,
        websockets.connect(transcript_uri) as transcript_ws,
        websockets.connect(pain_points_uri) as pain_points_ws,
        websockets.connect(slide_control_uri) as slide_control_ws,
        websockets.connect(coaching_uri) as coaching_ws,
    ):
        # Async HTTP client: an awaited POST yields the event loop instead of
        # blocking it (the old `requests.post` stalled the loop driving the
        # orchestrator + websockets for the full round-trip).
        response = await http_client.post(
            "/api/start-call",
            json={
                "config": {
                    "screen_mode": "single",
                    "modules": {
                        "talk_time": False,
                        "transcript": False,
                        "pain_points": True,
                        "presentation": True,
                        "post_call_report": True,
                    },
                    "llm": {"provider": "openai", "model": "gpt-4o-mini"},
                    "prospect": {
                        "name": "Riley Prospect",
                        "company": "Acme BV",
                        "industry": "SaaS",
                    },
                },
            },
            headers={"X-Sales-Copilot-Token": "test-config-e2e-token"},
            timeout=3.0,
        )
        assert response.status_code == 200

        await _wait_for_subscribers("transcript", minimum=2, timeout=5.0)
        await transcript_ws.send(
            json.dumps(
                {
                    "type": "transcript",
                    "text": "we zitten uren aan offertes",
                    "speaker": "prospect",
                    "start_ms": 120000,
                    "end_ms": 124500,
                }
            )
        )

        pain_payload = await _recv_json(pain_points_ws)
        assert pain_payload["type"] == "pain_point"
        assert pain_payload["category"] == "offerteproces"
        assert pain_payload["case_id"] == "case-017"

        slide_payload = await _recv_json(slide_control_ws)
        assert slide_payload == {"action": "navigate_to_case", "slide_id": "case-017"}

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(talk_time_ws.recv(), timeout=0.3)

        response = await http_client.post(
            "/api/end-call",
            headers={"X-Sales-Copilot-Token": "test-config-e2e-token"},
            timeout=3.0,
        )
        assert response.status_code == 200
        report_payload = await _recv_json(coaching_ws)
        assert report_payload["type"] == "report_ready"
        assert report_payload["prospect_name"] == "Riley Prospect"
        assert report_payload["prospect_company"] == "Acme BV"

    stop_event.set()
    await orchestrator
    # The report's post-call steps (enrichment -> rewrite -> delivery) run in their own task.
    await wait_for_pending_reports()
    assert blocked_requests == []
