"""Tap-health: "no tap" must never look like "tap open, source silent"."""

from __future__ import annotations

import asyncio
import io
import logging
import time

import numpy as np
import pytest

from sales_copilot.audio.capture import AudioConfig, AudioTeeStream, check_streams_liveness
from sales_copilot.audio.tap_health import (
    DEFAULT_SIGNAL_FLOOR_RMS,
    DEFAULT_SIGNAL_LOST_WARN_SECONDS,
    SIGNAL_LOST_MARKER,
    TAP_STATUS_MESSAGE_TYPE,
    TRANSCRIPT_MARKER_TYPE,
    DashboardTapState,
    TapAttachState,
    TapHealthTracker,
    TapSignalState,
    dashboard_tap_state,
    describe_silent_stream,
    evaluate_tap_health,
    monitor_tap_health,
    resolve_signal_floor_rms,
    resolve_signal_lost_seconds,
    stream_tap_health,
    tap_status_message,
)


class _Clock:
    """Injectable monotonic clock so every timing assertion is deterministic."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _loud(samples: int = 64) -> np.ndarray:
    return np.full((samples, 1), 0.2, dtype=np.float32)


def _digital_silence(samples: int = 64) -> np.ndarray:
    return np.zeros((samples, 1), dtype=np.float32)


def _quiet_but_real(samples: int = 64) -> np.ndarray:
    """Audible but soft: above the digital-silence floor, below the VAD speech floor."""

    return np.full((samples, 1), 0.002, dtype=np.float32)


def _room_tone(samples: int = 64) -> np.ndarray:
    """What a pause in a real call looks like: below the floor, but not zero.

    The 2026-09-10 recording measured 11 to 175 RMS on the int16 scale in the
    pauses between sentences. Nothing a live capture produces is bit-for-bit
    zero, which is exactly why "exactly zero" can be trusted as a fault signal.
    """

    return np.full((samples, 1), 0.0001, dtype=np.float32)


# --- the three states -------------------------------------------------------


def test_never_started_is_no_tap() -> None:
    health = TapHealthTracker(clock=_Clock()).snapshot()
    assert health.state is TapSignalState.NO_TAP
    assert health.attach_state is TapAttachState.NOT_STARTED


def test_attach_failure_is_no_tap_and_already_warned() -> None:
    tracker = TapHealthTracker(clock=_Clock())
    tracker.mark_attach_failed("binary ontbreekt")

    health = tracker.snapshot()
    assert health.state is TapSignalState.NO_TAP
    assert health.attach_state is TapAttachState.ATTACH_FAILED
    assert health.detail == "binary ontbreekt"
    assert health.warning_emitted is True


def test_attached_without_frames_is_its_own_state() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    clock.advance(12.0)

    health = tracker.snapshot()
    assert health.state is TapSignalState.ATTACHED_NO_FRAMES
    assert health.attached_seconds == pytest.approx(12.0)
    assert health.seconds_without_frames == pytest.approx(12.0)


def test_attached_with_only_zero_frames_is_attached_silent() -> None:
    """The state the old code could not see: the tap works, the source is mute."""

    tracker = TapHealthTracker(clock=_Clock())
    tracker.mark_attached("audiotee:all")
    for _ in range(50):
        assert tracker.observe(_digital_silence()) is False

    health = tracker.snapshot()
    assert health.state is TapSignalState.ATTACHED_SILENT
    assert health.chunks == 50
    assert health.signal_chunks == 0
    assert health.seconds_since_signal is None
    assert health.peak_dbfs == pytest.approx(-120.0)


def test_first_audible_frame_reports_once() -> None:
    tracker = TapHealthTracker(clock=_Clock())
    tracker.mark_attached("audiotee:all")

    assert tracker.observe(_digital_silence()) is False
    assert tracker.observe(_loud()) is True
    assert tracker.observe(_loud()) is False

    health = tracker.snapshot()
    assert health.state is TapSignalState.ATTACHED_SIGNAL
    assert health.signal_chunks == 2
    assert health.peak_dbfs > -20.0


def test_quiet_speech_still_counts_as_signal() -> None:
    """The floor is digital silence, not speech: a soft talker is not a dead tap."""

    tracker = TapHealthTracker(clock=_Clock())
    tracker.mark_attached("audiotee:all")
    assert tracker.observe(_quiet_but_real()) is True
    assert tracker.snapshot().state is TapSignalState.ATTACHED_SIGNAL


def test_detach_after_delivering_audio_is_no_tap() -> None:
    tracker = TapHealthTracker(clock=_Clock())
    tracker.mark_attached("audiotee:all")
    tracker.observe(_loud())
    tracker.mark_detached("subprocess gestopt")

    health = tracker.snapshot()
    assert health.state is TapSignalState.NO_TAP
    assert health.attach_state is TapAttachState.DETACHED
    assert health.warning_emitted is False


def test_signal_floor_is_configurable(monkeypatch) -> None:
    assert resolve_signal_floor_rms() == DEFAULT_SIGNAL_FLOOR_RMS
    monkeypatch.setenv("AUDIO_TAP_SIGNAL_FLOOR_RMS", "0.05")
    assert resolve_signal_floor_rms() == pytest.approx(0.05)

    tracker = TapHealthTracker(clock=_Clock())
    tracker.mark_attached("audiotee:all")
    # 0.002 cleared the default floor; it must not clear a floor of 0.05.
    assert tracker.observe(_quiet_but_real()) is False
    assert tracker.snapshot().state is TapSignalState.ATTACHED_SILENT


# --- warning discipline -----------------------------------------------------


def _health(tracker: TapHealthTracker):
    return tracker.snapshot()


def test_quiet_moment_in_a_real_call_never_warns() -> None:
    """The constraint: a pause in a sales call must not train the operator to ignore us.

    A tap that has carried audio stays quiet for an hour without producing a
    single warning, because "nobody is talking right now" is the conversation,
    not a fault. The pause is room tone, not zeros: that is what a live capture
    of a silent room actually delivers.
    """

    clock = _Clock()
    tracker = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    tracker.mark_attached("audiotee:all")
    tracker.observe(_loud())
    for _ in range(600):
        clock.advance(6.0)
        tracker.observe(_room_tone())

    health = _health(tracker)
    assert health.state is TapSignalState.ATTACHED_SIGNAL
    assert health.seconds_since_signal == pytest.approx(3600.0, abs=0.5)
    assert health.zero_run_seconds is None
    assert (
        evaluate_tap_health(
            "prospect",
            health,
            no_frames_warn_seconds=10.0,
            silent_warn_seconds=120.0,
        )
        is None
    )


def test_attached_silent_warns_only_after_the_threshold() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    for _ in range(10):
        tracker.observe(_digital_silence())

    clock.advance(119.0)
    assert (
        evaluate_tap_health("prospect", _health(tracker), no_frames_warn_seconds=10.0, silent_warn_seconds=120.0)
        is None
    )

    clock.advance(2.0)
    message = evaluate_tap_health("prospect", _health(tracker), no_frames_warn_seconds=10.0, silent_warn_seconds=120.0)
    assert message is not None
    # Names the two causes the objective asks for, and clears the tap itself.
    assert "ander apparaat" in message
    assert "gemute" in message
    assert "De tap zelf is in orde." in message


def test_attached_no_frames_warns_and_says_it_is_not_silence() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    clock.advance(11.0)

    message = evaluate_tap_health("prospect", _health(tracker), no_frames_warn_seconds=10.0, silent_warn_seconds=120.0)
    assert message is not None
    assert "geen enkel audioframe" in message
    assert "Dat is geen stilte" in message


def test_detached_mid_call_warns() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    tracker.observe(_loud())
    tracker.mark_detached("subprocess gestopt")

    message = evaluate_tap_health("prospect", _health(tracker), no_frames_warn_seconds=10.0, silent_warn_seconds=120.0)
    assert message is not None
    assert "weggevallen" in message


def test_stream_owned_warning_suppresses_the_heartbeat_warning() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    tracker.observe(_loud())
    tracker.mark_detached("subprocess gestopt", already_warned=True)

    assert (
        evaluate_tap_health("prospect", _health(tracker), no_frames_warn_seconds=10.0, silent_warn_seconds=120.0)
        is None
    )


# --- mid-call signal loss ---------------------------------------------------
#
# Reconstructed from a 40-minute Teams call on 2026-09-10. The system-audio
# channel carried signal for 16 minutes, then went to exactly zero for the
# remaining 23. Nothing warned, and the transcript read as if the prospect had
# stopped talking.


def _run_the_2026_09_10_call(tracker: TapHealthTracker, clock: _Clock, *, zero_seconds: float) -> None:
    """16 minutes of real conversation, then a tap that writes only zeros."""

    for _ in range(160):
        clock.advance(6.0)
        tracker.observe(_loud())
        clock.advance(0.1)
        tracker.observe(_room_tone())
    frames = max(1, int(zero_seconds / 0.1))
    for _ in range(frames):
        clock.advance(0.1)
        tracker.observe(_digital_silence())


def test_tap_that_stops_mid_call_is_its_own_state() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    tracker.mark_attached("audiotee:all")
    _run_the_2026_09_10_call(tracker, clock, zero_seconds=1380.0)

    health = _health(tracker)
    assert health.state is TapSignalState.ATTACHED_SIGNAL_LOST
    assert health.signal_chunks > 0
    assert health.zero_run_seconds == pytest.approx(1379.9, abs=0.5)
    assert health.zero_run_chunks == 13800


def test_signal_loss_warns_only_after_the_threshold() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    tracker.mark_attached("audiotee:all")
    _run_the_2026_09_10_call(tracker, clock, zero_seconds=29.0)

    assert _health(tracker).state is TapSignalState.ATTACHED_SIGNAL
    assert (
        evaluate_tap_health("prospect", _health(tracker), no_frames_warn_seconds=10.0, silent_warn_seconds=120.0)
        is None
    )

    clock.advance(2.0)
    tracker.observe(_digital_silence())
    message = evaluate_tap_health("prospect", _health(tracker), no_frames_warn_seconds=10.0, silent_warn_seconds=120.0)
    assert message is not None
    assert "exacte nullen" in message
    assert "ruisvloer" in message
    assert "gaat verloren" in message


def test_one_non_zero_sample_ends_the_run() -> None:
    """The discriminator, isolated: a single frame with a noise floor clears it."""

    clock = _Clock()
    tracker = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    tracker.mark_attached("audiotee:all")
    _run_the_2026_09_10_call(tracker, clock, zero_seconds=600.0)
    assert _health(tracker).state is TapSignalState.ATTACHED_SIGNAL_LOST

    clock.advance(0.1)
    tracker.observe(_room_tone())

    health = _health(tracker)
    assert health.state is TapSignalState.ATTACHED_SIGNAL
    assert health.zero_run_seconds is None
    assert health.zero_run_chunks == 0


def test_a_tap_that_never_carried_signal_keeps_the_old_message() -> None:
    """The pre-existing attached_silent path must be untouched by this fix."""

    clock = _Clock()
    tracker = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    tracker.mark_attached("audiotee:all")
    for _ in range(1300):
        clock.advance(0.1)
        tracker.observe(_digital_silence())

    health = _health(tracker)
    assert health.state is TapSignalState.ATTACHED_SILENT
    message = evaluate_tap_health("prospect", health, no_frames_warn_seconds=10.0, silent_warn_seconds=120.0)
    assert message is not None
    assert "ander apparaat" in message
    assert "De tap zelf is in orde." in message
    assert "exacte nullen" not in message


def test_a_live_peer_channel_sharpens_the_message() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    tracker.mark_attached("audiotee:all")
    _run_the_2026_09_10_call(tracker, clock, zero_seconds=60.0)
    health = _health(tracker)

    alone = evaluate_tap_health("prospect", health, no_frames_warn_seconds=10.0, silent_warn_seconds=120.0)
    with_peer = evaluate_tap_health(
        "prospect",
        health,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
        peer_label="self",
    )

    assert alone is not None and "self-kanaal" not in alone
    assert with_peer is not None
    assert "Het self-kanaal draagt in diezelfde periode wel signaal." in with_peer
    assert "defect in deze tap" in with_peer


def test_signal_lost_threshold_is_configurable(monkeypatch) -> None:
    assert resolve_signal_lost_seconds() == DEFAULT_SIGNAL_LOST_WARN_SECONDS
    monkeypatch.setenv("AUDIO_TAP_SIGNAL_LOST_WARN_SECONDS", "5")
    assert resolve_signal_lost_seconds() == pytest.approx(5.0)

    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    _run_the_2026_09_10_call(tracker, clock, zero_seconds=6.0)
    assert _health(tracker).state is TapSignalState.ATTACHED_SIGNAL_LOST


# --- the heartbeat ----------------------------------------------------------


class _FakeTapStream:
    def __init__(self, tracker: TapHealthTracker) -> None:
        self._tracker = tracker

    def tap_health(self):
        return self._tracker.snapshot()


class _NoHealthStream:
    chunks_received = 0
    device_label = "ULT WEAR"


def _run_monitor(streams_and_labels, *, beats: int, events: list[dict], **kwargs) -> None:
    async def _broadcast(payload: dict) -> None:
        events.append(payload)

    async def _run() -> None:
        await monitor_tap_health(
            streams_and_labels,
            _broadcast,
            asyncio.Event(),
            interval_seconds=0.001,
            max_beats=beats,
            **kwargs,
        )

    asyncio.run(_run())


def test_heartbeat_logs_state_at_info_without_raising_the_log_level(caplog) -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    tracker.observe(_loud())

    events: list[dict] = []
    with caplog.at_level(logging.INFO, logger="sales_copilot.audio.tap_health"):
        _run_monitor([(_FakeTapStream(tracker), "prospect")], beats=2, events=events)

    heartbeats = [r for r in caplog.records if "Audio tap heartbeat" in r.getMessage()]
    assert len(heartbeats) == 2
    assert all(r.levelno == logging.INFO for r in heartbeats)
    assert "state=attached_signal" in heartbeats[0].getMessage()
    assert "signal_chunks=1" in heartbeats[0].getMessage()
    # A healthy tap produces heartbeats and nothing else.
    assert events == []


def test_heartbeat_warns_once_per_stream() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    clock.advance(30.0)

    events: list[dict] = []
    _run_monitor(
        [(_FakeTapStream(tracker), "prospect")],
        beats=5,
        events=events,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
    )

    assert len(events) == 1
    assert events[0]["type"] == "audio_warning"
    assert events[0]["stream"] == "prospect"
    assert events[0]["tap_state"] == TapSignalState.ATTACHED_NO_FRAMES.value


def test_heartbeat_respects_labels_the_warmup_check_already_reported() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    clock.advance(30.0)

    events: list[dict] = []
    _run_monitor(
        [(_FakeTapStream(tracker), "prospect")],
        beats=3,
        events=events,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
        already_reported={"prospect"},
    )

    assert events == []


def test_heartbeat_skips_streams_that_do_not_report_health(caplog) -> None:
    events: list[dict] = []
    with caplog.at_level(logging.INFO, logger="sales_copilot.audio.tap_health"):
        _run_monitor([(_NoHealthStream(), "self")], beats=2, events=events)

    assert events == []
    assert not [r for r in caplog.records if "Audio tap heartbeat" in r.getMessage()]


def test_stream_tap_health_returns_none_for_plain_streams() -> None:
    assert stream_tap_health(_NoHealthStream()) is None


def _lost_and_healthy_pair() -> tuple[_Clock, TapHealthTracker, TapHealthTracker]:
    """The 2026-09-10 shape: prospect tap goes to zeros, mic keeps carrying audio."""

    clock = _Clock()
    lost = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    alive = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    lost.mark_attached("audiotee:all")
    alive.mark_attached("ULT WEAR")
    for _ in range(160):
        clock.advance(6.0)
        lost.observe(_loud())
        alive.observe(_loud())
        clock.advance(0.1)
        lost.observe(_room_tone())
        alive.observe(_room_tone())
    for _ in range(600):
        clock.advance(0.1)
        lost.observe(_digital_silence())
        alive.observe(_loud())
    return clock, lost, alive


def test_heartbeat_names_the_live_channel_when_one_stream_loses_signal() -> None:
    _clock, lost, alive = _lost_and_healthy_pair()

    events: list[dict] = []
    _run_monitor(
        [(_FakeTapStream(lost), "prospect"), (_FakeTapStream(alive), "self")],
        beats=2,
        events=events,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
    )

    warnings = [e for e in events if e["type"] == "audio_warning"]
    assert len(warnings) == 1
    assert warnings[0]["stream"] == "prospect"
    assert warnings[0]["tap_state"] == TapSignalState.ATTACHED_SIGNAL_LOST.value
    assert "Het self-kanaal draagt in diezelfde periode wel signaal." in warnings[0]["message"]


def test_heartbeat_writes_a_transcript_marker_at_the_moment_of_loss() -> None:
    """The transcript must say the audio stopped, not imply the speaker did."""

    clock = _Clock()
    lost = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    lost.mark_attached("audiotee:all")
    for _ in range(160):
        clock.advance(6.0)
        lost.observe(_loud())
    # 60 seconds of zeros, so the marker has to be placed a minute back.
    for _ in range(600):
        clock.advance(0.1)
        lost.observe(_digital_silence())

    markers: list[dict] = []
    events: list[dict] = []

    async def _broadcast(payload: dict) -> None:
        events.append(payload)

    async def _marker(payload: dict) -> None:
        markers.append(payload)

    async def _run() -> None:
        await monitor_tap_health(
            [(_FakeTapStream(lost), "prospect")],
            _broadcast,
            asyncio.Event(),
            interval_seconds=0.001,
            max_beats=2,
            no_frames_warn_seconds=10.0,
            silent_warn_seconds=120.0,
            broadcast_marker=_marker,
            clock=clock,
        )

    asyncio.run(_run())

    # One marker, not one per beat: it rides the same once-per-stream latch as
    # the warning.
    assert len(markers) == 1
    marker = markers[0]
    assert marker["type"] == TRANSCRIPT_MARKER_TYPE
    assert marker["marker"] == SIGNAL_LOST_MARKER
    assert marker["stream"] == "prospect"
    assert marker["speaker"] == "system"
    assert "audio ontbreekt vanaf hier" in marker["text"]
    assert marker["start_ms"] == marker["end_ms"]
    # The clock never moves inside the monitor here, so "now" is the end of the
    # zero run and the marker must sit ~60s (59.9s) before it, at 0.
    assert marker["start_ms"] == 0


def test_transcript_marker_timestamp_rewinds_to_where_the_audio_went() -> None:
    """The marker belongs where the audio stopped, not where the heartbeat looked.

    Five minutes into the monitor's own run, with a 60-second zero run behind
    it: the line goes at 04:00, which is the last moment the transcript above it
    can be trusted.
    """

    _clock, lost, _alive = _lost_and_healthy_pair()
    readings = iter([0.0, 300.0])

    markers: list[dict] = []

    async def _broadcast(payload: dict) -> None:
        return

    async def _marker(payload: dict) -> None:
        markers.append(payload)

    async def _run() -> None:
        await monitor_tap_health(
            [(_FakeTapStream(lost), "prospect")],
            _broadcast,
            asyncio.Event(),
            interval_seconds=0.001,
            max_beats=1,
            no_frames_warn_seconds=10.0,
            silent_warn_seconds=120.0,
            broadcast_marker=_marker,
            clock=lambda: next(readings),
        )

    asyncio.run(_run())

    assert len(markers) == 1
    # 300s elapsed minus the 59.9s the tap had already been writing zeros.
    assert markers[0]["start_ms"] == 240100


def test_no_marker_without_a_marker_broadcaster() -> None:
    """Every existing caller passes none; the warning path must not depend on it."""

    _clock, lost, _alive = _lost_and_healthy_pair()

    events: list[dict] = []
    _run_monitor(
        [(_FakeTapStream(lost), "prospect")],
        beats=2,
        events=events,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
    )

    assert len(events) == 1
    assert events[0]["tap_state"] == TapSignalState.ATTACHED_SIGNAL_LOST.value


def test_a_failing_marker_broadcast_does_not_break_the_monitor(caplog) -> None:
    _clock, lost, _alive = _lost_and_healthy_pair()

    async def _broadcast(payload: dict) -> None:
        return

    async def _marker(payload: dict) -> None:
        raise RuntimeError("hub down")

    async def _run() -> None:
        await monitor_tap_health(
            [(_FakeTapStream(lost), "prospect")],
            _broadcast,
            asyncio.Event(),
            interval_seconds=0.001,
            max_beats=2,
            no_frames_warn_seconds=10.0,
            silent_warn_seconds=120.0,
            broadcast_marker=_marker,
        )

    with caplog.at_level(logging.WARNING, logger="sales_copilot.audio.tap_health"):
        asyncio.run(_run())

    assert [r for r in caplog.records if "transcript marker" in r.getMessage()]


# --- the warmup liveness message -------------------------------------------


def test_liveness_message_names_the_tap_instead_of_the_microphone() -> None:
    tracker = TapHealthTracker(clock=_Clock())
    tracker.mark_attach_failed("binary ontbreekt")
    message = describe_silent_stream("prospect", "audiotee:all", tracker.snapshot())

    assert "niet geopend" in message
    assert "geen stilte" in message
    assert "microfoon" not in message


def test_liveness_message_separates_attached_but_empty() -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    message = describe_silent_stream("prospect", "audiotee:all", tracker.snapshot())

    assert "is open" in message
    assert "nog geen enkel frame" in message


def test_liveness_message_falls_back_for_streams_without_health() -> None:
    message = describe_silent_stream("self", "ULT WEAR", None)
    assert "ULT WEAR" in message
    assert "microfoon" in message


def test_check_streams_liveness_reports_tap_state() -> None:
    tracker = TapHealthTracker(clock=_Clock())
    tracker.mark_attach_failed("binary ontbreekt")

    class _Stream(_FakeTapStream):
        chunks_received = 0
        device_label = "audiotee:all"

    events: list[dict] = []

    async def _broadcast(payload: dict) -> None:
        events.append(payload)

    flagged = asyncio.run(check_streams_liveness([(_Stream(tracker), "prospect")], _broadcast, timeout=0.0))

    assert flagged == ["prospect"]
    assert events[0]["tap_state"] == TapSignalState.NO_TAP.value
    assert "niet geopend" in events[0]["message"]


# --- AudioTeeStream integration --------------------------------------------


def _fake_audiotee(monkeypatch, tmp_path, pcm: bytes):
    import sales_copilot.audio.capture as cap

    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")
    monkeypatch.setattr(cap, "_find_pid", lambda name: 123)  # noqa: ARG005

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(pcm)
            self.stderr = io.BytesIO(b"")

        def terminate(self) -> None:
            return

        def wait(self, timeout=None) -> int:  # noqa: ANN001
            return 0

        def kill(self) -> None:
            return

    monkeypatch.setattr(cap.subprocess, "Popen", lambda *a, **k: _FakeProc())  # noqa: ARG005
    return AudioConfig(
        sample_rate=16000,
        channels=1,
        chunk_size=4,
        capture_method="audiotee",
        target_process="Microsoft Teams",
        audiotee_path=str(audiotee_path),
    )


def _drain(stream: AudioTeeStream, *, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        thread = stream._reader_thread  # noqa: SLF001
        if thread is None or not thread.is_alive():
            return
        time.sleep(0.01)


def test_audiotee_missing_binary_reports_no_tap() -> None:
    config = AudioConfig(capture_method="audiotee", audiotee_path="/nope/audiotee")
    warnings: list[dict] = []
    stream = AudioTeeStream(config, tap_all=True, on_warning=warnings.append)
    stream.start()

    health = stream.tap_health()
    assert health.state is TapSignalState.NO_TAP
    assert health.attach_state is TapAttachState.ATTACH_FAILED
    assert health.warning_emitted is True
    assert len(warnings) == 1


def test_audiotee_delivering_digital_silence_reports_attached_silent(monkeypatch, tmp_path) -> None:
    """int16 zeros: frames arrive, nothing is in them. Not the same as no tap."""

    pcm = np.zeros(400, dtype=np.int16).tobytes()
    config = _fake_audiotee(monkeypatch, tmp_path, pcm)
    stream = AudioTeeStream(config, on_warning=lambda _payload: None)
    stream.start()
    _drain(stream)

    health = stream.tap_health()
    assert health.chunks == 100
    assert health.signal_chunks == 0
    # The subprocess pipe ended, so the tap is gone -- but the frames that did
    # arrive were measured, which is what tells the two failure modes apart.
    assert health.attach_state is TapAttachState.DETACHED
    stream.stop()


def test_audiotee_measures_signal_on_the_normalized_scale(monkeypatch, tmp_path) -> None:
    """Raw int16 must be normalized before measuring, or every tap looks alive."""

    pcm = np.full(400, 6000, dtype=np.int16).tobytes()
    config = _fake_audiotee(monkeypatch, tmp_path, pcm)
    stream = AudioTeeStream(config, on_warning=lambda _payload: None)
    stream.start()
    _drain(stream)

    health = stream.tap_health()
    assert health.signal_chunks == 100
    # 6000/32768 ~= 0.183 -> about -14.8 dBFS, nowhere near the int16 scale.
    assert -20.0 < health.peak_dbfs < -10.0
    stream.stop()


def test_audiotee_dying_after_delivering_audio_now_warns(monkeypatch, tmp_path) -> None:
    """The gap the parked objective ran into: a tap that stops mid-call said nothing."""

    pcm = np.full(400, 6000, dtype=np.int16).tobytes()
    config = _fake_audiotee(monkeypatch, tmp_path, pcm)
    warnings: list[dict] = []
    stream = AudioTeeStream(config, on_warning=warnings.append)
    stream.start()
    _drain(stream)

    assert len(warnings) == 1
    assert warnings[0]["stream"] == "prospect"
    assert "tijdens het gesprek gestopt" in warnings[0]["message"]
    # And it is NOT reported as a setup failure, which is a different fix.
    assert "Systeeminstellingen" not in warnings[0]["message"]

    health = stream.tap_health()
    assert health.state is TapSignalState.NO_TAP
    assert health.attach_state is TapAttachState.DETACHED
    assert health.warning_emitted is True
    stream.stop()


def test_audiotee_requested_stop_is_not_reported_as_a_fault(monkeypatch, tmp_path) -> None:
    """An orderly stop() must stay silent, or the detach warning becomes noise.

    A live tap does not hit EOF the moment it is read empty -- it blocks until
    the subprocess is told to go away. The fake mirrors that, so the test
    exercises the real ordering (stop() first, EOF second) rather than the
    fixture artefact of a pre-filled buffer.
    """

    import threading

    import sales_copilot.audio.capture as cap

    pcm = np.full(400, 6000, dtype=np.int16).tobytes()
    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")
    monkeypatch.setattr(cap, "_find_pid", lambda name: 123)  # noqa: ARG005

    released = threading.Event()
    drained = threading.Event()

    class _BlockingStdout:
        def __init__(self) -> None:
            self._buffer = io.BytesIO(pcm)

        def read(self, size: int) -> bytes:
            data = self._buffer.read(size)
            if data:
                return data
            drained.set()
            released.wait(timeout=5)
            return b""

        def close(self) -> None:
            return

    class _FakeProc:
        def __init__(self) -> None:
            self.stdout = _BlockingStdout()
            self.stderr = io.BytesIO(b"")

        def terminate(self) -> None:
            released.set()

        def wait(self, timeout=None) -> int:  # noqa: ANN001
            return 0

        def kill(self) -> None:
            released.set()

    monkeypatch.setattr(cap.subprocess, "Popen", lambda *a, **k: _FakeProc())  # noqa: ARG005

    config = AudioConfig(
        sample_rate=16000,
        channels=1,
        chunk_size=4,
        capture_method="audiotee",
        target_process="Microsoft Teams",
        audiotee_path=str(audiotee_path),
    )
    warnings: list[dict] = []
    stream = AudioTeeStream(config, on_warning=warnings.append)
    stream.start()
    assert drained.wait(timeout=5)
    assert stream.tap_health().state is TapSignalState.ATTACHED_SIGNAL

    stream.stop()

    assert warnings == []


def test_audiotee_reattach_stops_and_restarts_with_a_fresh_pid(monkeypatch, tmp_path) -> None:
    """``reattach()`` is a clean stop/start cycle that re-resolves the target
    process, exactly what a route change needs. One live subprocess instance
    per spawn (not a shared fake) so the old proc's teardown can never leak
    into the new one's state."""

    import threading

    import sales_copilot.audio.capture as cap

    pcm = np.full(400, 6000, dtype=np.int16).tobytes()
    audiotee_path = tmp_path / "audiotee"
    audiotee_path.write_text("fake")

    pids = iter([111, 222])
    monkeypatch.setattr(cap, "_find_pid", lambda name: next(pids))  # noqa: ARG005

    class _BlockingFakeProc:
        def __init__(self, data: bytes) -> None:
            self._buf = io.BytesIO(data)
            self._closed = threading.Event()
            self.stdout = self
            self.stderr = io.BytesIO(b"")

        def read(self, size: int) -> bytes:
            chunk = self._buf.read(size)
            if chunk:
                return chunk
            while not self._closed.is_set():
                time.sleep(0.005)
            return b""

        def close(self) -> None:
            return

        def terminate(self) -> None:
            self._closed.set()

        def kill(self) -> None:
            self._closed.set()

        def wait(self, timeout=None) -> int:  # noqa: ANN001
            return 0

    spawned: list[_BlockingFakeProc] = []

    def _popen(*args, **kwargs):  # noqa: ANN001, ARG001
        proc = _BlockingFakeProc(pcm)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(cap.subprocess, "Popen", _popen)

    config = AudioConfig(
        sample_rate=16000,
        channels=1,
        chunk_size=4,
        capture_method="audiotee",
        target_process="Microsoft Teams",
        audiotee_path=str(audiotee_path),
    )
    stream = AudioTeeStream(config, on_warning=lambda _payload: None)
    stream.start()

    deadline = time.time() + 2.0
    while time.time() < deadline and stream.tap_health().signal_chunks == 0:
        time.sleep(0.01)
    assert len(spawned) == 1
    assert stream.tap_health().state is TapSignalState.ATTACHED_SIGNAL
    chunks_before_reattach = stream.chunks_received

    stream.reattach()

    deadline = time.time() + 2.0
    while time.time() < deadline and len(spawned) < 2:
        time.sleep(0.01)
    assert len(spawned) == 2, "reattach() must stop the old subprocess and spawn a fresh one"

    deadline = time.time() + 2.0
    while time.time() < deadline and stream.chunks_received <= chunks_before_reattach:
        time.sleep(0.01)
    health = stream.tap_health()
    assert health.attach_state is TapAttachState.ATTACHED
    assert stream.chunks_received > chunks_before_reattach
    # History survives the cycle: it is a reattach, not a fresh session.
    assert health.signal_chunks > 0

    stream.stop()


# --- the dashboard tap_status broadcast -------------------------------------


def test_dashboard_tap_state_maps_all_five_backend_states() -> None:
    """The one place the five backend states fold into the three the dashboard shows."""

    assert dashboard_tap_state(TapSignalState.NO_TAP) is DashboardTapState.NO_TAP
    assert dashboard_tap_state(TapSignalState.ATTACHED_NO_FRAMES) is DashboardTapState.ATTACHED_NO_SIGNAL
    assert dashboard_tap_state(TapSignalState.ATTACHED_SILENT) is DashboardTapState.ATTACHED_NO_SIGNAL
    assert dashboard_tap_state(TapSignalState.ATTACHED_SIGNAL) is DashboardTapState.ATTACHED_SIGNAL
    assert dashboard_tap_state(TapSignalState.ATTACHED_SIGNAL_LOST) is DashboardTapState.ATTACHED_NO_SIGNAL


def test_tap_status_message_carries_the_mapped_dashboard_state_per_backend_state() -> None:
    """Force each of the five backend states directly via the tracker (no hardware)
    and check the published payload carries the matching dashboard_state."""

    clock = _Clock()

    never_started = TapHealthTracker(clock=clock).snapshot()
    assert tap_status_message("prospect", never_started) == {
        "type": TAP_STATUS_MESSAGE_TYPE,
        "stream": "prospect",
        "tap_state": TapSignalState.NO_TAP.value,
        "dashboard_state": DashboardTapState.NO_TAP.value,
    }

    no_frames_tracker = TapHealthTracker(clock=clock)
    no_frames_tracker.mark_attached("audiotee:all")
    clock.advance(12.0)
    no_frames = no_frames_tracker.snapshot()
    assert tap_status_message("self", no_frames)["dashboard_state"] == DashboardTapState.ATTACHED_NO_SIGNAL.value
    assert tap_status_message("self", no_frames)["tap_state"] == TapSignalState.ATTACHED_NO_FRAMES.value

    silent_tracker = TapHealthTracker(clock=clock)
    silent_tracker.mark_attached("audiotee:all")
    for _ in range(50):
        silent_tracker.observe(_digital_silence())
    silent = silent_tracker.snapshot()
    assert tap_status_message("prospect", silent)["dashboard_state"] == DashboardTapState.ATTACHED_NO_SIGNAL.value
    assert tap_status_message("prospect", silent)["tap_state"] == TapSignalState.ATTACHED_SILENT.value

    signal_tracker = TapHealthTracker(clock=clock)
    signal_tracker.mark_attached("audiotee:all")
    signal_tracker.observe(_loud())
    signal = signal_tracker.snapshot()
    assert tap_status_message("self", signal)["dashboard_state"] == DashboardTapState.ATTACHED_SIGNAL.value
    assert tap_status_message("self", signal)["tap_state"] == TapSignalState.ATTACHED_SIGNAL.value

    lost_tracker = TapHealthTracker(clock=clock, signal_lost_seconds=5.0)
    lost_tracker.mark_attached("audiotee:all")
    _run_the_2026_09_10_call(lost_tracker, clock, zero_seconds=6.0)
    lost = lost_tracker.snapshot()
    assert lost.state is TapSignalState.ATTACHED_SIGNAL_LOST
    assert tap_status_message("prospect", lost)["dashboard_state"] == DashboardTapState.ATTACHED_NO_SIGNAL.value
    assert tap_status_message("prospect", lost)["tap_state"] == TapSignalState.ATTACHED_SIGNAL_LOST.value


def test_heartbeat_publishes_tap_status_every_beat_even_when_healthy() -> None:
    """tap_status is not gated by the once-per-stream warning latch: a healthy
    tap that never warns must still keep publishing its state every beat."""

    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    tracker.observe(_loud())

    async def _broadcast(payload: dict) -> None:
        return

    statuses: list[dict] = []

    async def _status(payload: dict) -> None:
        statuses.append(payload)

    async def _run() -> None:
        await monitor_tap_health(
            [(_FakeTapStream(tracker), "prospect")],
            _broadcast,
            asyncio.Event(),
            interval_seconds=0.001,
            max_beats=3,
            broadcast_status=_status,
        )

    asyncio.run(_run())

    assert len(statuses) == 3
    assert all(s["type"] == TAP_STATUS_MESSAGE_TYPE for s in statuses)
    assert all(s["stream"] == "prospect" for s in statuses)
    assert all(s["dashboard_state"] == DashboardTapState.ATTACHED_SIGNAL.value for s in statuses)


def test_heartbeat_keeps_publishing_tap_status_after_the_warning_latches() -> None:
    """The audio_warning fires once per stream; tap_status must not inherit that
    latch, or the dashboard would freeze on the state at the moment of the fault."""

    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    clock.advance(30.0)

    async def _broadcast(payload: dict) -> None:
        return

    statuses: list[dict] = []

    async def _status(payload: dict) -> None:
        statuses.append(payload)

    async def _run() -> None:
        await monitor_tap_health(
            [(_FakeTapStream(tracker), "prospect")],
            _broadcast,
            asyncio.Event(),
            interval_seconds=0.001,
            max_beats=4,
            no_frames_warn_seconds=10.0,
            silent_warn_seconds=120.0,
            broadcast_status=_status,
        )

    asyncio.run(_run())

    assert len(statuses) == 4
    assert all(s["dashboard_state"] == DashboardTapState.ATTACHED_NO_SIGNAL.value for s in statuses)


def test_no_tap_status_without_a_status_broadcaster() -> None:
    """Every existing caller passes none; the warning/heartbeat path must not depend on it."""

    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    tracker.observe(_loud())

    events: list[dict] = []
    _run_monitor([(_FakeTapStream(tracker), "prospect")], beats=2, events=events)

    assert events == []


def test_heartbeat_does_not_publish_tap_status_for_streams_without_health() -> None:
    """test_stream_tap_health_returns_none_for_plain_streams' counterpart at the
    heartbeat level: a plain stream must stay silent on the status channel too."""

    statuses: list[dict] = []

    async def _broadcast(payload: dict) -> None:
        return

    async def _status(payload: dict) -> None:
        statuses.append(payload)

    async def _run() -> None:
        await monitor_tap_health(
            [(_NoHealthStream(), "self")],
            _broadcast,
            asyncio.Event(),
            interval_seconds=0.001,
            max_beats=2,
            broadcast_status=_status,
        )

    asyncio.run(_run())

    assert statuses == []


# --- reattach: a live tap delivering only zeros gets a clean stop/start -----
#
# ATTACHED_SIGNAL_LOST proves a stopped source (see the module docstring), but
# until now the monitor's only response was the one-shot warning. The
# subprocess is still alive in this state -- CallTapStream's existing
# restart-once path never fires because nothing died -- so the fix is a
# forced stop/start cycle, bounded to 3 attempts with increasing spacing.


class _ReattachableFakeStream:
    """A stream whose ``reattach()`` is fully test-controlled.

    Deliberately independent of ``AudioTeeStream``/``CallTapStream``: this
    exercises the monitor's reattach *decision* (when to call it, how often,
    what it broadcasts) without needing real subprocess plumbing. The actual
    stop/start cycle on each stream class is covered separately in
    test_audio_tap_health.py's AudioTeeStream section and in test_calltap.py.
    """

    def __init__(self, tracker: TapHealthTracker, *, on_reattach=None) -> None:  # noqa: ANN001
        self._tracker = tracker
        self.reattach_calls = 0
        self._on_reattach = on_reattach

    def tap_health(self):
        return self._tracker.snapshot()

    def reattach(self) -> None:
        self.reattach_calls += 1
        if self._on_reattach is not None:
            self._on_reattach(self.reattach_calls)


def test_reattach_is_attempted_when_tap_is_lost_and_peer_carries_signal() -> None:
    _clock, lost, alive = _lost_and_healthy_pair()
    lost_stream = _ReattachableFakeStream(lost)

    events: list[dict] = []
    _run_monitor(
        [(lost_stream, "prospect"), (_FakeTapStream(alive), "self")],
        beats=1,
        events=events,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
    )

    assert lost_stream.reattach_calls == 1
    # No warning yet: the attempt gets a chance before the operator is told.
    assert events == []


def test_reattach_that_restores_signal_settles_back_to_attached_signal() -> None:
    """The dispatch text: 'Een poging die signaal terugbrengt, zet de toestand terug naar attached_signal.'

    No extra bookkeeping needed to force the reset: the tracker already reads
    ATTACHED_SIGNAL as soon as a real frame arrives, the same way any other
    recovery does.
    """

    _clock, lost, alive = _lost_and_healthy_pair()

    def _fix_it(_attempt: int) -> None:
        lost.observe(_loud())

    lost_stream = _ReattachableFakeStream(lost, on_reattach=_fix_it)

    events: list[dict] = []
    _run_monitor(
        [(lost_stream, "prospect"), (_FakeTapStream(alive), "self")],
        beats=2,
        events=events,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
    )

    assert lost_stream.reattach_calls == 1
    assert lost.snapshot().state is TapSignalState.ATTACHED_SIGNAL
    assert events == []


def test_reattach_stops_after_three_failed_attempts_then_warns() -> None:
    """The dispatch text: 'maximaal 3 pogingen per gesprek ... daarna alleen de bestaande waarschuwing.'

    Backoff is counted in monitor beats (1, 2, 4): attempts land on beats
    1, 2 and 4, beat 3 is a cooldown, and beat 5 -- with the budget spent --
    finally falls through to the ordinary warning.
    """

    _clock, lost, alive = _lost_and_healthy_pair()
    lost_stream = _ReattachableFakeStream(lost)  # never fixes it

    events: list[dict] = []
    _run_monitor(
        [(lost_stream, "prospect"), (_FakeTapStream(alive), "self")],
        beats=5,
        events=events,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
    )

    assert lost_stream.reattach_calls == 3
    warnings = [e for e in events if e["type"] == "audio_warning"]
    assert len(warnings) == 1
    assert warnings[0]["stream"] == "prospect"
    assert warnings[0]["tap_state"] == TapSignalState.ATTACHED_SIGNAL_LOST.value


def test_reattach_is_not_attempted_when_both_sides_are_silent() -> None:
    """The dispatch text: 'Geen herstart als BEIDE kanten stil zijn ... dat is een pauze of einde gesprek.'

    The peer here never once carried signal, so ``peer_label`` never gets set
    -- the same gate the warning message's cross-channel sharpening already
    relies on.
    """

    clock = _Clock()
    lost = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    quiet_peer = TapHealthTracker(clock=clock, signal_lost_seconds=30.0)
    lost.mark_attached("audiotee:all")
    quiet_peer.mark_attached("ULT WEAR")
    _run_the_2026_09_10_call(lost, clock, zero_seconds=60.0)
    for _ in range(700):
        clock.advance(0.1)
        quiet_peer.observe(_digital_silence())

    lost_stream = _ReattachableFakeStream(lost)

    events: list[dict] = []
    _run_monitor(
        [(lost_stream, "prospect"), (_FakeTapStream(quiet_peer), "self")],
        beats=1,
        events=events,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
    )

    assert lost_stream.reattach_calls == 0
    # The prospect side's own warning path is untouched by this gate.
    warnings = [e for e in events if e["type"] == "audio_warning" and e["stream"] == "prospect"]
    assert len(warnings) == 1


def test_reattach_is_not_attempted_for_a_dead_subprocess() -> None:
    """A subprocess that actually exited is NO_TAP, not ATTACHED_SIGNAL_LOST --
    the existing detach path must stay exactly as it was, even when the stream
    happens to expose a ``reattach()``."""

    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    tracker.observe(_loud())
    tracker.mark_detached("subprocess gestopt")

    stream = _ReattachableFakeStream(tracker)

    events: list[dict] = []
    _run_monitor(
        [(stream, "prospect")],
        beats=1,
        events=events,
        no_frames_warn_seconds=10.0,
        silent_warn_seconds=120.0,
    )

    assert stream.reattach_calls == 0
    assert len(events) == 1
    assert "weggevallen" in events[0]["message"]


def test_reattach_marker_broadcast_on_each_attempt() -> None:
    _clock, lost, alive = _lost_and_healthy_pair()
    lost_stream = _ReattachableFakeStream(lost)

    markers: list[dict] = []

    async def _broadcast(payload: dict) -> None:
        return

    async def _marker(payload: dict) -> None:
        markers.append(payload)

    async def _run() -> None:
        await monitor_tap_health(
            [(lost_stream, "prospect"), (_FakeTapStream(alive), "self")],
            _broadcast,
            asyncio.Event(),
            interval_seconds=0.001,
            max_beats=1,
            no_frames_warn_seconds=10.0,
            silent_warn_seconds=120.0,
            broadcast_marker=_marker,
        )

    asyncio.run(_run())

    assert len(markers) == 1
    marker = markers[0]
    assert marker["type"] == TRANSCRIPT_MARKER_TYPE
    assert marker["marker"] == "audio_tap_reattached"
    assert marker["stream"] == "prospect"
    assert "poging 1/3" in marker["text"]


def test_a_failing_status_broadcast_does_not_break_the_monitor(caplog) -> None:
    clock = _Clock()
    tracker = TapHealthTracker(clock=clock)
    tracker.mark_attached("audiotee:all")
    tracker.observe(_loud())

    async def _broadcast(payload: dict) -> None:
        return

    async def _status(payload: dict) -> None:
        raise RuntimeError("hub down")

    async def _run() -> None:
        await monitor_tap_health(
            [(_FakeTapStream(tracker), "prospect")],
            _broadcast,
            asyncio.Event(),
            interval_seconds=0.001,
            max_beats=2,
            broadcast_status=_status,
        )

    with caplog.at_level(logging.WARNING, logger="sales_copilot.audio.tap_health"):
        asyncio.run(_run())

    assert [r for r in caplog.records if "Failed to broadcast tap_status" in r.getMessage()]
