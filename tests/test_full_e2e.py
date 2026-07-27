import asyncio
import json
import logging
import socket
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import uvicorn
import websockets

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import DetectorConfig, SlidesConfig, TalkTimeConfig, WebSocketConfig
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.pipeline import DetectionPipeline
from sales_copilot.modules.detector.router import PainPointRouter
from sales_copilot.modules.reports import generator as report_generator
from sales_copilot.modules.reports.__main__ import _build_generator_session
from sales_copilot.modules.reports.session import SessionTracker
from sales_copilot.modules.slides.case_db import Case, SQLiteCaseDB
from sales_copilot.modules.talk_time.publisher import TalkTimePublisher
from sales_copilot.modules.talk_time.tracker import TalkTimeTracker
from sales_copilot.websocket import hub
from tests.ws_helpers import ws_url


class _StubLLMClient:
    def confirm(self, _: str):
        raise AssertionError("LLM confirmation should not be called in full E2E test")


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
async def test_full_system_e2e(running_hub: int, tmp_path: Path, pro_feature_policy: FeaturePolicy) -> None:
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
    slides_config = SlidesConfig(case_db_sqlite_path=str(db_path), prospect_industry=None)
    injector = SlideInjector(pipeline, case_db, ws_config, slides_config, feature_policy=pro_feature_policy)

    stop_event = asyncio.Event()
    transcript_uri = ws_url("127.0.0.1", running_hub, "transcript")
    consumer_task = asyncio.create_task(_consume_transcripts(injector, transcript_uri, stop_event))

    original_reports_dir = report_generator.REPORTS_DIR
    report_generator.REPORTS_DIR = tmp_path / "reports"
    session_tracker = SessionTracker(
        ws_config=ws_config,
        db_path=db_path,
        now_iso=lambda: "2026-04-13T00:00:00+00:00",
        now_ms=lambda: 123456,
    )
    await session_tracker.start_session()

    talk_config = TalkTimeConfig(
        monologue_warning_seconds=9999,
        coaching_update_interval_ms=100,
        ratio_amber_threshold=1.0,
        ratio_red_threshold=1.0,
    )
    talk_tracker = TalkTimeTracker(talk_config)
    talk_tracker.record_speech("self", 0, 10000)
    talk_tracker.record_speech("prospect", 10000, 20000)
    current_time = {"ms": 20000}

    def now_ms() -> int:
        return current_time["ms"]

    talk_time_uri = ws_url("127.0.0.1", running_hub, "talk-time")
    phase_uri = ws_url("127.0.0.1", running_hub, "phase")
    pain_points_uri = ws_url("127.0.0.1", running_hub, "pain-points")
    slide_control_uri = ws_url("127.0.0.1", running_hub, "slide-control")

    try:
        async with (
            websockets.connect(talk_time_uri) as talk_client,
            websockets.connect(phase_uri) as phase_client,
            websockets.connect(pain_points_uri) as pain_points_client,
            websockets.connect(slide_control_uri) as slide_control_client,
            websockets.connect(transcript_uri) as transcript_sender,
            TalkTimePublisher(
                talk_tracker,
                config=talk_config,
                ws_config=ws_config,
                now_ms=now_ms,
            ) as publisher,
        ):
            publisher_task = asyncio.create_task(publisher.run())
            try:
                raw = await asyncio.wait_for(talk_client.recv(), timeout=1)
                payload = json.loads(raw)
                assert payload["type"] == "talk_time"
                assert "rolling_self_pct" in payload

                await phase_client.send(json.dumps({"type": "phase_change", "phase": "pitch"}))
                current_time["ms"] = 25000
                phase_payload = await _wait_for_phase(talk_client, "pitch")
                assert phase_payload["phase"] == "pitch"

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

                pain_payload = json.loads(
                    await asyncio.wait_for(pain_points_client.recv(), timeout=1.0)
                )
                latency = time.perf_counter() - start
                # Latency is recorded informationally only. A single round-trip
                # sample is too noisy under CI load for a hard threshold; the
                # functional gate is the wait_for(timeout=1.0) above, which fails
                # the test if the pain_point never arrives.
                logging.getLogger(__name__).info(
                    "full_system_e2e transcript->pain_point latency: %.3fs", latency
                )

                assert pain_payload["category"] == "offerteproces"
                assert pain_payload["case_id"] == "case-017"

                slide_payload = json.loads(
                    await asyncio.wait_for(slide_control_client.recv(), timeout=1.0)
                )
                assert slide_payload == {"action": "navigate_to_case", "slide_id": "case-017"}

                await asyncio.sleep(0.2)
                session_snapshot = await session_tracker.get_session_data()
                assert session_snapshot.transcript
                assert session_snapshot.pain_points
                assert session_snapshot.talk_time_snapshots
                assert session_snapshot.phase_transitions
            finally:
                publisher_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await publisher_task

        session = await session_tracker.end_session()
        report_session = _build_generator_session(session)
        report = report_generator.generate_report(report_session)
        report_files = list(report_generator.REPORTS_DIR.glob("*_report.json"))

        assert report.session_id == session.session_id
        assert report.pain_points_detected
        assert len(report_files) == 1
        assert report_files[0].read_text(encoding="utf-8")
    finally:
        stop_event.set()
        consumer_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer_task
        await injector.close()
        report_generator.REPORTS_DIR = original_reports_dir


@pytest.mark.asyncio
async def test_partial_transcript_routing(running_hub: int) -> None:
    """
    Hub routes partial_transcript events to /ws/transcript subscribers but
    the detector filter (_extract_transcript_fields) only passes type=transcript.
    Verifies: 2 partials + 1 final => client receives 3, detector-like sub sees 1.
    """
    transcript_uri = ws_url("127.0.0.1", running_hub, "transcript")

    all_events: list[dict] = []
    detector_events: list[dict] = []
    received = asyncio.Event()

    async def _subscriber(collector: list[dict], *, final_only: bool) -> None:
        async with websockets.connect(transcript_uri) as ws:
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                except TimeoutError:
                    if received.is_set():
                        break
                    continue
                payload = json.loads(raw)
                if final_only and payload.get("type") != "transcript":
                    continue
                collector.append(payload)

    sub_all_task = asyncio.create_task(_subscriber(all_events, final_only=False))
    sub_det_task = asyncio.create_task(_subscriber(detector_events, final_only=True))

    await asyncio.sleep(0.05)

    partial_1 = {
        "type": "partial_transcript",
        "text": "",
        "speaker": "prospect",
        "start_ms": 5000,
        "tentative_end_ms": 5500,
        "is_final": False,
    }
    partial_2 = {
        "type": "partial_transcript",
        "text": "",
        "speaker": "prospect",
        "start_ms": 5000,
        "tentative_end_ms": 6000,
        "is_final": False,
    }
    final_event = {
        "type": "transcript",
        "text": "wij zijn op zoek naar een oplossing",
        "speaker": "prospect",
        "start_ms": 5000,
        "end_ms": 6200,
        "is_final": True,
    }

    async with websockets.connect(transcript_uri) as sender:
        await sender.send(json.dumps(partial_1))
        await asyncio.sleep(0.05)
        await sender.send(json.dumps(partial_2))
        await asyncio.sleep(0.05)
        await sender.send(json.dumps(final_event))
        await asyncio.sleep(0.1)
        received.set()

    await asyncio.wait_for(sub_all_task, timeout=2.0)
    await asyncio.wait_for(sub_det_task, timeout=2.0)

    assert len(all_events) == 3, f"Expected 3 events (2 partial + 1 final), got {len(all_events)}"
    assert all_events[0]["type"] == "partial_transcript"
    assert all_events[1]["type"] == "partial_transcript"
    assert all_events[2]["type"] == "transcript"

    assert len(detector_events) == 1, f"Detector-like sub should see only 1 final, got {len(detector_events)}"
    assert detector_events[0]["type"] == "transcript"
    assert detector_events[0]["text"] == "wij zijn op zoek naar een oplossing"
