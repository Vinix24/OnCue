import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import uvicorn
import websockets

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import DetectorConfig, SlidesConfig, WebSocketConfig
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.pipeline import DetectionPipeline
from sales_copilot.modules.detector.router import PainPointRouter
from sales_copilot.modules.slides.case_db import Case, SQLiteCaseDB
from sales_copilot.websocket import hub
from tests.ws_helpers import ws_url


class _StubLLMClient:
    def confirm(self, _: str):
        raise AssertionError("LLM confirmation should not be called in embedding path")


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


async def _consume_transcripts(
    injector: SlideInjector,
    ws_url: str,
    stop_event: asyncio.Event,
) -> None:
    async with websockets.connect(ws_url) as ws:
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
            except TimeoutError:
                continue
            payload = json.loads(raw)
            if payload.get("type") != "transcript":
                continue
            text = payload.get("text")
            speaker = payload.get("speaker")
            timestamp_ms = payload.get("end_ms") or payload.get("start_ms")
            if not isinstance(text, str) or not isinstance(speaker, str):
                continue
            if not isinstance(timestamp_ms, (int, float)):
                timestamp_ms = int(time.time() * 1000)
            await injector.process_transcript(text, speaker, int(timestamp_ms))


@pytest.mark.asyncio
async def test_module3_e2e_pipeline(running_hub: int, tmp_path: Path, pro_feature_policy: FeaturePolicy) -> None:
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

    detector_config = DetectorConfig(
        confidence_threshold_high=0.0,
        confidence_threshold_low=0.0,
        debounce_seconds=45,
        only_classify_prospect=True,
    )
    router = PainPointRouter(detector_config)
    router.classify("warmup")

    pipeline = DetectionPipeline(
        detector_config,
        router=router,
        llm_client=_StubLLMClient(),
        debouncer=PainPointDebouncer(detector_config.debounce_seconds),
    )

    ws_config = WebSocketConfig(host="127.0.0.1", port=running_hub)
    slides_config = SlidesConfig(
        case_db_sqlite_path=str(db_path),
        prospect_industry=None,
    )
    injector = SlideInjector(pipeline, case_db, ws_config, slides_config, feature_policy=pro_feature_policy)
    stop_event = asyncio.Event()

    transcript_uri = ws_url("127.0.0.1", running_hub, "transcript")
    pain_points_uri = ws_url("127.0.0.1", running_hub, "pain-points")
    slide_control_uri = ws_url("127.0.0.1", running_hub, "slide-control")

    consumer_task = asyncio.create_task(_consume_transcripts(injector, transcript_uri, stop_event))
    try:
        async with (
            websockets.connect(pain_points_uri) as pain_points_client,
            websockets.connect(slide_control_uri) as slide_control_client,
            websockets.connect(transcript_uri) as transcript_sender,
        ):
            start = time.perf_counter()
            await transcript_sender.send(
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

            pain_raw = await asyncio.wait_for(pain_points_client.recv(), timeout=1.0)
            latency = time.perf_counter() - start
            assert latency < 0.5

            pain_payload = json.loads(pain_raw)
            assert pain_payload["type"] == "pain_point"
            assert pain_payload["category"] == "offerteproces"
            assert pain_payload["confidence"] >= 0.0
            assert pain_payload["trigger_phrase"] == "we zitten uren aan offertes"
            assert pain_payload["timestamp_ms"] == 124500
            assert pain_payload["case_matched"] is True
            assert pain_payload["case_id"] == "case-017"

            slide_raw = await asyncio.wait_for(slide_control_client.recv(), timeout=1.0)
            slide_payload = json.loads(slide_raw)
            assert slide_payload == {"action": "navigate_to_case", "slide_id": "case-017"}

            await transcript_sender.send(
                json.dumps(
                    {
                        "type": "transcript",
                        "text": "we zitten uren aan offertes",
                        "speaker": "prospect",
                        "start_ms": 130000,
                        "end_ms": 130500,
                    }
                )
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(pain_points_client.recv(), timeout=0.3)

            await transcript_sender.send(
                json.dumps(
                    {
                        "type": "transcript",
                        "text": "we zitten uren aan offertes",
                        "speaker": "self",
                        "start_ms": 140000,
                        "end_ms": 140500,
                    }
                )
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(pain_points_client.recv(), timeout=0.3)
    finally:
        stop_event.set()
        consumer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer_task
        await injector.close()
