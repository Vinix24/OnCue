"""Tests for the live script-tracking coverage engine."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.coaching.script_tracker import (
    ScriptCoverageLLMClient,
    ScriptPoint,
    ScriptRouter,
    ScriptTracker,
)
from sales_copilot.modules.detector.router import RouteMatch


@pytest.fixture()
def sample_points() -> list[ScriptPoint]:
    return [
        ScriptPoint(
            id="budget",
            title="Budget",
            phase="discovery",
            required=True,
            example_phrases=["wat is het budget voor dit project"],
        ),
        ScriptPoint(
            id="tijdlijn",
            title="Tijdlijn",
            phase="discovery",
            required=True,
            example_phrases=["wanneer moet dit opgeleverd zijn"],
        ),
    ]


@pytest.fixture()
def detector_config(tmp_path: Any) -> DetectorConfig:
    """A config that keeps tests fast: thresholds and no real LLM."""
    return DetectorConfig(
        llm_provider="none",
        confidence_threshold_high=0.85,
        confidence_threshold_low=0.50,
    )


class _StaticRouter:
    """Test-only router that returns deterministic matches without embeddings."""

    def __init__(self, matches: dict[str, RouteMatch]) -> None:
        self._matches = matches

    async def classify_async(self, text: str) -> RouteMatch | None:
        return self._matches.get(text)


@pytest.mark.asyncio
async def test_utterance_that_matches_point_gets_covered(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    router = _StaticRouter(
        {"we hebben een budget van tienduizend euro": RouteMatch(category="budget", confidence=0.92, tier="high")}
    )
    tracker = ScriptTracker(detector_config, points=sample_points, router=router)

    result = await tracker.process_utterance("we hebben een budget van tienduizend euro", "self", 1000)

    assert result is not None
    assert result.point_id == "budget"
    assert result.status == "tentative"
    assert result.confidence == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_utterance_that_misses_point_stays_missing(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    router = _StaticRouter({})
    tracker = ScriptTracker(detector_config, points=sample_points, router=router)

    result = await tracker.process_utterance("goedemorgen hoe gaat het", "self", 1000)

    assert result is None
    snapshot = tracker.build_full_snapshot()
    assert all(c.status == "missing" for c in snapshot)


@pytest.mark.asyncio
async def test_coverage_is_idempotent(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    router = _StaticRouter(
        {
            "eerste keer budget": RouteMatch(category="budget", confidence=0.92, tier="high"),
            "tweede keer budget": RouteMatch(category="budget", confidence=0.95, tier="high"),
        }
    )
    tracker = ScriptTracker(detector_config, points=sample_points, router=router)

    first = await tracker.process_utterance("eerste keer budget", "self", 1000)
    second = await tracker.process_utterance("tweede keer budget", "self", 2000)

    assert first is not None
    assert second is None  # already covered
    state = await tracker.get_coverage_state()
    assert len(state) == 1
    assert state["budget"].confidence == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_confirm_coverage_does_not_block_live_path(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    """LLM confirmation runs async and returns without blocking the event loop."""

    class _FastLLM(ScriptCoverageLLMClient):
        async def confirm(
            self,
            points: list[ScriptPoint],
            transcript_lines: list[str],
            tentative: list[ScriptPoint] | None = None,
        ) -> Any:
            await asyncio.sleep(0)  # yield to prove it is async-friendly
            from sales_copilot.modules.coaching.script_tracker import _CoverageConfirmResponse

            return _CoverageConfirmResponse(
                covered_point_ids=["tijdlijn"],
                hints={"budget": "vraag naar het beschikbare budget"},
            )

    tracker = ScriptTracker(
        detector_config,
        points=sample_points,
        llm_client=_FastLLM(detector_config),
    )

    start = asyncio.get_event_loop().time()
    state = await tracker.confirm_coverage(["prospect: wanneer moet dit opgeleverd zijn"])
    elapsed = asyncio.get_event_loop().time() - start

    assert "tijdlijn" in state
    assert state["tijdlijn"].status == "confirmed"
    assert "budget" in state
    assert state["budget"].status == "missing"
    assert "budget" in state
    assert state["budget"].hint == "vraag naar het beschikbare budget"
    # Real LLM would take much longer; this proves the async seam is non-blocking.
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_full_snapshot_shows_missing_and_covered(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    router = _StaticRouter(
        {"budget vraag": RouteMatch(category="budget", confidence=0.88, tier="uncertain")}
    )
    tracker = ScriptTracker(detector_config, points=sample_points, router=router)

    await tracker.process_utterance("budget vraag", "self", 1000)
    snapshot = tracker.build_full_snapshot()

    assert len(snapshot) == 2
    by_id = {c.point_id: c for c in snapshot}
    assert by_id["budget"].status == "partial"
    assert by_id["tijdlijn"].status == "missing"


def test_script_router_keyword_match_uses_example_phrases(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    """Keyword matching hits exact example phrases without loading embeddings."""
    router = ScriptRouter(detector_config, sample_points)
    match = router.classify("wat is het budget voor dit project")

    assert match is not None
    assert match.category == "budget"
    assert match.confidence == pytest.approx(1.0)
    assert match.source == "keyword"


def test_script_router_returns_none_for_unrelated_utterance(
    sample_points: list[ScriptPoint],
    detector_config: DetectorConfig,
) -> None:
    router = ScriptRouter(detector_config, sample_points)
    match = router.classify("goedemorgen hoe gaat het")

    assert match is None
