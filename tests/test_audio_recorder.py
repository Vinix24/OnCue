import json
import logging
import wave

import numpy as np
import pytest

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


def test_finalize_warns_when_a_started_stream_captured_no_audio(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A recorder that opened a stream but received zero frames must warn loudly.

    Reproduces the "recorder finalises an empty file, no signal anything was
    off" complaint for the case where recording genuinely was enabled and
    started: a stream was opened (e.g. the mic crashed before any audio
    arrived, see resolve_mic_device()) but write_chunk() was never fed
    anything real.
    """
    rec = AudioRecorder("empty-session", tmp_path, sample_rate=16000)
    # Simulate a stream that attached (WAV file opened) but never got audio --
    # write_chunk() itself no-ops on empty/None samples, so it cannot reach
    # _open_stream_locked() on its own.
    rec._open_stream_locked("mic")  # noqa: SLF001

    caplog.set_level(logging.WARNING)
    out = rec.finalize()

    assert (out / "mic.wav").exists()
    assert "zero audio frames" in caplog.text
    assert "empty-session" in caplog.text


def test_finalize_does_not_warn_for_a_normal_recording(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    rec = AudioRecorder("normal-session", tmp_path, sample_rate=16000)
    rec.write_chunk("mic", _sine(8000), 500)

    caplog.set_level(logging.WARNING)
    rec.finalize()

    assert "zero audio frames" not in caplog.text


def test_finalize_warns_when_no_stream_was_ever_opened(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A real, deliberately-ended session where no stream ever attached at all
    (mic/system capture never started) is the most likely real-world cause of
    an empty recording -- see the 2026-09-06 Windows field report, where
    RECORD_AUDIO=true produced a call with zero recorded audio and no warning
    at all, because the pre-fix warning was gated on ``self._stream_labels``
    being non-empty. Any non-silent finalize() with zero frames must warn,
    whether or not a stream was ever opened.
    """
    rec = AudioRecorder("untouched-session", tmp_path, sample_rate=16000)

    caplog.set_level(logging.WARNING)
    rec.finalize()

    assert "zero audio frames" in caplog.text
    assert "untouched-session" in caplog.text


def test_finalize_silent_suppresses_the_warning_for_a_replaced_recorder(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The one legitimate exception: a recorder that is being discarded because
    it is being replaced by a new session (start_session_recording's
    cleanup-of-previous), not because a real call deliberately ended. It may
    never have been the active recorder of a genuine call, so a zero-frame
    result there is not evidence of a broken capture path.
    """
    rec = AudioRecorder("replaced-session", tmp_path, sample_rate=16000)

    caplog.set_level(logging.WARNING)
    rec.finalize(silent=True)

    assert "zero audio frames" not in caplog.text


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


# --- Regression coverage for the 2026-09-06 Windows field report: --------
#
# "RECORD_AUDIO=true records nothing; the recorder finalizes empty right at
# session start (streams=[])." PR #223 added a warning for a zero-frame
# finalize, but gated it on ``self._stream_labels`` being non-empty -- which
# is exactly backwards for the most likely real cause: the mic/system audio
# stream never attaching at all, so write_chunk() (and therefore
# _open_stream_locked()) is never reached even once, and the finalize that
# should have screamed logged a harmless INFO line instead.
#
# The three scenarios below are simulated at the same singleton layer
# (start_session_recording / stop_session_recording) that the real orchestrator
# drives in src/sales_copilot/__main__.py, with "incoming audio chunks"
# mocked as direct write_chunk() calls on get_active_recorder() -- the same
# call transcriber/audio_bufferer.py and transcriber/whisper_direct.py make
# from _record_chunk() for every real audio frame.


def test_real_session_with_incoming_chunks_produces_a_nonempty_file(tmp_path) -> None:
    """RECORD_AUDIO=true, a call starts, and audio actually arrives: the
    resulting WAV file must have real content once the call ends.
    """
    start_session_recording("call-with-audio", tmp_path, sample_rate=16000, flush_seconds=5.0)
    try:
        recorder = get_active_recorder()
        assert recorder is not None
        # Simulate incoming audio chunks the way _record_chunk() does per frame.
        recorder.write_chunk("prospect", _sine(1600), 0)
        recorder.write_chunk("prospect", _sine(1600), 100)
        path = stop_session_recording()
    finally:
        recorder_module._active_recorder = None

    assert path is not None
    wav_path = path / "prospect.wav"
    assert wav_path.exists()
    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnframes() == 3200


def test_cleanup_of_an_unused_previous_recorder_does_not_false_alarm(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A previous recorder that was created but never actually used (no
    write_chunk ever reached it -- e.g. a defensive double-start with no call
    in between) must not trigger the "captured nothing" warning when the next
    session replaces it. That recorder was never the active recorder of a
    real, ended call.
    """
    caplog.set_level(logging.WARNING)

    start_session_recording("unused-previous", tmp_path, sample_rate=16000, flush_seconds=5.0)
    try:
        # No write_chunk() calls: this recorder is genuinely never touched,
        # simulating a restart/double-start before any audio arrived.
        start_session_recording("actual-call", tmp_path, sample_rate=16000, flush_seconds=5.0)
        assert "zero audio frames" not in caplog.text
    finally:
        stop_session_recording()
        recorder_module._active_recorder = None


def test_session_that_genuinely_captured_nothing_warns_loudly(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The actual field-reported bug: RECORD_AUDIO=true, a real call starts and
    ends, but the mic/system audio stream never attaches, so no write_chunk()
    call ever reaches the recorder. stop_session_recording() -- the real
    call-end path, not a defensive replace -- must warn loudly, exactly the
    "check that the mic/system audio streams actually started" signal an
    operator needs to diagnose a silently empty recording.
    """
    caplog.set_level(logging.WARNING)

    start_session_recording("call-with-no-audio", tmp_path, sample_rate=16000, flush_seconds=5.0)
    try:
        # No write_chunk() calls at all: audio capture never attached.
        pass
    finally:
        stop_session_recording()
        recorder_module._active_recorder = None

    assert "zero audio frames" in caplog.text
    assert "call-with-no-audio" in caplog.text
