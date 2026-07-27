import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from sales_copilot.core.config import TalkTimeConfig
from sales_copilot.modules.talk_time.publisher import TalkTimePublisher
from sales_copilot.modules.talk_time.tracker import CoachingAlert, TalkTimeState


class _FakeWebSocket:
    def __init__(self, incoming: list[str] | None = None) -> None:
        self.sent: list[str] = []
        self._incoming: asyncio.Queue[str] = asyncio.Queue()
        if incoming:
            for item in incoming:
                self._incoming.put_nowait(item)

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        return await self._incoming.get()


@dataclass
class _StubTracker:
    state: TalkTimeState
    alerts: list[CoachingAlert | None]
    phase: str = "discovery"

    def get_state(self, now_ms: int) -> TalkTimeState:
        return self.state

    def check_alerts(self, now_ms: int) -> CoachingAlert | None:
        if self.alerts:
            return self.alerts.pop(0)
        return None

    def set_phase(self, phase: str) -> None:
        self.phase = phase


async def _wait_for(condition: Callable[[], bool], timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < timeout:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("condition not met")


def _default_state() -> TalkTimeState:
    return TalkTimeState(
        phase="discovery",
        rolling_self_pct=0.5,
        rolling_prospect_pct=0.5,
        cumulative_self_pct=0.5,
        cumulative_prospect_pct=0.5,
        current_monologue_ms=0,
        monologue_speaker=None,
        call_duration_ms=1000,
        status="green",
    )


@pytest.mark.asyncio
async def test_publishes_state_at_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _default_state()
    tracker = _StubTracker(state=state, alerts=[])
    config = TalkTimeConfig(coaching_update_interval_ms=10)
    publisher = TalkTimePublisher(tracker, config=config)

    talk_ws = _FakeWebSocket()
    coach_ws = _FakeWebSocket()
    publisher._talk_time_ws = talk_ws
    publisher._coaching_ws = coach_ws

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(duration: float) -> None:
        sleep_calls.append(duration)
        await real_sleep(0)

    monkeypatch.setattr("sales_copilot.modules.talk_time.publisher.asyncio.sleep", fake_sleep)

    task = asyncio.create_task(publisher.run())
    await _wait_for(lambda: len(talk_ws.sent) >= 2, timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sleep_calls[0] == pytest.approx(0.01, rel=1e-3)
    payload = json.loads(talk_ws.sent[0])
    assert payload["type"] == "talk_time"


@pytest.mark.asyncio
async def test_forwards_coaching_alerts() -> None:
    state = _default_state()
    alert = CoachingAlert(
        alert_type="ratio_warning",
        message="Adjust talk-time ratio",
        severity="amber",
        timestamp_ms=1000,
    )
    tracker = _StubTracker(state=state, alerts=[alert])
    config = TalkTimeConfig(coaching_update_interval_ms=10)
    publisher = TalkTimePublisher(tracker, config=config)

    talk_ws = _FakeWebSocket()
    coach_ws = _FakeWebSocket()
    publisher._talk_time_ws = talk_ws
    publisher._coaching_ws = coach_ws

    task = asyncio.create_task(publisher.run())
    await _wait_for(lambda: len(coach_ws.sent) >= 1, timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    payload = json.loads(coach_ws.sent[0])
    assert payload["type"] == "coaching_alert"
    assert payload["alert_type"] == "ratio_warning"


@pytest.mark.asyncio
async def test_phase_change_handling() -> None:
    state = _default_state()
    tracker = _StubTracker(state=state, alerts=[])
    publisher = TalkTimePublisher(tracker)

    incoming = [json.dumps({"type": "phase_change", "phase": "pitch"})]
    phase_ws = _FakeWebSocket(incoming=incoming)
    publisher._phase_ws = phase_ws

    task = asyncio.create_task(publisher._listen_for_phase_changes())
    await _wait_for(lambda: tracker.phase == "pitch", timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
