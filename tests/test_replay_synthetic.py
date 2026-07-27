"""Test A — smoke test: synthetic WAV through AudioBufferer pipeline.

Committable, fast (~3 s), no external dependencies beyond the project itself.
Verifies that ReplayAudioStream + AudioBufferer integrate correctly end-to-end:
audio frames are read, speech is detected via RMS gating, and inference items
reach the SharedInferenceQueue.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from sales_copilot.audio.replay import ReplayAudioStream
from tests.conftest_replay import make_synthetic_wav, run_bufferer_until_eof


async def test_smoke_synthetic_wav_enqueues_speech_segments(tmp_path: Path) -> None:
    """ReplayAudioStream feeds AudioBufferer; at least one speech segment queued."""
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=5.0)

    queue = await run_bufferer_until_eof(wav, flush_wait_s=2.5, timeout_s=30.0)

    count = queue.qsize()
    assert count >= 1, f"Expected ≥1 speech segment in queue, got {count}"


async def test_smoke_stream_stopped_after_bufferer_run(tmp_path: Path) -> None:
    """managed_audio_stream guarantees stop() is called after bufferer exits."""
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=2.0)

    # We can't inspect the stream after run_bufferer_until_eof completes because
    # the local reference is inside the helper.  Instead verify that creating a
    # second stream on the same file works — meaning the first was properly closed.
    await run_bufferer_until_eof(wav, flush_wait_s=1.5, timeout_s=20.0)

    # Re-open the same file — would fail on Windows if the handle were still open.
    stream2 = ReplayAudioStream(wav)
    stream2.start()
    chunk = stream2.read()
    stream2.stop()
    assert chunk is not None


async def test_smoke_queue_items_have_expected_shape(tmp_path: Path) -> None:
    """Items enqueued by AudioBufferer carry float32 audio data."""
    wav = make_synthetic_wav(tmp_path / "tone.wav", duration_s=3.0)

    queue = await run_bufferer_until_eof(wav, flush_wait_s=2.0, timeout_s=20.0)

    assert queue.qsize() >= 1
    item = await queue.get()
    assert hasattr(item, "audio")
    assert item.audio.dtype == np.float32
    assert len(item.audio) > 0
    assert item.speaker == "prospect"


async def test_smoke_short_silence_produces_no_items(tmp_path: Path) -> None:
    """A silent WAV (all zeros) produces zero items — RMS gating works."""
    import wave

    silent_wav = tmp_path / "silence.wav"
    with wave.open(str(silent_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00" * (16000 * 2 * 2))  # 2 seconds of silence

    queue = await run_bufferer_until_eof(silent_wav, flush_wait_s=2.0, timeout_s=20.0)

    assert queue.qsize() == 0, "Silent audio must not enqueue any inference items"


async def test_smoke_longer_tone_produces_multiple_segments(tmp_path: Path) -> None:
    """A 10-second unbroken tone crosses max_buffer_hard_seconds -> multiple flushes.

    Under the continuous-buffer design, the soft cap (max_buffer_seconds) only
    flushes at a real pause (not self._in_speech); a tone with zero pauses is
    not fragmented there -- that would reproduce the old onset-clipping
    hallucination bug. Only the hard cap (default 7.0s) chops a genuinely
    unbroken utterance, plus one trailing flush once the tone ends at EOF.
    """
    wav = make_synthetic_wav(tmp_path / "long_tone.wav", duration_s=10.0)

    # max_buffer_hard_seconds default is 7.0s -> one mid-speech flush at ~7s,
    # plus one trailing flush for the remaining ~3s after EOF silence-decay.
    queue = await run_bufferer_until_eof(wav, flush_wait_s=2.5, timeout_s=30.0)

    assert queue.qsize() >= 2, (
        f"Expected >=2 segments from a 10s continuous tone (hard-cap + trailing EOF flush), "
        f"got {queue.qsize()}"
    )
