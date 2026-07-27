"""Tests for the unified objection/buying-signal card path and the Free/Pro
response-playbook gate ("detectie free, kaartenbak Pro").

Covers:
- ObjectionDetector is the sole emitter of objection + buying_signal cards;
  the WindowClassifier handler no longer publishes on those channels.
- Non-Pro sessions get a shared teaser response_suggestion on objection,
  buying_signal AND pain_point emissions.
- Pro sessions get the curated response_suggestion on all three.
- The WindowClassifier's pain_point (-> injector) and doubt (-> coaching)
  paths are unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sales_copilot.auth.feature_policy import (
    FEATURE_RESPONSE_PLAYBOOK,
    RESPONSE_LOCKED_TEASER,
    FeaturePolicy,
    resolve_response_suggestion,
)
from sales_copilot.core.config import DetectorConfig, SlidesConfig, WebSocketConfig
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.detector import __main__ as detector_main
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.objection_detector import ObjectionDetector, ObjectionRouter
from sales_copilot.modules.detector.pipeline import PainPointEvent
from sales_copilot.modules.detector.window_classifier import WindowDetection
from sales_copilot.modules.slides.case_db import Case

# The curated response packs are Pro content and are absent from the OSS
# export; tests that assert on their content only run where the pack exists.
_requires_curated_pack = pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "config" / "objection_responses.yaml").exists(),
    reason="curated Pro response pack absent (OSS export ships detection without the paid kaartenbak)",
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        pass


class _FakeDebouncer(PainPointDebouncer):
    def __init__(self) -> None:
        super().__init__(cooldown_seconds=45)
        self.recorded: list[str] = []

    def should_trigger(self, category: str) -> bool:
        return True

    def record_trigger(self, category: str) -> None:
        self.recorded.append(category)


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


def _matching_case(description: str | None) -> Case:
    return Case(
        id="case-gate-playbook",
        title="Playbook gate case",
        industry=None,
        pain_point="offerteproces",
        description=description,
        slide_html="<section></section>",
        metrics=None,
        priority=1,
    )


# ---------------------------------------------------------------------------
# 1. Single-source objection/buying_signal — no duplicate from WindowClassifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_classifier_no_longer_emits_objection_or_buying_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """objection/buying_signal detections from the WindowClassifier handler must
    not publish to /ws/objections or /ws/buying-signals anymore — ObjectionDetector
    (the fast embedding-match path) is now the sole emitter for those channels."""
    published: list[tuple[str, dict]] = []

    async def _fake_publish_ws(ws_config, channel, payload):
        published.append((channel, payload))

    monkeypatch.setattr(detector_main, "_publish_ws", _fake_publish_ws)

    ws_config = WebSocketConfig()
    config = DetectorConfig(enable_objection_detection=True)

    class _StubInjector:
        async def handle_window_detection(self, **kwargs):
            raise AssertionError("pain_point handler must not run for objection/buying_signal")

    for category, subcategory in (("objection", "prijs"), ("buying_signal", "koopsignaal")):
        detection = WindowDetection(
            category=category,
            subcategory=subcategory,
            confidence=0.9,
            evidence_quote="het is te duur voor ons",
            reasoning="test",
        )
        await detector_main._dispatch_window_detection(
            detection, 1000, _StubInjector(), ws_config, config, {}
        )

    assert published == []


@pytest.mark.asyncio
async def test_one_detected_objection_produces_exactly_one_emission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single detected objection produces exactly ONE emission on /ws/objections,
    even when both the WindowClassifier handler and the fast ObjectionDetector
    process the same utterance."""
    published: list[tuple[str, dict]] = []

    async def _fake_publish_ws(ws_config, channel, payload):
        published.append((channel, payload))

    monkeypatch.setattr(detector_main, "_publish_ws", _fake_publish_ws)

    config = DetectorConfig(enable_objection_detection=True)
    ws_config = WebSocketConfig()

    class _StubInjector:
        async def handle_window_detection(self, **kwargs):
            raise AssertionError("unexpected pain_point path")

    detection = WindowDetection(
        category="objection",
        subcategory="prijs",
        confidence=0.9,
        evidence_quote="het is te duur voor ons",
        reasoning="test",
    )
    await detector_main._dispatch_window_detection(
        detection, 1000, _StubInjector(), ws_config, config, {}
    )
    assert published == [], "WindowClassifier handler must not emit objection cards"

    objection_sent: list[str] = []

    async def _fake_ensure_ws():
        class _Ws:
            async def send(self, data: str) -> None:
                objection_sent.append(data)

        return _Ws()

    detector = ObjectionDetector(
        config,
        ws_config,
        router=ObjectionRouter(config),
        debouncer=_FakeDebouncer(),
    )
    monkeypatch.setattr(detector, "_ensure_ws", _fake_ensure_ws)

    event = await detector.process_transcript("het is te duur voor ons", "prospect", 1000)

    assert event is not None
    assert len(objection_sent) == 1, "expected exactly one objection emission"


# ---------------------------------------------------------------------------
# 2 & 3. Free/Pro response gate — objection, buying_signal, pain_point
# ---------------------------------------------------------------------------


@_requires_curated_pack
@pytest.mark.asyncio
async def test_free_entitlement_gates_objection_response_to_teaser(
    monkeypatch: pytest.MonkeyPatch,
    free_feature_policy: FeaturePolicy,
) -> None:
    config = DetectorConfig(enable_objection_detection=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config),
        debouncer=_FakeDebouncer(),
        feature_policy=free_feature_policy,
    )
    sent: list[str] = []

    async def _fake_ensure_ws():
        class _Ws:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _Ws()

    monkeypatch.setattr(detector, "_ensure_ws", _fake_ensure_ws)
    event = await detector.process_transcript("het is te duur voor ons", "prospect", 1)

    assert event is not None
    assert event.response_suggestion  # curated text always kept on the dataclass
    payload = json.loads(sent[0])
    assert payload["response_suggestion"] == RESPONSE_LOCKED_TEASER


@_requires_curated_pack
@pytest.mark.asyncio
async def test_pro_entitlement_carries_curated_objection_response(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    config = DetectorConfig(enable_objection_detection=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config),
        debouncer=_FakeDebouncer(),
        feature_policy=pro_feature_policy,
    )
    sent: list[str] = []

    async def _fake_ensure_ws():
        class _Ws:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _Ws()

    monkeypatch.setattr(detector, "_ensure_ws", _fake_ensure_ws)
    event = await detector.process_transcript("het is te duur voor ons", "prospect", 1)

    assert event is not None
    payload = json.loads(sent[0])
    assert payload["response_suggestion"] == event.response_suggestion
    assert payload["response_suggestion"] != RESPONSE_LOCKED_TEASER


@_requires_curated_pack
@pytest.mark.asyncio
async def test_free_entitlement_gates_buying_signal_response_to_teaser(
    monkeypatch: pytest.MonkeyPatch,
    free_feature_policy: FeaturePolicy,
) -> None:
    config = DetectorConfig(include_opportunities=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config),
        debouncer=_FakeDebouncer(),
        feature_policy=free_feature_policy,
    )
    sent: list[str] = []

    async def _fake_ensure_ws_buying_signals():
        class _Ws:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _Ws()

    monkeypatch.setattr(detector, "_ensure_ws_buying_signals", _fake_ensure_ws_buying_signals)
    event = await detector.process_transcript("ik wil hier wel gevolg aan geven", "prospect", 1)

    assert event is not None
    payload = json.loads(sent[0])
    assert payload["response_suggestion"] == RESPONSE_LOCKED_TEASER


@_requires_curated_pack
@pytest.mark.asyncio
async def test_pro_entitlement_carries_curated_buying_signal_response(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    config = DetectorConfig(include_opportunities=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config),
        debouncer=_FakeDebouncer(),
        feature_policy=pro_feature_policy,
    )
    sent: list[str] = []

    async def _fake_ensure_ws_buying_signals():
        class _Ws:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _Ws()

    monkeypatch.setattr(detector, "_ensure_ws_buying_signals", _fake_ensure_ws_buying_signals)
    event = await detector.process_transcript("ik wil hier wel gevolg aan geven", "prospect", 1)

    assert event is not None
    payload = json.loads(sent[0])
    assert payload["response_suggestion"] == event.response_suggestion
    assert payload["response_suggestion"] != RESPONSE_LOCKED_TEASER


@pytest.mark.asyncio
async def test_free_entitlement_gates_pain_point_response_to_teaser(
    monkeypatch: pytest.MonkeyPatch,
    free_feature_policy: FeaturePolicy,
) -> None:
    event = PainPointEvent(
        category="offerteproces",
        confidence=0.9,
        trigger_phrase="we verliezen offertes",
        timestamp_ms=100,
    )
    injector = SlideInjector(
        _StubPipeline(event),
        _StubCaseDB(_matching_case("Case-omschrijving met concrete aanpak")),
        WebSocketConfig(),
        SlidesConfig(),
        feature_policy=free_feature_policy,
    )
    pain_socket = _FakeWebSocket()

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return pain_socket
        raise AssertionError(f"unexpected connect: {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    await injector.process_transcript("we verliezen offertes", "prospect", 100)

    payload = json.loads(pain_socket.sent[0])
    assert payload["response_suggestion"] == RESPONSE_LOCKED_TEASER


@pytest.mark.asyncio
async def test_pro_entitlement_carries_curated_pain_point_response(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    description = "Case-omschrijving met concrete aanpak"
    event = PainPointEvent(
        category="offerteproces",
        confidence=0.9,
        trigger_phrase="we verliezen offertes",
        timestamp_ms=100,
    )
    injector = SlideInjector(
        _StubPipeline(event),
        _StubCaseDB(_matching_case(description)),
        WebSocketConfig(),
        SlidesConfig(),
        feature_policy=pro_feature_policy,
    )
    sockets = {"pain-points": _FakeWebSocket(), "slide-control": _FakeWebSocket()}

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return sockets["pain-points"]
        if url.endswith("/ws/slide-control"):
            return sockets["slide-control"]
        raise AssertionError(f"unexpected connect: {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    await injector.process_transcript("we verliezen offertes", "prospect", 100)

    payload = json.loads(sockets["pain-points"].sent[0])
    assert payload["response_suggestion"] == description


@pytest.mark.asyncio
async def test_handle_window_detection_pain_point_response_gate(
    monkeypatch: pytest.MonkeyPatch,
    free_feature_policy: FeaturePolicy,
    pro_feature_policy: FeaturePolicy,
) -> None:
    """handle_window_detection (the WindowClassifier -> injector path) applies the
    same Free/Pro response gate as process_transcript."""
    description = "Case-omschrijving met concrete aanpak"

    for policy, expected in (
        (free_feature_policy, RESPONSE_LOCKED_TEASER),
        (pro_feature_policy, description),
    ):
        injector = SlideInjector(
            _StubPipeline(None),
            _StubCaseDB(_matching_case(description)),
            WebSocketConfig(),
            SlidesConfig(),
            feature_policy=policy,
        )
        pain_socket = _FakeWebSocket()
        slide_socket = _FakeWebSocket()

        async def fake_connect(url: str, _pain=pain_socket, _slide=slide_socket) -> _FakeWebSocket:
            if url.endswith("/ws/pain-points"):
                return _pain
            if url.endswith("/ws/slide-control"):
                return _slide
            raise AssertionError(f"unexpected connect: {url}")

        monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

        await injector.handle_window_detection(
            category="offerteproces",
            confidence=0.9,
            evidence_quote="we verliezen offertes",
            timestamp_ms=200,
        )

        payload = json.loads(pain_socket.sent[0])
        assert payload["response_suggestion"] == expected


def test_feature_response_playbook_constant() -> None:
    assert FEATURE_RESPONSE_PLAYBOOK == "coaching.response_playbook"


def test_pro_policy_allows_response_playbook(pro_feature_policy: FeaturePolicy) -> None:
    assert pro_feature_policy.allows(FEATURE_RESPONSE_PLAYBOOK) is True


def test_free_policy_denies_response_playbook(free_feature_policy: FeaturePolicy) -> None:
    assert free_feature_policy.allows(FEATURE_RESPONSE_PLAYBOOK) is False


def test_resolve_response_suggestion_empty_curated_returns_empty_regardless_of_tier(
    free_feature_policy: FeaturePolicy, pro_feature_policy: FeaturePolicy
) -> None:
    assert resolve_response_suggestion(None, feature_policy=free_feature_policy) == ""
    assert resolve_response_suggestion("", feature_policy=pro_feature_policy) == ""


def test_resolve_response_suggestion_gates_by_tier(
    free_feature_policy: FeaturePolicy, pro_feature_policy: FeaturePolicy
) -> None:
    curated = "Bel de klant terug binnen 24 uur."
    assert resolve_response_suggestion(curated, feature_policy=free_feature_policy) == RESPONSE_LOCKED_TEASER
    assert resolve_response_suggestion(curated, feature_policy=pro_feature_policy) == curated


# ---------------------------------------------------------------------------
# 4. WindowClassifier pain_point and doubt paths remain unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_classifier_pain_point_path_unchanged() -> None:
    calls: list[dict] = []

    class _StubInjector:
        async def handle_window_detection(self, **kwargs):
            calls.append(kwargs)

    detection = WindowDetection(
        category="pain_point",
        subcategory="offerteproces",
        confidence=0.9,
        evidence_quote="we verliezen offertes",
        reasoning="test",
    )
    await detector_main._dispatch_window_detection(
        detection, 500, _StubInjector(), WebSocketConfig(), DetectorConfig(), {}
    )

    assert len(calls) == 1
    assert calls[0]["category"] == "offerteproces"
    assert calls[0]["evidence_quote"] == "we verliezen offertes"
    assert calls[0]["timestamp_ms"] == 500


@pytest.mark.asyncio
async def test_window_classifier_doubt_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[tuple[str, dict]] = []

    async def _fake_publish_ws(ws_config, channel, payload):
        published.append((channel, payload))

    monkeypatch.setattr(detector_main, "_publish_ws", _fake_publish_ws)

    class _StubInjector:
        async def handle_window_detection(self, **kwargs):
            raise AssertionError("doubt must not hit the pain_point path")

    detection = WindowDetection(
        category="doubt",
        subcategory="onzeker",
        confidence=0.7,
        evidence_quote="ik weet het niet zeker",
        reasoning="test",
    )
    await detector_main._dispatch_window_detection(
        detection, 700, _StubInjector(), WebSocketConfig(), DetectorConfig(), {"prospect": "klant"}
    )

    assert len(published) == 1
    channel, payload = published[0]
    assert channel == "coaching"
    assert payload["type"] == "coaching_alert"
    assert payload["subtype"] == "doubt"
    assert payload["speaker_label"] == "klant"
