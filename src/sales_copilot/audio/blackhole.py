from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .capture import (
    SYSTEM_CATCHUP_THRESHOLD_S,
    SYSTEM_RESTART_BACKOFF_MAX_S,
    SYSTEM_RESTART_BACKOFF_S,
    SYSTEM_STALL_THRESHOLD_S,
    SYSTEM_WATCHDOG_POLL_S,
    AudioConfig,
    _import_sounddevice,
    _reinit_portaudio_and_register,
    _TrackClock,
    _unregister_stream,
)
from .tap_health import TapHealth, TapHealthTracker

logger = logging.getLogger(__name__)


class BlackHoleStream:
    def __init__(
        self,
        config: AudioConfig,
        *,
        queue_maxsize: int = 100,
        device_name: str = "BlackHole 2ch",
        catchup_threshold_s: float = SYSTEM_CATCHUP_THRESHOLD_S,
        stall_threshold_s: float = SYSTEM_STALL_THRESHOLD_S,
        watchdog_poll_s: float = SYSTEM_WATCHDOG_POLL_S,
        reopen_backoff_s: float = SYSTEM_RESTART_BACKOFF_S,
        reopen_backoff_max_s: float = SYSTEM_RESTART_BACKOFF_MAX_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._device_name = device_name
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_maxsize)
        self._stream: Any = None
        self._lock = threading.Lock()
        self._chunks_received = 0
        self._health = TapHealthTracker()
        self._clock = clock
        self._track = _TrackClock(config, self._queue, clock)
        self._catchup_threshold_s = catchup_threshold_s
        self._stall_threshold_s = stall_threshold_s
        self._watchdog_poll_s = watchdog_poll_s
        self._reopen_backoff_s = reopen_backoff_s
        self._reopen_backoff_max_s = reopen_backoff_max_s
        self._backoff_s = reopen_backoff_s
        self._next_reopen_at = 0.0
        self._watchdog_stop = threading.Event()
        self._watchdog: threading.Thread | None = None

    @property
    def chunks_received(self) -> int:
        return self._chunks_received

    @property
    def device_label(self) -> str:
        return self._device_name

    def tap_health(self) -> TapHealth:
        return self._health.snapshot()

    def _callback(self, indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
        self._chunks_received += 1
        if self._health.observe(indata):
            logger.info(
                "BlackHole stream carrying signal: device=%s first audible frame after %s chunks.",
                self._device_name,
                self._chunks_received,
            )
        padded, outage_s = self._track.push(indata.copy(), self._catchup_threshold_s)
        if outage_s is not None:
            self._backoff_s = self._reopen_backoff_s
            logger.info(
                "BlackHole stream re-attached: device=%s after %.1fs of silence filled.",
                self._device_name,
                outage_s,
            )
            self._health.mark_attached(f"blackhole:{self._device_name}")
        elif padded:
            logger.info(
                "BlackHole stream caught up after a gap: device=%s filled %.1fs with silence.",
                self._device_name,
                padded * self._track.chunk_s,
            )

    def _open(self) -> Any:
        """Open and start an InputStream on the configured device. Raises on failure."""

        sd = _import_sounddevice()
        _reinit_portaudio_and_register(sd)
        stream = None
        try:
            stream = sd.InputStream(
                samplerate=self._config.sample_rate,
                channels=self._config.channels,
                dtype=self._config.dtype,
                blocksize=self._config.chunk_size,
                callback=self._callback,
                device=self._device_name,
            )
            stream.start()
        except Exception:
            if stream is not None:
                self._close(stream)
            else:
                _unregister_stream()
            raise
        return stream

    def _close(self, stream: Any) -> None:
        """Stop and close ``stream`` without raising; keeps the register count balanced."""

        try:
            stream.stop()
        except Exception:
            logger.warning("BlackHoleStream stop() raised on device=%s", self._device_name, exc_info=True)
        try:
            stream.close()
        except Exception:
            logger.warning("BlackHoleStream close() raised on device=%s", self._device_name, exc_info=True)
        finally:
            _unregister_stream()

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            self._track.reset()
            self._backoff_s = self._reopen_backoff_s
            self._next_reopen_at = 0.0
            try:
                self._stream = self._open()
            except Exception:
                self._stream = None
                self._health.mark_attach_failed(f"InputStream kon niet openen op '{self._device_name}'")
                raise
            self._health.mark_attached(f"blackhole:{self._device_name}")
            self._watchdog_stop = threading.Event()
            self._watchdog = threading.Thread(target=self._watchdog_loop, name="blackhole-watchdog", daemon=True)
            self._watchdog.start()

    def stop(self) -> None:
        self._watchdog_stop.set()
        watchdog = self._watchdog
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=5.0)
        with self._lock:
            self._watchdog = None
            stream = self._stream
            self._stream = None
            if stream is not None:
                self._close(stream)

    def read(self) -> np.ndarray | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    # -- watchdog ----------------------------------------------------------

    def _watchdog_loop(self) -> None:
        stop = self._watchdog_stop
        while not stop.is_set():
            try:
                self._watchdog_step()
            except Exception:
                logger.warning("BlackHole watchdog step failed; will retry.", exc_info=True)
            stop.wait(self._watchdog_poll_s)

    def _watchdog_step(self) -> None:
        """One watchdog pass: detect a stalled or dead stream, fill silence, reopen.

        Filling never waits on ``_lock``; only closing and reopening the stream
        does, and that is skipped while the lock is busy. Not armed before the
        first real chunk, so a device that never delivered keeps its existing
        behaviour.
        """

        if self._watchdog_stop.is_set() or not self._track.delivering:
            return
        now = self._clock()
        if not self._track.in_outage:
            stream = self._stream
            errored = stream is not None and getattr(stream, "active", True) is False
            stalled = self._track.silent_for(now) > self._stall_threshold_s
            if not (errored or stalled):
                return
            if self._track.begin_outage():
                logger.warning(
                    "BlackHole stream lost: device=%s %s; filling silence to the clock and reopening.",
                    self._device_name,
                    "input stream stopped" if errored else f"no audio for {self._track.silent_for(now):.1f}s",
                )
                self._health.mark_detached(f"blackhole:{self._device_name} lost")
        self._track.fill(now)
        if now < self._next_reopen_at:
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._watchdog_stop.is_set():
                return
            self._next_reopen_at = now + self._backoff_s
            self._backoff_s = min(self._backoff_s * 2, self._reopen_backoff_max_s)
            old = self._stream
            self._stream = None
            if old is not None:
                self._close(old)
            try:
                self._stream = self._open()
            except Exception:
                logger.warning("BlackHole reopen failed: device=%s", self._device_name, exc_info=True)
                return
            # Attached again; the outage ends, and is logged, on the first real chunk.
            self._health.mark_attached(f"blackhole:{self._device_name}")
        finally:
            self._lock.release()
