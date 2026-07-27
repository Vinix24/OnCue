from __future__ import annotations

import json
from pathlib import Path

import pytest

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.core.preset import load_preset
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.eval_utils import OPPORTUNITY_ROUTE_NAMES, is_objection_category
from sales_copilot.modules.detector.objection_detector import ObjectionDetector, ObjectionRouter

# The curated response packs are Pro content and are absent from the OSS
# export; tests that assert on their content only run where the pack exists.
_requires_curated_pack = pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "config" / "objection_responses.yaml").exists(),
    reason="curated Pro response pack absent (OSS export ships detection without the paid kaartenbak)",
)

# The acquisitie vertical preset is Pro content and is absent from the OSS
# export; tests that load it only run where the pack exists.
_requires_acquisitie_pack = pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "config" / "presets" / "acquisitie.yaml").exists(),
    reason="acquisitie vertical preset is Pro content, absent from the OSS export",
)


class _FakeDebouncer(PainPointDebouncer):
    def __init__(self, should: bool = True) -> None:
        super().__init__(cooldown_seconds=45)
        self._should = should

    def should_trigger(self, category: str) -> bool:
        return self._should

    def record_trigger(self, category: str) -> None:
        pass


def test_router_loads_only_objections_by_default() -> None:
    config = DetectorConfig(include_opportunities=False, include_negatives=False)
    router = ObjectionRouter(config)

    assert len(router.routes) == 5
    assert router._opportunity_categories == frozenset()
    assert router._include_opportunities is False


def test_router_loads_opportunity_routes_when_toggled() -> None:
    config = DetectorConfig(include_opportunities=True, include_negatives=False)
    router = ObjectionRouter(config)

    assert len(router.routes) == 9  # 5 objections + 4 opportunity routes
    assert router._opportunity_categories == OPPORTUNITY_ROUTE_NAMES
    assert router._include_opportunities is True

    route_names = {route.name for route in router.routes}
    assert "koopsignaal" in route_names
    assert "gap-behoefte" in route_names


def test_router_opportunity_toggle_overrides_config() -> None:
    config = DetectorConfig(include_opportunities=False, include_negatives=False)
    router = ObjectionRouter(config, include_opportunities=True)

    assert router._include_opportunities is True
    assert len(router.routes) == 9


def test_opportunity_labels_are_not_objection_categories() -> None:
    assert is_objection_category("koopsignaal") is False
    assert is_objection_category("gap-behoefte") is False
    assert is_objection_category("interesse-verdieping") is False
    assert is_objection_category("autoriteit-proces") is False


def test_router_routes_readiness_signal_to_opportunity() -> None:
    config = DetectorConfig(include_opportunities=True)
    router = ObjectionRouter(config)

    match = router.classify("ik wil hier wel gevolg aan geven")

    assert match is not None
    assert match.category == "koopsignaal"


def test_router_without_opportunities_may_overmatch_readiness_as_objection() -> None:
    config = DetectorConfig(include_opportunities=False)
    router = ObjectionRouter(config)

    match = router.classify("ik wil hier wel gevolg aan geven")

    # Without the opportunity class the router has no negative/readiness class,
    # so a readiness signal is forced into the closest objection route.
    assert match is not None
    assert match.category in {"timing", "scope", "autoriteit"}


@_requires_curated_pack
@pytest.mark.asyncio
async def test_detector_emits_opportunity_as_buying_signal(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    config = DetectorConfig(include_opportunities=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config),
        debouncer=_FakeDebouncer(True),
        feature_policy=pro_feature_policy,
    )

    sent: list[str] = []

    async def _fake_ensure_ws_buying_signals():
        class _FakeWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _FakeWs()

    monkeypatch.setattr(detector, "_ensure_ws_buying_signals", _fake_ensure_ws_buying_signals)

    event = await detector.process_transcript("ik wil hier wel gevolg aan geven", "prospect", 123)

    assert event is not None
    assert event.category == "koopsignaal"
    assert event.response_suggestion.startswith("Mooi")
    assert len(sent) == 1
    payload = json.loads(sent[0])
    assert payload["type"] == "buying_signal"
    assert payload["category"] == "koopsignaal"
    assert payload["response_suggestion"] == event.response_suggestion
    assert payload["timestamp_ms"] == 123


@pytest.mark.asyncio
async def test_detector_still_emits_objections_with_opportunities_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DetectorConfig(include_opportunities=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config),
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


# --- Live preset-mode regression guards --------------------------------------
# These tests reproduce the Codex review gate failure: in preset/cold-call mode
# the ObjectionRouter was passed preset.objections but only loaded opportunity
# routes when objections was None, so readiness/interest sentences were forced
# into objection categories.


def test_router_loads_opportunity_routes_alongside_preset_objections() -> None:
    preset = load_preset("sales")
    router = ObjectionRouter(
        DetectorConfig(include_opportunities=True, include_negatives=False),
        objections=preset.objections,
        include_opportunities=True,
    )

    route_names = {route.name for route in router.routes}
    # Preset objections stay present.
    assert "prijs" in route_names
    # Opportunity/readiness routes are merged in.
    assert "koopsignaal" in route_names
    assert router._opportunity_categories == OPPORTUNITY_ROUTE_NAMES
    assert len(router.routes) == 9  # 5 sales objections + 4 opportunity routes


def test_preset_sales_readiness_routes_to_opportunity() -> None:
    preset = load_preset("sales")
    router = ObjectionRouter(
        DetectorConfig(include_opportunities=True),
        objections=preset.objections,
        include_opportunities=True,
    )

    match = router.classify("ik wil hier wel gevolg aan geven")

    assert match is not None
    assert match.category == "koopsignaal"


def test_preset_sales_price_remains_objection() -> None:
    preset = load_preset("sales")
    router = ObjectionRouter(
        DetectorConfig(include_opportunities=True),
        objections=preset.objections,
        include_opportunities=True,
    )

    match = router.classify("het is te duur voor ons")

    assert match is not None
    assert match.category == "prijs"


@_requires_acquisitie_pack
def test_preset_acquisitie_readiness_routes_to_opportunity() -> None:
    preset = load_preset("acquisitie")
    router = ObjectionRouter(
        DetectorConfig(include_opportunities=True),
        objections=preset.objections,
        include_opportunities=True,
    )

    match = router.classify("ik wil hier wel gevolg aan geven")

    assert match is not None
    assert match.category == "koopsignaal"


@_requires_acquisitie_pack
def test_preset_acquisitie_budget_remains_objection() -> None:
    preset = load_preset("acquisitie")
    router = ObjectionRouter(
        DetectorConfig(include_opportunities=True),
        objections=preset.objections,
        include_opportunities=True,
    )

    match = router.classify("daar hebben we geen budget voor")

    assert match is not None
    assert match.category == "geen-budget"


@pytest.mark.asyncio
async def test_detector_in_preset_mode_emits_readiness_as_buying_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset = load_preset("sales")
    config = DetectorConfig(include_opportunities=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        objections=preset.objections,
        include_opportunities=True,
        debouncer=_FakeDebouncer(True),
    )

    sent: list[str] = []

    async def _fake_ensure_ws_buying_signals():
        class _FakeWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _FakeWs()

    monkeypatch.setattr(detector, "_ensure_ws_buying_signals", _fake_ensure_ws_buying_signals)

    event = await detector.process_transcript(
        "ik wil hier wel gevolg aan geven", "prospect", 123
    )

    assert event is not None
    assert event.category == "koopsignaal"
    assert len(sent) == 1
    payload = json.loads(sent[0])
    assert payload["type"] == "buying_signal"


# --- Opportunity response coverage guards ----------------------------------


@_requires_curated_pack
def test_every_opportunity_category_has_response() -> None:
    responses = ObjectionDetector._load_responses(  # noqa: SLF001
        "config/opportunity_responses.yaml", language="nl"
    )
    for category in OPPORTUNITY_ROUTE_NAMES:
        assert responses.get(category), f"no response wired for opportunity '{category}'"


@_requires_curated_pack
@pytest.mark.asyncio
async def test_detector_emits_each_opportunity_category(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    """Every opportunity route produces a buying_signal with a real response."""
    config = DetectorConfig(include_opportunities=True)
    detector = ObjectionDetector(
        config,
        WebSocketConfig(),
        router=ObjectionRouter(config),
        debouncer=_FakeDebouncer(True),
        feature_policy=pro_feature_policy,
    )

    sent: list[str] = []

    async def _fake_ensure_ws_buying_signals():
        class _FakeWs:
            async def send(self, data: str) -> None:
                sent.append(data)

        return _FakeWs()

    monkeypatch.setattr(detector, "_ensure_ws_buying_signals", _fake_ensure_ws_buying_signals)

    for category in OPPORTUNITY_ROUTE_NAMES:
        sent.clear()
        # Use the first configured utterance as the input so the keyword match
        # reliably returns the intended opportunity category.
        text = detector._router._route_utterances[category][0]
        event = await detector.process_transcript(text, "prospect", 1)

        assert event is not None, f"no event for opportunity '{category}'"
        assert event.category == category
        assert event.response_suggestion
        assert len(sent) == 1
        payload = json.loads(sent[0])
        assert payload["type"] == "buying_signal"
        assert payload["category"] == category
        assert payload["response_suggestion"] == event.response_suggestion
