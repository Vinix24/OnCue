"""End-to-end test for the sample-aha replay path.

Verifies that a first-time user can click "Toon me wat het doet" and see a
curated Dutch sales call flow through the hub → detector without a microphone,
Whisper model or cloud LLM key.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import requests

from sales_copilot.core.config import DetectorConfig, SlidesConfig
from sales_copilot.modules.detector.__main__ import main as detector_main
from sales_copilot.websocket import hub, hub_core
from sales_copilot.websocket.hub_auth import get_hub_token
from tests.conftest_replay import WebSocketEventCollector

SAMPLE_PATH = Path("data/samples/sample-aha.json")


@pytest.mark.asyncio
async def test_sample_aha_pipeline_produces_pain_point_and_objection(
    running_hub: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sample replay emits transcript, pain point, objection and suggestion events
    within the time budget, without audio hardware or a cloud LLM key."""
    host = "127.0.0.1"
    port = running_hub

    monkeypatch.setenv("WS_HUB_HOST", host)
    monkeypatch.setenv("WS_HUB_PORT", str(port))
    monkeypatch.setenv("LLM_PROVIDER", "none")
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-sample-aha")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "")

    assert SAMPLE_PATH.exists(), f"Curated sample missing: {SAMPLE_PATH}"

    token = get_hub_token()
    auth_headers = {"X-Sales-Copilot-Token": token}

    # Disable LLM-dependent features so the sample path works without an API key.
    detector_config = DetectorConfig(
        llm_provider="none",
        enable_suggestions=False,
        enable_summary=False,
        auto_phase_detection=False,
    )
    # Write the case DB into tmp_path — never the repo tree — so the run leaves
    # data/ pristine (only the curated sample ships in the OSS export).
    slides_config = SlidesConfig(case_db_sqlite_path=str(tmp_path / "cases_sample_aha.db"))

    collector_stop = asyncio.Event()
    collector = WebSocketEventCollector()
    await collector.start_all(host, port, collector_stop)

    detector_stop = asyncio.Event()
    detector_task = asyncio.create_task(
        detector_main(
            stop_event=detector_stop,
            register_signals=False,
            detector_config=detector_config,
            slides_config=slides_config,
            session_id="sample-aha-test",
        )
    )

    # Wait for the detector to subscribe to the transcript channel.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if len(hub._subscribers.get("transcript", set())) > 0:
            break
        await asyncio.sleep(0.05)
    assert len(hub._subscribers.get("transcript", set())) > 0, (
        "Detector did not subscribe to /ws/transcript"
    )

    start_s = time.monotonic()
    response = requests.post(
        f"http://{host}:{port}/api/sample-aha/start",
        headers=auth_headers,
        json={"speed": 3.0},
        timeout=5.0,
    )
    assert response.status_code == 200, (
        f"sample-aha/start failed: {response.status_code} {response.text}"
    )

    async def _wait_for_hub_state(expected: str, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if hub_core.status_state() == expected:
                return True
            await asyncio.sleep(0.05)
        return False

    # The player ends the call automatically when the sample finishes.
    assert await _wait_for_hub_state("waiting_for_config", 30.0), (
        "Hub did not return to waiting_for_config after sample"
    )
    elapsed_s = time.monotonic() - start_s

    # Allow the detector to flush its last events.
    await asyncio.sleep(0.5)

    collector_stop.set()
    detector_stop.set()
    await detector_task
    await collector.stop_all()

    assert elapsed_s < 30.0, (
        f"Sample-aha replay exceeded 30 s budget: {elapsed_s:.1f}s"
    )

    report = collector.report()
    assert report["transcripts"] >= 1, f"Expected transcripts, got: {report}"
    assert report["pain_points"] >= 1, f"Expected pain_points, got: {report}"
    assert report["objections"] >= 1, f"Expected objections, got: {report}"
    assert report["suggestions"] >= 1, f"Expected suggestions, got: {report}"

    pain_categories = {p.get("category") for p in collector.pain_points}
    objection_categories = {o.get("category") for o in collector.objections}
    assert "offerteproces" in pain_categories, (
        f"Expected offerteproces pain point, got: {collector.pain_points}"
    )
    assert "prijs" in objection_categories, (
        f"Expected prijs objection, got: {collector.objections}"
    )
