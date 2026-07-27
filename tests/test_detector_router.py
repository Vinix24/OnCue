import asyncio
import time

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.router import PainPointRouter, RouteMatch


@pytest.fixture(scope="session")
def pain_point_router() -> PainPointRouter:
    config = DetectorConfig.from_env()
    return PainPointRouter(config)


def test_loads_all_routes(pain_point_router: PainPointRouter) -> None:
    assert len(pain_point_router.routes) == 12


def test_classifies_offerteproces(pain_point_router: PainPointRouter) -> None:
    match = pain_point_router.classify("we zitten uren aan offertes")

    assert match is not None
    assert match.category == "offerteproces"
    assert match.confidence >= 0.80


def test_returns_none_for_non_match(pain_point_router: PainPointRouter) -> None:
    assert pain_point_router.classify("het weer is mooi vandaag") is None


def test_classification_under_100ms(pain_point_router: PainPointRouter) -> None:
    pain_point_router.classify("warmup")
    start = time.perf_counter()
    pain_point_router.classify("we zitten uren aan offertes")
    duration = time.perf_counter() - start

    assert duration < 0.1


@pytest.mark.asyncio
async def test_classify_async_matches_sync(pain_point_router: PainPointRouter) -> None:
    text = "we zitten uren aan offertes"
    sync_match = pain_point_router.classify(text)
    async_match = await pain_point_router.classify_async(text)

    assert async_match is not None
    assert async_match == sync_match


@pytest.mark.asyncio
async def test_classify_async_times_out_to_none() -> None:
    """A stalled model load/inference is bounded and degrades to no match.

    Guards the detector event loop: classify runs off-loop in a thread and a hang
    must return None rather than wedging all detection.
    """
    router = PainPointRouter(DetectorConfig.from_env())
    router._CLASSIFY_TIMEOUT_S = 0.05  # noqa: SLF001

    def _slow(_text: str) -> RouteMatch:
        time.sleep(0.3)
        return RouteMatch(category="x", confidence=1.0, tier="high")

    router.classify = _slow  # type: ignore[method-assign]  # shadow with a slow stand-in

    result = await asyncio.wait_for(router.classify_async("iets"), timeout=2.0)
    assert result is None
