from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

import websockets

from sales_copilot.auth.feature_policy import (
    FeaturePolicy,
    get_feature_policy,
    resolve_response_suggestion,
)
from sales_copilot.core.config import DetectorConfig, WebSocketConfig, load_yaml
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.router import (
    PainPointRouter,
    RouteMatch,
    _load_routes,
    _resolve_localized_config_path,
    routes_from_mapping,
)
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObjectionEvent:
    category: str
    confidence: float
    trigger_phrase: str
    response_suggestion: str
    timestamp_ms: int


@dataclass(frozen=True)
class OpportunityEvent:
    category: str
    confidence: float
    trigger_phrase: str
    response_suggestion: str
    timestamp_ms: int


class ObjectionRouter(PainPointRouter):
    def __init__(
        self,
        config: DetectorConfig | None = None,
        language: str = "nl",
        *,
        objections: dict | None = None,
        include_opportunities: bool | None = None,
        include_negatives: bool | None = None,
    ) -> None:
        self.config = config or DetectorConfig.from_env()
        self.language = (language or self.config.call_language).strip().lower()
        # When a vakgebied-preset is active its objections take precedence over
        # the global config/objections.yaml, so the fast route matches the same
        # objection set the LLM classifier uses (e.g. the cold-call set).
        if objections is not None:
            self.routes = routes_from_mapping(objections, source="preset objections")
        else:
            self.routes = _load_routes(self.config.objections_config, self.language)

        # Structural precision fix: load opportunity/readiness routes as a
        # separate class so near-misses are routed away from objections instead
        # of forced into the closest objection category.
        self._include_opportunities = (
            include_opportunities
            if include_opportunities is not None
            else self.config.include_opportunities
        )
        self._opportunity_categories: frozenset[str] = frozenset()
        # Structural precision fix: load opportunity/readiness routes in ALL
        # modes (preset objections and global config) when enabled. This keeps
        # readiness signals from being forced into the closest objection class
        # in the live preset/cold-call path.
        if self._include_opportunities:
            try:
                opportunity_routes = _load_routes(
                    self.config.opportunities_config, self.language
                )
            except FileNotFoundError:
                logger.warning(
                    "Opportunities config '%s' not found — continuing with objections only",
                    self.config.opportunities_config,
                )
                opportunity_routes = []
            self._opportunity_categories = frozenset(route.name for route in opportunity_routes)
            self.routes = self.routes + opportunity_routes

        # Structural precision fix #2: load a real negative/"none" class so
        # generic non-objection prospect speech (rambling, questions,
        # confirmations) competes directly against the objection/opportunity
        # routes for the best match, instead of being forced into the closest
        # objection category. A match that resolves to this class is filtered
        # out in classify() (PainPointRouter._configure_negative_class /
        # classify()) — see scripts/eval_objection_precision.py
        # (compute_precision_with_extra_class) for the precision comparison.
        self._configure_negative_class(include_negatives)

        self._router = None
        self._router_lock = threading.Lock()
        self._route_utterances = {
            route.name: [utterance.strip().lower() for utterance in route.utterances if utterance.strip()]
            for route in self.routes
        }


class ObjectionDetector:
    def __init__(
        self,
        config: DetectorConfig | None = None,
        ws_config: WebSocketConfig | None = None,
        *,
        language: str | None = None,
        router: ObjectionRouter | None = None,
        debouncer: PainPointDebouncer | None = None,
        objections: dict | None = None,
        include_opportunities: bool | None = None,
        include_negatives: bool | None = None,
        feature_policy: FeaturePolicy | None = None,
    ) -> None:
        self._config = config or DetectorConfig.from_env()
        self._ws_config = ws_config or WebSocketConfig.from_env()
        self._language = (language or self._config.call_language).strip().lower()
        self._router = router or ObjectionRouter(
            self._config,
            language=self._language,
            objections=objections,
            include_opportunities=include_opportunities,
            include_negatives=include_negatives,
        )
        self._opportunity_categories = self._router._opportunity_categories
        self._debouncer = debouncer or PainPointDebouncer(self._config.debounce_seconds)
        self._feature_policy = feature_policy or get_feature_policy()
        self._objection_responses = self._load_responses(
            self._config.objection_responses_config,
            language=self._language,
        )
        self._opportunity_responses = self._load_responses(
            self._config.opportunity_responses_config,
            language=self._language,
        )
        self._ws: websockets.ClientConnection | None = None
        self._ws_buying_signals: websockets.ClientConnection | None = None

    @staticmethod
    def _load_responses(path: str, language: str = "nl") -> dict[str, str]:
        resolved_path = _resolve_localized_config_path(path, language)
        try:
            data = load_yaml(resolved_path)
        except OSError:
            # The curated response packs are Pro content and may be absent
            # (e.g. the OSS distribution ships detection without the paid
            # kaartenbak). Detection must keep working; cards simply carry
            # no curated response text. Mirrors ObjectionResponsePicker._load.
            logger.warning("ObjectionDetector: could not read %s", resolved_path)
            return {}
        responses = data.get("responses", {})
        if not isinstance(responses, dict):
            raise ValueError(f"{resolved_path} must contain a 'responses' mapping")
        result: dict[str, str] = {}
        for category, template in responses.items():
            if isinstance(template, dict):
                # Structured format {acknowledge/reframe/evidence: [...]}: pick first available
                for strategy in ("acknowledge", "reframe", "evidence"):
                    examples = template.get(strategy, [])
                    if isinstance(examples, list) and examples:
                        result[str(category)] = examples[0]
                        break
            elif isinstance(template, list):
                if template:
                    result[str(category)] = template[0]
            elif isinstance(template, str):
                result[str(category)] = template
        return result

    async def process_transcript(
        self,
        text: str,
        speaker: str,
        timestamp_ms: int,
    ) -> ObjectionEvent | OpportunityEvent | None:
        if not self._config.enable_objection_detection:
            return None
        if self._config.only_classify_prospect and speaker == "self":
            return None

        match = await self._router.classify_async(text)
        if match is None or match.confidence < self._config.confidence_threshold_low:
            return None

        if match.category in self._opportunity_categories:
            return await self._process_opportunity(match, text, timestamp_ms)
        return await self._process_objection(match, text, timestamp_ms)

    async def _process_objection(
        self,
        match: RouteMatch,
        text: str,
        timestamp_ms: int,
    ) -> ObjectionEvent | None:
        if not self._debouncer.should_trigger(match.category):
            return None

        # The curated response packs are Pro content and may be absent (the
        # OSS distribution ships detection without the paid kaartenbak). The
        # card must still fire -- "detectie free, kaartenbak Pro": only the
        # curated response text is empty then, and the Free/Pro seam in
        # resolve_response_suggestion decides what the session sees.
        response = self._objection_responses.get(match.category, "")

        self._debouncer.record_trigger(match.category)
        event = ObjectionEvent(
            category=match.category,
            confidence=match.confidence,
            trigger_phrase=text,
            response_suggestion=response,
            timestamp_ms=timestamp_ms,
        )
        await self._send_event(event)
        return event

    async def _process_opportunity(
        self,
        match: RouteMatch,
        text: str,
        timestamp_ms: int,
    ) -> OpportunityEvent | None:
        if not self._debouncer.should_trigger(match.category):
            return None

        # See _process_objection: absent Pro response packs must not
        # suppress the buying-signal card itself.
        response = self._opportunity_responses.get(match.category, "")

        self._debouncer.record_trigger(match.category)
        event = OpportunityEvent(
            category=match.category,
            confidence=match.confidence,
            trigger_phrase=text,
            response_suggestion=response,
            timestamp_ms=timestamp_ms,
        )
        await self._send_buying_signal(event)
        return event

    async def close(self) -> None:
        for ws in (self._ws, self._ws_buying_signals):
            if ws is None:
                continue
            try:
                await ws.close()
            except Exception:
                pass
        self._ws = None
        self._ws_buying_signals = None

    async def _ensure_ws(self) -> websockets.ClientConnection:
        if self._ws is None or getattr(self._ws, "close_code", None) is not None:
            self._ws = await websockets.connect(
                channel_ws_url(self._ws_config, "objections")
            )
        return self._ws

    async def _ensure_ws_buying_signals(self) -> websockets.ClientConnection:
        if (
            self._ws_buying_signals is None
            or getattr(self._ws_buying_signals, "close_code", None) is not None
        ):
            self._ws_buying_signals = await websockets.connect(
                channel_ws_url(self._ws_config, "buying-signals")
            )
        return self._ws_buying_signals

    async def _send_event(self, event: ObjectionEvent) -> None:
        payload = {
            "type": "objection",
            "category": event.category,
            "confidence": event.confidence,
            "trigger_phrase": event.trigger_phrase,
            "response_suggestion": resolve_response_suggestion(
                event.response_suggestion, feature_policy=self._feature_policy
            ),
            "timestamp_ms": event.timestamp_ms,
        }
        ws = await self._ensure_ws()
        await ws.send(json.dumps(payload))

    async def _send_buying_signal(self, event: OpportunityEvent) -> None:
        payload = {
            "type": "buying_signal",
            "category": event.category,
            "confidence": event.confidence,
            "trigger_phrase": event.trigger_phrase,
            "response_suggestion": resolve_response_suggestion(
                event.response_suggestion, feature_policy=self._feature_policy
            ),
            "timestamp_ms": event.timestamp_ms,
        }
        ws = await self._ensure_ws_buying_signals()
        await ws.send(json.dumps(payload))
