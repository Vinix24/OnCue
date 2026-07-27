"""Tests for the time-to-flag measurement harness.

These tests use fake routers and fake LLM clients so they run without API keys
and without loading the embedding model. They verify:

- Route classification (fast vs llm vs none)
- Latency aggregation (p50/p95/min/max/mean)
- NDJSON log format and append behavior
- PassThroughDebouncer behavior
- Edge cases: empty input and single-sample reports
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.llm_confirm import PainPointDetection
from sales_copilot.modules.detector.pipeline import DetectionPipeline
from sales_copilot.modules.detector.router import RouteMatch
from tests.latency_harness import (
    PassThroughDebouncer,
    RouteStats,
    TimeToFlagHarness,
    _percentile,
)


@dataclass
class _FakeRouter:
    match: RouteMatch | None

    def classify(self, text: str) -> RouteMatch | None:  # noqa: ARG002
        return self.match


@dataclass
class _FakeLLM:
    confirmation: PainPointDetection | None
    delay_ms: float = 0.0
    called: int = 0

    def confirm(self, fragment: str) -> PainPointDetection | None:  # noqa: ARG002
        self.called += 1
        if self.delay_ms:
            time.sleep(self.delay_ms / 1000.0)
        return self.confirmation

    async def confirm_async(self, fragment: str) -> PainPointDetection | None:  # noqa: ARG002
        self.called += 1
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000.0)
        return self.confirmation


class _RecordingDebouncer(PainPointDebouncer):
    def __init__(self) -> None:
        super().__init__(cooldown_seconds=45)
        self.should_results: list[bool] = []
        self.recorded: list[str] = []

    def should_trigger(self, category: str) -> bool:
        result = super().should_trigger(category)
        self.should_results.append(result)
        return result

    def record_trigger(self, category: str) -> None:
        self.recorded.append(category)
        super().record_trigger(category)


def test_pass_through_debouncer_always_allows() -> None:
    debouncer = PassThroughDebouncer()
    assert debouncer.should_trigger("anything") is True
    assert debouncer.should_trigger("anything") is True


def test_percentile_empty_list_returns_zero() -> None:
    assert _percentile([], 50) == 0.0


def test_percentile_single_value() -> None:
    assert _percentile([42.0], 95) == 42.0


def test_percentile_linear_interpolation() -> None:
    values = [0.0, 100.0]
    assert _percentile(values, 50) == 50.0
    assert _percentile(values, 25) == 25.0


def test_route_stats_empty() -> None:
    stats = RouteStats(count=0, p50_ms=None, p95_ms=None, min_ms=None, max_ms=None, mean_ms=None)
    assert stats.count == 0
    assert stats.p50_ms is None


async def test_fast_route_does_not_call_llm(tmp_path: Path) -> None:
    config = DetectorConfig()
    router = _FakeRouter(RouteMatch("offerteproces", 0.95, "high"))
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )
    harness = TimeToFlagHarness(pipeline, output_path=tmp_path / "out.ndjson")

    report = await harness.run(["we zitten uren aan offertes"], iterations=3)

    assert report.fast.count == 3
    assert report.llm.count == 0
    assert llm.called == 0
    assert all(s.route == "fast" for s in report.samples)
    assert all(s.llm_called is False for s in report.samples)
    assert all(s.event_emitted is True for s in report.samples)


async def test_uncertain_route_calls_llm_and_reports_llm_latency(tmp_path: Path) -> None:
    config = DetectorConfig()
    router = _FakeRouter(RouteMatch("kosten", 0.65, "uncertain"))
    confirmation = PainPointDetection(
        category="kosten",
        confidence=0.72,
        trigger_phrase="te duur",
    )
    llm = _FakeLLM(confirmation, delay_ms=15.0)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )
    harness = TimeToFlagHarness(pipeline, output_path=tmp_path / "out.ndjson")

    report = await harness.run(["het is te duur"], iterations=3)

    assert report.fast.count == 0
    assert report.llm.count == 3
    assert llm.called == 3
    assert all(s.route == "llm" for s in report.samples)
    assert all(s.llm_called is True for s in report.samples)
    assert all(s.event_emitted is True for s in report.samples)
    assert all(s.llm_latency_ms is not None and s.llm_latency_ms >= 15.0 for s in report.samples)


async def test_low_confidence_route_is_none(tmp_path: Path) -> None:
    config = DetectorConfig()
    router = _FakeRouter(RouteMatch("kosten", 0.3, "none"))
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )
    harness = TimeToFlagHarness(pipeline, output_path=tmp_path / "out.ndjson")

    report = await harness.run(["willekeurige tekst"], iterations=2)

    assert report.fast.count == 0
    assert report.llm.count == 0
    assert llm.called == 0
    assert all(s.route == "none" for s in report.samples)
    assert all(s.event_emitted is False for s in report.samples)


async def test_llm_reject_still_counts_as_llm_route(tmp_path: Path) -> None:
    config = DetectorConfig()
    router = _FakeRouter(RouteMatch("kosten", 0.65, "uncertain"))
    llm = _FakeLLM(None, delay_ms=10.0)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )
    harness = TimeToFlagHarness(pipeline, output_path=tmp_path / "out.ndjson")

    report = await harness.run(["vague cost mention"], iterations=2)

    assert report.llm.count == 2
    assert all(s.route == "llm" for s in report.samples)
    assert all(s.event_emitted is False for s in report.samples)


async def test_percentile_calculation_for_report(tmp_path: Path) -> None:
    config = DetectorConfig()
    router = _FakeRouter(RouteMatch("offerteproces", 0.95, "high"))
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )
    harness = TimeToFlagHarness(pipeline, output_path=tmp_path / "out.ndjson")

    report = await harness.run(["trigger fast route"], iterations=5)

    fast = report.fast
    latencies = sorted(s.total_latency_ms for s in report.samples if s.route == "fast")
    assert fast.count == 5
    assert fast.min_ms == pytest.approx(latencies[0], abs=0.01)
    assert fast.max_ms == pytest.approx(latencies[-1], abs=0.01)
    assert fast.mean_ms == pytest.approx(sum(latencies) / len(latencies), abs=0.01)
    assert fast.p50_ms == pytest.approx(_percentile(latencies, 50), abs=0.01)
    assert fast.p95_ms == pytest.approx(_percentile(latencies, 95), abs=0.01)


async def test_ndjson_log_contains_summary_and_samples(tmp_path: Path) -> None:
    config = DetectorConfig()
    router = _FakeRouter(RouteMatch("offerteproces", 0.95, "high"))
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )
    output_path = tmp_path / "out.ndjson"
    harness = TimeToFlagHarness(pipeline, output_path=output_path)

    report = await harness.run(["fast"], iterations=2)

    lines = output_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3  # 1 summary + 2 samples
    summary = json.loads(lines[0])
    assert summary["type"] == "time_to_flag_summary"
    assert summary["run_id"] == report.run_id
    assert summary["fast"]["count"] == 2
    sample = json.loads(lines[1])
    assert sample["type"] == "time_to_flag_sample"
    assert sample["route"] == "fast"
    assert "total_latency_ms" in sample


async def test_ndjson_log_appends_across_runs(tmp_path: Path) -> None:
    config = DetectorConfig()
    router = _FakeRouter(RouteMatch("offerteproces", 0.95, "high"))
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )
    output_path = tmp_path / "out.ndjson"
    harness = TimeToFlagHarness(pipeline, output_path=output_path)

    await harness.run(["fast"], iterations=1)
    await harness.run(["fast"], iterations=1)

    lines = output_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 4  # 2 summaries + 2 samples
    assert all(json.loads(line)["type"] in {"time_to_flag_summary", "time_to_flag_sample"} for line in lines)


async def test_harness_uses_pass_through_debouncer_by_default_from_config() -> None:
    config = DetectorConfig()
    pipeline = DetectionPipeline(
        config=config,
        router=_FakeRouter(RouteMatch("offerteproces", 0.95, "high")),
        llm_client=_FakeLLM(None),
        debouncer=PassThroughDebouncer(),
    )
    harness = TimeToFlagHarness(pipeline, output_path=Path("/dev/null"))
    report = await harness.run(["same category", "same category"], iterations=2)
    assert report.fast.count == 4


async def test_real_router_produces_fast_route() -> None:
    """Sanity check that the real Dutch router can hit the fast route.

    This test loads the embedding model on first call, so it is slower than the
    fakes above. It guards against the harness being unusable with the real
    pipeline.
    """
    from sales_copilot.modules.detector.router import PainPointRouter

    config = DetectorConfig()
    router = PainPointRouter(config)
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )
    harness = TimeToFlagHarness(pipeline, output_path=Path("/dev/null"))

    sample = await harness.measure("we zitten uren aan offertes te werken", speaker="prospect")

    assert sample.route == "fast"
    assert sample.router_tier == "high"
    assert sample.llm_called is False
    assert sample.event_emitted is True
    assert sample.router_latency_ms is not None


async def test_empty_input_produces_empty_report_with_summary(tmp_path: Path) -> None:
    """Edge case: no utterances means zero samples but still a valid summary line."""
    config = DetectorConfig()
    pipeline = DetectionPipeline(
        config=config,
        router=_FakeRouter(RouteMatch("offerteproces", 0.95, "high")),
        llm_client=_FakeLLM(None),
        debouncer=PassThroughDebouncer(),
    )
    output_path = tmp_path / "out.ndjson"
    harness = TimeToFlagHarness(pipeline, output_path=output_path)

    report = await harness.run([], iterations=5)

    assert report.iterations == 0
    assert report.samples == []
    assert report.fast.count == 0
    assert report.llm.count == 0
    assert report.fast.p50_ms is None
    assert report.llm.p95_ms is None

    lines = output_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["type"] == "time_to_flag_summary"
    assert summary["iterations"] == 0
    assert summary["fast"]["count"] == 0
    assert summary["llm"]["count"] == 0


async def test_one_sample_report_statistics_equal_the_sample_latency(tmp_path: Path) -> None:
    """Edge case: a single sample should yield p50/p95/min/max/mean all equal."""
    config = DetectorConfig()
    router = _FakeRouter(RouteMatch("offerteproces", 0.95, "high"))
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )
    harness = TimeToFlagHarness(pipeline, output_path=tmp_path / "out.ndjson")

    report = await harness.run(["single utterance"], iterations=1)

    assert report.iterations == 1
    assert report.fast.count == 1
    sample = report.samples[0]
    latency = sample.total_latency_ms
    fast = report.fast
    assert fast.p50_ms == pytest.approx(latency, abs=0.01)
    assert fast.p95_ms == pytest.approx(latency, abs=0.01)
    assert fast.min_ms == pytest.approx(latency, abs=0.01)
    assert fast.max_ms == pytest.approx(latency, abs=0.01)
    assert fast.mean_ms == pytest.approx(latency, abs=0.01)


async def test_async_live_path_middle_band_is_embedding_only(tmp_path: Path) -> None:
    """The live async path must emit the provisional flag without waiting for the LLM.

    This is the key latency-packet verification: middle-band samples that previously
    blocked on ``llm_client.confirm()`` now return in embedding-only time.
    """
    config = DetectorConfig()
    router = _FakeRouter(RouteMatch("kosten", 0.65, "uncertain"))
    confirmation = PainPointDetection(
        category="kosten",
        confidence=0.72,
        trigger_phrase="te duur",
    )
    # A 200 ms artificial LLM delay must not affect the live flag latency.
    llm = _FakeLLM(confirmation, delay_ms=200.0)
    pipeline = DetectionPipeline(
        config=config,
        router=router,
        llm_client=llm,
        debouncer=PassThroughDebouncer(),
    )

    start = asyncio.get_event_loop().time()
    event = await pipeline.aprocess("het is te duur", speaker="prospect")
    live_latency_ms = (asyncio.get_event_loop().time() - start) * 1000

    assert event is not None
    assert event.provisional is True
    assert live_latency_ms < 50.0, (
        f"live middle-band flag took {live_latency_ms:.1f}ms, expected <50ms"
    )

    # Background LLM still completes and enriches the result.
    await asyncio.sleep(0.25)
    assert llm.called == 1
