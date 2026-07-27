from __future__ import annotations

import asyncio

import numpy as np

from sales_copilot.modules.transcriber.inference_queue import InferenceQueueItem, SharedInferenceQueue

_HIGH = 0
_LOW = 1


def _item(priority: int, enqueued_at_ms: int, speaker: str = "prospect") -> InferenceQueueItem:
    return InferenceQueueItem(
        priority=priority,
        enqueued_at_ms=enqueued_at_ms,
        audio=np.zeros(160, dtype=np.float32),
        speaker=speaker,
        start_ms=0,
        end_ms=100,
    )


async def test_queue_high_priority_dequeues_before_low() -> None:
    q = SharedInferenceQueue(max_size=4)
    await q.put(_item(_LOW, enqueued_at_ms=1, speaker="self"))
    await q.put(_item(_HIGH, enqueued_at_ms=2, speaker="prospect"))

    first = await q.get()

    assert first.priority == _HIGH
    assert first.speaker == "prospect"


async def test_queue_fifo_within_same_priority_older_first() -> None:
    q = SharedInferenceQueue(max_size=4)
    await q.put(_item(_HIGH, enqueued_at_ms=10))
    await q.put(_item(_HIGH, enqueued_at_ms=5))

    first = await q.get()

    assert first.enqueued_at_ms == 5


async def test_queue_qsize_accurate_after_puts_and_gets() -> None:
    q = SharedInferenceQueue(max_size=8)
    assert q.qsize() == 0

    await q.put(_item(_HIGH, enqueued_at_ms=1))
    await q.put(_item(_LOW, enqueued_at_ms=2))
    assert q.qsize() == 2

    await q.get()
    assert q.qsize() == 1

    await q.get()
    assert q.qsize() == 0


async def test_queue_full_drops_oldest_low_when_low_present() -> None:
    q = SharedInferenceQueue(max_size=3)
    for t in range(3):
        await q.put(_item(_LOW, enqueued_at_ms=t, speaker="self"))

    new_high = _item(_HIGH, enqueued_at_ms=99, speaker="prospect")
    await q.put(new_high)

    assert q.qsize() == 3
    assert q.dropped_count == 1
    items = [await q.get() for _ in range(3)]
    assert items[0].speaker == "prospect"


async def test_queue_full_no_low_drops_oldest_high() -> None:
    q = SharedInferenceQueue(max_size=2)
    await q.put(_item(_HIGH, enqueued_at_ms=1))
    await q.put(_item(_HIGH, enqueued_at_ms=2))

    await q.put(_item(_HIGH, enqueued_at_ms=3))

    assert q.qsize() == 2
    assert q.dropped_count == 1


async def test_queue_dropped_count_increments_per_drop() -> None:
    q = SharedInferenceQueue(max_size=2)
    for t in range(4):
        await q.put(_item(_LOW, enqueued_at_ms=t, speaker="self"))

    assert q.dropped_count == 2


async def test_queue_concurrent_producers_high_items_drain_first() -> None:
    q = SharedInferenceQueue(max_size=32)

    async def put_low() -> None:
        for t in range(5):
            await q.put(_item(_LOW, enqueued_at_ms=t, speaker="self"))
            await asyncio.sleep(0)

    async def put_high() -> None:
        for t in range(5):
            await q.put(_item(_HIGH, enqueued_at_ms=t, speaker="prospect"))
            await asyncio.sleep(0)

    await asyncio.gather(put_low(), put_high())

    drained = []
    while q.qsize() > 0:
        drained.append(await q.get())

    priorities = [x.priority for x in drained]
    first_low_idx = next((i for i, p in enumerate(priorities) if p == _LOW), len(priorities))
    assert all(p == _HIGH for p in priorities[:first_low_idx])


async def test_queue_numpy_items_compare_without_raising() -> None:
    q = SharedInferenceQueue(max_size=4)
    for priority, ts in [(_HIGH, 3), (_LOW, 1), (_HIGH, 1), (_LOW, 2)]:
        await q.put(_item(priority, enqueued_at_ms=ts))

    results = [await q.get() for _ in range(4)]

    assert results[0].priority == _HIGH
    assert results[1].priority == _HIGH
    assert results[2].priority == _LOW
    assert results[3].priority == _LOW
