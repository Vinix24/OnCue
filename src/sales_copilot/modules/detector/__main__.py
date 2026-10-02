from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

import websockets
from websockets.exceptions import ConnectionClosed

from sales_copilot.auth.feature_policy import (
    FEATURE_DEEP_INSIGHTS,
    FEATURE_SCRIPT_TRACKING_COMPUTE,
    get_feature_policy,
)
from sales_copilot.core.config import DetectorConfig, InsightConfig, SlidesConfig, WebSocketConfig, load_env
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
from sales_copilot.modules.detector.status_publisher import DetectorStatusPublisher
from sales_copilot.modules.detector.suggestions import SuggestionEngine
from sales_copilot.modules.detector.summary import SummaryEngine
from sales_copilot.modules.detector.window_classifier import WindowClassifier, WindowDetection
from sales_copilot.modules.slides.case_db import SQLiteCaseDB
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)

# Bounded proof-of-life for the consume loop. The hot-path instrumentation in
# _consume_transcripts is DEBUG (default LOG_LEVEL is INFO), so a detector
# dropping every single chunk otherwise looks identical, at the level anybody
# actually runs at, to a detector that was never started (D-4d04b337). This
# heartbeat is counted in received chunks rather than wall-clock time so it
# still fires even when every chunk is being filtered out downstream.
_HEARTBEAT_CHUNK_INTERVAL = 20

# The deep-insight lane (modules/insight/) is excluded from the public OSS
# snapshot, so InsightEngine must not be imported at module scope: that would
# crash the detector on startup when the directory is absent. It is resolved
# lazily behind the entitlement gate by _resolve_insight_engine() and cached
# here. The module-level name is also the seam the deep-lane tests patch via
# monkeypatch.setattr(detector_main, "InsightEngine", ...), so the resolver
# must read globals() rather than bind its own import result.
InsightEngine = None


def _resolve_insight_engine():
    """Return the deep-insight engine class, or None when the lane is absent.

    The import happens here so the detector keeps running when modules/insight/
    is not installed (the public snapshot ships without it). The absent case is
    cached as False so the INFO notice below is emitted once per process.
    """
    engine = globals().get("InsightEngine")
    if engine is not None:
        return None if engine is False else engine
    try:
        from sales_copilot.modules.insight import engine as _insight_module
    except ImportError:
        logger.info("Deep insight lane not installed; detector continues without deep insights.")
        globals()["InsightEngine"] = False
        return None
    engine_cls = _insight_module.InsightEngine
    globals()["InsightEngine"] = engine_cls
    return engine_cls


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


def _redact_ws_url(url: str) -> str:
    """Strip HTTP Basic userinfo (the hub token) before a URL hits the log."""
    parsed = urlsplit(url)
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


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


class _StatusChannel:
    """One reused hub connection for the detector-status heartbeat.

    Mirrors ``TalkTimePublisher``'s persistent-connection shape rather than this
    module's per-message ``_publish_ws``: the status channel fires every few
    seconds for the whole call, so a fresh connect per tick would churn the hub
    and -- worse -- emit a WARNING every few seconds whenever the hub is briefly
    away. Here a failure drops the connection, is reported to the caller (which
    logs it at DEBUG) and reconnects on the next tick. The #203 log heartbeat
    remains the backstop either way.
    """

    def __init__(self, ws_config: WebSocketConfig) -> None:
        self._ws_config = ws_config
        self._ws: websockets.ClientConnection | None = None

    async def send(self, channel: str, payload: dict[str, object]) -> None:
        if self._ws is None:
            self._ws = await websockets.connect(channel_ws_url(self._ws_config, channel))
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:
            self._ws = None
            raise

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            logger.debug("Could not close the detector-status channel", exc_info=True)


def _log_detector_counts(prefix: str, counts: dict[str, int]) -> None:
    logger.info(
        "%s: received=%s dispatched=%s skipped_not_prospect=%s buffered_below_min=%s "
        "debounced=%s classified=%s dropped_none=%s dropped_low_confidence=%s",
        prefix,
        counts["received"],
        counts["dispatched"],
        counts["skipped_not_prospect"],
        counts["buffered_below_min"],
        counts["debounced"],
        counts["classified"],
        counts["dropped_none"],
        counts["dropped_low_confidence"],
    )


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
    insight_engine: InsightEngine | None,
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
    status_publisher: DetectorStatusPublisher | None = None,
) -> None:
    last_classified_at: float | None = None
    objection_tasks: list[asyncio.Task[None]] = []
    counts: dict[str, int] = {
        "received": 0,
        "skipped_not_prospect": 0,
        "buffered_below_min": 0,
        "debounced": 0,
        "classified": 0,
        "dropped_none": 0,
        "dropped_low_confidence": 0,
        "dispatched": 0,
    }

    # Proof-of-life before the first chunk: the dashboard must be able to show
    # "listening, nothing to report yet" from the moment the loop is up, not
    # only once transcript traffic starts. A detector that never publishes this
    # is one the seller can see is missing.
    if status_publisher is not None:
        await status_publisher.publish(counts, force=True)

    while not stop_event.is_set():
        # Throttled inside publish(): evaluated every poll tick so the pulse
        # keeps arriving even when no transcript chunk does.
        if status_publisher is not None:
            await status_publisher.publish(counts)

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
        counts["received"] += 1
        if status_publisher is not None:
            status_publisher.note_chunk()
        if counts["received"] == 1:
            logger.info(
                "first transcript chunk received, speaker=%s, end_ms=%s",
                speaker,
                timestamp_ms,
            )
            if status_publisher is not None:
                await status_publisher.publish(counts, force=True)
        if counts["received"] % _HEARTBEAT_CHUNK_INTERVAL == 0:
            _log_detector_counts("Detector heartbeat", counts)
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
        if insight_engine is not None:
            await insight_engine.enqueue(text, speaker, timestamp_ms)
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
            counts["skipped_not_prospect"] += 1
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
            counts["buffered_below_min"] += 1
            logger.debug(
                "Detector skipped classify: buffer size %s below min_chunks_to_classify=%s",
                len(window_buf),
                config.min_chunks_to_classify,
            )
            continue

        now = time.monotonic()
        if last_classified_at is not None and (now - last_classified_at) < config.classification_debounce_seconds:
            elapsed = now - last_classified_at
            counts["debounced"] += 1
            logger.debug(
                "Detector skipped classify: debounce active elapsed=%.3fs threshold=%.3fs",
                elapsed,
                config.classification_debounce_seconds,
            )
            continue

        last_classified_at = now
        counts["classified"] += 1
        logger.debug(
            "Detector invoking WindowClassifier: provider=%s model=%s buffer_size=%s latest_chunk=%r",
            getattr(window_classifier, "provider", config.llm_provider),
            getattr(window_classifier, "model", config.llm_model),
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
                counts["dropped_none"] += 1
                logger.debug("Detector dropped detection: category=none")
                continue
            if detection.confidence < config.confidence_threshold_low:
                counts["dropped_low_confidence"] += 1
                logger.debug(
                    "Detector dropped detection: confidence %.3f below threshold %.3f",
                    detection.confidence,
                    config.confidence_threshold_low,
                )
                continue
            counts["dispatched"] += 1
            await _dispatch_window_detection(detection, timestamp_ms, injector, ws_config, config, ui_labels)

    _log_detector_counts("Detector session summary", counts)
    if status_publisher is not None:
        await status_publisher.publish(counts, force=True, stopped=True)

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
    insight_config: InsightConfig | None = None,
    slides_config: SlidesConfig | None = None,
    stop_event: asyncio.Event | None = None,
    register_signals: bool = True,
    context_docs: list[str] | None = None,
    session_id: str | None = None,
    client_slug: str | None = None,
) -> None:
    load_env()
    configure_logging()
    detector_config = detector_config or DetectorConfig.from_env()
    insight_config = insight_config or InsightConfig.from_env()
    slides_config = slides_config or SlidesConfig.from_env()
    ws_config = WebSocketConfig.from_env()
    transcript_url = _channel_url(ws_config, "transcript")
    logger.info("Connecting to transcript stream: %s", _redact_ws_url(transcript_url))

    stop_event = stop_event or asyncio.Event()
    injector: SlideInjector | None = None
    suggestion_engine: SuggestionEngine | None = None
    objection_detector: ObjectionDetector | None = None
    script_tracker: ScriptTracker | None = None
    phase_task: asyncio.Task[None] | None = None
    suggestions_task: asyncio.Task[None] | None = None
    summary_task: asyncio.Task[None] | None = None
    insight_task: asyncio.Task[None] | None = None
    script_tracking_task: asyncio.Task[None] | None = None

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
            logger.info("Subscribed to transcript channel: %s", _redact_ws_url(transcript_url))
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
            insight_engine = None
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
            if insight_config.enabled:
                # Two-layer gate, same shape as script tracking's .compute/.live
                # split: constructing the engine is gated on entitlement here (so a
                # non-entitled install never spends deep-lane LLM budget), and
                # hub_core's broadcast layer gates the "insights" channel again as
                # defense-in-depth. InsightEngine is resolved lazily inside the
                # gate so a snapshot without modules/insight/ never crashes.
                if get_feature_policy().allows(FEATURE_DEEP_INSIGHTS):
                    engine_cls = _resolve_insight_engine()
                    if engine_cls is not None:
                        insight_engine = engine_cls(
                            detector_config,
                            insight_config,
                            ws_config,
                            context_docs=context_docs,
                            client_slug=client_slug,
                            session_id=session_id,
                        )
                        insight_task = asyncio.create_task(
                            insight_engine.run(stop_event),
                            name="detector-insight",
                        )
                        logger.info("Deep insight task started.")
                else:
                    logger.info("Deep insights disabled: %s not entitled.", FEATURE_DEEP_INSIGHTS)
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
            status_channel = _StatusChannel(ws_config)
            try:
                await _consume_transcripts(
                    injector,
                    suggestion_engine,
                    summary_engine,
                    insight_engine,
                    script_tracker,
                    ws,
                    stop_event,
                    window_buf=window_buf,
                    window_classifier=window_classifier,
                    objection_detector=objection_detector,
                    ws_config=ws_config,
                    config=detector_config,
                    ui_labels=preset.ui_labels,
                    status_publisher=DetectorStatusPublisher(status_channel.send, detector_config),
                )
            finally:
                await status_channel.close()
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
        if insight_task is not None:
            insight_task.cancel()
            try:
                await insight_task
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
