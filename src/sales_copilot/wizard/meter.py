"""Terminal dB meter used as live proof during wizard steps."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np

from sales_copilot.audio.levels import render_meter_bar, rms_dbfs

MeterEmitter = Callable[[float], None]


def terminal_emitter(
    *,
    width: int = 20,
    floor_db: float = -60.0,
    prefix: str = "dB ",
    file: Any = sys.stdout,
) -> MeterEmitter:
    """Return a callable that overwrites the current terminal line with a bar."""

    def _emit(dbfs: float) -> None:
        bar = render_meter_bar(dbfs, width=width, floor_db=floor_db)
        file.write(f"\r{prefix}[{bar}] {dbfs:6.1f} dBFS")
        file.flush()

    return _emit


def websocket_emitter(loop: asyncio.AbstractEventLoop) -> MeterEmitter:
    """Return a callable that broadcasts dBFS samples to the ``wizard`` WS channel.

    The proof runs in a worker thread (``asyncio.to_thread``), so values are
    forwarded with ``run_coroutine_threadsafe`` to avoid blocking the hub loop.
    """

    from sales_copilot.websocket import hub_core

    def _emit(dbfs: float) -> None:
        asyncio.run_coroutine_threadsafe(
            hub_core.broadcast("wizard", {"type": "meter", "dbfs": round(float(dbfs), 2)}),
            loop,
        )

    return _emit


def clear_line(file: Any = sys.stdout) -> None:
    """Clear the current terminal line (best-effort)."""

    file.write("\r" + " " * 60 + "\r")
    file.flush()


def record_mic_levels(
    duration_seconds: float = 3.0,
    sample_rate: int = 16000,
    chunk_size: int = 1024,
) -> Iterator[float]:
    """Yield dBFS values from the default microphone for ``duration_seconds``.

    This is a generator so the caller controls display timing. It is wrapped in
    a context manager by ``meter_session`` so the stream is always stopped.
    """

    from sales_copilot.audio.capture import AudioConfig, MicStream

    config = AudioConfig(
        sample_rate=sample_rate,
        channels=1,
        dtype="float32",
        chunk_size=chunk_size,
        capture_method="mic",
    )
    stream = MicStream(config)
    stream.start()
    try:
        deadline = time.monotonic() + duration_seconds
        while time.monotonic() < deadline:
            frame = stream.read()
            if frame is None:
                time.sleep(0.02)
                continue
            yield rms_dbfs(frame)
    finally:
        stream.stop()


def meter_session(
    emitter: MeterEmitter | None = None,
    duration_seconds: float = 3.0,
) -> tuple[float, float]:
    """Run a live dB meter and return ``(peak_db, mean_db)``.

    The default ``emitter`` writes a block bar to stdout. Tests can pass a
    no-op emitter to capture values through the returned tuple.
    """

    emit = emitter or terminal_emitter()
    values: list[float] = []
    try:
        for dbfs in record_mic_levels(duration_seconds=duration_seconds):
            values.append(dbfs)
            emit(dbfs)
    finally:
        clear_line()

    if not values:
        return -120.0, -120.0
    arr = np.asarray(values, dtype=np.float64)
    return float(np.max(arr)), float(np.mean(arr))
