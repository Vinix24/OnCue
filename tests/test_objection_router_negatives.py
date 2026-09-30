from __future__ import annotations

import json

import pytest

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.eval_utils import NEGATIVE_ROUTE_NAMES, is_objection_category
from sales_copilot.modules.detector.objection_detector import ObjectionDetector, ObjectionRouter
from sales_copilot.modules.detector.router import PainPointRouter

# The exact live over-matching failure reported in the dispatch: rambling
# prospect speech tagged as an objection/opportunity route instead of being
# recognized as generic non-objection speech.
_LIVE_OVER_MATCH_UTTERANCE = "ja ja ja, dat was de eerste vraag van IT die ik kreeg"


class _FakeDebouncer(PainPointDebouncer):
    def __init__(self, should: bool = True) -> None:
        super().__init__(cooldown_seconds=45)
        self._should = should

    def should_trigger(self, category: str) -> bool:
        return self._should

    def record_trigger(self, category: str) -> None:
        pass


def test_router_loads_negative_route_by_default() -> None:
    config = DetectorConfig()
    router = ObjectionRouter(config, include_opportunities=False)

    assert router._include_negatives is True
    assert router._negative_categories == NEGATIVE_ROUTE_NAMES
    route_names = {route.name for route in router.routes}
    assert "negative" in route_names


def test_router_omits_negative_route_when_disabled() -> None:
    config = DetectorConfig(include_negatives=False)
    router = ObjectionRouter(config, include_opportunities=False)

    assert router._include_negatives is False
    assert router._negative_categories == frozenset()
    route_names = {route.name for route in router.routes}
    assert "negative" not in route_names


def test_router_negative_toggle_overrides_config() -> None:
    config = DetectorConfig(include_negatives=False)
    router = ObjectionRouter(config, include_opportunities=False, include_negatives=True)

    assert router._include_negatives is True
    assert "negative" in {route.name for route in router.routes}


def test_negative_label_is_not_an_objection_category() -> None:
    assert is_objection_category("negative") is False


def test_negative_utterance_returns_none_instead_of_forced_objection() -> None:
    """Structural fix: a generic non-objection utterance that a negatives-off
    router forces into the nearest objection category now returns no match."""
    config = DetectorConfig()

    router_without_negatives = ObjectionRouter(
        config, include_opportunities=True, include_negatives=False
    )
    baseline_match = router_without_negatives.classify(_LIVE_OVER_MATCH_UTTERANCE)
    assert baseline_match is not None  # reproduces the reported over-match

    router_with_negatives = ObjectionRouter(
        config, include_opportunities=True, include_negatives=True
    )
    fixed_match = router_with_negatives.classify(_LIVE_OVER_MATCH_UTTERANCE)
    assert fixed_match is None


@pytest.mark.parametrize(
    "text",
    [
        "kunt u dat nog eens uitleggen",
        "wat zijn de volgende stappen",
        "ik begrijp het idee",
        "het budget is goedgekeurd",
    ],
)
def test_representative_negatives_return_none(text: str) -> None:
    config = DetectorConfig()
    router = ObjectionRouter(config, include_opportunities=True, include_negatives=True)

    assert router.classify(text) is None


def test_real_objection_still_classifies_with_negatives_enabled() -> None:
    """No regression: a genuine objection still routes correctly when the
    negative class is live."""
    config = DetectorConfig()
    router = ObjectionRouter(config, include_opportunities=True, include_negatives=True)

    match = router.classify("het is te duur voor ons")

    assert match is not None
    assert match.category == "prijs"


def test_real_opportunity_still_classifies_with_negatives_enabled() -> None:
    """No regression: opportunity routing keeps working alongside the negative class."""
    config = DetectorConfig()
    router = ObjectionRouter(config, include_opportunities=True, include_negatives=True)

    match = router.classify("ik wil hier wel gevolg aan geven")

    assert match is not None
    assert match.category == "koopsignaal"


def test_pain_point_router_also_gets_the_negative_class_by_default() -> None:
    """Structural fix extended 2026-09-05: PainPointRouter is a separate
    class/instance from ObjectionRouter but gets the SAME negative-class fix,
    wired the same way (INCLUDE_NEGATIVES / config/negatives.yaml), because
    the same live over-match was reproduced there ("een maand of vijf, zes"
    classified as pain point `rapportage` at a hardcoded 0.95). This
    supersedes the old assumption that PainPointRouter was untouched by the
    negative class — see scripts/eval_pain_point_precision.py."""
    config = DetectorConfig()
    router = PainPointRouter(config)

    assert router._include_negatives is True
    assert router._negative_categories == NEGATIVE_ROUTE_NAMES
    assert len(router.routes) == 13  # 12 pain points + the shared negative route
    assert "negative" in {route.name for route in router.routes}

    # A real pain point still classifies correctly with the negative class live.
    match = router.classify("we zitten uren aan offertes")
    assert match is not None
    assert match.category == "offerteproces"


def test_pain_point_router_omits_negative_route_when_disabled() -> None:
    config = DetectorConfig(include_negatives=False)
    router = PainPointRouter(config)

    assert router._include_negatives is False
    assert router._negative_categories == frozenset()
    assert len(router.routes) == 12
    assert "negative" not in {route.name for route in router.routes}


def test_pain_point_router_negative_toggle_overrides_config() -> None:
    config = DetectorConfig(include_negatives=True)
    router = PainPointRouter(config, include_negatives=False)

    assert router._include_negatives is False
    assert "negative" not in {route.name for route in router.routes}


@pytest.mark.asyncio
async def test_detector_emits_no_event_for_negative_utterance(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    config = DetectorConfig()
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config, include_opportunities=True, include_negatives=True),
        debouncer=_FakeDebouncer(True),
        feature_policy=pro_feature_policy,
    )

    sent: list[str] = []

    async def _fake_ensure_ws():
        class _FakeWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _FakeWs()

    async def _fake_ensure_ws_buying_signals():
        class _FakeWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _FakeWs()

    monkeypatch.setattr(detector, "_ensure_ws", _fake_ensure_ws)
    monkeypatch.setattr(detector, "_ensure_ws_buying_signals", _fake_ensure_ws_buying_signals)

    event = await detector.process_transcript(_LIVE_OVER_MATCH_UTTERANCE, "prospect", 123)

    assert event is None
    assert sent == []


@pytest.mark.asyncio
async def test_detector_still_emits_objection_with_negatives_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DetectorConfig()
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config, include_opportunities=True, include_negatives=True),
        debouncer=_FakeDebouncer(True),
    )

    sent: list[str] = []

    async def _fake_ensure_ws():
        class _FakeWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _FakeWs()

    monkeypatch.setattr(detector, "_ensure_ws", _fake_ensure_ws)

    event = await detector.process_transcript("het is te duur voor ons", "prospect", 123)

    assert event is not None
    assert event.category == "prijs"
    assert len(sent) == 1
    payload = json.loads(sent[0])
    assert payload["type"] == "objection"
    assert payload["category"] == "prijs"
