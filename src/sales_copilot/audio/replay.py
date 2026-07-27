from __future__ import annotations

import logging
import time
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class ReplayAudioStream:
    """Replays a 16 kHz mono WAV file through the AudioStream protocol.

    Implements the same sync interface as MicStream / BlackHoleStream so it can
    be plugged into AudioBufferer or any other AudioStream consumer unchanged.

    By default (real_time=False, paced=False) chunks are returned as fast as
    the caller can consume them — suitable for tests. Set real_time=True with
    a speed_multiplier for diagnostic slow-motion replays. Note: real_time=True
    uses time.sleep() and must not be called from inside an asyncio task.

    paced=True is the asyncio-safe alternative to real_time=True: instead of
    sleeping, read() returns None (a buffer underrun, which AudioBufferer
    already tolerates) until the wall clock catches up with the audio position
    divided by speed_multiplier. This is what the live replay capture path
    (AUDIO_CAPTURE_METHOD=replay) uses so a recorded session plays back at
    real-time pace without blocking the event loop.
    """

    def __init__(
        self,
        wav_path: Path | str,
        chunk_size_frames: int = 512,
        real_time: bool = False,
        speed_multiplier: float = 1.0,
        paced: bool = False,
    ) -> None:
        self._wav_path = Path(wav_path)
        self._chunk_size_frames = chunk_size_frames
        self._real_time = real_time
        self._speed_multiplier = max(0.1, speed_multiplier)
        self._paced = paced
        self._wav: wave.Wave_read | None = None
        self._sample_rate: int = 16000
        self._eof: bool = False
        self._pace_origin: float | None = None
        self._frames_delivered: int = 0

    def start(self) -> None:
        self._wav = wave.open(str(self._wav_path), "rb")
        self._sample_rate = self._wav.getframerate()
        if self._sample_rate != 16000:
            self._wav.close()
            self._wav = None
            raise ValueError(
                f"Fixture {self._wav_path.name} must be 16 kHz mono — "
                f"got {self._sample_rate} Hz"
            )
        self._eof = False
        self._pace_origin = time.monotonic() if self._paced else None
        self._frames_delivered = 0
        logger.debug("ReplayAudioStream started: %s", self._wav_path.name)

    def stop(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None
        logger.debug("ReplayAudioStream stopped: %s", self._wav_path.name)

    def read(self) -> np.ndarray | None:
        """Return next chunk as float32 mono, or None at EOF / when paced."""
        if self._wav is None or self._eof:
            return None
        if self._paced and self._pace_origin is not None:
            elapsed_s = time.monotonic() - self._pace_origin
            budget_frames = int(elapsed_s * self._sample_rate * self._speed_multiplier)
            if self._frames_delivered >= budget_frames:
                # Wall clock hasn't caught up with the audio position yet:
                # report an underrun instead of blocking the caller's loop.
                return None
        raw = self._wav.readframes(self._chunk_size_frames)
        if not raw:
            self._eof = True
            return None
        int16 = np.frombuffer(raw, dtype=np.int16)
        chunk = int16.astype(np.float32) / 32768.0
        self._frames_delivered += len(chunk)
        if self._real_time:
            duration_s = len(chunk) / float(self._sample_rate)
            time.sleep(duration_s / self._speed_multiplier)
        return chunk

    @property
    def at_eof(self) -> bool:
        return self._eof
