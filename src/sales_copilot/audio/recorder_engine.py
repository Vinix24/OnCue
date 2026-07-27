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
from sales_copilot.audio.telephony_guard import ProcessWatcher, TelephonyTapGuard, get_telephony_tap_guard
from sales_copilot.audio.wasapi import WasapiLoopbackStream
from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.paths import resolve_app_path, resolve_app_resource

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class EngineSnapshot:
    recording: bool
    elapsed_s: float
    directory: str | None
    streams: list[StreamSnapshot]


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
    ) -> None:
        self._out_dir = out_dir
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
        )
        self._feature_policy = feature_policy
        self._pid_watcher = pid_watcher
        self._telephony_guard: TelephonyTapGuard = (
            get_telephony_tap_guard() if feature_policy is None else TelephonyTapGuard(feature_policy=feature_policy)
        )
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._recorder: AudioRecorder | None = None
        self._directory: str | None = None
        self._start_monotonic: float | None = None
        self._recording = False
        self._streams: list[tuple[str, _Stream]] = []
        self._trackers: dict[str, LevelTracker] = {}
        self._cursor_ms: dict[str, float] = {}
        self._started_labels: set[str] = set()

    @property
    def recording(self) -> bool:
        return self._recording

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
                ("prospect", WasapiLoopbackStream(self._config)),
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
            for label, stream in self._build_streams():
                try:
                    stream.start()
                    self._started_labels.add(label)
                except Exception:  # noqa: BLE001 - a dead stream must not abort the other
                    pass
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

    def pump(self, now: float) -> None:
        """Drain queued frames into the WAV files and update level trackers."""
        recorder = self._recorder
        if recorder is None:
            return
        for label, stream in self._streams:
            tick_peak: float | None = None
            while True:
                frame = stream.read()
                if frame is None:
                    break
                recorder.write_chunk(label, frame, int(self._cursor_ms[label]))
                with self._lock:
                    self._cursor_ms[label] += (len(frame) / self._sample_rate) * 1000.0
                db = rms_dbfs(frame)
                tick_peak = db if tick_peak is None else max(tick_peak, db)
            with self._lock:
                self._trackers[label].observe(
                    tick_peak if tick_peak is not None else SILENCE_FLOOR_DB, now
                )

    def snapshot(self, now: float | None = None) -> EngineSnapshot:
        ts = time.monotonic() if now is None else now
        with self._lock:
            elapsed = ts - self._start_monotonic if (self._recording and self._start_monotonic) else 0.0
            streams = [
                StreamSnapshot(
                    label=label,
                    display_db=self._trackers[label].display_db,
                    peak_db=self._trackers[label].peak_db,
                    silent=self._trackers[label].is_silent(ts),
                    started=label in self._started_labels,
                )
                for label, _stream in self._streams
            ]
            return EngineSnapshot(
                recording=self._recording,
                elapsed_s=max(0.0, elapsed),
                directory=self._directory,
                streams=streams,
            )

    def stop(self) -> Path | None:
        with self._lock:
            if not self._recording:
                return None
            self._stop_event.set()
            thread = self._thread
            self._thread = None
            self._recording = False
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        for _label, stream in self._streams:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001
                pass
        return self._recorder.finalize() if self._recorder is not None else None
