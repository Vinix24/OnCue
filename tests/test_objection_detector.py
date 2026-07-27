from __future__ import annotations

import json
from pathlib import Path

import pytest

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.objection_detector import ObjectionDetector, ObjectionRouter

# The curated response packs are Pro content and are absent from the OSS
# export; tests that assert on their content only run where the pack exists.
_requires_curated_pack = pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "config" / "objection_responses.yaml").exists(),
    reason="curated Pro response pack absent (OSS export ships detection without the paid kaartenbak)",
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _FakeDebouncer(PainPointDebouncer):
    def __init__(self, should: bool = True) -> None:
        super().__init__(cooldown_seconds=45)
        self._should = should
        self.recorded: list[str] = []

    def should_trigger(self, category: str) -> bool:
        return self._should

    def record_trigger(self, category: str) -> None:
        self.recorded.append(category)


@pytest.fixture(scope="session")
def objection_router() -> ObjectionRouter:
    config = DetectorConfig(enable_objection_detection=True)
    return ObjectionRouter(config)


def test_objection_router_classifies_prijs(objection_router: ObjectionRouter) -> None:
    match = objection_router.classify("het is te duur voor ons")

    assert match is not None
    assert match.category == "prijs"
    assert match.confidence >= 0.85


@_requires_curated_pack
@pytest.mark.asyncio
async def test_objection_detector_includes_response_suggestion(
    monkeypatch: pytest.MonkeyPatch, pro_feature_policy: FeaturePolicy
) -> None:
    config = DetectorConfig(enable_objection_detection=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config),
        debouncer=_FakeDebouncer(True),
        feature_policy=pro_feature_policy,
    )
    fake_ws = _FakeWebSocket()

    async def _fake_ensure_ws():
        return fake_ws

    monkeypatch.setattr(detector, "_ensure_ws", _fake_ensure_ws)

    event = await detector.process_transcript("het is te duur voor ons", "prospect", 123)

    assert event is not None
    assert event.category == "prijs"
    assert event.response_suggestion.startswith("Ik begrijp dat budget belangrijk is")
    payload = json.loads(fake_ws.sent[0])
    assert payload["type"] == "objection"
    assert payload["category"] == "prijs"
    assert payload["response_suggestion"] == event.response_suggestion
    assert payload["timestamp_ms"] == 123


@pytest.mark.asyncio
async def test_objection_detector_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    config = DetectorConfig(enable_objection_detection=False)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(DetectorConfig(enable_objection_detection=True)),
        debouncer=_FakeDebouncer(True),
    )
    fake_ws = _FakeWebSocket()

    async def _fake_ensure_ws():
        return fake_ws

    monkeypatch.setattr(detector, "_ensure_ws", _fake_ensure_ws)

    event = await detector.process_transcript("het is te duur voor ons", "prospect", 123)

    assert event is None
    assert fake_ws.sent == []


@pytest.mark.asyncio
async def test_objection_detector_no_regression_on_objection_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opportunity matches must never be emitted on /ws/objections."""
    config = DetectorConfig(include_opportunities=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config),
        debouncer=_FakeDebouncer(True),
    )
    objection_sent: list[str] = []
    opportunity_sent: list[str] = []

    async def _fake_objections_ws():
        class _FakeWs:
            async def send(self, data: str) -> None:
                objection_sent.append(data)

        return _FakeWs()

    async def _fake_buying_signals_ws():
        class _FakeWs:
            async def send(self, data: str) -> None:
                opportunity_sent.append(data)

        return _FakeWs()

    monkeypatch.setattr(detector, "_ensure_ws", _fake_objections_ws)
    monkeypatch.setattr(detector, "_ensure_ws_buying_signals", _fake_buying_signals_ws)

    opportunity_event = await detector.process_transcript(
        "ik wil hier wel gevolg aan geven", "prospect", 123
    )

    assert opportunity_event is not None
    assert opportunity_event.category == "koopsignaal"
    assert objection_sent == []
    assert len(opportunity_sent) == 1
    payload = json.loads(opportunity_sent[0])
    assert payload["type"] == "buying_signal"

    objection_event = await detector.process_transcript(
        "het is te duur voor ons", "prospect", 456
    )

    assert objection_event is not None
    assert objection_event.category == "prijs"
    assert len(objection_sent) == 1
    objection_payload = json.loads(objection_sent[0])
    assert objection_payload["type"] == "objection"
    assert objection_payload["category"] == "prijs"
