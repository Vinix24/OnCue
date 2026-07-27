import json
import wave

import numpy as np

from sales_copilot.audio import recorder as recorder_module
from sales_copilot.audio.recorder import (
    AudioRecorder,
    get_active_recorder,
    start_session_recording,
    stop_session_recording,
)


def _sine(samples: int, freq: float = 440.0, sample_rate: int = 16000) -> np.ndarray:
    t = np.arange(samples, dtype=np.float32) / float(sample_rate)
    return (0.25 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def test_recorder_writes_wav_per_stream(tmp_path) -> None:
    rec = AudioRecorder("session-x", tmp_path, sample_rate=16000, channels=1)

    chunk = _sine(8000)
    rec.write_chunk("mic", chunk, 500)
    rec.write_chunk("system", chunk, 500)
    rec.write_chunk("mic", chunk, 1000)

    out = rec.finalize()
    mic_path = out / "mic.wav"
    system_path = out / "system.wav"

    assert mic_path.exists()
    assert system_path.exists()

    with wave.open(str(mic_path), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getnframes() == 8000 * 2

    with wave.open(str(system_path), "rb") as wf:
        assert wf.getnframes() == 8000


def test_recorder_metadata_tracks_streams(tmp_path) -> None:
    rec = AudioRecorder("meta-session", tmp_path, sample_rate=16000)
    rec.write_chunk("mic", _sine(16000), 0)
    rec.write_chunk("system", _sine(8000), 0)
    out = rec.finalize()

    metadata = json.loads((out / "metadata.json").read_text())
    assert metadata["session_id"] == "meta-session"
    assert metadata["sample_rate"] == 16000
    assert metadata["channels"] == 1
    assert set(metadata["stream_labels"]) == {"mic", "system"}
    assert metadata["duration_ms"] == 1000
    assert metadata["duration_ms_per_stream"]["mic"] == 1000
    assert metadata["duration_ms_per_stream"]["system"] == 500
    assert metadata["started_at"]
    assert metadata["ended_at"]


def test_recorder_flush_bounds_buffer(tmp_path) -> None:
    rec = AudioRecorder(
        "flush-session",
        tmp_path,
        sample_rate=16000,
        flush_seconds=1.0,
    )
    chunk = _sine(1600)  # 100 ms
    for i in range(20):
        rec.write_chunk("mic", chunk, i * 100)

    # Lower bound: the periodic flush must actually move samples to disk
    # mid-stream (before finalize), not only at the end. The 1.0s flush window
    # fires once at the 1000 ms boundary, so at least one window of frames is
    # written while chunks are still arriving.
    flushed_frames = rec._frames_written.get("mic", 0)
    assert flushed_frames >= 16000

    # Upper bound: the in-memory buffer must not grow without bound; cap at
    # ~2x the flush window of samples. It must also retain the post-flush
    # remainder (non-empty) rather than silently flushing everything.
    total_buffered = sum(len(c) for c in rec._buffers.get("mic", []))
    assert 1600 <= total_buffered <= 2 * 16000

    rec.finalize()
    with wave.open(str(tmp_path / "flush-session" / "mic.wav"), "rb") as wf:
        assert wf.getnframes() == 1600 * 20


def test_finalize_is_idempotent(tmp_path) -> None:
    rec = AudioRecorder("idem", tmp_path)
    rec.write_chunk("mic", _sine(800), 0)
    rec.finalize()
    # Second call must not crash and must not corrupt the file
    rec.finalize()
    with wave.open(str(tmp_path / "idem" / "mic.wav"), "rb") as wf:
        assert wf.getnframes() == 800


def test_active_recorder_lifecycle(tmp_path) -> None:
    assert get_active_recorder() is None
    rec = start_session_recording(
        "global-session",
        tmp_path,
        sample_rate=16000,
        flush_seconds=1.0,
    )
    try:
        assert get_active_recorder() is rec
        active = get_active_recorder()
        assert active is not None
        active.write_chunk("mic", _sine(1600), 0)
    finally:
        path = stop_session_recording()

    assert path == tmp_path / "global-session"
    assert (path / "mic.wav").exists()
    assert (path / "metadata.json").exists()
    assert get_active_recorder() is None


def test_start_session_replaces_previous(tmp_path) -> None:
    first = start_session_recording("first", tmp_path)
    second = start_session_recording("second", tmp_path)
    try:
        assert first is not second
        assert get_active_recorder() is second
        # First should already have been finalized
        assert (tmp_path / "first" / "metadata.json").exists()
    finally:
        stop_session_recording()
        # Defensive cleanup of module state
        recorder_module._active_recorder = None
