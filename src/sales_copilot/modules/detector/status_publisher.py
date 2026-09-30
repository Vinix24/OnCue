"""Publish the detector's own consume-loop counters to the hub.

#203 made "detector running but dropping everything" distinguishable from
"detector never started" -- in the log. The seller does not read the log. On
2026-09-05 four live calls produced nine transcripts and zero detections and
the dashboard said nothing at all, so "listening, this call simply has no pain
points yet" and "something upstream is broken" looked identical on screen.

This module carries the same counters #203 already maintains in
``_consume_transcripts`` onto the WebSocket hub, so the dashboard can render
them. It mirrors ``modules/talk_time/publisher.py``: a small object that owns
the cadence, builds one payload dict from live state, and hands it to a send
callable. The send is injected rather than owned so this module needs no
websockets import of its own -- ``detector/__main__.py`` supplies its existing
``_publish_ws`` and keeps the network seam in the one place the architecture
allow-list already blesses.

**Counters and timestamps only.** Transcript text never crosses this channel:
the payload is built here, from the counters dict and the detector config, and
there is no code path through which an utterance can reach it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from sales_copilot.core.config import DetectorConfig

logger = logging.getLogger(__name__)

# The hub channel the dashboard subscribes to. Registered in
# websocket/hub_core.py::_ALLOWED_CHANNELS.
DETECTOR_STATUS_CHANNEL = "detector-status"

# Bounded cadence. This is a status signal, not a stream: at most one publish
# per interval regardless of how many chunks arrive. Deliberately time-based
# rather than chunk-based (unlike the log heartbeat, which is counted in chunks
# so it still fires when everything downstream is filtered out): the dashboard
# needs a pulse even when NO chunks arrive at all, because "no audio reaching
# the detector" is exactly one of the failures the seller has to be able to see.
STATUS_INTERVAL_SECONDS = 5.0

# States the payload can carry. `listening` = alive, nothing received yet;
# `active` = alive and receiving; `stopped` = the consume loop has ended.
_STATE_LISTENING = "listening"
_STATE_ACTIVE = "active"
_STATE_STOPPED = "stopped"

SendPayload = Callable[[str, dict[str, object]], Awaitable[None]]


class DetectorStatusPublisher:
    """Throttled publisher for the consume loop's own counters.

    ``publish`` is safe to call on every iteration of the consume loop: it
    returns without sending unless the interval has elapsed or ``force`` is
    set. ``force`` is used for the three moments that must never be throttled
    away -- loop start, the first transcript chunk, and the closing summary.
    """

    def __init__(
        self,
        send: SendPayload,
        config: DetectorConfig,
        *,
        interval_seconds: float = STATUS_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._send = send
        self._config = config
        self._interval = max(0.0, float(interval_seconds))
        self._monotonic = monotonic
        self._wall_clock_ms = wall_clock_ms or (lambda: int(time.time() * 1000))
        self._started_at = monotonic()
        self._last_published_at: float | None = None
        self._last_chunk_at: float | None = None

    def note_chunk(self) -> None:
        """Record that a transcript chunk was accepted, for the staleness age."""
        self._last_chunk_at = self._monotonic()

    def build_payload(self, counts: dict[str, int], *, state: str) -> dict[str, object]:
        """Build the wire payload. Counters, config and timestamps only."""
        now = self._monotonic()
        last_chunk_age_ms: int | None = None
        if self._last_chunk_at is not None:
            last_chunk_age_ms = int((now - self._last_chunk_at) * 1000)
        return {
            "type": "detector_status",
            "state": state,
            "counts": dict(counts),
            "uptime_ms": int((now - self._started_at) * 1000),
            "last_chunk_age_ms": last_chunk_age_ms,
            "interval_ms": int(self._interval * 1000),
            "thresholds": {
                "low": self._config.confidence_threshold_low,
                "high": self._config.confidence_threshold_high,
            },
            "policy": {
                "only_classify_prospect": self._config.only_classify_prospect,
                "min_chunks_to_classify": self._config.min_chunks_to_classify,
                "classification_debounce_seconds": self._config.classification_debounce_seconds,
            },
            "timestamp_ms": self._wall_clock_ms(),
        }

    def _resolve_state(self, counts: dict[str, int], stopped: bool) -> str:
        if stopped:
            return _STATE_STOPPED
        return _STATE_ACTIVE if counts.get("received", 0) > 0 else _STATE_LISTENING

    def _due(self, now: float) -> bool:
        if self._last_published_at is None:
            return True
        return (now - self._last_published_at) >= self._interval

    async def publish(
        self,
        counts: dict[str, int],
        *,
        force: bool = False,
        stopped: bool = False,
    ) -> bool:
        """Publish the counters when due. Returns whether a payload was sent."""
        now = self._monotonic()
        if not force and not self._due(now):
            return False
        self._last_published_at = now
        payload = self.build_payload(counts, state=self._resolve_state(counts, stopped))
        try:
            await self._send(DETECTOR_STATUS_CHANNEL, payload)
        except Exception:
            # A status signal must never take the detector down with it. The
            # log heartbeat from #203 stays the backstop.
            logger.debug("Could not publish detector status", exc_info=True)
            return False
        return True
