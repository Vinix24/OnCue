from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections.abc import Callable

import websockets
from websockets.exceptions import ConnectionClosed

from sales_copilot.auth.feature_policy import FEATURE_SCRIPT_TRACKING_COMPUTE, get_feature_policy
from sales_copilot.core.config import DetectorConfig, SlidesConfig, WebSocketConfig, load_env
from sales_copilot.core.logging import configure_logging
from sales_copilot.core.preset import load_preset
from sales_copilot.modules.coaching import ScriptTracker, ScriptTrackerEngine
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.llm_confirm import LLMConfirmClient
from sales_copilot.modules.detector.objection_detector import ObjectionDetector
from sales_copilot.modules.detector.phase_detector import AutomaticPhaseDetector
from sales_copilot.modules.detector.pipeline import DetectionPipeline
from sales_copilot.modules.detector.router import PainPointRouter
from sales_copilot.modules.detector.sliding_window import SlidingWindowBuffer, TranscriptChunk
from sales_copilot.modules.detector.suggestions import SuggestionEngine
from sales_copilot.modules.detector.summary import SummaryEngine
from sales_copilot.modules.detector.window_classifier import WindowClassifier, WindowDetection
from sales_copilot.modules.slides.case_db import SQLiteCaseDB
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode_payload(raw: str | bytes) -> object:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _extract_transcript_fields(
    payload: object,
    *,
    now_ms: Callable[[], int] | None = None,
) -> tuple[str, str, int] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "transcript":
        return None
    text = payload.get("text")
    speaker = payload.get("speaker")
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(speaker, str) or not speaker.strip():
        return None

    timestamp_ms = payload.get("end_ms")
    if timestamp_ms is None:
        timestamp_ms = payload.get("start_ms")
    if not isinstance(timestamp_ms, (int, float)):
        timestamp_ms = (now_ms or _now_ms)()

    return text, speaker, int(timestamp_ms)


def _channel_url(ws_config: WebSocketConfig, channel: str) -> str:
    return channel_ws_url(ws_config, channel)


def _print_banner(
    detector_config: DetectorConfig,
    slides_config: SlidesConfig,
    ws_config: WebSocketConfig,
    *,
    route_count: int,
    case_count: int,
    script_tracking_enabled: bool,
) -> None:
    logger.info("OnCue — Detector Module")
    logger.info("LLM: %s (%s)", detector_config.llm_provider, detector_config.llm_model)
    logger.info("Embedding model: %s", detector_config.embedding_model)
    logger.info(
        "Confidence thresholds: high=%s low=%s",
        detector_config.confidence_threshold_high,
        detector_config.confidence_threshold_low,
    )
    logger.info("Debounce seconds: %s", detector_config.debounce_seconds)
    logger.info(
        "Window classifier: size=%s min_chunks=%s debounce=%.1fs",
        detector_config.sliding_window_size,
        detector_config.min_chunks_to_classify,
        detector_config.classification_debounce_seconds,
    )
    logger.info("Auto phase detection: %s", detector_config.auto_phase_detection)
    logger.info("Objection detection: %s", detector_config.enable_objection_detection)
    logger.info("Suggestions enabled: %s", detector_config.enable_suggestions)
    logger.info("Conversation summary: %s", detector_config.enable_summary)
    logger.info("Script tracking: %s", script_tracking_enabled)
    logger.info("Call language: %s", detector_config.call_language)
    logger.info("Routes loaded: %s | Cases loaded: %s", route_count, case_count)
    logger.info("Prospect industry: %s", slides_config.prospect_industry or "(any)")
    logger.info("WebSocket hub: ws://%s:%s", ws_config.host, ws_config.port)


async def _publish_ws(
    ws_config: WebSocketConfig,
    channel: str,
    payload: dict[str, object],
) -> None:
    url = channel_ws_url(ws_config, channel)
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(payload))
    except Exception:
        logger.warning("Failed to publish to /%s (type=%s)", channel, payload.get("type"))


async def _dispatch_window_detection(
    detection: WindowDetection,
    timestamp_ms: int,
    injector: SlideInjector,
    ws_config: WebSocketConfig,
    config: DetectorConfig,
    ui_labels: dict,
) -> None:
    # objection and buying_signal cards are single-sourced from ObjectionDetector
    # (the fast embedding-match path) so the curated response_suggestion and the
    # Free/Pro gate are only applied once. See objection_detector.py.
    speaker_label = ui_labels.get("prospect", "prospect")
    if detection.category == "pain_point":
        await injector.handle_window_detection(
            category=detection.subcategory or "pain_point",
            confidence=detection.confidence,
            evidence_quote=detection.evidence_quote,
            timestamp_ms=timestamp_ms,
        )
    elif detection.category == "doubt":
        await _publish_ws(
            ws_config,
            "coaching",
            {
                "type": "coaching_alert",
                "subtype": "doubt",
                "category": detection.subcategory or "doubt",
                "confidence": detection.confidence,
                "trigger_phrase": detection.evidence_quote,
                "timestamp_ms": timestamp_ms,
                "speaker_label": speaker_label,
            },
        )


async def _detect_objection(
    objection_detector: ObjectionDetector,
    text: str,
    speaker: str,
    timestamp_ms: int,
) -> None:
    """Run the fast objection match off the transcript-consume hot path."""
    try:
        await objection_detector.process_transcript(text, speaker, timestamp_ms)
    except Exception:
        logger.debug("Fast objection path failed for chunk, continuing", exc_info=True)


async def _consume_transcripts(
    injector: SlideInjector,
    suggestion_engine: SuggestionEngine | None,
    summary_engine: SummaryEngine | None,
    script_tracker: ScriptTracker | None,
    ws: websockets.ClientConnection,
    stop_event: asyncio.Event,
    *,
    window_buf: SlidingWindowBuffer,
    window_classifier: WindowClassifier,
    objection_detector: ObjectionDetector | None,
    ws_config: WebSocketConfig,
    config: DetectorConfig,
    ui_labels: dict,
) -> None:
    last_classified_at: float | None = None
    objection_tasks: list[asyncio.Task[None]] = []

    while not stop_event.is_set():
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
        except TimeoutError:
            continue
        except ConnectionClosed:
            logger.info("Transcript WebSocket closed.")
            break

        payload = _decode_payload(raw)
        fields = _extract_transcript_fields(payload)
        if fields is None:
            logger.debug("Detector skipped payload: transcript fields missing or invalid")
            continue
        text, speaker, timestamp_ms = fields
        logger.debug(
            "Detector received transcript chunk: speaker=%s end_ms=%s text=%r",
            speaker,
            timestamp_ms,
            text[:200],
        )

        if suggestion_engine is not None:
            await suggestion_engine.enqueue(text, speaker, timestamp_ms)
        if summary_engine is not None:
            await summary_engine.enqueue(text, speaker, timestamp_ms)
        if script_tracker is not None:
            asyncio.create_task(script_tracker.process_utterance(text, speaker, timestamp_ms))

        # Fast objection path: embedding-match against the preset's objection set
        # and emit a ready-made rebuttal. Run each classification in its own
        # offloaded task so the event loop never blocks; the detector's debouncer
        # prevents duplicate emissions for the same category.
        if objection_detector is not None:
            objection_tasks.append(
                asyncio.create_task(
                    _detect_objection(objection_detector, text, speaker, timestamp_ms)
                )
            )

        if config.only_classify_prospect and speaker != "prospect":
            logger.debug(
                "Detector skipped classify: only_classify_prospect=%s speaker=%s",
                config.only_classify_prospect,
                speaker,
            )
            continue

        # Graceful degradation: when no cloud LLM is configured, run the local
        # embedding/deterministic pipeline on every prospect chunk so the demo
        # still surfaces pain points without an API key.
        if config.llm_provider in {"none", ""}:
            await injector.process_transcript(text, speaker, timestamp_ms)

        chunk = TranscriptChunk(
            text=text,
            speaker=speaker,
            start_ms=int(payload.get("start_ms", 0)) if isinstance(payload, dict) else 0,
            end_ms=timestamp_ms,
        )
        window_buf.add(chunk)
        window_text = window_buf.context_text()
        logger.debug(
            "Detector buffered chunk: size=%s/%s min_chunks=%s latest_end_ms=%s window_text=%r",
            len(window_buf),
            config.sliding_window_size,
            config.min_chunks_to_classify,
            timestamp_ms,
            window_text[:500],
        )

        if len(window_buf) < config.min_chunks_to_classify:
            logger.debug(
                "Detector skipped classify: buffer size %s below min_chunks_to_classify=%s",
                len(window_buf),
                config.min_chunks_to_classify,
            )
            continue

        now = time.monotonic()
        if last_classified_at is not None and (now - last_classified_at) < config.classification_debounce_seconds:
            elapsed = now - last_classified_at
            logger.debug(
                "Detector skipped classify: debounce active elapsed=%.3fs threshold=%.3fs",
                elapsed,
                config.classification_debounce_seconds,
            )
            continue

        last_classified_at = now
        logger.debug(
            "Detector invoking WindowClassifier: provider=%s model=%s buffer_size=%s latest_chunk=%r",
            config.llm_provider,
            config.llm_model,
            len(window_buf),
            (window_buf.latest_chunk().text[:200] if window_buf.latest_chunk() else ""),
        )

        try:
            analysis = await window_classifier.classify(
                window_text=window_text,
                latest_chunk=window_buf.latest_chunk(),
            )
        except Exception:
            logger.exception("Window classification failed")
            continue

        logger.debug(
            "Detector classify result: detections=%s",
            [
                {
                    "category": det.category,
                    "subcategory": det.subcategory,
                    "confidence": det.confidence,
                    "evidence_quote": det.evidence_quote[:120],
                }
                for det in analysis.detections
            ],
        )
        for detection in analysis.detections:
            if detection.category == "none":
                logger.debug("Detector dropped detection: category=none")
                continue
            if detection.confidence < config.confidence_threshold_low:
                logger.debug(
                    "Detector dropped detection: confidence %.3f below threshold %.3f",
                    detection.confidence,
                    config.confidence_threshold_low,
                )
                continue
            await _dispatch_window_detection(detection, timestamp_ms, injector, ws_config, config, ui_labels)

    # Give in-flight objection classifications a bounded chance to emit before
    # teardown so a cold embedding-model load does not swallow objections.
    if objection_tasks:
        pending = [task for task in objection_tasks if not task.done()]
        if pending:
            done, _ = await asyncio.wait(pending, timeout=3.0)
            for task in done:
                try:
                    await task
                except asyncio.CancelledError:
                    pass


async def main(
    *,
    detector_config: DetectorConfig | None = None,
    slides_config: SlidesConfig | None = None,
    stop_event: asyncio.Event | None = None,
    register_signals: bool = True,
    context_docs: list[str] | None = None,
    session_id: str | None = None,
) -> None:
    load_env()
    configure_logging()
    detector_config = detector_config or DetectorConfig.from_env()
    slides_config = slides_config or SlidesConfig.from_env()
    ws_config = WebSocketConfig.from_env()
    transcript_url = _channel_url(ws_config, "transcript")
    logger.info("Connecting to transcript stream: %s", transcript_url)

    stop_event = stop_event or asyncio.Event()
    injector: SlideInjector | None = None
    suggestion_engine: SuggestionEngine | None = None
    objection_detector: ObjectionDetector | None = None
    phase_task: asyncio.Task[None] | None = None
    suggestions_task: asyncio.Task[None] | None = None
    summary_task: asyncio.Task[None] | None = None

    def _request_shutdown() -> None:
        if not stop_event.is_set():
            logger.info("Shutdown requested. Stopping...")
            stop_event.set()

    if register_signals:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: _request_shutdown())

    try:
        async with websockets.connect(transcript_url) as ws:
            # Router/model loading can be CPU-heavy; keep the event loop responsive.
            router = await asyncio.to_thread(
                PainPointRouter,
                detector_config,
                detector_config.call_language,
            )
            llm_client = LLMConfirmClient(detector_config, context_docs=context_docs)
            debouncer = PainPointDebouncer(detector_config.debounce_seconds)
            pipeline = DetectionPipeline(
                detector_config,
                router=router,
                llm_client=llm_client,
                debouncer=debouncer,
                session_id=session_id,
            )

            case_db = SQLiteCaseDB(slides_config.case_db_sqlite_path)
            await case_db.initialize()
            cases = await case_db.list_cases()

            preset = load_preset(detector_config.preset_name)
            logger.info("Preset loaded: %s", preset.name)

            injector = SlideInjector(pipeline, case_db, ws_config, slides_config)
            window_buf = SlidingWindowBuffer(detector_config.sliding_window_size)
            window_classifier = WindowClassifier(detector_config, detector_config.llm_provider, preset=preset)

            objection_detector = None
            if detector_config.enable_objection_detection:
                objection_detector = ObjectionDetector(
                    detector_config,
                    ws_config,
                    language=detector_config.call_language,
                    objections=preset.objections,
                    include_opportunities=detector_config.include_opportunities,
                    include_negatives=detector_config.include_negatives,
                )
                logger.info("Fast objection detector ready (preset routes).")

            script_tracker: ScriptTracker | None = None
            script_tracking_task: asyncio.Task[None] | None = None
            script_tracking_enabled = False
            if detector_config.enable_script_tracking:
                # Two-layer capability gate (finding #1): `.compute` is what
                # decides whether ScriptTracker runs at all -- it is granted
                # on every tier (Free included), so coverage is computed and
                # persisted for the post-call scorecard regardless of tier.
                # `.live`, checked separately inside ScriptTrackerEngine's
                # single publish choke point, decides whether the computed
                # coverage is pushed to the live widget. Do not gate this
                # instantiation on `.live` -- that would silently regress
                # Free back to the old all-or-nothing flag.
                if get_feature_policy().allows(FEATURE_SCRIPT_TRACKING_COMPUTE):
                    # Phase 3: pass the call's session_id (the same id that
                    # flows into reports_main) so coverage checkpoints are
                    # persisted per-call and the post-call Free scorecard can
                    # read the snapshot back as its source of truth.
                    script_tracker = ScriptTracker(detector_config, session_id=session_id)
                    script_engine = ScriptTrackerEngine(script_tracker, ws_config)
                    script_tracking_task = asyncio.create_task(
                        script_engine.run(stop_event),
                        name="script-tracking",
                    )
                    script_tracking_enabled = True
                    logger.info("Script tracking started.")
                else:
                    logger.info(
                        "Script tracking disabled: %s not entitled.",
                        FEATURE_SCRIPT_TRACKING_COMPUTE,
                    )

            suggestion_engine = None
            summary_engine = None
            if detector_config.enable_suggestions:
                suggestion_engine = SuggestionEngine(
                    detector_config,
                    ws_config,
                    context_docs=context_docs,
                )
                suggestions_task = asyncio.create_task(
                    suggestion_engine.run(stop_event),
                    name="detector-suggestions",
                )
                logger.info("Suggestion task started.")
            if detector_config.enable_summary:
                summary_engine = SummaryEngine(detector_config, ws_config)
                summary_task = asyncio.create_task(
                    summary_engine.run(stop_event),
                    name="detector-summary",
                )
                logger.info("Conversation summary task started.")
            if detector_config.auto_phase_detection:
                phase_detector = AutomaticPhaseDetector(detector_config, ws_config)
                phase_task = asyncio.create_task(phase_detector.run(stop_event))
                logger.info("Automatic phase detection task started.")
            _print_banner(
                detector_config,
                slides_config,
                ws_config,
                route_count=len(router.routes),
                case_count=len(cases),
                script_tracking_enabled=script_tracking_enabled,
            )
            await _consume_transcripts(
                injector,
                suggestion_engine,
                summary_engine,
                script_tracker,
                ws,
                stop_event,
                window_buf=window_buf,
                window_classifier=window_classifier,
                objection_detector=objection_detector,
                ws_config=ws_config,
                config=detector_config,
                ui_labels=preset.ui_labels,
            )
    finally:
        if suggestions_task is not None:
            suggestions_task.cancel()
            try:
                await suggestions_task
            except asyncio.CancelledError:
                pass
        if phase_task is not None:
            phase_task.cancel()
            try:
                await phase_task
            except asyncio.CancelledError:
                pass
        if summary_task is not None:
            summary_task.cancel()
            try:
                await summary_task
            except asyncio.CancelledError:
                pass
        if script_tracking_task is not None:
            script_tracking_task.cancel()
            try:
                await script_tracking_task
            except asyncio.CancelledError:
                pass
        if script_tracker is not None:
            # Final checkpoint: the confirmation cycle checkpoints at its own
            # cadence, so a call that ends before the next cycle would
            # otherwise leave the scorecard reading a stale (or empty)
            # snapshot. Checkpoint is a no-op when disabled or sessionless.
            await script_tracker.checkpoint()
        if injector is not None:
            await injector.close()
        if suggestion_engine is not None:
            await suggestion_engine.close()
        if objection_detector is not None:
            await objection_detector.close()

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
