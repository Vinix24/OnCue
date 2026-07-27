"""Unit tests for ReplayAudioStream and WebSocketEventCollector infrastructure."""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import numpy as np
import pytest
import websockets

from sales_copilot.audio.replay import ReplayAudioStream
from sales_copilot.websocket import hub
from tests.conftest_replay import WebSocketEventCollector, make_synthetic_wav
from tests.ws_helpers import ws_url


async def _wait_until(predicate, *, timeout: float = 2.0, interval: float = 0.02) -> bool:
    """Poll *predicate* until truthy or *timeout* elapses — no fixed sleeps.

    Replaces brittle ``asyncio.sleep`` waits in the collector tests so they do
    not flake on slow CI where event delivery lags behind a fixed interval.
    """
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return bool(predicate())

# ---------------------------------------------------------------------------
# ReplayAudioStream — unit tests
# ---------------------------------------------------------------------------


def test_replay_stream_reads_synthetic_wav(tmp_path: Path) -> None:
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=1.0)

    stream = ReplayAudioStream(wav, chunk_size_frames=512)
    stream.start()

    chunks: list[np.ndarray] = []
    while True:
        chunk = stream.read()
        if chunk is None:
            break
        chunks.append(chunk)

    stream.stop()

    assert len(chunks) > 0
    assert all(c.dtype == np.float32 for c in chunks)
    assert all(np.abs(c).max() <= 1.0 for c in chunks)
    assert stream.at_eof


def test_replay_stream_at_eof_false_before_reading(tmp_path: Path) -> None:
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=0.5)
    stream = ReplayAudioStream(wav)
    stream.start()
    assert not stream.at_eof
    stream.stop()


def test_replay_stream_at_eof_true_after_exhausted(tmp_path: Path) -> None:
    wav = make_synthetic_wav(tmp_path / "short.wav", duration_s=0.1)
    stream = ReplayAudioStream(wav, chunk_size_frames=16000)
    stream.start()
    stream.read()  # first chunk
    stream.read()  # EOF
    assert stream.at_eof
    stream.stop()


def test_replay_stream_read_returns_none_after_eof(tmp_path: Path) -> None:
    wav = make_synthetic_wav(tmp_path / "short.wav", duration_s=0.1)
    stream = ReplayAudioStream(wav, chunk_size_frames=16000)
    stream.start()
    while stream.read() is not None:
        pass
    assert stream.read() is None  # subsequent calls safe
    stream.stop()


def test_replay_stream_stop_before_start_is_safe(tmp_path: Path) -> None:
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=0.5)
    stream = ReplayAudioStream(wav)
    stream.stop()  # no-op — must not raise


def test_replay_stream_read_before_start_returns_none(tmp_path: Path) -> None:
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=0.5)
    stream = ReplayAudioStream(wav)
    assert stream.read() is None


def test_replay_stream_rejects_non_16khz_wav(tmp_path: Path) -> None:
    bad_wav = tmp_path / "bad_rate.wav"
    with wave.open(str(bad_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)  # wrong sample rate
        wf.writeframes(b"\x00" * 1000)

    stream = ReplayAudioStream(bad_wav)
    with pytest.raises(ValueError, match="16 kHz"):
        stream.start()


def test_replay_stream_chunk_size_respected(tmp_path: Path) -> None:
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=1.0)
    stream = ReplayAudioStream(wav, chunk_size_frames=256)
    stream.start()
    chunk = stream.read()
    stream.stop()
    assert chunk is not None
    assert len(chunk) == 256


def test_replay_stream_satisfies_audiostream_protocol(tmp_path: Path) -> None:
    from sales_copilot.audio.capture import AudioStream

    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=0.5)
    stream = ReplayAudioStream(wav)
    assert isinstance(stream, AudioStream)


# ---------------------------------------------------------------------------
# make_synthetic_wav — helper tests
# ---------------------------------------------------------------------------


def test_make_synthetic_wav_produces_valid_16khz_file(tmp_path: Path) -> None:
    wav = make_synthetic_wav(tmp_path / "out.wav", duration_s=2.0)
    with wave.open(str(wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2
        assert wf.getnframes() == 32000


def test_make_synthetic_wav_has_nonzero_rms(tmp_path: Path) -> None:
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=1.0)
    stream = ReplayAudioStream(wav)
    stream.start()
    chunks = []
    while True:
        c = stream.read()
        if c is None:
            break
        chunks.append(c)
    stream.stop()
    audio = np.concatenate(chunks)
    rms = float(np.sqrt(np.mean(np.square(audio))))
    assert rms > 0.005, f"RMS {rms:.4f} must exceed AudioBufferer min_rms"


# ---------------------------------------------------------------------------
# WebSocketEventCollector — integration tests (require a running hub)
# ---------------------------------------------------------------------------


async def test_collector_collects_broadcast_events(running_hub: int) -> None:
    stop = asyncio.Event()
    collector = WebSocketEventCollector()
    await collector.start_all("127.0.0.1", running_hub, stop)

    # Wait until the transcript consumer has registered with the hub instead of
    # sleeping a fixed interval before broadcasting.
    await _wait_until(lambda: "transcript" in hub._subscribers)

    async with websockets.connect(ws_url("127.0.0.1", running_hub, "transcript")) as ws:
        await ws.send(json.dumps({"type": "transcript", "text": "hello", "speaker": "prospect"}))
        # Poll until the broadcast event is collected rather than sleeping.
        await _wait_until(lambda: len(collector.transcripts) >= 1)

    stop.set()
    await collector.stop_all()

    assert len(collector.transcripts) >= 1
    assert collector.transcripts[0].get("type") == "transcript"


async def test_collector_report_counts_events(running_hub: int) -> None:
    stop = asyncio.Event()
    collector = WebSocketEventCollector()
    await collector.start_all("127.0.0.1", running_hub, stop)
    await _wait_until(lambda: "pain-points" in hub._subscribers)

    async with websockets.connect(ws_url("127.0.0.1", running_hub, "pain-points")) as ws:
        await ws.send(json.dumps({"type": "pain_point", "category": "offerteproces"}))
        await ws.send(json.dumps({"type": "pain_point", "category": "kosten"}))
        await _wait_until(lambda: len(collector.pain_points) >= 2)

    stop.set()
    await collector.stop_all()

    report = collector.report()
    assert report["pain_points"] >= 2


async def test_collector_handles_hub_disconnect_gracefully(running_hub: int) -> None:
    stop = asyncio.Event()
    collector = WebSocketEventCollector()
    await collector.start_all("127.0.0.1", running_hub, stop)
    # Wait until at least the transcript consumer connected, then disconnect.
    await _wait_until(lambda: "transcript" in hub._subscribers)
    stop.set()
    await collector.stop_all()
    # Verify the collector stopped cleanly: report() returns non-negative integers.
    # We do not assert zero counts because hub._subscribers is process-wide; other
    # concurrently-running hub fixtures may broadcast into this collector.
    report = collector.report()
    for key in ("transcripts", "pain_points", "objections", "buying_signals"):
        assert isinstance(report[key], int)
        assert report[key] >= 0
