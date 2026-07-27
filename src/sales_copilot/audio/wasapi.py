"""Windows WASAPI loopback prospect stream (system/render-endpoint tap).

The macOS prospect stream taps the whole system-output mix via AudioTee
(``tap_all``). WASAPI loopback on the default render endpoint is the Windows
analog: ``soundcard`` opens a loopback "microphone" on the default speaker and
delivers whatever plays through it. ``WasapiLoopbackStream`` exposes the exact
same surface as ``AudioTeeStream``/``BlackHoleStream`` (``start``/``stop``/
``read`` plus ``chunks_received`` and ``device_label``), so the recorder,
talk-time VAD and ``check_streams_liveness`` all consume it unchanged.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .capture import AudioConfig, _drop_oldest_and_put

logger = logging.getLogger(__name__)

# Mirrors check_device_health's silence threshold (mean-abs there; RMS here
# per the WASAPI plan spec) -- both pick out "the endpoint delivered genuine
# far-side audio" from digital silence.
_REAL_AUDIO_RMS_THRESHOLD = 0.0005


class WasapiLoopbackStream:
    """WASAPI loopback capture on the default Windows render endpoint.

    ``start()`` is degrade-not-raise on any setup failure (missing
    ``soundcard``, non-Windows platform, no default endpoint, recorder error,
    rate/shape mismatch), deliberately following ``AudioTeeStream`` and
    unlike ``MicStream``/``BlackHoleStream`` which raise: a background
    prospect stream must never crash the call.
    """

    def __init__(
        self,
        config: AudioConfig,
        *,
        queue_maxsize: int = 100,
        on_warning: Callable[[dict[str, Any]], None] | None = None,
        empty_pull_sleep_s: float = 0.01,
        silence_timeout_multiplier: float = 3.0,
    ) -> None:
        self._config = config
        self._on_warning = on_warning
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_maxsize)
        self._stop_event = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._recorder_cm: Any = None
        self._recorder: Any = None
        self._lock = threading.Lock()
        self._chunks_received = 0
        self._real_frames_received = 0
        self._degraded = False
        # A pull that returns zero frames sleeps this long before retrying
        # (SoundCard WASAPI silence-at-start, bastibe/SoundCard#166), and the
        # accumulator synthesizes a zero-filled chunk once this multiple of
        # the expected chunk duration has passed without filling, so the
        # downstream continuous buffer never wedges.
        self._empty_pull_sleep_s = empty_pull_sleep_s
        self._silence_timeout_multiplier = silence_timeout_multiplier

    @property
    def chunks_received(self) -> int:
        return self._chunks_received

    @property
    def real_frames_received(self) -> int:
        return self._real_frames_received

    @property
    def device_label(self) -> str:
        return "wasapi:loopback"

    def start(self) -> None:
        with self._lock:
            if self._reader_thread is not None:
                return

            self._stop_event.clear()

            if sys.platform != "win32":
                self._emit_warning(
                    "WASAPI-loopback-capture is alleen beschikbaar op Windows; deze stream blijft stil."
                )
                self._degraded = True
                return

            try:
                sc = _import_soundcard()
            except Exception:
                self._emit_warning(
                    "Python-package 'soundcard' ontbreekt. Installeer met "
                    "`pip install .[windows]` om WASAPI-loopback-capture te gebruiken."
                )
                self._degraded = True
                return

            try:
                speaker = sc.default_speaker()
            except Exception:
                speaker = None
            if speaker is None:
                self._emit_warning(
                    "Geen standaard audio-uitvoerapparaat gevonden; WASAPI-loopback-capture kan niet starten."
                )
                self._degraded = True
                return

            try:
                microphone = sc.get_microphone(speaker.id, include_loopback=True)
            except Exception:
                self._emit_warning(
                    "Kon geen WASAPI-loopback-apparaat openen voor het standaard uitvoerapparaat."
                )
                self._degraded = True
                return

            blocksize = self._config.chunk_size * 2
            try:
                recorder_cm = microphone.recorder(samplerate=self._config.sample_rate, blocksize=blocksize)
                recorder = recorder_cm.__enter__()
            except Exception:
                self._emit_warning("Kon de WASAPI-loopback-recorder niet openen.")
                self._degraded = True
                return

            self._recorder_cm = recorder_cm
            self._recorder = recorder
            self._degraded = False
            self._reader_thread = threading.Thread(
                target=self._read_loop, name="wasapi-loopback-reader", daemon=True
            )
            self._reader_thread.start()

    def _read_loop(self) -> None:
        accum = np.zeros((0,), dtype=np.float32)
        shape_checked = False
        last_emit = time.monotonic()
        pacing_timeout = (self._config.chunk_size / float(self._config.sample_rate)) * self._silence_timeout_multiplier

        while not self._stop_event.is_set():
            try:
                block = self._recorder.record(numframes=self._config.chunk_size)
            except Exception:
                self._emit_warning("WASAPI-loopback-apparaat gaf een fout of verdween tijdens opname.")
                self._degraded = True
                return

            if not shape_checked:
                shape_checked = True
                if not self._validate_first_block_shape(block):
                    self._emit_warning(
                        "Onverwachte vorm of samplerate van WASAPI-loopback-audio "
                        f"(verwacht (frames, kanalen) op {self._config.sample_rate} Hz)."
                    )
                    self._degraded = True
                    return

            try:
                frames = int(block.shape[0]) if isinstance(block, np.ndarray) and block.ndim == 2 else 0

                if frames > 0:
                    mono = np.mean(block.astype(np.float32, copy=False), axis=1)
                    accum = np.concatenate([accum, mono])
                    while accum.shape[0] >= self._config.chunk_size:
                        frame = accum[: self._config.chunk_size]
                        accum = accum[self._config.chunk_size :]
                        self._emit_chunk(frame)
                        last_emit = time.monotonic()
                else:
                    time.sleep(self._empty_pull_sleep_s)

                if accum.shape[0] < self._config.chunk_size and (time.monotonic() - last_emit) >= pacing_timeout:
                    self._emit_silence_chunk()
                    last_emit = time.monotonic()
            except Exception:
                self._emit_warning("WASAPI-loopback-apparaat gaf een fout of verdween tijdens opname.")
                self._degraded = True
                return

    def _validate_first_block_shape(self, block: Any) -> bool:
        if block is None:
            return True
        if not isinstance(block, np.ndarray):
            return False
        if block.ndim != 2:
            return False
        if block.shape[1] < 1:
            return False
        return True

    def _emit_chunk(self, mono_frame: np.ndarray) -> None:
        frame = mono_frame.reshape(self._config.chunk_size, 1).astype(np.float32, copy=False)
        rms = float(np.sqrt(np.mean(np.square(frame))))
        if rms > _REAL_AUDIO_RMS_THRESHOLD:
            self._real_frames_received += 1
        self._chunks_received += 1
        _drop_oldest_and_put(self._queue, frame)

    def _emit_silence_chunk(self) -> None:
        frame = np.zeros((self._config.chunk_size, 1), dtype=np.float32)
        self._chunks_received += 1
        _drop_oldest_and_put(self._queue, frame)

    def _emit_warning(self, message: str) -> None:
        logger.error("WasapiLoopbackStream: %s", message)
        if self._on_warning is None:
            return
        try:
            self._on_warning({"type": "audio_warning", "stream": "prospect", "message": message})
        except Exception:
            logger.warning("on_warning callback raised for WASAPI loopback stream", exc_info=True)

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            thread = self._reader_thread
            self._reader_thread = None
            recorder_cm = self._recorder_cm
            self._recorder_cm = None
            self._recorder = None

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

        if recorder_cm is not None:
            try:
                recorder_cm.__exit__(None, None, None)
            except Exception:
                logger.warning("WasapiLoopbackStream recorder __exit__ raised", exc_info=True)

    def read(self) -> np.ndarray | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None


def _import_soundcard() -> Any:
    import soundcard as sc  # type: ignore[import-not-found]

    return sc
