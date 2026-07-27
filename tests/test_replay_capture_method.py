"""Tests for AUDIO_CAPTURE_METHOD=replay — the live replay capture path.

Committable, fast, no real recordings and no audio hardware: session WAVs are
synthetic tones written to tmp_path, so CI stays green. Covers:
- DualAudioCapture builds ReplayAudioStream sources from self.wav/prospect.wav
- clear errors on missing config / directory / WAVs
- frames from a factory-built replay stream flow through AudioBufferer
"""

from __future__ import annotations

import asyncio
import unittest.mock
from pathlib import Path

import pytest

from sales_copilot.audio.capture import AudioConfig, DualAudioCapture
from sales_copilot.audio.replay import ReplayAudioStream
from tests.conftest_replay import make_synthetic_wav


def _make_session_dir(tmp_path: Path, duration_s: float = 1.0) -> Path:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    make_synthetic_wav(session_dir / "self.wav", duration_s=duration_s)
    make_synthetic_wav(session_dir / "prospect.wav", duration_s=duration_s)
    return session_dir


def _replay_config(session_dir: Path, *, speed: float = 1.0) -> AudioConfig:
    return AudioConfig(capture_method="replay", replay_session_dir=str(session_dir), replay_speed=speed)


def test_replay_factory_builds_replay_streams(tmp_path: Path) -> None:
    session_dir = _make_session_dir(tmp_path)

    self_stream, prospect_stream = DualAudioCapture(_replay_config(session_dir)).create()

    assert isinstance(self_stream, ReplayAudioStream)
    assert isinstance(prospect_stream, ReplayAudioStream)
    assert self_stream._wav_path == session_dir / "self.wav"
    assert prospect_stream._wav_path == session_dir / "prospect.wav"


def test_replay_factory_requires_session_dir() -> None:
    config = AudioConfig(capture_method="replay", replay_session_dir=None)
    with pytest.raises(ValueError, match="REPLAY_SESSION_DIR"):
        DualAudioCapture(config).create()


def test_replay_factory_missing_directory(tmp_path: Path) -> None:
    config = _replay_config(tmp_path / "does-not-exist")
    with pytest.raises(FileNotFoundError, match="Replay session not found"):
        DualAudioCapture(config).create()


def test_replay_factory_missing_wav(tmp_path: Path) -> None:
    session_dir = _make_session_dir(tmp_path)
    (session_dir / "prospect.wav").unlink()

    with pytest.raises(FileNotFoundError, match="prospect.wav"):
        DualAudioCapture(_replay_config(session_dir)).create()


def test_replay_streams_deliver_frames(tmp_path: Path) -> None:
    session_dir = _make_session_dir(tmp_path)
    # High speed so the paced budget allows immediate delivery.
    self_stream, prospect_stream = DualAudioCapture(_replay_config(session_dir, speed=1000.0)).create()

    for stream in (self_stream, prospect_stream):
        stream.start()
        try:
            chunk = stream.read()
            assert chunk is not None and len(chunk) > 0
        finally:
            stream.stop()


async def test_replay_frames_flow_through_bufferer(tmp_path: Path) -> None:
    """Factory-built replay stream feeds AudioBufferer; speech reaches the queue."""
    from sales_copilot.modules.transcriber.audio_bufferer import AudioBufferer
    from sales_copilot.modules.transcriber.inference_queue import SharedInferenceQueue

    session_dir = _make_session_dir(tmp_path, duration_s=3.0)
    # High speed so the paced stream does not throttle this test to real-time.
    _, prospect_stream = DualAudioCapture(_replay_config(session_dir, speed=100.0)).create()
    assert isinstance(prospect_stream, ReplayAudioStream)

    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=512)
    # Patch _load_vad so construction never loads Silero / torch in tests.
    with unittest.mock.patch.object(AudioBufferer, "_load_vad", return_value=None):
        bufferer = AudioBufferer(
            audio_stream=prospect_stream,
            queue=queue,
            priority=0,
            speaker="prospect",
            sample_rate=16000,
            max_buffer_seconds=2.5,
            transcribe_live=True,
        )

    stop_event = asyncio.Event()

    async def _watch_eof() -> None:
        while not prospect_stream.at_eof:
            await asyncio.sleep(0.005)
        await asyncio.sleep(1.0)  # let the bufferer flush the final segment
        stop_event.set()

    await asyncio.wait_for(
        asyncio.gather(bufferer.run(stop_event), _watch_eof()),
        timeout=30.0,
    )
    assert queue.qsize() >= 1, f"Expected ≥1 speech segment in queue, got {queue.qsize()}"


def test_paced_stream_throttles_without_blocking(tmp_path: Path) -> None:
    """paced=True returns None (underrun) instead of sleeping until wall-clock catches up."""
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=5.0)
    stream = ReplayAudioStream(wav, chunk_size_frames=512, paced=True, speed_multiplier=0.1)
    stream.start()
    try:
        # At 0.1x, the initial budget is ~0 frames: reads return None until
        # enough wall time passes, and never block.
        assert stream.read() is None
    finally:
        stream.stop()
