from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

import pytest

from sales_copilot.modules.talk_time import __main__ as talk_time_main
from sales_copilot.modules.talk_time.tracker import TalkTimeTracker


class _FakePublisher:
    def __init__(self, tracker: TalkTimeTracker) -> None:
        self._tracker = tracker
        self.snapshot_durations_ms: list[int] = []

    async def publish_snapshot(self, now_ms: int) -> None:
        self.snapshot_durations_ms.append(self._tracker.get_state(now_ms).call_duration_ms)

    async def publish_alert_if_needed(self, now_ms: int) -> None:
        _ = now_ms


def _monotonic_now_ms_factory() -> Callable[[], int]:
    started = time.monotonic()

    def _now_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    return _now_ms


async def _wait_for(condition: Callable[[], bool], timeout_s: float) -> None:
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("condition not met")


@pytest.mark.asyncio
async def test_heartbeat_emits_after_start_call() -> None:
    tracker = TalkTimeTracker()
    publisher = _FakePublisher(tracker)
    now_ms = _monotonic_now_ms_factory()
    controller = talk_time_main._HeartbeatController(  # noqa: SLF001
        tracker,
        publisher,
        now_ms=now_ms,
        heartbeat_ms=100,
    )

    stop_event = asyncio.Event()
    task = asyncio.create_task(controller.run(stop_event))
    try:
        controller.handle_start_call()
        await _wait_for(lambda: len(publisher.snapshot_durations_ms) >= 1, timeout_s=1.5)
        assert publisher.snapshot_durations_ms[0] > 0
    finally:
        stop_event.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_heartbeat_stops_on_end_call() -> None:
    tracker = TalkTimeTracker()
    publisher = _FakePublisher(tracker)
    now_ms = _monotonic_now_ms_factory()
    controller = talk_time_main._HeartbeatController(  # noqa: SLF001
        tracker,
        publisher,
        now_ms=now_ms,
        heartbeat_ms=80,
    )

    stop_event = asyncio.Event()
    task = asyncio.create_task(controller.run(stop_event))
    try:
        controller.handle_start_call()
        await _wait_for(lambda: len(publisher.snapshot_durations_ms) >= 1, timeout_s=1.5)
        controller.handle_end_call()
        count_after_end = len(publisher.snapshot_durations_ms)
        await asyncio.sleep(0.25)
        assert len(publisher.snapshot_durations_ms) == count_after_end
    finally:
        stop_event.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_heartbeat_idempotent_on_restart() -> None:
    tracker = TalkTimeTracker()
    publisher = _FakePublisher(tracker)
    # Controllable clock: the asserted durations are driven by this value, not by
    # real wall-clock timing, so the test is deterministic under any load. Real
    # asyncio scheduling only affects *when* a snapshot fires (handled by _wait_for).
    clock_ms = {"value": 0}

    def now_ms() -> int:
        return clock_ms["value"]

    controller = talk_time_main._HeartbeatController(  # noqa: SLF001
        tracker,
        publisher,
        now_ms=now_ms,
        heartbeat_ms=10,
    )

    stop_event = asyncio.Event()
    task = asyncio.create_task(controller.run(stop_event))
    try:
        controller.handle_start_call()  # call starts at t=0
        clock_ms["value"] = 1000  # 1s of (controlled) call time elapsed
        await _wait_for(
            lambda: bool(publisher.snapshot_durations_ms)
            and publisher.snapshot_durations_ms[-1] >= 1000,
            timeout_s=1.5,
        )
        first_duration = publisher.snapshot_durations_ms[-1]
        before_restart_count = len(publisher.snapshot_durations_ms)

        controller.handle_start_call()  # restart resets the call clock to now (t=1000)
        clock_ms["value"] = 1100  # only 100ms since the restart
        await _wait_for(
            lambda: len(publisher.snapshot_durations_ms) > before_restart_count
            and publisher.snapshot_durations_ms[-1] < first_duration,
            timeout_s=1.5,
        )
        restarted_duration = publisher.snapshot_durations_ms[-1]

        assert first_duration >= 1000
        assert restarted_duration < first_duration
    finally:
        stop_event.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_heartbeat_start_resets_tracker_state() -> None:
    tracker = TalkTimeTracker()
    tracker.record_speech("self", 0, 500)
    tracker.record_speech("prospect", 500, 1000)
    assert tracker.get_state(1000).cumulative_self_pct == pytest.approx(0.5)

    publisher = _FakePublisher(tracker)
    now_ms = _monotonic_now_ms_factory()
    controller = talk_time_main._HeartbeatController(  # noqa: SLF001
        tracker,
        publisher,
        now_ms=now_ms,
        heartbeat_ms=100,
    )

    controller.handle_start_call()
    state = tracker.get_state(now_ms())
    assert state.cumulative_self_pct == 0.0
    assert state.cumulative_prospect_pct == 0.0


@pytest.mark.asyncio
async def test_heartbeat_no_emit_before_start() -> None:
    tracker = TalkTimeTracker()
    publisher = _FakePublisher(tracker)
    now_ms = _monotonic_now_ms_factory()
    controller = talk_time_main._HeartbeatController(  # noqa: SLF001
        tracker,
        publisher,
        now_ms=now_ms,
        heartbeat_ms=80,
    )

    stop_event = asyncio.Event()
    task = asyncio.create_task(controller.run(stop_event))
    try:
        await asyncio.sleep(0.25)
        assert publisher.snapshot_durations_ms == []
    finally:
        stop_event.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
