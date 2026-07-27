"""Test B — real call replay: WAV fixtures through AudioBufferer pipeline.

Requires local audio fixtures in data/sessions/.  Skipped automatically in CI
where those files are absent.  Run locally with:

    pytest -m audio_fixture tests/test_replay_real_call.py -v

These tests do NOT run Whisper / MLX — they verify that real session recordings
can be read by ReplayAudioStream and that AudioBufferer detects speech segments
(via RMS gating, VAD disabled).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot.audio.replay import ReplayAudioStream
from tests.conftest_replay import run_bufferer_until_eof

_SESSION_SHORT = Path("data/sessions/2026-05-03T10-29-05")
_SESSION_LONG = Path("data/sessions/2026-05-01T15-52-26")


# ---------------------------------------------------------------------------
# Short session (5-11 min)
# ---------------------------------------------------------------------------


@pytest.mark.audio_fixture
async def test_real_call_short_self_stream_reads_without_error() -> None:
    """self.wav from the short session is readable and reaches EOF cleanly."""
    wav = _SESSION_SHORT / "self.wav"
    if not wav.exists():
        pytest.skip(f"audio fixture not present: {wav}")

    stream = ReplayAudioStream(wav, chunk_size_frames=512, real_time=False)
    stream.start()
    chunks_read = 0
    while True:
        chunk = stream.read()
        if chunk is None:
            break
        chunks_read += 1
    stream.stop()

    assert stream.at_eof
    assert chunks_read > 0


@pytest.mark.audio_fixture
async def test_real_call_short_self_stream_enqueues_speech(tmp_path: Path) -> None:
    """Replaying self.wav through AudioBufferer produces ≥10 speech segments."""
    wav = _SESSION_SHORT / "self.wav"
    if not wav.exists():
        pytest.skip(f"audio fixture not present: {wav}")

    queue = await run_bufferer_until_eof(
        wav,
        speaker="self",
        flush_wait_s=3.0,
        timeout_s=120.0,
    )

    count = queue.qsize()
    assert count >= 10, (
        f"Expected ≥10 speech segments from real call self.wav, got {count}. "
        "Possible causes: VAD is blocking, file is silent, or RMS gating is too strict."
    )


@pytest.mark.audio_fixture
async def test_real_call_short_prospect_stream_enqueues_speech(tmp_path: Path) -> None:
    """Replaying prospect.wav through AudioBufferer produces ≥10 speech segments."""
    wav = _SESSION_SHORT / "prospect.wav"
    if not wav.exists():
        pytest.skip(f"audio fixture not present: {wav}")

    queue = await run_bufferer_until_eof(
        wav,
        speaker="prospect",
        flush_wait_s=3.0,
        timeout_s=120.0,
    )

    count = queue.qsize()
    assert count >= 10, (
        f"Expected ≥10 speech segments from real call prospect.wav, got {count}."
    )


@pytest.mark.audio_fixture
async def test_real_call_short_no_sigabrt() -> None:
    """Running both streams sequentially completes without fatal errors."""
    self_wav = _SESSION_SHORT / "self.wav"
    prospect_wav = _SESSION_SHORT / "prospect.wav"

    if not self_wav.exists() or not prospect_wav.exists():
        pytest.skip(f"audio fixture incomplete: {_SESSION_SHORT}")

    # Run self then prospect sequentially — proves no resource leak.
    queue_self = await run_bufferer_until_eof(self_wav, speaker="self", timeout_s=120.0)
    queue_prospect = await run_bufferer_until_eof(prospect_wav, speaker="prospect", timeout_s=120.0)

    assert queue_self.qsize() >= 1
    assert queue_prospect.qsize() >= 1


# ---------------------------------------------------------------------------
# Long session (17 min) — read-only sanity check
# ---------------------------------------------------------------------------


@pytest.mark.audio_fixture
async def test_real_call_long_session_readable() -> None:
    """17-min self.wav opens, reads first chunk, closes cleanly."""
    wav = _SESSION_LONG / "self.wav"
    if not wav.exists():
        pytest.skip(f"audio fixture not present: {wav}")

    stream = ReplayAudioStream(wav, chunk_size_frames=512, real_time=False)
    stream.start()
    chunk = stream.read()
    stream.stop()

    assert chunk is not None, "First chunk from long session WAV must not be None"
    assert len(chunk) == 512
