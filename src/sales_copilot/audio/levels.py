"""Audio level metering: dBFS computation, bar rendering, and silence tracking.

Pure and dependency-light so it can drive a terminal VU meter (record_call.py)
today and a dashboard meter later. No wall-clock is read internally — callers
pass a monotonic timestamp into ``observe``/``is_silent`` so the behaviour is
deterministic and testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SILENCE_FLOOR_DB = -120.0


def rms_dbfs(frame: np.ndarray | None) -> float:
    """Return the RMS level of a float32 [-1, 1] frame in dBFS.

    Empty/silent input returns ``SILENCE_FLOOR_DB`` instead of ``-inf``.
    """
    if frame is None:
        return SILENCE_FLOOR_DB
    arr = np.asarray(frame, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return SILENCE_FLOOR_DB
    rms = float(np.sqrt(np.mean(np.square(arr.astype(np.float64)))))
    if rms <= 0.0:
        return SILENCE_FLOOR_DB
    return max(SILENCE_FLOOR_DB, 20.0 * math.log10(rms))


def render_meter_bar(dbfs: float, *, width: int = 20, floor_db: float = -60.0) -> str:
    """Render a fixed-width block bar for ``dbfs`` mapped onto ``[floor_db, 0]``."""
    if width <= 0:
        return ""
    span = -floor_db
    frac = (dbfs - floor_db) / span if span > 0 else 0.0
    frac = min(1.0, max(0.0, frac))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


@dataclass
class LevelTracker:
    """Smoothed level + silence state for one audio stream.

    ``display_db`` is a peak-hold value that decays at ``decay_db_per_s`` so the
    bar falls smoothly instead of flickering. A stream counts as silent when no
    frame at or above ``silence_db`` has been seen for ``silence_seconds``.
    """

    silence_db: float = -50.0
    silence_seconds: float = 4.0
    decay_db_per_s: float = 80.0
    display_db: float = field(default=SILENCE_FLOOR_DB, init=False)
    peak_db: float = field(default=SILENCE_FLOOR_DB, init=False)
    _last_loud_ts: float | None = field(default=None, init=False)
    _last_ts: float | None = field(default=None, init=False)
    _first_ts: float | None = field(default=None, init=False)

    def observe(self, dbfs: float, now: float) -> None:
        if self._last_ts is not None:
            decayed = self.display_db - self.decay_db_per_s * (now - self._last_ts)
            self.display_db = max(dbfs, decayed)
        else:
            self.display_db = dbfs
            self._first_ts = now
        self._last_ts = now
        self.peak_db = max(self.peak_db, dbfs)
        if dbfs >= self.silence_db:
            self._last_loud_ts = now

    def is_silent(self, now: float) -> bool:
        if self._last_loud_ts is None:
            # No loud frame ever; silent once the grace window since monitoring
            # began (the first observation) has elapsed.
            start = self._first_ts if self._first_ts is not None else now
            return (now - start) >= self.silence_seconds
        return (now - self._last_loud_ts) >= self.silence_seconds
