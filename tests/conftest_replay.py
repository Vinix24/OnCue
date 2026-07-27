"""Shared fixtures and helpers for replay-based integration tests."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
import unittest.mock
import wave
from pathlib import Path

import numpy as np
import pytest_asyncio
import uvicorn
import websockets

from sales_copilot.websocket import hub
from tests.ws_helpers import ws_url

logger = logging.getLogger(__name__)

_CHANNEL_MAP: dict[str, str] = {
    "transcript": "transcripts",
    "pain-points": "pain_points",
    "objections": "objections",
    "buying-signals": "buying_signals",
    "coaching": "coaching",
    "suggestions": "suggestions",
    "summaries": "summaries",
    "talk-time": "talk_time_snapshots",
    "phase": "phase_transitions",
}


class WebSocketEventCollector:
    """Captures all events published to hub channels for assertion in tests."""

    def __init__(self) -> None:
        self.transcripts: list[dict] = []
        self.pain_points: list[dict] = []
        self.objections: list[dict] = []
        self.buying_signals: list[dict] = []
        self.coaching: list[dict] = []
        self.suggestions: list[dict] = []
        self.summaries: list[dict] = []
        self.talk_time_snapshots: list[dict] = []
        self.phase_transitions: list[dict] = []
        self._tasks: list[asyncio.Task] = []

    async def _consume_channel(
        self,
        channel: str,
        attr: str,
        ws_url: str,
        stop: asyncio.Event,
    ) -> None:
        target: list = getattr(self, attr)
        try:
            async with websockets.connect(ws_url) as ws:
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                    except TimeoutError:
                        continue
                    try:
                        target.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

    async def start_all(self, host: str, port: int, stop: asyncio.Event) -> None:
        for channel, attr in _CHANNEL_MAP.items():
            url = ws_url(host, port, channel)
            self._tasks.append(
                asyncio.create_task(
                    self._consume_channel(channel, attr, url, stop),
                    name=f"collector-{channel}",
                )
            )

    async def stop_all(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    def report(self) -> dict[str, int]:
        return {
            "transcripts": len(self.transcripts),
            "pain_points": len(self.pain_points),
            "objections": len(self.objections),
            "buying_signals": len(self.buying_signals),
            "coaching": len(self.coaching),
            "suggestions": len(self.suggestions),
            "summaries": len(self.summaries),
        }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for {host}:{port}")


@pytest_asyncio.fixture()
async def running_hub():
    """Start a uvicorn hub on a free port; yield the port; tear down.

    Shared canonical fixture (re-exported via ``tests/conftest.py``). Several
    e2e modules still define their own ``running_hub`` because their teardown
    diverges from this one and consolidating them is not behaviour-preserving:
    ``test_session_tracker``/``test_config_e2e`` additionally reset
    ``hub.reset_config_state()`` to avoid call-state leaking into the next test,
    and ``test_start_call_e2e`` yields a ``(host, port)`` tuple rather than the
    bare int yielded here. Reconciling those signatures + teardown semantics is
    out of scope for the low-tier test-hygiene pass (audit item 45) — folding
    them in blindly risks cross-test state leakage, so the local fixtures stay.
    """
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
        thread.join(timeout=3)
        hub._subscribers.clear()


def make_synthetic_wav(
    path: Path,
    duration_s: float = 5.0,
    sample_rate: int = 16000,
    frequency_hz: float = 440.0,
    amplitude: float = 0.5,
) -> Path:
    """Write a 16 kHz mono WAV with a 440 Hz sine tone.

    RMS is ~0.35, well above AudioBufferer.min_rms (0.005), so every chunk
    is classified as speech without needing the Silero VAD model.
    """
    n_samples = int(duration_s * sample_rate)
    t = np.linspace(0.0, duration_s, n_samples, endpoint=False)
    audio = (np.sin(2 * np.pi * frequency_hz * t) * amplitude).astype(np.float32)
    int16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(int16.tobytes())
    return path


async def run_bufferer_until_eof(
    wav_path: Path,
    *,
    chunk_size: int = 512,
    speaker: str = "prospect",
    queue_maxsize: int = 512,
    flush_wait_s: float = 2.5,
    timeout_s: float = 60.0,
) -> object:
    """Run a WAV through AudioBufferer (VAD disabled) until EOF.

    Returns the populated SharedInferenceQueue.  The caller can inspect
    ``queue.qsize()`` to count detected speech segments.
    """
    from sales_copilot.audio.replay import ReplayAudioStream
    from sales_copilot.modules.transcriber.audio_bufferer import AudioBufferer
    from sales_copilot.modules.transcriber.inference_queue import SharedInferenceQueue

    stream = ReplayAudioStream(wav_path, chunk_size_frames=chunk_size, real_time=False)
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=queue_maxsize)

    # Patch _load_vad so construction never loads Silero / torch in tests.
    with unittest.mock.patch.object(AudioBufferer, "_load_vad", return_value=None):
        bufferer = AudioBufferer(
            audio_stream=stream,
            queue=queue,
            priority=0,
            speaker=speaker,
            sample_rate=16000,
            max_buffer_seconds=2.5,
            transcribe_live=True,
        )

    stop_event = asyncio.Event()

    async def _watch_eof() -> None:
        while not stream.at_eof:
            await asyncio.sleep(0.005)
        # Give the bufferer time to flush the final speech segment.
        await asyncio.sleep(flush_wait_s)
        stop_event.set()

    await asyncio.wait_for(
        asyncio.gather(bufferer.run(stop_event), _watch_eof()),
        timeout=timeout_s,
    )
    return queue
