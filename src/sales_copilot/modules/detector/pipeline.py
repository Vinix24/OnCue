from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.llm_confirm import LLMConfirmClient
from sales_copilot.modules.detector.router import PainPointRouter, RouteMatch

if TYPE_CHECKING:
    from sales_copilot.core.audit_ledger import AuditWriter

logger = logging.getLogger(__name__)


class DetectionHooks(Protocol):
    """Opt-in instrumentation callbacks for the detection cascade.

    Implementations must not raise: the production detection path catches and
    logs any hook exception so that a faulty harness cannot break live calls.
    """

    def on_classify_start(self) -> None: ...
    def on_classify_end(self, match: RouteMatch | None, latency_ms: float) -> None: ...
    def on_llm_start(self) -> None: ...
    def on_llm_end(
        self,
        confirmation: object | None,
        latency_ms: float,
        tokens: dict[str, int] | None,
    ) -> None: ...
    def on_event(self, event: PainPointEvent | None) -> None: ...


class _NoOpDetectionHooks:
    def on_classify_start(self) -> None: pass
    def on_classify_end(self, match: RouteMatch | None, latency_ms: float) -> None: pass
    def on_llm_start(self) -> None: pass
    def on_llm_end(
        self,
        confirmation: object | None,
        latency_ms: float,
        tokens: dict[str, int] | None,
    ) -> None: pass
    def on_event(self, event: PainPointEvent | None) -> None: pass


@dataclass(frozen=True)
class PainPointEvent:
    category: str
    confidence: float
    trigger_phrase: str
    timestamp_ms: int
    live: bool = True
    provisional: bool = False


class DetectionPipeline:
    def __init__(
        self,
        config: DetectorConfig | None = None,
        router: PainPointRouter | None = None,
        llm_client: LLMConfirmClient | None = None,
        debouncer: PainPointDebouncer | None = None,
        audit_writer: AuditWriter | None = None,
        session_id: str | None = None,
        hooks: DetectionHooks | None = None,
    ) -> None:
        self.config = config or DetectorConfig.from_env()
        self._hooks = hooks or _NoOpDetectionHooks()
        self.router = router or PainPointRouter(self.config)
        self.llm_client = llm_client or LLMConfirmClient(self.config)
        self.provider = getattr(self.llm_client, "provider", self.config.llm_provider)
        self.debouncer = debouncer or PainPointDebouncer(self.config.debounce_seconds)
        self._session_id = session_id or str(uuid.uuid4())
        self._pending_confirmations: set[asyncio.Task[None]] = set()
        if self.config.preset_name == "recruitment":
            if audit_writer is not None:
                self._audit_writer: AuditWriter | None = audit_writer
            else:
                from sales_copilot.core.audit_ledger import get_audit_writer
                self._audit_writer = get_audit_writer(require_hmac=True)
        else:
            self._audit_writer = None

    def _safe_hook(self, name: str, *args: Any, **kwargs: Any) -> None:
        """Invoke a hook by name and swallow any exception to protect live detection."""
        try:
            getattr(self._hooks, name)(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.exception("Detection hook %r raised; ignoring", name)

    def process(self, text: str, speaker: str) -> PainPointEvent | None:
        if self.config.only_classify_prospect and speaker == "self":
            return None

        transcript_line = text.strip()
        logger.debug("Detector route check: %s", transcript_line)

        pii_hits = 0
        redacted_len = 0
        start_ms = int(time.time() * 1000) if self._audit_writer is not None else 0

        # llm_text is what gets sent to the router and LLM classifier.
        # For recruitment preset (DPIA Art. 9 obligation) redact PII before any LLM call.
        llm_text = transcript_line
        if self._audit_writer is not None:
            from sales_copilot.core.pii_filter import redact_pii
            clean, pii_hits = redact_pii(transcript_line)
            redacted_len = len(transcript_line) - len(clean)
            llm_text = clean

        self._safe_hook("on_classify_start")
        classify_start = time.perf_counter()
        match = self.router.classify(llm_text)
        classify_latency_ms = (time.perf_counter() - classify_start) * 1000
        self._safe_hook("on_classify_end", match, classify_latency_ms)

        if match is None:
            logger.debug("Router: no match above threshold")
            event = None
        elif match.confidence >= self.config.confidence_threshold_high:
            logger.info("Pain-point confirmed: %s (%.2f)", match.category, match.confidence)
            event = self._emit(match.category, match.confidence, transcript_line or text)
        elif match.confidence < self.config.confidence_threshold_low:
            event = None
        else:
            logger.debug("LLM confirm start: provider=%s", self.provider)
            self._safe_hook("on_llm_start")
            llm_start = time.perf_counter()
            confirmation = self.llm_client.confirm(llm_text)
            llm_latency_ms = (time.perf_counter() - llm_start) * 1000
            usage: dict[str, int] | None = getattr(
                self.llm_client, "last_usage", None
            )
            self._safe_hook("on_llm_end", confirmation, llm_latency_ms, usage)
            if confirmation is None:
                logger.debug("LLM confirm rejected")
                event = None
            else:
                logger.info(
                    "Pain-point confirmed: %s (%.2f)",
                    confirmation.category,
                    confirmation.confidence,
                )
                event = self._emit(
                    confirmation.category,
                    confirmation.confidence,
                    confirmation.trigger_phrase or transcript_line or text,
                )

        self._safe_hook("on_event", event)

        if self._audit_writer is not None:
            from sales_copilot.core.audit_ledger import build_audit_record
            latency_ms = int(time.time() * 1000) - start_ms
            self._audit_writer.write(
                build_audit_record(
                    session_id=self._session_id,
                    event_type="detection" if event is not None else "text_processed",
                    pii_hits=pii_hits,
                    redacted_len=redacted_len,
                    detection_count=1 if event is not None else 0,
                    model_id=self.config.llm_model,
                    latency_ms=latency_ms,
                )
            )

        return event

    async def aprocess(
        self,
        text: str,
        speaker: str,
        on_enrichment: Callable[[PainPointEvent | None], Awaitable[None]] | None = None,
    ) -> PainPointEvent | None:
        """Live async entry point: emit immediately, confirm the LLM in the background.

        For high-confidence matches the result is final and returned immediately.
        For middle-band matches a provisional event is returned immediately and the
        LLM confirmation is scheduled as a background task. When the LLM completes,
        the existing hooks (``on_llm_end``, ``on_event``) and the optional
        ``on_enrichment`` callback receive the final decision.
        """
        if self.config.only_classify_prospect and speaker == "self":
            return None

        transcript_line = text.strip()
        logger.debug("Detector async route check: %s", transcript_line)

        pii_hits = 0
        redacted_len = 0
        start_ms = int(time.time() * 1000) if self._audit_writer is not None else 0

        llm_text = transcript_line
        if self._audit_writer is not None:
            from sales_copilot.core.pii_filter import redact_pii
            clean, pii_hits = redact_pii(transcript_line)
            redacted_len = len(transcript_line) - len(clean)
            llm_text = clean

        self._safe_hook("on_classify_start")
        classify_start = time.perf_counter()
        match = self.router.classify(llm_text)
        classify_latency_ms = (time.perf_counter() - classify_start) * 1000
        self._safe_hook("on_classify_end", match, classify_latency_ms)

        if match is None:
            logger.debug("Router: no match above threshold")
            event = None
        elif match.confidence >= self.config.confidence_threshold_high:
            logger.info("Pain-point confirmed: %s (%.2f)", match.category, match.confidence)
            event = self._emit(
                match.category,
                match.confidence,
                transcript_line or text,
                live=True,
                provisional=False,
            )
        elif match.confidence < self.config.confidence_threshold_low:
            event = None
        else:
            logger.debug("LLM confirm start (async): provider=%s", self.provider)
            self._safe_hook("on_llm_start")
            event = self._emit(
                match.category,
                match.confidence,
                transcript_line or text,
                live=True,
                provisional=True,
            )
            if event is not None:
                task = asyncio.create_task(
                    self._confirm_async(llm_text, event, on_enrichment)
                )
                self._pending_confirmations.add(task)
                task.add_done_callback(self._pending_confirmations.discard)

        self._safe_hook("on_event", event)

        if self._audit_writer is not None:
            from sales_copilot.core.audit_ledger import build_audit_record
            latency_ms = int(time.time() * 1000) - start_ms
            self._audit_writer.write(
                build_audit_record(
                    session_id=self._session_id,
                    event_type="detection" if event is not None else "text_processed",
                    pii_hits=pii_hits,
                    redacted_len=redacted_len,
                    detection_count=1 if event is not None else 0,
                    model_id=self.config.llm_model,
                    latency_ms=latency_ms,
                )
            )

        return event

    async def _confirm_async(
        self,
        llm_text: str,
        provisional_event: PainPointEvent,
        on_enrichment: Callable[[PainPointEvent | None], Awaitable[None]] | None,
    ) -> None:
        """Run LLM confirmation off the live path and notify hooks/callbacks."""
        try:
            llm_start = time.perf_counter()
            confirmation = await self.llm_client.confirm_async(llm_text)
            llm_latency_ms = (time.perf_counter() - llm_start) * 1000
            usage: dict[str, int] | None = getattr(
                self.llm_client, "last_usage", None
            )
            self._safe_hook("on_llm_end", confirmation, llm_latency_ms, usage)

            if confirmation is None:
                logger.debug("LLM confirm rejected (async)")
                self._safe_hook("on_event", None)
                if on_enrichment is not None:
                    await on_enrichment(None)
                return

            logger.info(
                "Pain-point confirmed (async): %s (%.2f)",
                confirmation.category,
                confirmation.confidence,
            )
            final_event = PainPointEvent(
                category=confirmation.category,
                confidence=confirmation.confidence,
                trigger_phrase=confirmation.trigger_phrase or provisional_event.trigger_phrase,
                timestamp_ms=int(time.time() * 1000),
                live=True,
                provisional=False,
            )
            self._safe_hook("on_event", final_event)
            if on_enrichment is not None:
                await on_enrichment(final_event)
        except Exception:
            logger.exception("Async LLM confirmation failed")

    def _emit(
        self,
        category: str,
        confidence: float,
        trigger_phrase: str,
        live: bool = True,
        provisional: bool = False,
    ) -> PainPointEvent | None:
        if not self.debouncer.should_trigger(category):
            return None
        self.debouncer.record_trigger(category)
        timestamp_ms = int(time.time() * 1000)
        return PainPointEvent(
            category=category,
            confidence=confidence,
            trigger_phrase=trigger_phrase,
            timestamp_ms=timestamp_ms,
            live=live,
            provisional=provisional,
        )
