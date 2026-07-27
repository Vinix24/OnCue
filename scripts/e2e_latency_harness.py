#!/usr/bin/env python3
"""End-to-end latency harness: replays a recorded call segment through the FULL
live-copilot chain and attributes the 5s-to-screen budget to each stage.

Answers: is the risk in VAD, Whisper transcription, detection/routing, the
escalation-LLM generation, or the emit/render step?

This is a COMPOSITION harness: it drives the real production modules directly
rather than reimplementing their logic. Two modes share this file (``--v2``
picks the second one) rather than duplicating a whole script:

**v1 (default) -- DetectionPipeline.** Only reached live as the no-LLM-provider
graceful-degradation fallback (see the v2 note below), but useful as a
detection-pipeline-shaped baseline:

    stage 1  VAD segment-close      AudioBufferer (real) -> SharedInferenceQueue
    stage 2  Whisper done           the real TranscriptionBackend.transcribe()
    stage 3  detection/router done  DetectionPipeline.router.classify() (real)
    stage 4  escalation-LLM done    DetectionPipeline's LLMConfirmClient (real),
                                     via DetectionHooks (an existing extension
                                     point on DetectionPipeline -- no fork)
    stage 5  emit/render            SlideInjector.process_transcript() (real)

Stage 1's timestamp is recorded by wrapping ``SharedInferenceQueue.put()`` in a
transparent timing proxy (``_TimedQueue``) -- the same non-invasive wrapper
pattern ``tests/latency_harness.py`` already uses for the router/LLM client.
Stage 3/4 are recorded via ``DetectionHooks``, a callback protocol pipeline.py
already exposes for exactly this purpose. Stage 5 is recorded via a capture
websocket stand-in patched onto ``SlideInjector`` before the send (again the
same pattern as ``tests/latency_harness.py``).

The one real modification to a production module: ``SlideInjector.
process_transcript()`` gained an optional ``precomputed_event`` kwarg (see
``src/sales_copilot/modules/copilot/injector.py``). Without it this harness
would have to re-run ``DetectionPipeline.process()`` a second time just to get
the injector to publish -- doubling every escalation-LLM call. The kwarg
defaults to unset, so every other caller is unaffected.

**v2 (``--v2``) -- the LIVE SlidingWindowBuffer + WindowClassifier path.**
``detector/__main__.py``'s live ``_consume_transcripts`` loop routes pain-point/
objection detection through ``SlidingWindowBuffer`` + ``WindowClassifier`` when
an LLM provider is configured (the common case); ``DetectionPipeline`` (v1,
above) is only reached as the no-LLM-provider fallback. v2 instruments that
live path instead:

    stage 1  VAD segment-close      same ``_TimedQueue`` proxy as v1
    stage 2  Whisper done           same real ``TranscriptionBackend`` as v1
    stage 3  window-buffer gating   ``SlidingWindowBuffer.add()`` + the same
                                     ``min_chunks_to_classify``/
                                     ``classification_debounce_seconds`` gate
                                     ``detector/__main__.py`` applies (mirrored,
                                     not forked -- it lives inline in
                                     ``_consume_transcripts``, not behind a
                                     reusable function)
    stage 4  window-classify (LLM)  ``WindowClassifier.classify(..., use_streaming=
                                     True)`` (real) -- ``use_streaming`` is a new
                                     opt-in kwarg (default ``False``, so every
                                     production call site is unaffected) that
                                     drains ``LLMClient.astream()`` instead of
                                     ``acreate()``, populating
                                     ``LLMClient.last_ttft_ms`` (the streaming
                                     seam from #79). Timed via
                                     ``WindowClassifierHooks``, a new
                                     ``DetectionHooks``-shaped callback protocol
                                     on ``WindowClassifier`` (default no-op).
    stage 5  emit/render            ``detector/__main__.py``'s real (unmodified,
                                     imported) ``_dispatch_window_detection()``,
                                     via the same capture-websocket stand-in v1
                                     uses on ``SlideInjector``

v2 reuses the SAME ``SegmentResult``/``build_report``/``render_markdown``
shape as v1 (mapping window-buffer-gating -> ``router_done_ts`` and
window-classify-done -> ``llm_end_ts``, ``escalated=True`` iff the gate let a
segment reach ``WindowClassifier.classify()``) so v1 and v2 numbers land in
the same table and are directly comparable. The one addition is a new
``llm_ttft_ms`` field/stage row -- always ``null`` for v1 (non-streaming path,
same limitation as before), populated for v2 in ``--run`` mode.

Known limitations (flagged rather than silently faked):

- **TTFT is not observable in v1.** The escalation-LLM call goes through
  ``core/llm_client.py``'s ``instructor``-patched, non-streaming ``create()``/
  ``acreate()`` -- there is no intermediate token boundary to hook without
  adding raw-streaming support to that seam. ``llm_ttft_ms`` is reported as
  ``null`` for every v1 segment. Use ``--v2 --run`` for a real number.
- **Only prospect audio is replayed.** Production shares one
  ``SharedInferenceQueue`` across self+prospect streams (backlog contention
  from self-speech is real). This harness replays ``prospect.wav`` only, so
  reported whisper-stage latency is a lower bound versus a call where the rep
  is also talking a lot.
- **Sync escalation, not the live provisional/enrichment split (v1 only).**
  The live async path (``pipeline.aprocess()``) emits a provisional flag
  immediately and confirms in the background. v1 uses the synchronous
  ``pipeline.process()`` instead, so one segment yields one clean decision and
  one emit timestamp.
- **v2 measures the pain_point emit path only.** ``_dispatch_window_detection``
  routes ``doubt`` detections through a raw ``websockets.connect()`` call to the
  coaching-hub channel (``_publish_ws``), not through ``SlideInjector`` -- there
  is no live hub in this harness, so that send is not capturable the way the
  pain_point -> ``SlideInjector.handle_window_detection`` path is. Those
  segments are still counted (outcome ``classified_not_emitted``) but never get
  an ``emit_ts``.
- **v2 has no free (LLM-cost-free) preview of what WOULD be detected.**
  Unlike v1's router-only dry-run, ``WindowClassifier`` has no local
  pre-filter -- classification IS the LLM call. v2's ``--dry-run`` instruments
  VAD/Whisper/gating only and reports a token/cost *estimate* (real
  system-prompt/user-prompt sizes, no call made); ``--run`` is required for
  real per-stage LLM/TTFT numbers.

CLI:

    # v1, safe default: VAD/Whisper/detection stages only, no paid LLM calls.
    .venv/bin/python scripts/e2e_latency_harness.py --dry-run

    # v1 real run: also calls the escalation LLM for segments that escalate.
    .venv/bin/python scripts/e2e_latency_harness.py --run \\
        --candidate openrouter:anthropic/claude-haiku-4.5 --top-n 30

    # v2, safe default: VAD/Whisper/gating stages only, no paid LLM calls.
    .venv/bin/python scripts/e2e_latency_harness.py --v2 --dry-run

    # v2 real run: drives the live WindowClassifier path, TTFT included.
    .venv/bin/python scripts/e2e_latency_harness.py --v2 --run \\
        --candidate openrouter:anthropic/claude-haiku-4.5 --top-n 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from sales_copilot.audio.replay import ReplayAudioStream  # noqa: E402
from sales_copilot.core.config import (  # noqa: E402
    DetectorConfig,
    SlidesConfig,
    TranscriberConfig,
    WebSocketConfig,
    load_env,
    with_overrides,
)
from sales_copilot.core.preset import load_preset  # noqa: E402
from sales_copilot.modules.copilot.injector import SlideInjector  # noqa: E402
from sales_copilot.modules.detector.__main__ import _dispatch_window_detection  # noqa: E402
from sales_copilot.modules.detector.eval_shared import (  # noqa: E402
    ModelSpec,
    PricingTable,
    is_provider_available,
    parse_model_specs,
    percentile,
)
from sales_copilot.modules.detector.pipeline import DetectionPipeline, PainPointEvent  # noqa: E402
from sales_copilot.modules.detector.router import RouteMatch  # noqa: E402
from sales_copilot.modules.detector.sliding_window import SlidingWindowBuffer, TranscriptChunk  # noqa: E402
from sales_copilot.modules.detector.window_classifier import WindowAnalysis, WindowClassifier  # noqa: E402
from sales_copilot.modules.slides.case_db import SQLiteCaseDB  # noqa: E402
from sales_copilot.modules.transcriber.audio_bufferer import AudioBufferer  # noqa: E402
from sales_copilot.modules.transcriber.backends import create_backend  # noqa: E402
from sales_copilot.modules.transcriber.backends.base import TranscriptionBackend  # noqa: E402
from sales_copilot.modules.transcriber.inference_queue import (  # noqa: E402
    InferenceQueueItem,
    SharedInferenceQueue,
)
from sales_copilot.modules.transcriber.inference_worker import InferenceWorker  # noqa: E402

_DEFAULT_SESSIONS_DIR = REPO_ROOT / "data" / "sessions"
_DEFAULT_REPORT_MD = REPO_ROOT / "claudedocs" / "2026-07-18-e2e-latency-breakdown.md"
_DEFAULT_REPORT_JSON = REPO_ROOT / "claudedocs" / "2026-07-18-e2e-latency-breakdown.json"
_DEFAULT_REPORT_MD_V2 = REPO_ROOT / "claudedocs" / "2026-07-18-e2e-latency-breakdown-v2.md"
_DEFAULT_REPORT_JSON_V2 = REPO_ROOT / "claudedocs" / "2026-07-18-e2e-latency-breakdown-v2.json"
_DEFAULT_TOP_N = 30
_DEFAULT_TIMEOUT_MS = 7000
_MIN_TEXT_LENGTH = 2  # mirrors InferenceWorker's default min_text_length
_INFERENCE_TIMEOUT_S = 30.0  # mirrors InferenceWorker's default inference_timeout_s
_FIVE_S_BUDGET_MS = 5000.0
_EOF_SETTLE_S = 3.0  # let AudioBufferer flush its final segment after EOF (mirrors
# tests/conftest_replay.py's run_bufferer_until_eof flush_wait_s)
_QUEUE_POLL_TIMEOUT_S = 0.2
_CHARS_PER_TOKEN = 4  # same rough heuristic as scripts/run_fase_b.py / eval_suggestion.py
_ESTIMATED_CONFIRM_OUTPUT_TOKENS = 150  # PainPointDetection{category,confidence,trigger_phrase}
_ESTIMATED_WINDOW_OUTPUT_TOKENS = 220  # WindowAnalysis{detections:[0-3 x WindowDetection]}

# Best-effort candidate for the escalation-LLM stage: Claude Haiku 4.5 via
# OpenRouter. NOT verified against the live OpenRouter feed -- this worker
# profile has no WebFetch/WebSearch. Override with --candidate if this slug is
# wrong (see Open Items in the dispatch report). Haiku 4.5 predates 4.6, so
# thinking defaults OFF (non-reasoning), which is what we want for a
# low-latency confirm call.
_DEFAULT_CANDIDATE = ModelSpec(
    provider="openrouter",
    model="anthropic/claude-haiku-4.5",
    description=(
        "Claude Haiku 4.5 via OpenRouter -- default escalation-LLM candidate for the E2E "
        "harness (thinking OFF by default, pre-4.6). Model id unverified against the live "
        "OpenRouter feed."
    ),
)


# ---------------------------------------------------------------------------
# Stage 1: VAD segment-close -- transparent SharedInferenceQueue timing proxy
# ---------------------------------------------------------------------------


class _TimedQueue:
    """Transparent ``SharedInferenceQueue`` proxy recording ``perf_counter()`` at ``put()``.

    Same non-invasive wrapper pattern as ``tests/latency_harness.py``'s
    ``_TimedRouter``/``_TimedLLMClient``: ``AudioBufferer`` and
    ``SharedInferenceQueue`` are both untouched -- this only observes the
    moment ``AudioBufferer`` hands off a VAD-closed segment.
    """

    def __init__(self, queue: SharedInferenceQueue) -> None:
        self._queue = queue
        self.close_ts: dict[int, float] = {}

    async def put(self, item: InferenceQueueItem) -> None:
        self.close_ts[id(item)] = time.perf_counter()
        await self._queue.put(item)

    async def get(self) -> InferenceQueueItem:
        return await self._queue.get()

    def qsize(self) -> int:
        return self._queue.qsize()


# ---------------------------------------------------------------------------
# Stage 3 + 4: detection/router + escalation-LLM -- DetectionHooks recorder
# ---------------------------------------------------------------------------


class _SegmentHooks:
    """``DetectionHooks`` implementation recording per-segment classify/LLM timestamps.

    Reused across segments (``reset()`` between calls) since the harness
    processes one segment at a time -- mirroring the single-worker production
    model, see module docstring.
    """

    def __init__(self) -> None:
        self.classify_end_ts: float | None = None
        self.match: RouteMatch | None = None
        self.llm_start_ts: float | None = None
        self.llm_end_ts: float | None = None
        self.llm_tokens: dict[str, int] | None = None

    def reset(self) -> None:
        self.classify_end_ts = None
        self.match = None
        self.llm_start_ts = None
        self.llm_end_ts = None
        self.llm_tokens = None

    def on_classify_start(self) -> None:
        pass

    def on_classify_end(self, match: RouteMatch | None, latency_ms: float) -> None:  # noqa: ARG002
        self.classify_end_ts = time.perf_counter()
        self.match = match

    def on_llm_start(self) -> None:
        self.llm_start_ts = time.perf_counter()

    def on_llm_end(
        self,
        confirmation: object | None,  # noqa: ARG002
        latency_ms: float,  # noqa: ARG002
        tokens: dict[str, int] | None,
    ) -> None:
        self.llm_end_ts = time.perf_counter()
        self.llm_tokens = tokens

    def on_event(self, event: PainPointEvent | None) -> None:  # noqa: ARG002
        pass


# ---------------------------------------------------------------------------
# v2 stage 4: window-classify (LLM) -- WindowClassifierHooks recorder
# ---------------------------------------------------------------------------


class _WindowHooks:
    """``WindowClassifierHooks`` implementation recording one classify call's timestamps.

    Reused across segments within a session (``reset()`` between calls), same
    pattern as ``_SegmentHooks`` above.
    """

    def __init__(self) -> None:
        self.classify_end_ts: float | None = None
        self.analysis: WindowAnalysis | None = None
        self.ttft_ms: float | None = None

    def reset(self) -> None:
        self.classify_end_ts = None
        self.analysis = None
        self.ttft_ms = None

    def on_classify_start(self) -> None:
        pass

    def on_classify_end(
        self,
        analysis: WindowAnalysis,
        latency_ms: float,  # noqa: ARG002
        ttft_ms: float | None,
    ) -> None:
        self.classify_end_ts = time.perf_counter()
        self.analysis = analysis
        self.ttft_ms = ttft_ms


# ---------------------------------------------------------------------------
# Stage 5: emit/render -- capture websocket stand-in
# ---------------------------------------------------------------------------


class _CaptureWebSocket:
    """WebSocket stand-in recording every ``send()`` -- times stage 5 (emit/render).

    Same capture pattern as ``tests/latency_harness.py``'s
    ``_CaptureWebSocket``, duplicated here (not imported) so ``scripts/`` stays
    independent of ``tests/``.
    """

    def __init__(self) -> None:
        self.closed = False
        self.close_code: int | None = None
        self.send_timestamps: list[float] = []

    async def send(self, data: str) -> None:  # noqa: ARG002
        self.send_timestamps.append(time.perf_counter())

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Per-segment result
# ---------------------------------------------------------------------------


@dataclass
class SegmentResult:
    session: str
    index: int
    text: str = ""
    outcome: str = "empty"
    escalated: bool = False
    category: str | None = None
    vad_close_ts: float = 0.0
    whisper_done_ts: float | None = None
    router_done_ts: float | None = None
    llm_end_ts: float | None = None
    emit_ts: float | None = None
    llm_ttft_ms: float | None = None  # populated only by --v2 --run (streamed classify call)

    def stage_ms(self) -> dict[str, float | None]:
        def _delta(a: float | None, b: float | None) -> float | None:
            return (b - a) * 1000 if a is not None and b is not None else None

        decision_ts = self.llm_end_ts if self.escalated else self.router_done_ts
        return {
            "vad_to_whisper_ms": _delta(self.vad_close_ts, self.whisper_done_ts),
            "whisper_to_router_ms": _delta(self.whisper_done_ts, self.router_done_ts),
            "router_to_llm_ms": _delta(self.router_done_ts, self.llm_end_ts) if self.escalated else None,
            "decision_to_emit_ms": _delta(decision_ts, self.emit_ts),
            "total_ms": _delta(self.vad_close_ts, self.emit_ts),
            "llm_ttft_ms": self.llm_ttft_ms,
        }


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def discover_sessions(sessions_dir: Path) -> list[Path]:
    """Return session directories under ``sessions_dir`` that have a ``prospect.wav``.

    Sorted for determinism. Only the prospect stream is replayed -- see the
    "Only prospect audio is replayed" limitation in the module docstring.
    """
    if not sessions_dir.exists():
        return []
    return sorted(p for p in sessions_dir.iterdir() if p.is_dir() and (p / "prospect.wav").exists())


def _build_backend_config(config: TranscriberConfig) -> dict[str, Any]:
    """Mirror ``sales_copilot.modules.transcriber.__main__._backend_config``.

    Not imported directly (that module is a ``__main__`` entry point with
    signal-handler side effects on import in some environments) -- this is a
    small, literal mirror of a config-dict builder, not pipeline logic.
    """
    if config.backend in {"whisper.cpp", "whisper_cpp"}:
        return {
            "backend": "whisper.cpp",
            "language": config.language,
            "whisper_cpp_binary": config.whisper_cpp_binary,
            "whisper_cpp_model_path": config.whisper_cpp_model_path,
            "whisper_cpp_threads": config.whisper_cpp_threads,
            "whisper_cpp_server_binary": config.whisper_cpp_server_binary,
        }
    return {"backend": config.backend, "language": config.language}


# ---------------------------------------------------------------------------
# Per-segment processing
# ---------------------------------------------------------------------------


async def _process_segment(
    item: InferenceQueueItem,
    vad_close_ts: float,
    *,
    session: str,
    index: int,
    backend: TranscriptionBackend,
    pipeline: DetectionPipeline,
    injector: SlideInjector,
    hooks: _SegmentHooks,
    run_llm: bool,
    config: DetectorConfig,
) -> SegmentResult:
    result = SegmentResult(session=session, index=index, vad_close_ts=vad_close_ts)

    try:
        raw_text = await asyncio.wait_for(backend.transcribe(item.audio), timeout=_INFERENCE_TIMEOUT_S)
    except TimeoutError:
        result.outcome = "whisper_timeout"
        return result
    result.whisper_done_ts = time.perf_counter()

    cleaned = raw_text.strip()
    if InferenceWorker._is_hallucination(cleaned) or len(cleaned) < _MIN_TEXT_LENGTH:  # noqa: SLF001
        result.outcome = "hallucination"
        return result
    result.text = cleaned

    if not run_llm:
        # Dry-run: classify only, mirroring DetectionPipeline.process()'s routing
        # thresholds directly so the LLM is never called (no paid calls in dry-run).
        match = pipeline.router.classify(cleaned)
        result.router_done_ts = time.perf_counter()
        if match is None or match.confidence < config.confidence_threshold_low:
            result.outcome = "no_match"
        elif match.confidence >= config.confidence_threshold_high:
            result.outcome = "would_emit_fast"
            result.category = match.category
        else:
            result.escalated = True
            result.outcome = "would_escalate"
            result.category = match.category
        return result

    hooks.reset()
    event = await asyncio.to_thread(pipeline.process, cleaned, "prospect")
    result.router_done_ts = hooks.classify_end_ts
    if hooks.llm_start_ts is not None:
        result.escalated = True
        result.llm_end_ts = hooks.llm_end_ts

    if event is None:
        if hooks.match is None:
            result.outcome = "no_match"
        elif result.escalated:
            result.outcome = "llm_rejected"
        else:
            result.outcome = "debounced"
        return result

    result.category = event.category
    capture_ws = _CaptureWebSocket()
    injector._pain_points_ws = capture_ws  # noqa: SLF001
    injector._slide_control_ws = _CaptureWebSocket()  # noqa: SLF001
    await injector.process_transcript(result.text, "prospect", item.end_ms, precomputed_event=event)
    if capture_ws.send_timestamps:
        result.emit_ts = capture_ws.send_timestamps[0]
        result.outcome = "emitted"
    else:
        result.outcome = "emit_failed"
    return result


# ---------------------------------------------------------------------------
# Session-level orchestration
# ---------------------------------------------------------------------------


async def _run_session(
    session_dir: Path,
    *,
    backend: TranscriptionBackend,
    pipeline: DetectionPipeline,
    injector: SlideInjector,
    hooks: _SegmentHooks,
    run_llm: bool,
    config: DetectorConfig,
    remaining_budget: int,
) -> list[SegmentResult]:
    wav = session_dir / "prospect.wav"
    results: list[SegmentResult] = []
    if remaining_budget <= 0 or not wav.exists():
        return results

    real_queue = SharedInferenceQueue(max_size=64)
    timed_queue = _TimedQueue(real_queue)
    stream = ReplayAudioStream(wav, chunk_size_frames=512, real_time=True)
    bufferer = AudioBufferer(
        audio_stream=stream,
        queue=timed_queue,
        priority=0,
        speaker="prospect",
        transcribe_live=True,
    )
    stop_event = asyncio.Event()

    async def _watch_eof() -> None:
        while not stream.at_eof:
            await asyncio.sleep(0.01)
        await asyncio.sleep(_EOF_SETTLE_S)
        stop_event.set()

    async def _consume() -> None:
        index = 0
        while True:
            try:
                item = await asyncio.wait_for(real_queue.get(), timeout=_QUEUE_POLL_TIMEOUT_S)
            except TimeoutError:
                if stop_event.is_set() and real_queue.qsize() == 0:
                    return
                continue
            vad_close_ts = timed_queue.close_ts.pop(id(item), time.perf_counter())
            result = await _process_segment(
                item,
                vad_close_ts,
                session=session_dir.name,
                index=index,
                backend=backend,
                pipeline=pipeline,
                injector=injector,
                hooks=hooks,
                run_llm=run_llm,
                config=config,
            )
            results.append(result)
            index += 1
            if len(results) >= remaining_budget:
                stop_event.set()
                return

    watcher_task = asyncio.create_task(_watch_eof())
    bufferer_task = asyncio.create_task(bufferer.run(stop_event))
    consumer_task = asyncio.create_task(_consume())

    await consumer_task
    watcher_task.cancel()
    bufferer_task.cancel()
    for task in (watcher_task, bufferer_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    return results


# ---------------------------------------------------------------------------
# v2 per-segment processing: SlidingWindowBuffer + WindowClassifier live path
# ---------------------------------------------------------------------------


async def _process_window_segment(
    item: InferenceQueueItem,
    vad_close_ts: float,
    *,
    session: str,
    index: int,
    backend: TranscriptionBackend,
    window_buf: SlidingWindowBuffer,
    window_classifier: WindowClassifier,
    injector: SlideInjector,
    window_hooks: _WindowHooks,
    run_llm: bool,
    config: DetectorConfig,
    ws_config: WebSocketConfig,
    ui_labels: dict,
    last_classified_at: float | None,
) -> tuple[SegmentResult, float | None]:
    """Mirrors ``detector/__main__.py``'s ``_consume_transcripts`` window-buffer gate
    (``min_chunks_to_classify``/``classification_debounce_seconds``) and dispatch for
    one VAD-closed segment -- that gate lives inline in the live consume loop, not
    behind a reusable function, so it is mirrored here rather than forked out of it.
    Returns the segment result and the (possibly updated) debounce timestamp for the
    caller to thread into the next call, mirroring ``_consume_transcripts``'s own
    ``last_classified_at`` local -- scoped per harness session here instead of per
    live detector process.
    """
    result = SegmentResult(session=session, index=index, vad_close_ts=vad_close_ts)

    try:
        raw_text = await asyncio.wait_for(backend.transcribe(item.audio), timeout=_INFERENCE_TIMEOUT_S)
    except TimeoutError:
        result.outcome = "whisper_timeout"
        return result, last_classified_at
    result.whisper_done_ts = time.perf_counter()

    cleaned = raw_text.strip()
    if InferenceWorker._is_hallucination(cleaned) or len(cleaned) < _MIN_TEXT_LENGTH:  # noqa: SLF001
        result.outcome = "hallucination"
        return result, last_classified_at
    result.text = cleaned

    chunk = TranscriptChunk(text=cleaned, speaker="prospect", start_ms=item.start_ms, end_ms=item.end_ms)
    window_buf.add(chunk)
    window_text = window_buf.context_text()

    if len(window_buf) < config.min_chunks_to_classify:
        result.router_done_ts = time.perf_counter()
        result.outcome = "buffer_below_min"
        return result, last_classified_at

    now = time.monotonic()
    if last_classified_at is not None and (now - last_classified_at) < config.classification_debounce_seconds:
        result.router_done_ts = time.perf_counter()
        result.outcome = "debounced"
        return result, last_classified_at

    result.router_done_ts = time.perf_counter()

    if not run_llm:
        # Dry-run: the gate passed (this segment WOULD trigger a classify call), but
        # WindowClassifier has no local pre-filter to preview -- classification IS the
        # LLM call. Advance the debounce clock as a real classify call would, so the
        # dry-run segment/outcome counts match what --run would actually gate through.
        result.outcome = "would_classify"
        return result, now

    window_hooks.reset()
    analysis = await window_classifier.classify(
        window_text=window_text,
        latest_chunk=window_buf.latest_chunk(),
        use_streaming=True,
    )
    result.escalated = True
    result.llm_end_ts = window_hooks.classify_end_ts
    result.llm_ttft_ms = window_hooks.ttft_ms

    capture_ws = _CaptureWebSocket()
    injector._pain_points_ws = capture_ws  # noqa: SLF001
    injector._slide_control_ws = _CaptureWebSocket()  # noqa: SLF001

    saw_actionable = False
    first_category: str | None = None
    saw_pain_point = False
    for detection in analysis.detections:
        if detection.category == "none" or detection.confidence < config.confidence_threshold_low:
            continue
        saw_actionable = True
        if first_category is None:
            first_category = detection.subcategory or detection.category
        # doubt publishes over a raw websocket to a live coaching hub (see
        # _dispatch_window_detection) that this offline harness does not run -- that
        # send is not capturable here, see the module docstring's known limitations.
        await _dispatch_window_detection(detection, item.end_ms, injector, ws_config, config, ui_labels)
        if detection.category == "pain_point":
            saw_pain_point = True

    if capture_ws.send_timestamps:
        result.emit_ts = capture_ws.send_timestamps[0]
        result.category = first_category
        result.outcome = "emitted"
    elif saw_pain_point:
        result.outcome = "emit_failed"
        result.category = first_category
    elif saw_actionable:
        result.outcome = "classified_not_emitted"
        result.category = first_category
    else:
        result.outcome = "classified_no_detection"

    return result, now


async def _run_session_v2(
    session_dir: Path,
    *,
    backend: TranscriptionBackend,
    window_classifier: WindowClassifier,
    injector: SlideInjector,
    window_hooks: _WindowHooks,
    run_llm: bool,
    config: DetectorConfig,
    ws_config: WebSocketConfig,
    ui_labels: dict,
    remaining_budget: int,
) -> list[SegmentResult]:
    """v2's session-level orchestration -- same VAD/queue/watcher composition as
    ``_run_session`` (SharedInferenceQueue/_TimedQueue/ReplayAudioStream/AudioBufferer),
    duplicated rather than shared with ``_run_session`` so the already-shipped v1 path
    stays untouched (zero regression risk) while v2 gets its own per-session
    ``SlidingWindowBuffer``/debounce-clock state, which v1 has no equivalent of.
    """
    wav = session_dir / "prospect.wav"
    results: list[SegmentResult] = []
    if remaining_budget <= 0 or not wav.exists():
        return results

    real_queue = SharedInferenceQueue(max_size=64)
    timed_queue = _TimedQueue(real_queue)
    stream = ReplayAudioStream(wav, chunk_size_frames=512, real_time=True)
    bufferer = AudioBufferer(
        audio_stream=stream,
        queue=timed_queue,
        priority=0,
        speaker="prospect",
        transcribe_live=True,
    )
    stop_event = asyncio.Event()
    window_buf = SlidingWindowBuffer(config.sliding_window_size)

    async def _watch_eof() -> None:
        while not stream.at_eof:
            await asyncio.sleep(0.01)
        await asyncio.sleep(_EOF_SETTLE_S)
        stop_event.set()

    async def _consume() -> None:
        last_classified_at: float | None = None
        while True:
            try:
                item = await asyncio.wait_for(real_queue.get(), timeout=_QUEUE_POLL_TIMEOUT_S)
            except TimeoutError:
                if stop_event.is_set() and real_queue.qsize() == 0:
                    return
                continue
            vad_close_ts = timed_queue.close_ts.pop(id(item), time.perf_counter())
            result, last_classified_at = await _process_window_segment(
                item,
                vad_close_ts,
                session=session_dir.name,
                index=len(results),
                backend=backend,
                window_buf=window_buf,
                window_classifier=window_classifier,
                injector=injector,
                window_hooks=window_hooks,
                run_llm=run_llm,
                config=config,
                ws_config=ws_config,
                ui_labels=ui_labels,
                last_classified_at=last_classified_at,
            )
            results.append(result)
            if len(results) >= remaining_budget:
                stop_event.set()
                return

    watcher_task = asyncio.create_task(_watch_eof())
    bufferer_task = asyncio.create_task(bufferer.run(stop_event))
    consumer_task = asyncio.create_task(_consume())

    await consumer_task
    watcher_task.cancel()
    bufferer_task.cancel()
    for task in (watcher_task, bufferer_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
    return results


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------


@dataclass
class StageStats:
    count: int
    p50_ms: float | None
    p95_ms: float | None
    min_ms: float | None
    max_ms: float | None


def _stage_stats(values: list[float]) -> StageStats:
    if not values:
        return StageStats(count=0, p50_ms=None, p95_ms=None, min_ms=None, max_ms=None)
    return StageStats(
        count=len(values),
        p50_ms=percentile(values, 0.5),
        p95_ms=percentile(values, 0.95),
        min_ms=min(values),
        max_ms=max(values),
    )


_STAGE_KEYS = [
    ("vad_to_whisper_ms", "VAD close -> Whisper done"),
    ("whisper_to_router_ms", "Whisper done -> router/gating done"),
    ("router_to_llm_ms", "Router/gating done -> escalation-LLM done (v1: escalated only; v2: classified only)"),
    ("decision_to_emit_ms", "Decision -> emit/render"),
    ("total_ms", "TOTAL (VAD close -> emit)"),
    ("llm_ttft_ms", "Escalation-LLM TTFT (first token; v2 --run only, else null)"),
]


@dataclass
class Report:
    mode: str
    candidate: str
    effective_timeout_ms: int
    sessions_scanned: int
    segments: list[SegmentResult]
    stage_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcome_counts: dict[str, int] = field(default_factory=dict)
    escalation_rate: float | None = None
    over_5s_count: int = 0
    over_5s_total: int = 0
    dry_run_cost_preview: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["segments"] = [
            {**asdict(seg), **seg.stage_ms()} for seg in self.segments
        ]
        return payload


def build_report(
    segments: list[SegmentResult],
    *,
    mode: str,
    candidate: ModelSpec,
    effective_timeout_ms: int,
    sessions_scanned: int,
    dry_run_cost_preview: dict[str, Any] | None,
) -> Report:
    report = Report(
        mode=mode,
        candidate=str(candidate),
        effective_timeout_ms=effective_timeout_ms,
        sessions_scanned=sessions_scanned,
        segments=segments,
        dry_run_cost_preview=dry_run_cost_preview,
    )

    for key, _label in _STAGE_KEYS:
        values = [seg.stage_ms()[key] for seg in segments]
        values = [v for v in values if v is not None]
        report.stage_stats[key] = asdict(_stage_stats(values))

    for seg in segments:
        report.outcome_counts[seg.outcome] = report.outcome_counts.get(seg.outcome, 0) + 1

    classified = [seg for seg in segments if seg.outcome not in {"hallucination", "whisper_timeout", "empty"}]
    escalated = [seg for seg in classified if seg.escalated]
    if classified:
        report.escalation_rate = len(escalated) / len(classified)

    total_values = [seg.stage_ms()["total_ms"] for seg in segments if seg.stage_ms()["total_ms"] is not None]
    report.over_5s_total = len(total_values)
    report.over_5s_count = sum(1 for v in total_values if v > _FIVE_S_BUDGET_MS)

    return report


def render_markdown(report: Report) -> str:
    lines: list[str] = [
        "# E2E latency breakdown -- pilot 5s-to-screen budget",
        "",
        f"**Mode:** {report.mode}  ",
        f"**Escalation-LLM candidate:** `{report.candidate}`  ",
        f"**Effective LLM_TIMEOUT_MS:** {report.effective_timeout_ms} ms  ",
        f"**Sessions scanned:** {report.sessions_scanned}  ",
        f"**Segments processed:** {len(report.segments)}",
        "",
        "## Per-stage breakdown",
        "",
        "| Stage | n | p50 | p95 | min | max |",
        "|---|---:|---:|---:|---:|---:|",
    ]

    def _fmt(v: float | None) -> str:
        return "—" if v is None else f"{v:.0f} ms"

    for key, label in _STAGE_KEYS:
        stats = report.stage_stats.get(key, {})
        lines.append(
            f"| {label} | {stats.get('count', 0)} | {_fmt(stats.get('p50_ms'))} | "
            f"{_fmt(stats.get('p95_ms'))} | {_fmt(stats.get('min_ms'))} | {_fmt(stats.get('max_ms'))} |"
        )
    lines.append("")

    lines.append("## 5s budget")
    lines.append("")
    if report.over_5s_total:
        pct = 100.0 * report.over_5s_count / report.over_5s_total
        lines.append(
            f"**{report.over_5s_count}/{report.over_5s_total}** emitted segments "
            f"({pct:.1f}%) exceeded the 5000 ms budget (VAD close -> emit)."
        )
    else:
        lines.append("No segments reached the emit stage (nothing to compare against the budget).")
    lines.append("")

    is_v2 = report.mode.startswith("v2")
    lines.append("## Escalation rate" if not is_v2 else "## Classify rate (gate -> WindowClassifier)")
    lines.append("")
    if report.escalation_rate is not None:
        if is_v2:
            lines.append(
                f"**{report.escalation_rate * 100:.1f}%** of transcribed segments passed the "
                "window-buffer gate and reached WindowClassifier."
            )
        else:
            lines.append(
                f"**{report.escalation_rate * 100:.1f}%** of classified segments escalated to the "
                "LLM-confirm route."
            )
    else:
        lines.append("No classified segments (nothing escalated/gated-through or matched).")
    lines.append("")

    lines.append("## Outcome counts")
    lines.append("")
    lines.append("| Outcome | Count |")
    lines.append("|---|---:|")
    for outcome, count in sorted(report.outcome_counts.items()):
        lines.append(f"| {outcome} | {count} |")
    lines.append("")

    if report.dry_run_cost_preview is not None:
        preview = report.dry_run_cost_preview
        count_key = "would_classify_count" if is_v2 else "would_escalate_count"
        verb = "would classify" if is_v2 else "would escalate"
        lines.append("## Dry-run cost preview (no provider called)")
        lines.append("")
        lines.append(f"- Segments that {verb}: {preview[count_key]}")
        lines.append(f"- Estimated input tokens: {preview['input_tokens']}")
        lines.append(f"- Estimated output tokens: {preview['output_tokens']}")
        cost = preview.get("estimated_cost")
        lines.append(
            f"- Estimated cost: {'unknown (no price entry)' if cost is None else f'${cost:.4f}'}"
        )
        lines.append("")
        lines.append("Run with `--run` to execute the real (paid) escalation-LLM calls.")
        lines.append("")

    lines.append("## Known limitations")
    lines.append("")
    if is_v2:
        lines.append(
            "- **TTFT is only real in `--v2 --run`**: `llm_ttft_ms` comes from "
            "`LLMClient.last_ttft_ms`, populated by draining `WindowClassifier.classify(..., "
            "use_streaming=True)`'s `astream()` call. `--v2 --dry-run` makes no LLM call, so it "
            "is `null` there, same as any dry-run."
        )
        lines.append(
            "- **pain_point emit path only**: `doubt` detections publish over a raw websocket "
            "to a live coaching hub (`_dispatch_window_detection` -> `_publish_ws`), not through "
            "`SlideInjector` -- there is no live hub in this harness, so that send is not "
            "capturable the way pain_point -> `SlideInjector.handle_window_detection` is. Those "
            "segments are counted (`classified_not_emitted`) but never get an `emit_ts`."
        )
        lines.append(
            "- **No free preview**: unlike v1's router-only dry-run, `WindowClassifier` has no "
            "local pre-filter -- classification IS the LLM call, so `--v2 --dry-run` can only "
            "report a token/cost *estimate* (real prompt sizes, no call made), not a real "
            "would-classify decision."
        )
    else:
        lines.append(
            "- **TTFT not observable**: the escalation-LLM call uses `core/llm_client.py`'s "
            "non-streaming `instructor` seam; `llm_ttft_ms` is not reported. Use `--v2 --run` "
            "for a real number (see module docstring)."
        )
        lines.append(
            "- **Production-wiring gap**: this mode instruments `DetectionPipeline` "
            "(`process`/`aprocess`), reached live only as the no-LLM-provider fallback. The live "
            "`detector/__main__.py` default path routes pain-point/objection detection through "
            "`SlidingWindowBuffer` + `WindowClassifier` instead -- use `--v2` to instrument that "
            "path."
        )
    lines.append(
        "- **Prospect-only replay**: `self.wav` is not fed through the shared whisper queue, "
        "so the whisper stage does not reflect self-speech backlog contention."
    )
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dry-run cost preview
# ---------------------------------------------------------------------------


def estimate_dry_run_cost(
    segments: list[SegmentResult],
    pipeline: DetectionPipeline,
    pricing: PricingTable,
    candidate: ModelSpec,
) -> dict[str, Any]:
    """Estimate escalation-LLM cost for segments that WOULD escalate, using the
    real ``LLMConfirmClient`` system/user prompt so the char-count is exact,
    not guessed -- only the output-token count is a heuristic (the confirm
    call returns a short structured object)."""
    would_escalate = [seg for seg in segments if seg.outcome == "would_escalate"]
    total_chars = 0
    llm_client = pipeline.llm_client
    system_prompt = getattr(llm_client, "system_prompt", "")
    for seg in would_escalate:
        user_prompt = llm_client._user_prompt(seg.text) if hasattr(llm_client, "_user_prompt") else seg.text  # noqa: SLF001
        total_chars += len(system_prompt) + len(user_prompt)

    input_tokens = max(1, total_chars // _CHARS_PER_TOKEN) if would_escalate else 0
    output_tokens = len(would_escalate) * _ESTIMATED_CONFIRM_OUTPUT_TOKENS
    cost = pricing.estimate_cost(candidate.provider, candidate.model, input_tokens, output_tokens)
    return {
        "would_escalate_count": len(would_escalate),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost": cost,
    }


def estimate_dry_run_cost_v2(
    segments: list[SegmentResult],
    window_classifier: WindowClassifier,
    pricing: PricingTable,
    candidate: ModelSpec,
) -> dict[str, Any] | None:
    """v2's ``estimate_dry_run_cost``: sizes the real ``WindowClassifier`` system/user
    prompt for segments that WOULD reach ``classify()`` (outcome ``would_classify``).

    Returns ``None`` when the classifier was built with provider ``"none"``
    (``WindowClassifier.__init__`` returns early in that case and never builds
    ``system_prompt`` -- there is nothing to size)."""
    if not window_classifier.system_prompt:
        return None
    would_classify = [seg for seg in segments if seg.outcome == "would_classify"]
    total_chars = 0
    for seg in would_classify:
        user_prompt = window_classifier._build_user_prompt(seg.text, "discovery")  # noqa: SLF001
        total_chars += len(window_classifier.system_prompt) + len(user_prompt)

    input_tokens = max(1, total_chars // _CHARS_PER_TOKEN) if would_classify else 0
    output_tokens = len(would_classify) * _ESTIMATED_WINDOW_OUTPUT_TOKENS
    cost = pricing.estimate_cost(candidate.provider, candidate.model, input_tokens, output_tokens)
    return {
        "would_classify_count": len(would_classify),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost": cost,
    }


# ---------------------------------------------------------------------------
# Top-level driver
# ---------------------------------------------------------------------------


async def run_harness(args: argparse.Namespace) -> Report:
    load_dotenv(dotenv_path=REPO_ROOT / ".env", override=True)
    load_env()

    candidate = _DEFAULT_CANDIDATE
    if args.candidate:
        parsed = parse_model_specs(args.candidate)
        candidate = parsed[0] if parsed else _DEFAULT_CANDIDATE
    run_llm = bool(args.run)

    available, reason = is_provider_available(candidate.provider)
    if run_llm and not available:
        print(f"WARNING: candidate provider {candidate.provider!r} may not be usable: {reason}", file=sys.stderr)
    # In dry-run, LLMConfirmClient's/WindowClassifier's LLMClient.build_client() raises at
    # construction time when the candidate provider's API key is missing (see
    # core/llm_client.py:_require_key) -- but dry-run never calls the LLM, only
    # its pure system_prompt/_user_prompt string-builders (for the cost
    # preview), which don't need a live client. Fall back to provider "none"
    # to keep dry-run runnable without credentials; --run always uses the real
    # candidate so a missing key surfaces as a normal call failure.
    pipeline_provider = candidate.provider if (run_llm or available) else "none"

    detector_config = DetectorConfig.from_env()
    detector_config = with_overrides(
        detector_config,
        llm_provider=pipeline_provider,
        llm_model=candidate.model,
        llm_timeout_ms=args.timeout_ms,
    )
    slides_config = SlidesConfig.from_env()
    transcriber_config = TranscriberConfig.from_env()

    backend = create_backend(_build_backend_config(transcriber_config))
    await backend.start(asyncio.Event())
    await backend.warmup()

    try:
        if args.v2:
            return await _run_harness_v2(
                args, backend=backend, detector_config=detector_config, slides_config=slides_config,
                candidate=candidate, run_llm=run_llm,
            )
        return await _run_harness_v1(
            args, backend=backend, detector_config=detector_config, slides_config=slides_config,
            candidate=candidate, run_llm=run_llm,
        )
    finally:
        await backend.stop()


async def _run_harness_v1(
    args: argparse.Namespace,
    *,
    backend: TranscriptionBackend,
    detector_config: DetectorConfig,
    slides_config: SlidesConfig,
    candidate: ModelSpec,
    run_llm: bool,
) -> Report:
    hooks = _SegmentHooks()
    pipeline = DetectionPipeline(config=detector_config, hooks=hooks)
    pipeline.router.classify("warmup zin om het embeddingmodel te laden")

    case_db = SQLiteCaseDB(slides_config.case_db_sqlite_path)
    await case_db.initialize()
    # ws_config is never actually dialed: _process_segment always pre-patches
    # _pain_points_ws/_slide_control_ws with _CaptureWebSocket before any send.
    injector = SlideInjector(pipeline, case_db, ws_config=WebSocketConfig.from_env(), slides_config=slides_config)

    sessions = discover_sessions(Path(args.sessions_dir))
    all_segments: list[SegmentResult] = []
    for session_dir in sessions:
        remaining = args.top_n - len(all_segments)
        if remaining <= 0:
            break
        segments = await _run_session(
            session_dir,
            backend=backend,
            pipeline=pipeline,
            injector=injector,
            hooks=hooks,
            run_llm=run_llm,
            config=detector_config,
            remaining_budget=remaining,
        )
        all_segments.extend(segments)

    dry_run_cost_preview = None
    if not run_llm:
        pricing = PricingTable.from_path(REPO_ROOT / "config" / "eval_model_prices.yaml")
        dry_run_cost_preview = estimate_dry_run_cost(all_segments, pipeline, pricing, candidate)

    return build_report(
        all_segments,
        mode="run" if run_llm else "dry-run",
        candidate=candidate,
        effective_timeout_ms=args.timeout_ms,
        sessions_scanned=len(sessions),
        dry_run_cost_preview=dry_run_cost_preview,
    )


async def _run_harness_v2(
    args: argparse.Namespace,
    *,
    backend: TranscriptionBackend,
    detector_config: DetectorConfig,
    slides_config: SlidesConfig,
    candidate: ModelSpec,
    run_llm: bool,
) -> Report:
    preset = load_preset(detector_config.preset_name)
    window_hooks = _WindowHooks()
    window_classifier = WindowClassifier(
        detector_config, detector_config.llm_provider, preset=preset, hooks=window_hooks
    )

    case_db = SQLiteCaseDB(slides_config.case_db_sqlite_path)
    await case_db.initialize()
    ws_config = WebSocketConfig.from_env()
    # pipeline is a duck-typed stand-in, not a real DetectionPipeline: SlideInjector only
    # ever does getattr(pipeline, "llm_client", None) with it (for the Pro-only dynamic-
    # slide-generation path, not exercised here) -- see injector.py. Passing one avoids
    # loading the embedding-model router / constructing an LLMConfirmClient, neither of
    # which the v2 (window-classify) path uses.
    injector = SlideInjector(SimpleNamespace(), case_db, ws_config=ws_config, slides_config=slides_config)

    sessions = discover_sessions(Path(args.sessions_dir))
    all_segments: list[SegmentResult] = []
    for session_dir in sessions:
        remaining = args.top_n - len(all_segments)
        if remaining <= 0:
            break
        segments = await _run_session_v2(
            session_dir,
            backend=backend,
            window_classifier=window_classifier,
            injector=injector,
            window_hooks=window_hooks,
            run_llm=run_llm,
            config=detector_config,
            ws_config=ws_config,
            ui_labels=preset.ui_labels,
            remaining_budget=remaining,
        )
        all_segments.extend(segments)

    dry_run_cost_preview = None
    if not run_llm:
        pricing = PricingTable.from_path(REPO_ROOT / "config" / "eval_model_prices.yaml")
        dry_run_cost_preview = estimate_dry_run_cost_v2(all_segments, window_classifier, pricing, candidate)

    return build_report(
        all_segments,
        mode="v2-run" if run_llm else "v2-dry-run",
        candidate=candidate,
        effective_timeout_ms=args.timeout_ms,
        sessions_scanned=len(sessions),
        dry_run_cost_preview=dry_run_cost_preview,
    )


def _write_reports(report: Report, report_md: Path, report_json: Path) -> None:
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text(render_markdown(report), encoding="utf-8")
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report.to_json_dict(), indent=2, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--candidate",
        type=str,
        default=None,
        help=f"Escalation-LLM override as provider:model. Default: {_DEFAULT_CANDIDATE}",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=_DEFAULT_TIMEOUT_MS,
        help=(
            "LLM_TIMEOUT_MS override for the escalation-LLM call. Applied AFTER "
            "DetectorConfig.from_env() (see run_harness), so this always wins over both "
            f"the shell env and a live-copilot .env. Default: {_DEFAULT_TIMEOUT_MS}"
        ),
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=_DEFAULT_SESSIONS_DIR,
        help=f"Root with <session-id>/prospect.wav fixtures. Default: {_DEFAULT_SESSIONS_DIR}",
    )
    parser.add_argument("--top-n", type=int, default=_DEFAULT_TOP_N, help=f"Cap on segments. Default: {_DEFAULT_TOP_N}")
    parser.add_argument(
        "--report-md",
        type=Path,
        default=None,
        help=f"Default: {_DEFAULT_REPORT_MD} (v1) / {_DEFAULT_REPORT_MD_V2} (--v2)",
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        default=None,
        help=f"Default: {_DEFAULT_REPORT_JSON} (v1) / {_DEFAULT_REPORT_JSON_V2} (--v2)",
    )
    parser.add_argument(
        "--v2",
        action="store_true",
        help=(
            "Instrument the LIVE SlidingWindowBuffer + WindowClassifier path instead of "
            "DetectionPipeline (see module docstring). Combine with --dry-run/--run as usual."
        ),
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="VAD/Whisper/detection stages only, no paid LLM calls (default behavior).",
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="Real run: also executes the escalation-LLM calls (paid).",
    )
    args = parser.parse_args(argv)
    if args.report_md is None:
        args.report_md = _DEFAULT_REPORT_MD_V2 if args.v2 else _DEFAULT_REPORT_MD
    if args.report_json is None:
        args.report_json = _DEFAULT_REPORT_JSON_V2 if args.v2 else _DEFAULT_REPORT_JSON

    sessions_dir = Path(args.sessions_dir)
    if not discover_sessions(sessions_dir):
        print(
            f"No session fixtures with prospect.wav found under {sessions_dir}. "
            "This is expected in CI/dispatch worktrees (data/sessions/ is gitignored); "
            "run this in the main checkout with real session recordings.",
            file=sys.stderr,
        )
        return 0

    report = asyncio.run(run_harness(args))
    _write_reports(report, Path(args.report_md), Path(args.report_json))
    print(render_markdown(report))
    print(f"\nMarkdown report: {args.report_md}")
    print(f"JSON report: {args.report_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
