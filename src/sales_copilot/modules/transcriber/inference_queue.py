from __future__ import annotations

import asyncio
import heapq
import logging
from dataclasses import dataclass, field

import numpy as np

from sales_copilot.modules.transcriber.engine import Speaker

logger = logging.getLogger(__name__)


@dataclass(order=True)
class InferenceQueueItem:
    priority: int
    enqueued_at_ms: int
    audio: np.ndarray = field(compare=False)
    speaker: Speaker = field(compare=False)
    start_ms: int = field(compare=False)
    end_ms: int = field(compare=False)
    speech_start_ms: int | None = field(default=None, compare=False)


class SharedInferenceQueue:
    def __init__(self, max_size: int = 32) -> None:
        self._heap: list[InferenceQueueItem] = []
        self._max_size = max_size
        self._lock: asyncio.Lock = asyncio.Lock()
        self._not_empty: asyncio.Event = asyncio.Event()
        self._dropped_count: int = 0

    async def put(self, item: InferenceQueueItem) -> None:
        async with self._lock:
            if len(self._heap) >= self._max_size:
                low_indices = [i for i, x in enumerate(self._heap) if x.priority > 0]
                if low_indices:
                    oldest_idx = min(low_indices, key=lambda i: self._heap[i].enqueued_at_ms)
                    self._heap.pop(oldest_idx)
                    heapq.heapify(self._heap)
                    self._dropped_count += 1
                    logger.debug("queue_dropped speaker=self")
                else:
                    oldest_idx = min(range(len(self._heap)), key=lambda i: self._heap[i].enqueued_at_ms)
                    dropped = self._heap.pop(oldest_idx)
                    heapq.heapify(self._heap)
                    self._dropped_count += 1
                    logger.warning(
                        "queue full, no LOW items to drop — dropping oldest HIGH item speaker=%s",
                        dropped.speaker,
                    )
            heapq.heappush(self._heap, item)
            self._not_empty.set()

    async def get(self) -> InferenceQueueItem:
        while True:
            async with self._lock:
                if self._heap:
                    item = heapq.heappop(self._heap)
                    if not self._heap:
                        self._not_empty.clear()
                    return item
            await self._not_empty.wait()

    def qsize(self) -> int:
        return len(self._heap)

    @property
    def dropped_count(self) -> int:
        return self._dropped_count
