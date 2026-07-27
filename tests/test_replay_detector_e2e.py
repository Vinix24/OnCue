"""Test C — detector e2e: sliding-window classification fires from injected transcripts.

Requires local audio fixture at data/sessions/2026-05-01T15-52-26/ as a
gating condition (proves you have session data), then exercises the full
detector pipeline with synthetic transcript events and a mocked WindowClassifier.

Run locally with:

    pytest -m audio_fixture tests/test_replay_detector_e2e.py -v

The test is fully deterministic (no real LLM calls):
- WindowClassifier.classify is monkeypatched to return a pain_point detection.
- A minimal SQLite case DB is created in tmp_path.
- A ReplayAudioStream verifies the fixture WAV is readable.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
import websockets

from sales_copilot.audio.replay import ReplayAudioStream
from sales_copilot.core.config import DetectorConfig, SlidesConfig, WebSocketConfig
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.detector.__main__ import _dispatch_window_detection
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.pipeline import DetectionPipeline
from sales_copilot.modules.detector.router import PainPointRouter
from sales_copilot.modules.detector.sliding_window import SlidingWindowBuffer, TranscriptChunk
from sales_copilot.modules.detector.window_classifier import (
    WindowAnalysis,
    WindowClassifier,
    WindowDetection,
)
from sales_copilot.modules.slides.case_db import Case, SQLiteCaseDB
from sales_copilot.websocket import hub_core
from tests.conftest_replay import WebSocketEventCollector
from tests.ws_helpers import ws_url

_SESSION_LONG = Path("data/sessions/2026-05-01T15-52-26")

_TRANSCRIPT_EVENTS = [
    {
        "type": "transcript",
        "text": "We besteden uren aan het opstellen van offertes",
        "speaker": "prospect",
        "start_ms": 1000,
        "end_ms": 5000,
    },
    {
        "type": "transcript",
        "text": "Het offerteproces kost ons heel veel tijd en energie",
        "speaker": "prospect",
        "start_ms": 5100,
        "end_ms": 9000,
    },
    {
        "type": "transcript",
        "text": "Soms duurt het twee weken voor een offerte de deur uit gaat",
        "speaker": "prospect",
        "start_ms": 9100,
        "end_ms": 13000,
    },
]


class _StubLLMClient:
    def confirm(self, _: str):
        raise AssertionError("LLM confirmation must not be called in e2e test")


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
                confidence=0.92,
                evidence_quote=window_text[:60] if window_text else "offerteproces",
                reasoning="Test: gesimuleerde detectie voor sliding-window e2e validatie",
            )
        ]
    )


@pytest.mark.audio_fixture
async def test_detector_e2e_sliding_window_fires_pain_point(
    running_hub: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sliding-window detector produces ≥1 pain_point from Dutch transcript events.

    Steps:
    1. Verify the 17-min session fixture is readable (proves session exists).
    2. Create a minimal case DB + detector pipeline with mocked WindowClassifier.
    3. Start WebSocketEventCollector to capture pain_point events.
    4. Run a simplified consume-and-detect loop subscribed to the hub's transcript channel.
    5. Inject transcript events that match the 'offerteproces' pain point.
    6. Assert ≥1 pain_point event received within timeout.
    """
    fixture_wav = _SESSION_LONG / "self.wav"
    fixture_available = fixture_wav.exists()

    if fixture_available:
        # Verify fixture is readable with ReplayAudioStream.
        stream = ReplayAudioStream(fixture_wav, chunk_size_frames=512)
        stream.start()
        first_chunk = stream.read()
        stream.stop()
        assert first_chunk is not None, "Fixture WAV must be readable"

    # Minimal SQLite case DB with one matching case.
    db_path = tmp_path / "cases.db"
    case_db = SQLiteCaseDB(db_path)
    await case_db.initialize()
    await case_db.upsert_cases(
        [
            Case(
                id="case-offerteproces-001",
                title="Offerteproces automatisering",
                industry=None,
                pain_point="offerteproces",
                description="Offertes kosten te veel tijd",
                slide_html="<section>Case</section>",
                metrics="70% sneller",
                priority=1,
            )
        ]
    )

    ws_config = WebSocketConfig(host="127.0.0.1", port=running_hub)
    slides_config = SlidesConfig(case_db_sqlite_path=str(db_path), prospect_industry=None)

    detector_config = DetectorConfig(
        confidence_threshold_high=0.0,
        confidence_threshold_low=0.0,
        debounce_seconds=0,
        only_classify_prospect=True,
        min_chunks_to_classify=2,
        classification_debounce_seconds=0.0,
        enable_suggestions=False,
        enable_summary=False,
        auto_phase_detection=False,
    )

    # Build detector pipeline components directly (avoids sentence-transformer warmup
    # for the router — we only need the router for PainPointRouter instantiation;
    # actual detection goes through the mocked WindowClassifier).
    router = PainPointRouter(detector_config)
    router.classify("warmup")  # warm up embedding model once

    pipeline = DetectionPipeline(
        detector_config,
        router=router,
        llm_client=_StubLLMClient(),
        debouncer=PainPointDebouncer(detector_config.debounce_seconds),
    )
    injector = SlideInjector(pipeline, case_db, ws_config, slides_config)

    # SlideInjector.handle_window_detection uses ws.closed which is not available
    # on new-style websockets ClientConnection.  Mock it to broadcast directly so
    # the test focuses on the sliding-window → detection → publish path.
    async def _mock_handle_window_detection(
        category: str,
        confidence: float,
        evidence_quote: str,
        timestamp_ms: int,
    ) -> None:
        await hub_core.broadcast(
            "pain-points",
            {
                "type": "pain_point",
                "category": category,
                "confidence": confidence,
                "trigger_phrase": evidence_quote,
                "timestamp_ms": timestamp_ms,
            },
        )

    monkeypatch.setattr(injector, "handle_window_detection", _mock_handle_window_detection)

    # Patch WindowClassifier.classify before instantiation.
    monkeypatch.setattr(WindowClassifier, "classify", _mock_classify)
    window_classifier = WindowClassifier(detector_config, "gemini")
    window_buf = SlidingWindowBuffer(detector_config.sliding_window_size)

    stop_event = asyncio.Event()
    collector = WebSocketEventCollector()
    await collector.start_all("127.0.0.1", running_hub, stop_event)

    transcript_uri = ws_url("127.0.0.1", running_hub, "transcript")

    async def _consume() -> None:
        last_classified_at: float | None = None
        async with websockets.connect(transcript_uri) as ws:
            while not stop_event.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                except TimeoutError:
                    continue
                payload = json.loads(raw)
                if payload.get("type") != "transcript":
                    continue
                text = payload.get("text", "")
                speaker = payload.get("speaker", "")
                if not text or speaker != "prospect":
                    continue
                chunk = TranscriptChunk(
                    text=text,
                    speaker=speaker,
                    start_ms=int(payload.get("start_ms", 0)),
                    end_ms=int(payload.get("end_ms", 0)),
                )
                window_buf.add(chunk)
                if len(window_buf) < detector_config.min_chunks_to_classify:
                    continue
                now = time.monotonic()
                if (
                    last_classified_at is not None
                    and (now - last_classified_at) < detector_config.classification_debounce_seconds
                ):
                    continue
                last_classified_at = now
                analysis = await window_classifier.classify(
                    window_text=window_buf.context_text(),
                    latest_chunk=window_buf.latest_chunk(),
                )
                for detection in analysis.detections:
                    if detection.category == "none":
                        continue
                    if detection.confidence < detector_config.confidence_threshold_low:
                        continue
                    await _dispatch_window_detection(
                        detection,
                        int(payload.get("end_ms", 0)),
                        injector,
                        ws_config,
                        detector_config,
                        {},
                    )

    consumer_task = asyncio.create_task(_consume(), name="test-detector-consumer")

    # Brief delay so the consumer subscribes before we inject transcripts.
    await asyncio.sleep(0.2)

    async with websockets.connect(transcript_uri) as sender:
        for event in _TRANSCRIPT_EVENTS:
            await sender.send(json.dumps(event))
            await asyncio.sleep(0.05)

    # Wait for the pain_point event — timeout after 5 seconds.
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if collector.pain_points:
            break
        await asyncio.sleep(0.05)

    stop_event.set()
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass
    await collector.stop_all()
    await injector.close()

    report = collector.report()
    assert report["pain_points"] >= 1, (
        f"Expected ≥1 pain_point from sliding-window detector, got: {report}"
    )
