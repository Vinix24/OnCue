"""Gate tests: slide-control injection is Pro-only, pain_point events are always sent."""

from __future__ import annotations

import json

import pytest

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.auth.license_format import FEATURE_DYNAMIC_SLIDES
from sales_copilot.core.config import SlidesConfig, WebSocketConfig
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.copilot.slide_generator import GeneratedSlide
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

    def process(self, text: str, speaker: str) -> PainPointEvent | None:
        return self._event


class _StubCaseDB:
    def __init__(self, result: Case | None) -> None:
        self._result = result

    async def find_case(self, pain_point: str, industry: str | None = None) -> Case | None:
        return self._result


class _StubSlideGenerator:
    def __init__(self, generated: GeneratedSlide | None) -> None:
        self.generated = generated
        self.calls: list[str] = []

    async def generate(self, category: str, *, on_partial=None) -> GeneratedSlide | None:
        self.calls.append(category)
        return self.generated

    def slide_payload(self, category: str, generated: GeneratedSlide) -> dict[str, object]:
        return {"title": generated.title}


def _matching_case() -> Case:
    return Case(
        id="case-gate-001",
        title="Gate test case",
        industry=None,
        pain_point="offerteproces",
        description=None,
        slide_html="<section></section>",
        metrics=None,
        priority=1,
    )


def _pain_point_event() -> PainPointEvent:
    return PainPointEvent(
        category="offerteproces",
        confidence=0.92,
        trigger_phrase="we verliezen offertes",
        timestamp_ms=5000,
    )


# ---------------------------------------------------------------------------
# Free tier: slide-control suppressed; pain_point always flows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_tier_process_transcript_no_slide_control_case_match(
    monkeypatch: pytest.MonkeyPatch,
    free_feature_policy: FeaturePolicy,
) -> None:
    """Free tier + matching case -> pain_point sent, NO slide-control sent."""
    injector = SlideInjector(
        _StubPipeline(_pain_point_event()),
        _StubCaseDB(_matching_case()),
        WebSocketConfig(),
        SlidesConfig(),
        feature_policy=free_feature_policy,
    )

    pain_socket = _FakeWebSocket()

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return pain_socket
        raise AssertionError(f"unexpected slide-control connect on free tier: {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("we verliezen offertes", "prospect", 5000)

    assert result is not None
    assert result.slide_control_msg is None
    assert len(pain_socket.sent) == 1
    pain_payload = json.loads(pain_socket.sent[0])
    assert pain_payload["type"] == "pain_point"
    assert pain_payload["category"] == "offerteproces"
    assert pain_payload["case_matched"] is True


@pytest.mark.asyncio
async def test_free_tier_process_transcript_no_dynamic_slide(
    monkeypatch: pytest.MonkeyPatch,
    free_feature_policy: FeaturePolicy,
) -> None:
    """Free tier + no case + dynamic_slides=True -> pain_point sent, NO slide-control sent."""
    generated = GeneratedSlide(title="T", description="D", metrics=["M"])
    slide_generator = _StubSlideGenerator(generated)
    injector = SlideInjector(
        _StubPipeline(_pain_point_event()),
        _StubCaseDB(None),
        WebSocketConfig(),
        SlidesConfig(dynamic_slides=True),
        slide_generator=slide_generator,
        feature_policy=free_feature_policy,
    )

    pain_socket = _FakeWebSocket()

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return pain_socket
        raise AssertionError(f"unexpected slide-control connect on free tier: {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("we verliezen offertes", "prospect", 5000)

    assert result is not None
    assert result.slide_control_msg is None
    assert slide_generator.calls == []
    assert len(pain_socket.sent) == 1


@pytest.mark.asyncio
async def test_free_tier_handle_window_detection_no_slide_control(
    monkeypatch: pytest.MonkeyPatch,
    free_feature_policy: FeaturePolicy,
) -> None:
    """Free tier handle_window_detection with matching case -> pain_point sent, NO slide-control."""
    injector = SlideInjector(
        _StubPipeline(None),
        _StubCaseDB(_matching_case()),
        WebSocketConfig(),
        SlidesConfig(),
        feature_policy=free_feature_policy,
    )

    pain_socket = _FakeWebSocket()

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return pain_socket
        raise AssertionError(f"unexpected slide-control connect on free tier: {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    await injector.handle_window_detection(
        category="offerteproces",
        confidence=0.90,
        evidence_quote="we verliezen offertes",
        timestamp_ms=6000,
    )

    assert len(pain_socket.sent) == 1
    pain_payload = json.loads(pain_socket.sent[0])
    assert pain_payload["type"] == "pain_point"
    assert pain_payload["case_matched"] is True


# ---------------------------------------------------------------------------
# Pro tier: slide-control flows as expected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pro_tier_process_transcript_navigate_to_case(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    """Pro tier + matching case -> navigate_to_case slide_control_msg."""
    injector = SlideInjector(
        _StubPipeline(_pain_point_event()),
        _StubCaseDB(_matching_case()),
        WebSocketConfig(),
        SlidesConfig(),
        feature_policy=pro_feature_policy,
    )

    sockets = {
        "pain-points": _FakeWebSocket(),
        "slide-control": _FakeWebSocket(),
    }

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return sockets["pain-points"]
        if url.endswith("/ws/slide-control"):
            return sockets["slide-control"]
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("we verliezen offertes", "prospect", 5000)

    assert result is not None
    assert result.slide_control_msg == {"action": "navigate_to_case", "slide_id": "case-gate-001"}
    assert len(sockets["pain-points"].sent) == 1
    assert len(sockets["slide-control"].sent) == 1
    slide_payload = json.loads(sockets["slide-control"].sent[0])
    assert slide_payload["action"] == "navigate_to_case"
    assert slide_payload["slide_id"] == "case-gate-001"


@pytest.mark.asyncio
async def test_pro_tier_process_transcript_inject_generated_slide(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    """Pro tier + no case + dynamic_slides=True -> inject_generated_slide."""
    generated = GeneratedSlide(
        title="Sneller offerteren",
        description="Automatisering halveert doorlooptijd.",
        metrics=["-50% doorlooptijd"],
    )
    slide_generator = _StubSlideGenerator(generated)
    injector = SlideInjector(
        _StubPipeline(_pain_point_event()),
        _StubCaseDB(None),
        WebSocketConfig(),
        SlidesConfig(dynamic_slides=True),
        slide_generator=slide_generator,
        feature_policy=pro_feature_policy,
    )

    sockets = {
        "pain-points": _FakeWebSocket(),
        "slide-control": _FakeWebSocket(),
    }

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return sockets["pain-points"]
        if url.endswith("/ws/slide-control"):
            return sockets["slide-control"]
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("we verliezen offertes", "prospect", 5000)

    assert result is not None
    assert result.slide_control_msg is not None
    assert result.slide_control_msg["action"] == "inject_generated_slide"
    assert result.slide_control_msg["pain_point"] == "offerteproces"
    assert len(sockets["slide-control"].sent) == 1
    slide_payload = json.loads(sockets["slide-control"].sent[0])
    assert slide_payload["action"] == "inject_generated_slide"


# ---------------------------------------------------------------------------
# Feature constant sanity check
# ---------------------------------------------------------------------------


def test_feature_dynamic_slides_constant() -> None:
    """FEATURE_DYNAMIC_SLIDES has the expected string value."""
    assert FEATURE_DYNAMIC_SLIDES == "presentation.dynamic_slides"


def test_pro_policy_allows_dynamic_slides(pro_feature_policy: FeaturePolicy) -> None:
    """Pro policy grants presentation.dynamic_slides."""
    assert pro_feature_policy.allows(FEATURE_DYNAMIC_SLIDES) is True


def test_free_policy_denies_dynamic_slides(free_feature_policy: FeaturePolicy) -> None:
    """Free policy denies presentation.dynamic_slides."""
    assert free_feature_policy.allows(FEATURE_DYNAMIC_SLIDES) is False
