from __future__ import annotations

import asyncio

import numpy as np
import pytest

from sales_copilot.modules.transcriber.audio_bufferer import AudioBufferer, TurnState
from sales_copilot.modules.transcriber.inference_queue import SharedInferenceQueue

_SAMPLE_RATE = 16000
_SPEECH_RMS = 0.1


def _speech_frame(samples: int = 1600) -> np.ndarray:
    """Return a mono float32 frame with RMS well above the default min_rms."""
    return np.full(samples, _SPEECH_RMS, dtype=np.float32)


def _silent_frame(samples: int = 1600) -> np.ndarray:
    return np.zeros(samples, dtype=np.float32)


class _FakeStream:
    def __init__(self, frames: list[np.ndarray | None]) -> None:
        self._frames = list(frames)
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read(self) -> np.ndarray | None:
        if self._frames:
            return self._frames.pop(0)
        return None


def _make_bufferer(
    stream: _FakeStream,
    queue: SharedInferenceQueue,
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_buffer_seconds: float = 4.0,
    max_buffer_hard_seconds: float = 7.0,
    silence_gap_seconds: float = 0.05,
    min_segment_seconds: float = 0.0,
    long_silence_escape_seconds: float = 1.0,
    pre_roll_ms: int = 0,
    hangover_ms: int = 0,
    vad_enter: float = 0.5,
    vad_exit: float = 0.35,
    min_rms: float = 0.005,
    turn_backchannel_guard_ms: int = 200,
    speaker: str = "prospect",
    priority: int = 0,
    transcribe_live: bool = True,
    turn_state: TurnState | None = None,
) -> AudioBufferer:
    """Build an AudioBufferer with Silero disabled and fast-flush defaults.

    The production defaults (silence_gap=1.0s, min_segment=1.2s, ...) are tuned
    for real speech cadence, which would make every test wait a second or more.
    These test defaults keep the same *decision logic* but flush quickly so
    tests stay fast and deterministic; individual tests override whichever
    parameter they are exercising.
    """
    monkeypatch.setattr(AudioBufferer, "_load_vad", staticmethod(lambda: None))
    return AudioBufferer(
        audio_stream=stream,
        queue=queue,
        priority=priority,
        speaker=speaker,
        sample_rate=_SAMPLE_RATE,
        max_buffer_seconds=max_buffer_seconds,
        max_buffer_hard_seconds=max_buffer_hard_seconds,
        silence_gap_seconds=silence_gap_seconds,
        min_segment_seconds=min_segment_seconds,
        long_silence_escape_seconds=long_silence_escape_seconds,
        pre_roll_ms=pre_roll_ms,
        hangover_ms=hangover_ms,
        vad_enter=vad_enter,
        vad_exit=vad_exit,
        min_rms=min_rms,
        turn_backchannel_guard_ms=turn_backchannel_guard_ms,
        transcribe_live=transcribe_live,
        turn_state=turn_state,
    )


async def _run_until_queue_has(
    bufferer: AudioBufferer,
    queue: SharedInferenceQueue,
    n: int,
    timeout: float = 3.0,
) -> None:
    stop_event = asyncio.Event()

    async def _watch() -> None:
        while queue.qsize() < n:
            await asyncio.sleep(0.01)
        stop_event.set()

    await asyncio.wait_for(
        asyncio.gather(bufferer.run(stop_event), _watch()),
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Basic flush triggers
# ---------------------------------------------------------------------------


async def test_bufferer_silence_after_speech_triggers_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    stream = _FakeStream([_speech_frame(800)])
    bufferer = _make_bufferer(stream, queue, monkeypatch)

    await _run_until_queue_has(bufferer, queue, 1)

    assert queue.qsize() == 1


async def test_bufferer_frames_below_min_rms_are_not_buffered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    # Silent frame (RMS=0) below default min_rms=0.005
    stream = _FakeStream([_silent_frame(1600)])
    bufferer = _make_bufferer(stream, queue, monkeypatch)

    stop_event = asyncio.Event()

    async def _drain_then_stop() -> None:
        await asyncio.sleep(0.2)
        stop_event.set()

    await asyncio.wait_for(
        asyncio.gather(bufferer.run(stop_event), _drain_then_stop()),
        timeout=2.0,
    )

    assert queue.qsize() == 0


async def test_bufferer_flush_enqueues_item_with_correct_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    stream = _FakeStream([_speech_frame(800)])
    bufferer = _make_bufferer(stream, queue, monkeypatch, speaker="prospect", priority=0)

    await _run_until_queue_has(bufferer, queue, 1)

    item = await queue.get()
    assert item.speaker == "prospect"
    assert item.priority == 0
    assert item.start_ms >= 0
    assert item.end_ms > item.start_ms


async def test_bufferer_stop_event_flushes_pending_speech(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    stream = _FakeStream([_speech_frame(800)])
    bufferer = _make_bufferer(stream, queue, monkeypatch)

    stop_event = asyncio.Event()

    async def _watch() -> None:
        while queue.qsize() < 1:
            await asyncio.sleep(0.01)
        stop_event.set()

    await asyncio.wait_for(
        asyncio.gather(bufferer.run(stop_event), _watch()),
        timeout=3.0,
    )

    assert queue.qsize() >= 1


async def test_bufferer_multiple_speech_segments_across_real_silence_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two speech bursts separated by a genuine silence gap produce two items.

    A brief single-frame gap does NOT fragment a segment under the continuous-
    buffer design (that is the point of the hangover/carry-forward fix) — the
    gap has to actually elapse in wall-clock time past hangover+silence_gap.
    """
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    stream = _FakeStream([_speech_frame(800)])
    bufferer = _make_bufferer(stream, queue, monkeypatch)

    stop_event = asyncio.Event()
    task = asyncio.create_task(bufferer.run(stop_event))
    try:
        await asyncio.wait_for(_poll_until(lambda: queue.qsize() >= 1), timeout=2.0)
        assert queue.qsize() == 1

        stream._frames.append(_speech_frame(800))
        await asyncio.wait_for(_poll_until(lambda: queue.qsize() >= 2), timeout=2.0)
        assert queue.qsize() == 2
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)


async def test_bufferer_with_transcribe_live_false_does_not_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    stream = _FakeStream([_speech_frame(1600)])
    bufferer = _make_bufferer(stream, queue, monkeypatch, transcribe_live=False)

    stop_event = asyncio.Event()

    async def _drain_then_stop() -> None:
        await asyncio.sleep(0.3)
        stop_event.set()

    await asyncio.wait_for(
        asyncio.gather(bufferer.run(stop_event), _drain_then_stop()),
        timeout=2.0,
    )

    assert queue.qsize() == 0


async def test_bufferer_with_transcribe_live_true_enqueues_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    stream = _FakeStream([_speech_frame(1600)])
    bufferer = _make_bufferer(stream, queue, monkeypatch, transcribe_live=True)

    await _run_until_queue_has(bufferer, queue, 1)

    assert queue.qsize() == 1


async def test_bufferer_recorder_still_called_when_transcribe_live_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sales_copilot.modules.transcriber.audio_bufferer as bufferer_module

    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    stream = _FakeStream([_speech_frame(1600)])
    bufferer = _make_bufferer(stream, queue, monkeypatch, transcribe_live=False)

    write_calls: list[tuple[str, np.ndarray, int]] = []

    class _FakeRecorder:
        def write_chunk(self, label: str, samples: np.ndarray, timestamp_ms: int) -> None:
            write_calls.append((label, samples, timestamp_ms))

    monkeypatch.setattr(bufferer_module, "get_active_recorder", lambda: _FakeRecorder())

    stop_event = asyncio.Event()

    async def _drain_then_stop() -> None:
        await asyncio.sleep(0.3)
        stop_event.set()

    await asyncio.wait_for(
        asyncio.gather(bufferer.run(stop_event), _drain_then_stop()),
        timeout=2.0,
    )

    assert len(write_calls) > 0
    assert queue.qsize() == 0


# ---------------------------------------------------------------------------
# Continuous-buffer segmentation (P0 hallucination fix)
# ---------------------------------------------------------------------------


async def test_bufferer_sub_vad_dip_does_not_fragment_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brief sub-VAD dip inside an utterance stays in ONE continuous segment.

    Feeding Whisper only VAD-positive frames (voiced-only, discontinuous audio)
    was the deepest hallucination cause. This asserts the fix: the dip's
    samples are retained in the flushed buffer, not skipped or fragmented into
    a separate item.
    """
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    voiced = _speech_frame(800)  # 0.05s
    dip = _silent_frame(160)  # 0.01s, well under a 0.2s hangover
    stream = _FakeStream([voiced, dip, voiced])
    bufferer = _make_bufferer(stream, queue, monkeypatch, hangover_ms=200)

    await _run_until_queue_has(bufferer, queue, 1)

    assert queue.qsize() == 1
    item = await queue.get()
    assert len(item.audio) == 800 + 160 + 800


async def test_bufferer_min_segment_floor_carries_short_speech_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A segment under the min-segment floor is held, not dropped or flushed.

    It only reaches the queue once later speech pushes it past the floor, and
    the flushed audio includes the original short burst (proving carry-forward
    rather than the short fragment being discarded).
    """
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    stream = _FakeStream([_speech_frame(800)])  # 0.05s, below the floor
    bufferer = _make_bufferer(
        stream,
        queue,
        monkeypatch,
        min_segment_seconds=0.2,
        silence_gap_seconds=0.05,
        long_silence_escape_seconds=3.0,
    )

    stop_event = asyncio.Event()
    task = asyncio.create_task(bufferer.run(stop_event))
    try:
        await asyncio.sleep(0.3)
        assert queue.qsize() == 0, "short segment under the floor must not flush on its own"

        stream._frames.append(_speech_frame(4000))  # 0.25s more -> total 0.3s clears the floor
        await asyncio.wait_for(_poll_until(lambda: queue.qsize() >= 1), timeout=2.0)
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=3.0)

    assert queue.qsize() == 1
    item = await queue.get()
    assert len(item.audio) == 800 + 4000


async def test_bufferer_hard_cap_flushes_mid_speech_without_dropping_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous speech past max_buffer_hard_seconds flushes mid-utterance.

    The segment carries forward (does not wait for silence) and no audio is
    dropped or duplicated across the flush boundary.
    """
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    frames = [_speech_frame(800) for _ in range(4)]  # 4 * 0.05s = 0.2s continuous
    stream = _FakeStream(frames)
    bufferer = _make_bufferer(
        stream,
        queue,
        monkeypatch,
        # max_buffer_hard_seconds is clamped to >= max_buffer_seconds (the hard
        # cap is always a ceiling above the soft one), so both must be set to
        # the same small value here to actually trip the hard cap early.
        max_buffer_seconds=0.1,
        max_buffer_hard_seconds=0.1,  # trips every 2 frames
        silence_gap_seconds=10.0,
        long_silence_escape_seconds=10.0,
    )

    await _run_until_queue_has(bufferer, queue, 2, timeout=5.0)

    item1 = await queue.get()
    item2 = await queue.get()
    assert len(item1.audio) + len(item2.audio) == 800 * 4
    assert item2.start_ms == item1.end_ms, "no gap/drop across the hard-cap flush boundary"


async def test_bufferer_pre_roll_prepends_onset_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio just before speech onset is prepended so the first word is not clipped."""
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    # 3 sub-threshold frames (2400 samples); a 100ms/1600-sample pre-roll ring
    # keeps only the most recent 1600 samples (the last two frames).
    stream = _FakeStream(
        [_silent_frame(800), _silent_frame(800), _silent_frame(800), _speech_frame(800)]
    )
    bufferer = _make_bufferer(stream, queue, monkeypatch, pre_roll_ms=100)

    await _run_until_queue_has(bufferer, queue, 1)

    item = await queue.get()
    assert len(item.audio) == 1600 + 800


async def test_bufferer_turn_flush_via_shared_turn_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other speaker sustaining the floor flushes this speaker's segment.

    Backed by a TurnState shared across two AudioBufferers on the same event
    loop (self + prospect), so a genuine turn hand-off does not have to wait
    for this speaker's own silence_gap to elapse.
    """
    queue: SharedInferenceQueue = SharedInferenceQueue(max_size=8)
    turn_state = TurnState()

    a_stream = _FakeStream([_speech_frame(800)])  # speaks briefly, then goes quiet
    b_stream = _FakeStream([])  # starts silent; takes the floor later

    bufferer_a = _make_bufferer(
        a_stream,
        queue,
        monkeypatch,
        speaker="self",
        min_segment_seconds=0.05,
        silence_gap_seconds=5.0,
        long_silence_escape_seconds=5.0,
        max_buffer_seconds=5.0,
        max_buffer_hard_seconds=5.0,
        turn_backchannel_guard_ms=100,
        turn_state=turn_state,
    )
    bufferer_b = _make_bufferer(
        b_stream,
        queue,
        monkeypatch,
        speaker="prospect",
        min_segment_seconds=5.0,
        silence_gap_seconds=5.0,
        long_silence_escape_seconds=5.0,
        max_buffer_seconds=5.0,
        max_buffer_hard_seconds=5.0,
        turn_backchannel_guard_ms=100,
        # The fake stream delivers all 5 frames near-instantly (no real-time
        # pacing), unlike a real mic callback. A longer hangover keeps B's
        # turn_state onset alive long enough for A to observe it sustained
        # past turn_backchannel_guard_ms, instead of decaying back to
        # inactive within microseconds of the burst draining.
        hangover_ms=300,
        turn_state=turn_state,
    )

    stop_event = asyncio.Event()
    task_a = asyncio.create_task(bufferer_a.run(stop_event))
    task_b = asyncio.create_task(bufferer_b.run(stop_event))

    try:
        await asyncio.sleep(0.15)
        assert queue.qsize() == 0, "self's segment must not flush on silence_gap alone (set to 5s)"

        b_stream._frames.extend([_speech_frame(800)] * 5)
        await asyncio.wait_for(_poll_until(lambda: queue.qsize() >= 1), timeout=2.0)
    finally:
        stop_event.set()
        await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=3.0)

    speakers_flushed = []
    while queue.qsize() > 0:
        speakers_flushed.append((await queue.get()).speaker)
    assert "self" in speakers_flushed


async def test_turn_state_other_sustained_ignores_short_backchannel() -> None:
    """A quick backchannel ('ja'/'hm') below the guard threshold does not count."""
    turn_state = TurnState()
    turn_state.set_active("prospect", 100.0)

    assert turn_state.other_sustained("self", 100.3, min_dur_s=0.7) is False
    assert turn_state.other_sustained("self", 100.8, min_dur_s=0.7) is True


async def _poll_until(predicate, interval: float = 0.01) -> None:
    while not predicate():
        await asyncio.sleep(interval)
