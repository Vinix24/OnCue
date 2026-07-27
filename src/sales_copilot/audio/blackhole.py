from __future__ import annotations

import logging
import queue
import threading

import numpy as np

from .capture import (
    AudioConfig,
    _drop_oldest_and_put,
    _import_sounddevice,
    _reinit_portaudio_and_register,
    _unregister_stream,
)

logger = logging.getLogger(__name__)


class BlackHoleStream:
    def __init__(
        self,
        config: AudioConfig,
        *,
        queue_maxsize: int = 100,
        device_name: str = "BlackHole 2ch",
    ) -> None:
        self._config = config
        self._device_name = device_name
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_maxsize)
        self._stream = None
        self._lock = threading.Lock()
        self._chunks_received = 0

    @property
    def chunks_received(self) -> int:
        return self._chunks_received

    @property
    def device_label(self) -> str:
        return self._device_name

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return

            sd = _import_sounddevice()

            def callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
                self._chunks_received += 1
                _drop_oldest_and_put(self._queue, indata.copy())

            _reinit_portaudio_and_register(sd)
            try:
                self._stream = sd.InputStream(
                    samplerate=self._config.sample_rate,
                    channels=self._config.channels,
                    dtype=self._config.dtype,
                    blocksize=self._config.chunk_size,
                    callback=callback,
                    device=self._device_name,
                )
                self._stream.start()
            except Exception:
                self._stream = None
                _unregister_stream()
                raise

    def stop(self) -> None:
        with self._lock:
            if self._stream is None:
                return
            stream = self._stream
            self._stream = None
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

    def read(self) -> np.ndarray | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None
