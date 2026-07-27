"""End-to-end time-to-flag latency measurement harness.

Measures how long it takes from the moment an utterance enters the detector
pipeline until the live pain-point flag is published on ``/ws/pain-points``.
That spans two production stages:

1. ``DetectionPipeline.process()`` – semantic-router classification and, for
   uncertain matches, a synchronous LLM confirmation.
2. ``SlideInjector.process_transcript()`` – case lookup and the WebSocket send
   that actually publishes the ``pain_point`` message.

The two detection routes that exist in ``pipeline.py`` are:

1. **Fast route** – ``PainPointRouter.classify()`` returns a high-confidence
   match (``tier="high"``). The pipeline emits directly without calling the
   LLM. In production this is the embedding/keyword path (~5 ms once warm).
2. **LLM route** – ``PainPointRouter.classify()`` returns an uncertain match
   (``confidence_threshold_low <= score < confidence_threshold_high``). The
   pipeline synchronously blocks on ``llm_client.confirm()`` before it emits or
   rejects the flag. This is the 9–27 s path we want to baseline before deciding
   whether to make it async.

The harness is deliberately non-invasive: it wraps the existing router and LLM
client instances in timing proxies, times the full publish path, and writes the
results to an NDJSON log. It does not change ``pipeline.py`` or ``injector.py``.

Usage in tests (fake components, no API key needed):

    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=fake_router,
        llm_client=fake_llm,
        debouncer=PassThroughDebouncer(),
    )
    harness = TimeToFlagHarness(pipeline, output_path="data/baseline.ndjson")
    report = await harness.run(["fast utterance", "uncertain utterance"], iterations=20)

Usage for a real baseline (requires a configured LLM provider + API key):

    python tests/latency_harness.py

The harness uses a pass-through debouncer by default so repeated identical
utterances keep producing flags; otherwise the 45 s cooldown would hide the
second, third, … sample of the same pain point. For the live baseline it also
builds a ``SlideInjector`` with a local WebSocket capture so the stop timestamp
is the moment the ``pain_point`` message would be sent, not just the moment
``DetectionPipeline.process()`` returns.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from sales_copilot.core.config import (
    DetectorConfig,
    SlidesConfig,
    WebSocketConfig,
    load_env,
)
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.pipeline import DetectionPipeline, PainPointEvent
from sales_copilot.modules.detector.router import RouteMatch
from sales_copilot.modules.slides.case_db import SQLiteCaseDB


@dataclass(frozen=True)
class LatencySample:
    """One observation: utterance in, latency + routing decision out."""

    run_id: str
    iteration: int
    utterance: str
    speaker: str
    route: str  # "fast", "llm", "none"
    router_tier: str | None
    router_confidence: float | None
    router_category: str | None
    llm_called: bool
    event_emitted: bool
    event_category: str | None
    total_latency_ms: float
    router_latency_ms: float | None
    llm_latency_ms: float | None
    timestamp_ns: int


@dataclass(frozen=True)
class RouteStats:
    """Aggregated latency statistics for one route."""

    count: int
    p50_ms: float | None
    p95_ms: float | None
    min_ms: float | None
    max_ms: float | None
    mean_ms: float | None


@dataclass(frozen=True)
class LatencyReport:
    """Full report for one harness run."""

    run_id: str
    timestamp_ns: int
    iterations: int
    fast: RouteStats
    llm: RouteStats
    samples: list[LatencySample] = field(repr=False)


class PassThroughDebouncer(PainPointDebouncer):
    """Debouncer that never blocks a trigger.

    The production debouncer has a 45 s cooldown per category. When we want N
    independent samples of the same utterance we need that cooldown out of the
    way; the latency it adds is negligible and not what we are baselining here.
    """

    def __init__(self) -> None:  # noqa: D107
        super().__init__(cooldown_seconds=0)

    def should_trigger(self, category: str) -> bool:  # noqa: D102, ARG002
        return True

    def record_trigger(self, category: str) -> None:  # noqa: D102, ARG002
        pass


class _TimedRouter:
    """Transparent router wrapper that records the last match and its latency."""

    def __init__(self, router: Any) -> None:
        self._router = router
        self.last_match: RouteMatch | None = None
        self.last_latency_ms: float | None = None

    def classify(self, text: str) -> RouteMatch | None:
        start = time.perf_counter()
        match = self._router.classify(text)
        self.last_latency_ms = (time.perf_counter() - start) * 1000
        self.last_match = match
        return match


class _TimedLLMClient:
    """Transparent LLM-client wrapper that records whether confirm() was called."""

    def __init__(self, llm_client: Any) -> None:
        self._llm_client = llm_client
        self.called = False
        self.async_called = False
        self.last_latency_ms: float | None = None
        self.last_async_latency_ms: float | None = None
        # Forward provider so pipeline introspection keeps working after patching.
        self.provider = getattr(llm_client, "provider", None)

    def confirm(self, fragment: str) -> Any:
        self.called = True
        start = time.perf_counter()
        result = self._llm_client.confirm(fragment)
        self.last_latency_ms = (time.perf_counter() - start) * 1000
        return result

    async def confirm_async(self, fragment: str) -> Any:
        self.async_called = True
        start = time.perf_counter()
        result = await self._llm_client.confirm_async(fragment)
        self.last_async_latency_ms = (time.perf_counter() - start) * 1000
        return result


class _CaptureWebSocket:
    """WebSocket stand-in that records the moment a message is sent.

    Used by the harness to stop the timer at the first ``/ws/pain-points`` send
    without requiring a running WebSocket hub.
    """

    def __init__(self, on_send: Any = None) -> None:
        self.closed = False
        self.close_code: int | None = None
        self.on_send = on_send
        self.send_count = 0

    async def send(self, data: str) -> None:  # noqa: ARG002
        self.send_count += 1
        if self.on_send is not None:
            self.on_send()

    async def close(self) -> None:
        self.closed = True


def _percentile(values: list[float], percentile: float) -> float:
    """Linear-interpolation percentile for a list of floats."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * percentile / 100.0
    lower = int(k)
    upper = min(lower + 1, len(sorted_vals) - 1)
    if lower == upper:
        return sorted_vals[lower]
    return sorted_vals[lower] + (k - lower) * (sorted_vals[upper] - sorted_vals[lower])


def _route_stats(samples: list[LatencySample]) -> RouteStats:
    latencies = [s.total_latency_ms for s in samples]
    if not latencies:
        return RouteStats(
            count=0,
            p50_ms=None,
            p95_ms=None,
            min_ms=None,
            max_ms=None,
            mean_ms=None,
        )
    return RouteStats(
        count=len(latencies),
        p50_ms=_percentile(latencies, 50),
        p95_ms=_percentile(latencies, 95),
        min_ms=min(latencies),
        max_ms=max(latencies),
        mean_ms=sum(latencies) / len(latencies),
    )


class TimeToFlagHarness:
    """Measure end-to-end time-to-flag for the fast and LLM routes.

    The harness mutates the supplied ``pipeline.router`` and
    ``pipeline.llm_client`` attributes by wrapping them in timing proxies. This
    is intentional: it keeps the measurement seam outside ``pipeline.py``.

    When an ``injector`` is supplied, the wall-clock measurement starts just
    before ``SlideInjector.process_transcript()`` and stops at the first
    ``/ws/pain-points`` send. That is the true live-flag timestamp used in
    production; measuring only ``pipeline.process()`` underreports the baseline.
    """

    def __init__(
        self,
        pipeline: DetectionPipeline,
        injector: SlideInjector | None = None,
        output_path: Path | str = "data/time_to_flag_baseline.ndjson",
    ) -> None:
        self.pipeline = pipeline
        self.injector = injector
        self.output_path = Path(output_path)
        self._timed_router = _TimedRouter(pipeline.router)
        self._timed_llm = _TimedLLMClient(pipeline.llm_client)
        # Patch the pipeline so every process() call is observed.
        pipeline.router = self._timed_router  # type: ignore[assignment]
        pipeline.llm_client = self._timed_llm  # type: ignore[assignment]

    async def measure(
        self,
        utterance: str,
        speaker: str = "prospect",
        iteration: int = 0,
    ) -> LatencySample:
        """Run one utterance through the full publish path and record its latency."""
        self._timed_router.last_match = None
        self._timed_router.last_latency_ms = None
        self._timed_llm.called = False
        self._timed_llm.last_latency_ms = None

        event: PainPointEvent | None = None
        if self.injector is not None:
            stop: float | None = None

            def _record_stop() -> None:
                nonlocal stop
                if stop is None:
                    stop = time.perf_counter()

            # Replace cached sockets so the first /ws/pain-points send gives us
            # the exact publish timestamp without needing a running hub.
            pain_ws = _CaptureWebSocket(_record_stop)
            slide_ws = _CaptureWebSocket()
            self.injector._pain_points_ws = pain_ws  # type: ignore[attr-defined]
            self.injector._slide_control_ws = slide_ws  # type: ignore[attr-defined]

            start = time.perf_counter()
            result = await self.injector.process_transcript(utterance, speaker, 0)
            if stop is None:
                stop = time.perf_counter()
            total_ms = (stop - start) * 1000
            event = result.pain_point_event if result is not None else None
        else:
            start = time.perf_counter()
            event = self.pipeline.process(utterance, speaker=speaker)
            total_ms = (time.perf_counter() - start) * 1000

        match = self._timed_router.last_match
        route = self._classify_route(match, self._timed_llm.called, event)
        return LatencySample(
            run_id="",
            iteration=iteration,
            utterance=utterance,
            speaker=speaker,
            route=route,
            router_tier=match.tier if match else None,
            router_confidence=match.confidence if match else None,
            router_category=match.category if match else None,
            llm_called=self._timed_llm.called,
            event_emitted=event is not None,
            event_category=event.category if event else None,
            total_latency_ms=total_ms,
            router_latency_ms=self._timed_router.last_latency_ms,
            llm_latency_ms=self._timed_llm.last_latency_ms,
            timestamp_ns=time.time_ns(),
        )

    @staticmethod
    def _classify_route(
        match: RouteMatch | None,
        llm_called: bool,
        event: PainPointEvent | None,
    ) -> str:
        """Bucket a sample into the route that produced the flag decision."""
        if llm_called:
            return "llm"
        if event is not None:
            return "fast"
        return "none"

    async def run(
        self,
        utterances: list[str],
        iterations: int = 10,
        speaker: str = "prospect",
    ) -> LatencyReport:
        """Measure all utterances for N iterations and write an NDJSON log."""
        run_id = str(uuid.uuid4())
        samples: list[LatencySample] = []
        for iteration in range(iterations):
            for utterance in utterances:
                sample = await self.measure(utterance, speaker=speaker, iteration=iteration)
                sample = replace(sample, run_id=run_id)
                samples.append(sample)
        return self._finish(run_id, samples)

    def _finish(self, run_id: str, samples: list[LatencySample]) -> LatencyReport:
        fast_samples = [s for s in samples if s.route == "fast"]
        llm_samples = [s for s in samples if s.route == "llm"]
        report = LatencyReport(
            run_id=run_id,
            timestamp_ns=time.time_ns(),
            iterations=len(samples),
            fast=_route_stats(fast_samples),
            llm=_route_stats(llm_samples),
            samples=samples,
        )
        self._write(report)
        return report

    def _write(self, report: LatencyReport) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8") as handle:
            summary = {
                "type": "time_to_flag_summary",
                "run_id": report.run_id,
                "timestamp_ns": report.timestamp_ns,
                "iterations": report.iterations,
                "fast": asdict(report.fast),
                "llm": asdict(report.llm),
            }
            handle.write(json.dumps(summary, default=str) + "\n")
            for sample in report.samples:
                record = {"type": "time_to_flag_sample", **asdict(sample)}
                handle.write(json.dumps(record, default=str) + "\n")

    @classmethod
    def from_config(
        cls,
        config: DetectorConfig | None = None,
        output_path: Path | str = "data/time_to_flag_baseline.ndjson",
    ) -> TimeToFlagHarness:
        """Build a harness around a real pipeline and slide injector.

        This is the entry point for live baseline runs. It requires a working
        detector environment (semantic-router, sentence-transformers, instructor,
        and a configured LLM provider + API key).
        """
        load_env()
        config = config or DetectorConfig.from_env()
        slides_config = SlidesConfig.from_env()
        # Keep the baseline focused on the pain-point publish; disable optional
        # generated-slide LLM calls that happen after the flag.
        slides_config = replace(slides_config, dynamic_slides=False)
        ws_config = WebSocketConfig.from_env()
        case_db = SQLiteCaseDB(slides_config.case_db_sqlite_path)
        asyncio.run(case_db.initialize())
        pipeline = DetectionPipeline(
            config=config,
            debouncer=PassThroughDebouncer(),
        )
        injector = SlideInjector(pipeline, case_db, ws_config, slides_config)
        return cls(pipeline, injector=injector, output_path=output_path)


async def _async_main() -> None:  # pragma: no cover
    """Run a baseline measurement against the real pipeline."""
    harness = TimeToFlagHarness.from_config()
    # Utterances chosen to hit both routes with the default Dutch pain points.
    utterances = [
        # High-confidence fast-route candidates.
        "we zitten uren aan offertes te werken",
        "het offerteproces kost ons enorm veel tijd",
        # Uncertain candidates that should fall through to LLM confirm.
        "de prijs voelt eigenlijk best wel hoog",
        "we twijfelen of dit wel binnen het budget past",
    ]
    report = await harness.run(utterances, iterations=5)
    print(
        f"Fast route p50={report.fast.p50_ms:.2f}ms "
        f"p95={report.fast.p95_ms:.2f}ms (n={report.fast.count})"
    )
    print(
        f"LLM route  p50={report.llm.p50_ms:.2f}ms "
        f"p95={report.llm.p95_ms:.2f}ms (n={report.llm.count})"
    )
    print(f"NDJSON log: {harness.output_path}")


def main() -> None:  # pragma: no cover
    """Synchronous entry point for the baseline runner."""
    asyncio.run(_async_main())


if __name__ == "__main__":  # pragma: no cover
    main()
