import json

import pytest

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import SlidesConfig, WebSocketConfig
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.detector.pipeline import PainPointEvent
from sales_copilot.modules.slides.case_db import Case


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _StubPipeline:
    def __init__(self, event: PainPointEvent | None) -> None:
        self._event = event
        self.calls: list[tuple[str, str]] = []

    def process(self, text: str, speaker: str) -> PainPointEvent | None:
        self.calls.append((text, speaker))
        return self._event


class _StubCaseDB:
    def __init__(self, result: Case | None) -> None:
        self._result = result
        self.calls: list[tuple[str, str | None]] = []

    async def find_case(self, pain_point: str, industry: str | None = None) -> Case | None:
        self.calls.append((pain_point, industry))
        return self._result


@pytest.mark.asyncio
async def test_process_transcript_returns_none_when_no_pain_point(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = _StubPipeline(event=None)
    case_db = _StubCaseDB(result=None)
    injector = SlideInjector(pipeline, case_db, WebSocketConfig(), SlidesConfig())

    async def fail_connect(url: str) -> _FakeWebSocket:
        raise AssertionError(f"unexpected websocket connect to {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fail_connect)

    result = await injector.process_transcript("hello", "prospect", 1234)
    assert result is None
    assert pipeline.calls == [("hello", "prospect")]


@pytest.mark.asyncio
async def test_emits_pain_point_and_slide_control_when_case_found(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    event = PainPointEvent(
        category="offerteproces",
        confidence=0.91,
        trigger_phrase="we verliezen offertes",
        timestamp_ms=111,
    )
    pipeline = _StubPipeline(event=event)
    case = Case(
        id="case-001",
        title="Case 1",
        industry="manufacturing",
        pain_point="offerteproces",
        description=None,
        slide_html="<section></section>",
        metrics=None,
        priority=1,
    )
    case_db = _StubCaseDB(result=case)
    slides_config = SlidesConfig(prospect_industry="manufacturing")
    injector = SlideInjector(pipeline, case_db, WebSocketConfig(), slides_config, feature_policy=pro_feature_policy)

    sockets = {
        "pain-points": _FakeWebSocket(),
        "slide-control": _FakeWebSocket(),
    }
    connect_calls: list[str] = []

    async def fake_connect(url: str) -> _FakeWebSocket:
        connect_calls.append(url)
        if url.endswith("/ws/pain-points"):
            return sockets["pain-points"]
        if url.endswith("/ws/slide-control"):
            return sockets["slide-control"]
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("text", "prospect", 4567)

    assert result is not None
    assert case_db.calls == [("offerteproces", "manufacturing")]
    assert len(sockets["pain-points"].sent) == 1
    assert len(sockets["slide-control"].sent) == 1

    pain_payload = json.loads(sockets["pain-points"].sent[0])
    assert pain_payload["type"] == "pain_point"
    assert pain_payload["category"] == "offerteproces"
    assert pain_payload["case_matched"] is True
    assert pain_payload["case_id"] == "case-001"
    assert pain_payload["timestamp_ms"] == 4567

    slide_payload = json.loads(sockets["slide-control"].sent[0])
    assert slide_payload == {"action": "navigate_to_case", "slide_id": "case-001"}

    assert result.slide_control_msg == slide_payload
    assert result.case == case


@pytest.mark.asyncio
async def test_emits_pain_point_without_slide_control_when_no_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = PainPointEvent(
        category="capaciteit",
        confidence=0.8,
        trigger_phrase="we hebben te weinig mensen",
        timestamp_ms=222,
    )
    pipeline = _StubPipeline(event=event)
    case_db = _StubCaseDB(result=None)
    injector = SlideInjector(pipeline, case_db, WebSocketConfig(), SlidesConfig())

    pain_socket = _FakeWebSocket()

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return pain_socket
        raise AssertionError(f"unexpected websocket connect to {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("text", "prospect", 999)

    assert result is not None
    assert result.case is None
    assert result.slide_control_msg is None
    assert len(pain_socket.sent) == 1

    payload = json.loads(pain_socket.sent[0])
    assert payload["case_matched"] is False
    assert payload["case_id"] is None
