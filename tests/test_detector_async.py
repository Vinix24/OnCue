"""Tests for the async live detection path (aprocess).

These verify that the LLM confirmation is moved off the live critical path:
- Middle-band matches emit a provisional event immediately.
- The LLM confirmation runs in the background.
- Hooks receive both the provisional and final signals.
- No duplicate ``pain_point`` events are produced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.llm_confirm import PainPointDetection
from sales_copilot.modules.detector.pipeline import DetectionPipeline, PainPointEvent
from sales_copilot.modules.detector.router import RouteMatch


@dataclass
class _FakeRouter:
    match: RouteMatch | None

    def classify(self, text: str) -> RouteMatch | None:  # noqa: ARG002
        return self.match


@dataclass
class _AsyncFakeLLM:
    confirmation: PainPointDetection | None
    delay_ms: float = 0.0
    called: int = 0

    async def confirm_async(self, fragment: str) -> PainPointDetection | None:  # noqa: ARG002
        self.called += 1
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000.0)
        return self.confirmation

    def confirm(self, fragment: str) -> PainPointDetection | None:  # noqa: ARG002
        return None


class _SpyHooks:
    def __init__(self) -> None:
        self.events: list[PainPointEvent | None] = []
        self.llm_ends: list[tuple[Any, float]] = []

    def on_classify_start(self) -> None:
        pass

    def on_classify_end(self, match: RouteMatch | None, latency_ms: float) -> None:  # noqa: ARG002
        pass

    def on_llm_start(self) -> None:
        pass

    def on_llm_end(
        self,
        confirmation: object | None,
        latency_ms: float,
        tokens: dict[str, int] | None,  # noqa: ARG002
    ) -> None:
        self.llm_ends.append((confirmation, latency_ms))

    def on_event(self, event: PainPointEvent | None) -> None:
        self.events.append(event)


class _PassThroughDebouncer(PainPointDebouncer):
    def __init__(self) -> None:
        super().__init__(cooldown_seconds=0)

    def should_trigger(self, category: str) -> bool:  # noqa: ARG002
        return True

    def record_trigger(self, category: str) -> None:  # noqa: ARG002
        pass


async def test_aprocess_middle_band_emits_provisional_immediately() -> None:
    confirmation = PainPointDetection(
        category="offerteproces",
        confidence=0.92,
        trigger_phrase="offertes",
    )
    llm = _AsyncFakeLLM(confirmation, delay_ms=50.0)
    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.65, "uncertain", "embedding")),
        llm_client=llm,
        debouncer=_PassThroughDebouncer(),
    )

    start = asyncio.get_event_loop().time()
    event = await pipeline.aprocess("we zitten uren aan offertes", speaker="prospect")
    elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000

    assert event is not None
    assert event.category == "offerteproces"
    assert event.live is True
    assert event.provisional is True
    assert elapsed_ms < 20.0, "aprocess must not wait for the LLM"

    # Give the background task time to finish.
    await asyncio.sleep(0.1)

    assert llm.called == 1


async def test_aprocess_background_confirm_calls_enrichment() -> None:
    confirmation = PainPointDetection(
        category="offerteproces",
        confidence=0.92,
        trigger_phrase="offertes",
    )
    llm = _AsyncFakeLLM(confirmation, delay_ms=10.0)
    enrichment_calls: list[PainPointEvent | None] = []

    async def on_enrichment(event: PainPointEvent | None) -> None:
        enrichment_calls.append(event)

    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.65, "uncertain", "embedding")),
        llm_client=llm,
        debouncer=_PassThroughDebouncer(),
    )

    provisional = await pipeline.aprocess(
        "we zitten uren aan offertes",
        speaker="prospect",
        on_enrichment=on_enrichment,
    )

    assert provisional is not None
    assert provisional.provisional is True

    await asyncio.sleep(0.05)

    assert len(enrichment_calls) == 1
    final = enrichment_calls[0]
    assert final is not None
    assert final.provisional is False
    assert final.category == "offerteproces"


async def test_aprocess_hooks_receive_provisional_and_final() -> None:
    confirmation = PainPointDetection(
        category="offerteproces",
        confidence=0.92,
        trigger_phrase="offertes",
    )
    llm = _AsyncFakeLLM(confirmation, delay_ms=10.0)
    hooks = _SpyHooks()
    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.65, "uncertain", "embedding")),
        llm_client=llm,
        debouncer=_PassThroughDebouncer(),
        hooks=hooks,
    )

    await pipeline.aprocess("we zitten uren aan offertes", speaker="prospect")
    assert len(hooks.events) == 1
    assert hooks.events[0] is not None
    assert hooks.events[0].provisional is True

    await asyncio.sleep(0.05)

    assert len(hooks.llm_ends) == 1
    assert hooks.llm_ends[0][0] is confirmation
    assert len(hooks.events) == 2
    assert hooks.events[1] is not None
    assert hooks.events[1].provisional is False


async def test_aprocess_rejection_calls_enrichment_with_none() -> None:
    llm = _AsyncFakeLLM(None, delay_ms=10.0)
    enrichment_calls: list[PainPointEvent | None] = ["pending"]  # type: ignore[list-item]

    async def on_enrichment(event: PainPointEvent | None) -> None:
        enrichment_calls[0] = event

    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.65, "uncertain", "embedding")),
        llm_client=llm,
        debouncer=_PassThroughDebouncer(),
    )

    provisional = await pipeline.aprocess(
        "we zitten uren aan offertes",
        speaker="prospect",
        on_enrichment=on_enrichment,
    )

    assert provisional is not None
    assert provisional.provisional is True

    await asyncio.sleep(0.05)

    assert enrichment_calls[0] is None


async def test_aprocess_high_confidence_skips_llm() -> None:
    llm = _AsyncFakeLLM(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.95, "high", "embedding")),
        llm_client=llm,
        debouncer=_PassThroughDebouncer(),
    )

    event = await pipeline.aprocess("we zitten uren aan offertes", speaker="prospect")

    assert event is not None
    assert event.category == "offerteproces"
    assert event.provisional is False
    assert llm.called == 0


async def test_aprocess_low_confidence_returns_none() -> None:
    llm = _AsyncFakeLLM(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.3, "none", "embedding")),
        llm_client=llm,
        debouncer=_PassThroughDebouncer(),
    )

    event = await pipeline.aprocess("we zitten uren aan offertes", speaker="prospect")

    assert event is None
    assert llm.called == 0


async def test_aprocess_self_speaker_skipped() -> None:
    pipeline = DetectionPipeline(
        config=DetectorConfig(only_classify_prospect=True),
        router=_FakeRouter(RouteMatch("offerteproces", 0.95, "high", "embedding")),
        llm_client=_AsyncFakeLLM(None),
        debouncer=_PassThroughDebouncer(),
    )

    event = await pipeline.aprocess("we zitten uren aan offertes", speaker="self")

    assert event is None
