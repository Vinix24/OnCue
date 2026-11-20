"""Frontend-agnostic call recorder engine.

Captures two tracks — prospect (all system audio via AudioTee) and self (your
microphone) — to 16 kHz mono WAV while exposing a live level ``snapshot`` that a
terminal VU meter or a browser REC button can both render. The capture/drain
loop runs in a background thread; ``snapshot`` is safe to poll from another
thread (terminal redraw) or an asyncio task (websocket push).

Tests inject fake streams and drive ``pump`` manually with ``start(run_loop=
False)`` for deterministic, hardware-free coverage.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from sales_copilot.audio.capture import AudioConfig, AudioTeeStream, MicStream, normalize_capture_method
from sales_copilot.audio.levels import SILENCE_FLOOR_DB, LevelTracker, rms_dbfs
from sales_copilot.audio.recorder import AudioRecorder
from sales_copilot.audio.tap_health import (
    DEFAULT_HEARTBEAT_SECONDS,
    DEFAULT_REATTACH_MAX_ATTEMPTS,
    ReattachAction,
    TapHealth,
    TapHealthTracker,
    TapSignalState,
    carrying_signal_labels,
    plan_reattach,
    resolve_no_system_audio_seconds,
)
from sales_copilot.audio.telephony_guard import ProcessWatcher, TelephonyTapGuard, get_telephony_tap_guard
from sales_copilot.audio.wasapi_process import build_windows_prospect_stream
from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import AutoStopConfig, env, load_env
from sales_copilot.core.paths import resolve_app_path, resolve_app_resource

logger = logging.getLogger(__name__)

#: The track the tap watchdog guards: all system audio, the one AudioTee delivers.
TAP_LABEL = "prospect"
#: How long the "recovered" tap state stays in the snapshot after a reattach worked.
TAP_RECOVERED_SHOW_S = 10.0

DEFAULT_AUDIOTEE_PATH = str(resolve_app_resource("bin/audiotee"))


class _Stream(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read(self) -> np.ndarray | None: ...


@dataclass(frozen=True)
class StreamSnapshot:
    label: str
    display_db: float
    peak_db: float
    silent: bool
    started: bool
    error: str | None = None


@dataclass(frozen=True)
class EngineSnapshot:
    recording: bool
    elapsed_s: float
    directory: str | None
    streams: list[StreamSnapshot]
    paused: bool = False
    auto_stop_s: int | None = None
    #: ``ok``, ``lost`` (zeros while the mic carries signal), ``gave_up`` (attempts spent) or
    #: ``recovered`` (for a few seconds after a reattach worked).
    tap_state: str = "ok"
    tap_attempts: int = 0


def _configured_capture_route() -> str:
    """``WINDOWS_CAPTURE_ROUTE`` from the environment or ``.env`` (scripts/record_call.py never loads it)."""

    load_env()
    return env("WINDOWS_CAPTURE_ROUTE") or "endpoint"


class RecorderEngine:
    def __init__(
        self,
        *,
        out_dir: str | Path = resolve_app_path("data/sessions"),
        mic_device: int | str | None = None,
        prospect_process: str | None = None,
        sample_rate: int = 16000,
        chunk_size: int = 1024,
        silence_db: float = -50.0,
        silence_seconds: float = 4.0,
        flush_seconds: float = 2.0,
        audiotee_path: str = DEFAULT_AUDIOTEE_PATH,
        streams: dict[str, _Stream] | None = None,
        feature_policy: FeaturePolicy | None = None,
        pid_watcher: ProcessWatcher | None = None,
        capture_method: str | None = None,
        retry_initial_s: float = 5.0,
        retry_max_s: float = 30.0,
        auto_stop: AutoStopConfig | None = None,
        tap_beat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
        tap_no_system_audio_seconds: float | None = None,
    ) -> None:
        self._tap_beat_s = tap_beat_seconds
        self._tap_no_system_s = (
            resolve_no_system_audio_seconds() if tap_no_system_audio_seconds is None else tap_no_system_audio_seconds
        )
        self._tap_no_system_since: float | None = None
        self._auto_stop_config = auto_stop or AutoStopConfig()
        self._out_dir = out_dir
        self._retry_initial_s = retry_initial_s
        self._retry_max_s = retry_max_s
        self._prospect_process = prospect_process
        self._sample_rate = sample_rate
        self._silence_db = silence_db
        self._silence_seconds = silence_seconds
        self._flush_seconds = flush_seconds
        self._injected = streams
        self._config = AudioConfig(
            sample_rate=sample_rate,
            channels=1,
            dtype="float32",
            chunk_size=chunk_size,
            mic_device=mic_device,
            audiotee_path=audiotee_path,
            target_process=prospect_process,
            capture_method=normalize_capture_method(capture_method, platform=sys.platform),
            windows_capture_route=_configured_capture_route(),
        )
        self._feature_policy = feature_policy
        self._pid_watcher = pid_watcher
        self._telephony_guard: TelephonyTapGuard = (
            get_telephony_tap_guard() if feature_policy is None else TelephonyTapGuard(feature_policy=feature_policy)
        )
        self._lock = threading.Lock()
        # Lock order: _pump_lock first, then _lock. Never take _pump_lock while holding _lock.
        self._pump_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._recorder: AudioRecorder | None = None
        self._directory: str | None = None
        self._start_monotonic: float | None = None
        self._recording = False
        self._paused = False
        self._pause_wall_start: float | None = None
        self._pause_offset_ms = 0
        self._paused_total_s = 0.0
        self._pauses: list[dict[str, int]] = []
        self._markers: list[int] = []
        self._silence_armed = False
        self._last_loud_s = 0.0
        self._auto_stop_s: int | None = None
        self._streams: list[tuple[str, _Stream]] = []
        self._trackers: dict[str, LevelTracker] = {}
        self._cursor_ms: dict[str, float] = {}
        self._started_labels: set[str] = set()
        self._errors: dict[str, str] = {}
        self._retry_at: dict[str, float] = {}
        self._retry_delay: dict[str, float] = {}
        self._tap_clock = 0.0
        self._health: dict[str, TapHealthTracker] = {}
        self._tap_state = "ok"
        self._tap_events: list[dict[str, int | float | str]] = []
        self._tap_attempts = 0
        self._tap_next_beat = 0
        self._tap_recovered_at: float | None = None
        self._tap_last_heartbeat = 0

    @staticmethod
    def _describe_error(exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}".strip()
        text = " ".join(text.split())
        return text if len(text) <= 200 else text[:197] + "..."

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def markers(self) -> list[int]:
        with self._lock:
            return list(self._markers)

    @property
    def auto_stop_s(self) -> int | None:
        """Seconds of silence that ended this recording; None while it was stopped by hand."""
        with self._lock:
            return self._auto_stop_s

    def _recording_time(self, now: float) -> float:
        """Recording time in seconds: wall time minus finished pauses, frozen while paused."""
        if self._start_monotonic is None:
            return 0.0
        wall_now = self._pause_wall_start if self._paused and self._pause_wall_start is not None else now
        return max(0.0, wall_now - self._start_monotonic - self._paused_total_s)

    def _build_streams(self) -> list[tuple[str, _Stream]]:
        if self._injected is not None:
            return list(self._injected.items())

        if self._config.capture_method == "wasapi":
            # WASAPI whole-endpoint loopback cannot exclude per-process telephony
            # audio the way macOS AudioTee excludes avconferenced via
            # --exclude-processes, so the free-tier telephony-exclusion guard is a
            # no-op on Windows (operator-approved R3 position, see
            # claudedocs/2026-07-13-plan-windows-wasapi-capture.md). CallTap
            # (avconferenced phone-relay) is macOS-specific and does not exist on
            # Windows. Short-circuit here, before any TelephonyTapGuard /
            # _find_pid / pgrep call below -- pgrep does not exist on Windows and
            # raises FileNotFoundError.
            return [
                ("prospect", build_windows_prospect_stream(self._config)),
                ("self", MicStream(self._config, device=self._config.mic_device)),
            ]

        telephony_exclusions = self._telephony_guard.excluded_telephony_processes()

        if self._prospect_process:
            if self._telephony_guard.is_target_excluded(self._prospect_process, self._pid_watcher):
                logger.warning(
                    "Target process '%s' is a telephony source and requires a Pro license; "
                    "falling back to a filtered system tap that excludes telephony processes.",
                    self._prospect_process,
                )
                prospect: _Stream = AudioTeeStream(
                    self._config,
                    tap_all=True,
                    exclude_processes=telephony_exclusions,
                )
            else:
                prospect = AudioTeeStream(self._config)
        else:
            # A whole-system tap in the free tier must still exclude telephony
            # processes; otherwise a phone call would be captured even when no
            # explicit target process was set.
            if telephony_exclusions:
                prospect = AudioTeeStream(
                    self._config,
                    tap_all=True,
                    exclude_processes=telephony_exclusions,
                )
            else:
                prospect = AudioTeeStream(self._config, tap_all=True)
        self_stream: _Stream = MicStream(self._config, device=self._config.mic_device)
        return [("prospect", prospect), ("self", self_stream)]

    def start(self, *, run_loop: bool = True) -> Path:
        with self._lock:
            if self._recording:
                return Path(self._directory or self._out_dir)
            session_id = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            self._recorder = AudioRecorder(
                session_id, self._out_dir, sample_rate=self._sample_rate, flush_seconds=self._flush_seconds
            )
            self._directory = str(self._recorder.directory)
            self._streams = []
            self._trackers = {}
            self._cursor_ms = {}
            self._started_labels = set()
            self._errors = {}
            self._retry_at = {}
            self._retry_delay = {}
            self._health = {}
            self._tap_clock = time.monotonic()
            self._tap_state = "ok"
            self._tap_events = []
            self._tap_attempts = 0
            self._tap_next_beat = 0
            self._tap_recovered_at = None
            self._tap_last_heartbeat = 0
            self._tap_no_system_since = None
            self._paused = False
            self._pause_wall_start = None
            self._paused_total_s = 0.0
            self._pauses = []
            self._markers = []
            self._silence_armed = False
            self._last_loud_s = 0.0
            self._auto_stop_s = None
            began = time.monotonic()
            for label, stream in self._build_streams():
                self._health[label] = TapHealthTracker(clock=self._tap_now, is_microphone=label != TAP_LABEL)
                try:
                    stream.start()
                    self._started_labels.add(label)
                    self._note_tap_attached_locked(label, 0.0)
                except Exception as exc:  # a dead stream must not abort the other
                    self._errors[label] = self._describe_error(exc)
                    self._retry_delay[label] = self._retry_initial_s
                    self._retry_at[label] = began + self._retry_initial_s
                    logger.error(
                        "Recorder stream '%s' failed to start (device=%s): %s; retrying in background",
                        label,
                        getattr(stream, "device_label", None) or self._config.mic_device or "default",
                        self._errors[label],
                    )
                self._streams.append((label, stream))
                self._trackers[label] = LevelTracker(
                    silence_db=self._silence_db, silence_seconds=self._silence_seconds
                )
                self._cursor_ms[label] = 0.0
            self._stop_event.clear()
            self._start_monotonic = time.monotonic()
            self._recording = True
            directory = Path(self._directory)
            if run_loop:
                self._thread = threading.Thread(target=self._run, name="recorder-engine", daemon=True)
                self._thread.start()
        return directory

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.pump(time.monotonic())
            time.sleep(0.02)

    def _retry_failed_streams(self, now: float) -> None:
        """Retry streams that failed at start; align a late track with leading silence."""
        recorder = self._recorder
        if recorder is None or self._start_monotonic is None:
            return
        for label, stream in self._streams:
            if label in self._started_labels or now < self._retry_at.get(label, float("inf")):
                continue
            try:
                stream.start()
            except Exception as exc:  # keep retrying with backoff
                reason = self._describe_error(exc)
                delay = min(self._retry_delay.get(label, self._retry_initial_s) * 2, self._retry_max_s)
                with self._lock:
                    self._errors[label] = reason
                    self._retry_delay[label] = delay
                    self._retry_at[label] = now + delay
                logger.warning("Recorder stream '%s' retry failed: %s (next try in %.0f s)", label, reason, delay)
                continue
            offset_ms = self._recording_time(now) * 1000.0
            lead_frames = int(round(offset_ms * self._sample_rate / 1000.0))
            if lead_frames > 0:
                recorder.write_chunk(label, np.zeros(lead_frames, dtype=np.float32), 0)
            recorder.note_late_start(label, int(round(offset_ms)))
            with self._lock:
                self._cursor_ms[label] = offset_ms
                self._started_labels.add(label)
                self._errors.pop(label, None)
                self._retry_at.pop(label, None)
                self._retry_delay.pop(label, None)
                self._note_tap_attached_locked(label, self._recording_time(now))
            logger.info(
                "Recorder stream '%s' recovered %.1f s into the call; leading silence added",
                label,
                offset_ms / 1000.0,
            )

    def pump(self, now: float, *, retry: bool = True) -> None:
        """Drain queued frames into the WAV files and update level trackers."""
        with self._pump_lock:
            self._pump_locked(now, retry=retry)

    def _pump_locked(self, now: float, *, retry: bool) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        self._tap_clock = now
        if retry:
            self._retry_failed_streams(now)
        for label, stream in self._streams:
            tick_peak: float | None = None
            while True:
                frame = stream.read()
                if frame is None:
                    break
                if not self._paused:
                    recorder.write_chunk(label, frame, int(self._cursor_ms[label]))
                    with self._lock:
                        self._cursor_ms[label] += (len(frame) / self._sample_rate) * 1000.0
                self._health[label].observe(frame)
                db = rms_dbfs(frame)
                tick_peak = db if tick_peak is None else max(tick_peak, db)
            with self._lock:
                self._trackers[label].observe(
                    tick_peak if tick_peak is not None else SILENCE_FLOOR_DB, now
                )
                if tick_peak is not None and not self._paused:
                    self._note_loudness_locked(label, tick_peak, now)
        self._watch_tap(now)
        self._check_auto_stop(now)

    def _tap_now(self) -> float:
        return self._tap_clock

    def _tap_event_locked(self, t: float, event: str, text: str, *, record: bool = True) -> None:
        """One tap state change: a recorder.log line and (unless ``record`` is off) a metadata.json entry."""
        if record:
            self._tap_events.append({"t": round(t, 1), "event": event})
        logger.info("Tap [%s] t=%.1fs: %s", TAP_LABEL, t, text)

    def _note_tap_attached_locked(self, label: str, t: float) -> None:
        self._health[label].mark_attached(label)
        if label == TAP_LABEL:
            self._tap_event_locked(t, "attached", "attached", record=False)

    def _watch_no_system_audio_locked(
        self, t: float, health: TapHealth, snapshots: list[tuple[str, TapHealth | None]]
    ) -> None:
        """Warning only: the system track is exact zero from the start while the mic carries signal.

        Exact zeros are also what the tap gives while nothing plays yet, so this waits
        ``AUDIO_TAP_NO_SYSTEM_AUDIO_WARN_SECONDS`` of mic signal. It never reattaches. The
        first real system signal clears it.
        """
        if health.state is TapSignalState.ATTACHED_SIGNAL:
            self._tap_no_system_since = None
            if self._tap_state == "no_system_audio":
                self._tap_state = "ok"
                self._tap_event_locked(t, "system_audio_present", "system audio is present again")
            return
        every_frame_zero = health.chunks > 0 and health.zero_run_chunks == health.chunks
        mic_carries = any(p != TAP_LABEL for p in carrying_signal_labels(snapshots))
        if health.state is not TapSignalState.ATTACHED_SILENT or not every_frame_zero or not mic_carries:
            self._tap_no_system_since = None
            return
        if self._tap_no_system_since is None:
            self._tap_no_system_since = t
        if self._tap_state == "ok" and t - self._tap_no_system_since >= self._tap_no_system_s:
            self._tap_state = "no_system_audio"
            waited = t - self._tap_no_system_since
            self._tap_event_locked(
                t, "no_system_audio", f"no system audio since the start while the mic carried signal for {waited:.0f} s"
            )

    def _watch_tap(self, now: float) -> None:
        """Exact zeros on the system track while the mic carries signal is a lost tap.

        Same decision as the live copilot (tracker zero-run state, ``plan_reattach``), run on the
        engine's clock; at most ``DEFAULT_REATTACH_MAX_ATTEMPTS`` per call. The stream's silence
        fill keeps the WAV on the recording clock meanwhile.
        """
        if self._start_monotonic is None or TAP_LABEL not in self._health:
            return
        t = self._recording_time(now)
        snapshots = [(label, tracker.snapshot()) for label, tracker in self._health.items()]
        health = dict(snapshots)[TAP_LABEL]
        stream = dict(self._streams).get(TAP_LABEL)
        beats = max(0, int((now - self._start_monotonic) / self._tap_beat_s)) if self._tap_beat_s > 0 else 0
        with self._lock:
            if beats > self._tap_last_heartbeat:
                self._tap_last_heartbeat = beats
                logger.info("Tap [%s] heartbeat t=%.0fs: %s %s", TAP_LABEL, t, self._tap_state, health.summary())
            lost = health.state is TapSignalState.ATTACHED_SIGNAL_LOST
            if not lost:
                if self._tap_state in ("lost", "gave_up"):
                    self._tap_state = "ok"
                    self._tap_recovered_at = t
                    self._tap_event_locked(
                        t, "reattached", f"reattached, signal is back ({self._tap_attempts} attempts)"
                    )
                    # The limit is per dropout: a second, independent dropout gets its own attempts.
                    self._tap_attempts = 0
                    self._tap_next_beat = 0
                self._watch_no_system_audio_locked(t, health, snapshots)
                return
            peer = next((p for p in carrying_signal_labels(snapshots) if p != TAP_LABEL), None)
            if peer is None:
                return  # both quiet: the end of the call or a pause, not a dropout
            if self._tap_state == "ok":
                self._tap_state = "lost"
                self._tap_recovered_at = None
                self._tap_event_locked(
                    t, "lost", f"lost, exact zeros for {health.zero_run_seconds or 0:.0f} s while {peer} carries signal"
                )
            reattach = getattr(stream, "reattach", None)
            plan = plan_reattach(
                health,
                peer_label=peer,
                can_reattach=callable(reattach),
                attempts=self._tap_attempts,
                beats=beats,
                next_beat=self._tap_next_beat,
            )
            if plan.action is ReattachAction.REATTACH:
                self._tap_attempts = plan.attempt
                self._tap_next_beat = plan.next_beat
                self._tap_event_locked(
                    t,
                    f"reattach_attempt_{plan.attempt}",
                    f"reattach attempt {plan.attempt}/{DEFAULT_REATTACH_MAX_ATTEMPTS}",
                )
            elif (
                plan.action is ReattachAction.NONE
                and self._tap_state == "lost"
                and self._tap_attempts >= DEFAULT_REATTACH_MAX_ATTEMPTS
            ):
                self._tap_state = "gave_up"
                self._tap_event_locked(t, "gave_up", f"gave up after {self._tap_attempts} reattach attempts")
                return
            else:
                return
        # Outside _lock: a reattach restarts a subprocess and must not block the snapshot readers.
        try:
            reattach()
        except Exception:
            logger.warning("Tap [%s] reattach raised", TAP_LABEL, exc_info=True)

    def _note_loudness_locked(self, label: str, db: float, now: float) -> None:
        """A chunk above its track's threshold arms the silence clock and restarts it."""
        if db > self._auto_stop_config.threshold_db(label):
            self._silence_armed = True
            self._last_loud_s = max(self._last_loud_s, self._recording_time(now))

    def _check_auto_stop(self, now: float) -> None:
        """Flag the recording for stop once every track has been quiet for the window.

        The clock runs on recording time, so a pause (which freezes it) suspends
        the silence count, and it only starts after the first loud chunk.
        The owner of the engine sees ``auto_stop_s`` and calls ``stop``.
        """
        config = self._auto_stop_config
        with self._lock:
            if not config.enabled or not self._recording or self._paused or not self._silence_armed:
                return
            if self._auto_stop_s is not None:
                return
            quiet_s = self._recording_time(now) - self._last_loud_s
            if quiet_s >= config.silence_seconds:
                self._auto_stop_s = int(round(config.silence_seconds))
                logger.info("Auto-stop: both tracks silent for %.0f s", quiet_s)

    def snapshot(self, now: float | None = None) -> EngineSnapshot:
        ts = time.monotonic() if now is None else now
        with self._lock:
            elapsed = self._recording_time(ts) if self._recording else 0.0
            streams = [
                StreamSnapshot(
                    label=label,
                    display_db=self._trackers[label].display_db,
                    peak_db=self._trackers[label].peak_db,
                    silent=self._trackers[label].is_silent(ts),
                    started=label in self._started_labels,
                    error=self._errors.get(label),
                )
                for label, _stream in self._streams
            ]
            tap_state = self._tap_state
            if tap_state == "ok" and self._tap_recovered_at is not None:
                if self._recording_time(ts) - self._tap_recovered_at < TAP_RECOVERED_SHOW_S:
                    tap_state = "recovered"
            return EngineSnapshot(
                tap_state=tap_state,
                tap_attempts=self._tap_attempts,
                recording=self._recording,
                elapsed_s=max(0.0, elapsed),
                directory=self._directory,
                streams=streams,
                paused=self._paused,
                auto_stop_s=self._auto_stop_s,
            )

    def pause(self, now: float | None = None) -> bool:
        """Stop writing audio; the clock freezes. Returns False when it does not apply."""
        ts = time.monotonic() if now is None else now
        with self._pump_lock:
            with self._lock:
                applicable = self._recording and not self._paused
            if not applicable:
                logger.info("pause ignored: recording=%s paused=%s", self._recording, self._paused)
                return False
            self.pump(ts)
            with self._lock:
                if not self._recording or self._paused:
                    return False
                self._pause_offset_ms = int(round(self._recording_time(ts) * 1000.0))
                self._pause_wall_start = ts
                self._paused = True
            return True

    def resume(self, now: float | None = None) -> bool:
        """Discard what the pause captured and start writing again."""
        ts = time.monotonic() if now is None else now
        with self._pump_lock:
            with self._lock:
                applicable = self._recording and self._paused
            if not applicable:
                logger.info("resume ignored: recording=%s paused=%s", self._recording, self._paused)
                return False
            self.pump(ts)
            with self._lock:
                if not self._recording or not self._paused:
                    return False
                self._close_pause_locked(ts)
                self._paused = False
            return True

    def add_marker(self, now: float | None = None) -> bool:
        ts = time.monotonic() if now is None else now
        with self._lock:
            if not self._recording:
                logger.info("marker ignored: not recording")
                return False
            self._markers.append(int(round(self._recording_time(ts) * 1000.0)))
        return True

    def _close_pause_locked(self, now: float) -> None:
        start = self._pause_wall_start if self._pause_wall_start is not None else now
        wall_s = max(0.0, now - start)
        self._paused_total_s += wall_s
        self._pauses.append({"offset_ms": self._pause_offset_ms, "wall_duration_ms": int(round(wall_s * 1000.0))})
        self._pause_wall_start = None

    def stop(self) -> Path | None:
        with self._lock:
            if not self._recording:
                return None
            self._stop_event.set()
            stop_ts = time.monotonic()
            elapsed_s = self._recording_time(stop_ts)
            auto_stop_s = self._auto_stop_s
            if self._paused:
                self._close_pause_locked(stop_ts)
            thread = self._thread
            self._thread = None
            self._recording = False
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        for _label, stream in self._streams:
            try:
                stream.stop()
            except Exception:  # vnx-silent-except: best-effort stream teardown
                pass
        recorder = self._recorder
        if recorder is None:
            return None
        self.pump(time.monotonic(), retry=False)
        self._pad_tracks_to(recorder, elapsed_s)
        with self._lock:
            recorder.set_timeline(self._markers, self._pauses)
            recorder.set_tap_events(self._tap_events)
            if auto_stop_s is not None:
                recorder.note_auto_stop(auto_stop_s, int(round(elapsed_s * 1000.0)))
        for label, _stream in self._streams:
            if label not in self._started_labels:
                reason = self._errors.get(label, "never started")
                recorder.note_failed_stream(label, reason)
                logger.error("Recorder stream '%s' never started during this recording: %s", label, reason)
        return recorder.finalize()

    def _pad_tracks_to(self, recorder: AudioRecorder, elapsed_s: float) -> None:
        """End-of-call safety net: bring every started track up to the call length.

        Mid-call gaps are the streams' job; this only covers what is still
        missing after the final drain. The streams' own counters are untouched.
        """
        for label, _stream in self._streams:
            if label not in self._started_labels:
                continue
            padded_ms = recorder.pad_track_to(label, elapsed_s * 1000.0)
            if padded_ms > 0:
                logger.info("Padded track '%s' with %.2f s of silence at end of call", label, padded_ms / 1000.0)
