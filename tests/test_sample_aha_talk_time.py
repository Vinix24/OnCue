"""The sample replay drives the real TalkTimeTracker with the sample's own time."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sales_copilot.core.config import TalkTimeConfig
from sales_copilot.modules.talk_time.tracker import TalkTimeTracker
from sales_copilot.sample_aha import player as player_module
from sales_copilot.sample_aha.player import SampleAhaPlayer

SAMPLES = [
    pytest.param(Path("data/samples/sample-aha.json"), id="nl"),
    pytest.param(Path("data/samples/sample-aha_en.json"), id="en"),
]


class _FakeConnection:
    def __init__(self, url: str, sent: dict[str, list[dict]]) -> None:
        self._channel = url.rsplit("/", 1)[-1]
        self._sent = sent

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send(self, raw: str) -> None:
        self._sent.setdefault(self._channel, []).append(json.loads(raw))


async def _replay(sample: Path, monkeypatch: pytest.MonkeyPatch, speed: float) -> dict[str, list[dict]]:
    sent: dict[str, list[dict]] = {}
    monkeypatch.setattr(
        player_module.websockets,
        "connect",
        lambda url, **_: _FakeConnection(url, sent),
    )
    await SampleAhaPlayer(sample_path=sample, token="t", speed=speed)._play()
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", SAMPLES)
async def test_player_publishes_what_the_tracker_returns(
    sample: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = await _replay(sample, monkeypatch, speed=1000.0)

    tracker = TalkTimeTracker(TalkTimeConfig.from_env())
    expected_states: list[dict] = []
    expected_alerts: list[dict] = []
    for event in json.loads(sample.read_text(encoding="utf-8"))["events"]:
        tracker.record_speech(event["speaker"], event["start_ms"], event["end_ms"])
        expected_states.append(tracker.get_state(event["end_ms"]).to_dict())
        alert = tracker.check_alerts(event["end_ms"])
        if alert is not None:
            expected_alerts.append(alert.to_dict())

    assert sent["talk-time"] == expected_states
    assert sent["coaching"] == expected_alerts
    assert any(a["alert_type"] == "monologue_warning" for a in sent["coaching"])
    assert {s["status"] for s in sent["talk-time"]} != {"green"}


@pytest.mark.asyncio
async def test_replay_speed_does_not_change_the_coaching(monkeypatch: pytest.MonkeyPatch) -> None:
    sample = Path("data/samples/sample-aha_en.json")
    fast = await _replay(sample, monkeypatch, speed=1000.0)
    faster = await _replay(sample, monkeypatch, speed=5000.0)
    assert fast["talk-time"] == faster["talk-time"]
    assert fast["coaching"] == faster["coaching"]


@pytest.mark.asyncio
async def test_missing_sample_stays_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(FileNotFoundError):
        await _replay(tmp_path / "absent.json", monkeypatch, speed=1000.0)
