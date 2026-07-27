from __future__ import annotations

import json
from dataclasses import dataclass

import websockets

from sales_copilot.auth.feature_policy import (
    FeaturePolicy,
    get_feature_policy,
    resolve_response_suggestion,
)
from sales_copilot.auth.license_format import FEATURE_DYNAMIC_SLIDES
from sales_copilot.core.config import SlidesConfig, WebSocketConfig
from sales_copilot.modules.copilot.slide_generator import GeneratedSlide, SlideGenerator
from sales_copilot.modules.detector.pipeline import DetectionPipeline, PainPointEvent
from sales_copilot.modules.slides.case_db import Case, CaseDB
from sales_copilot.websocket.hub_auth import channel_ws_url

# Sentinel distinguishing "no override given" from a legitimate `event=None`
# (no pain point detected) in `process_transcript`'s `precomputed_event` param.
_UNSET: object = object()


@dataclass(frozen=True)
class SlideInjectionResult:
    pain_point_event: PainPointEvent
    case: Case | None
    slide_control_msg: dict[str, object] | None
    pain_point_msg: dict[str, object]


class SlideInjector:
    def __init__(
        self,
        pipeline: DetectionPipeline,
        case_db: CaseDB,
        ws_config: WebSocketConfig,
        slides_config: SlidesConfig,
        *,
        slide_generator: SlideGenerator | None = None,
        feature_policy: FeaturePolicy | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._case_db = case_db
        self._ws_config = ws_config
        self._slides_config = slides_config
        self._pipeline_llm_client = getattr(self._pipeline, "llm_client", None)
        self._slide_generator = slide_generator
        self._feature_policy = feature_policy or get_feature_policy()
        self._slides_enabled = self._feature_policy.allows(FEATURE_DYNAMIC_SLIDES)

        self._pain_points_ws: websockets.ClientConnection | None = None
        self._slide_control_ws: websockets.ClientConnection | None = None

    async def process_transcript(
        self,
        text: str,
        speaker: str,
        timestamp_ms: int,
        *,
        precomputed_event: PainPointEvent | None | object = _UNSET,
    ) -> SlideInjectionResult | None:
        """Run the pain-point decision, look up a case, and publish the result.

        ``precomputed_event`` lets a caller that already ran the pipeline itself
        (e.g. a latency harness that needs distinct classify/LLM/emit timestamps)
        hand the decision straight to the case-lookup + publish step instead of
        triggering a second classification. Leave unset for normal callers —
        every existing call site is unaffected.
        """

        async def _on_enrichment(enriched: object | None) -> None:
            if enriched is None:
                return
            from sales_copilot.modules.detector.pipeline import PainPointEvent
            final = enriched if isinstance(enriched, PainPointEvent) else None
            if final is None:
                return
            final_case = await self._case_db.find_case(
                final.category,
                self._slides_config.prospect_industry,
            )
            enrichment_msg: dict[str, object] = {
                "type": "pain_point_enrichment",
                "category": final.category,
                "confidence": final.confidence,
                "trigger_phrase": final.trigger_phrase,
                "timestamp_ms": final.timestamp_ms,
                "case_matched": final_case is not None,
                "case_id": final_case.id if final_case else None,
            }
            await self._send_json(await self._ensure_pain_points_ws(), enrichment_msg)

        if precomputed_event is not _UNSET:
            event = precomputed_event
        elif hasattr(self._pipeline, "aprocess"):
            event = await self._pipeline.aprocess(text, speaker, on_enrichment=_on_enrichment)
        else:
            event = self._pipeline.process(text, speaker)
        if event is None:
            return None

        case = await self._case_db.find_case(
            event.category,
            self._slides_config.prospect_industry,
        )

        pain_point_msg: dict[str, object] = {
            "type": "pain_point",
            "category": event.category,
            "confidence": event.confidence,
            "trigger_phrase": event.trigger_phrase,
            "timestamp_ms": timestamp_ms,
            "live": event.live,
            "provisional": event.provisional,
            "case_matched": case is not None,
            "case_id": case.id if case else None,
            "response_suggestion": resolve_response_suggestion(
                case.description if case else None, feature_policy=self._feature_policy
            ),
        }

        await self._send_json(await self._ensure_pain_points_ws(), pain_point_msg)

        slide_control_msg: dict[str, object] | None = None
        if self._slides_enabled:
            if case is not None:
                slide_control_msg = {
                    "action": "navigate_to_case",
                    "slide_id": case.id,
                }
                await self._send_json(await self._ensure_slide_control_ws(), slide_control_msg)
            elif self._slides_config.dynamic_slides:
                slide_generator = self._ensure_slide_generator()

                async def _on_partial_slide(partial: GeneratedSlide) -> None:
                    await self._send_json(
                        await self._ensure_slide_control_ws(),
                        {
                            "action": "update_generated_slide_partial",
                            "pain_point": event.category,
                            "slide": slide_generator.partial_slide_payload(event.category, partial),
                        },
                    )

                generated = await slide_generator.generate(event.category, on_partial=_on_partial_slide)
                if generated is not None:
                    slide_control_msg = {
                        "action": "inject_generated_slide",
                        "pain_point": event.category,
                        "slide": slide_generator.slide_payload(event.category, generated),
                    }
                    await self._send_json(await self._ensure_slide_control_ws(), slide_control_msg)

        return SlideInjectionResult(
            pain_point_event=event,
            case=case,
            slide_control_msg=slide_control_msg,
            pain_point_msg=pain_point_msg,
        )

    async def handle_window_detection(
        self,
        category: str,
        confidence: float,
        evidence_quote: str,
        timestamp_ms: int,
    ) -> None:
        """Publish a pain point and inject a slide for a pre-classified window detection."""
        pain_point_msg: dict[str, object] = {
            "type": "pain_point",
            "category": category,
            "confidence": confidence,
            "trigger_phrase": evidence_quote,
            "timestamp_ms": timestamp_ms,
            "case_matched": False,
            "case_id": None,
        }
        case = await self._case_db.find_case(category, self._slides_config.prospect_industry)
        pain_point_msg["case_matched"] = case is not None
        pain_point_msg["case_id"] = case.id if case else None
        pain_point_msg["response_suggestion"] = resolve_response_suggestion(
            case.description if case else None, feature_policy=self._feature_policy
        )
        await self._send_json(await self._ensure_pain_points_ws(), pain_point_msg)

        if self._slides_enabled:
            if case is not None:
                await self._send_json(
                    await self._ensure_slide_control_ws(),
                    {"action": "navigate_to_case", "slide_id": case.id},
                )
            elif self._slides_config.dynamic_slides:
                slide_generator = self._ensure_slide_generator()

                async def _on_partial_slide(partial: GeneratedSlide) -> None:
                    await self._send_json(
                        await self._ensure_slide_control_ws(),
                        {
                            "action": "update_generated_slide_partial",
                            "pain_point": category,
                            "slide": slide_generator.partial_slide_payload(category, partial),
                        },
                    )

                generated = await slide_generator.generate(category, on_partial=_on_partial_slide)
                if generated is not None:
                    await self._send_json(
                        await self._ensure_slide_control_ws(),
                        {
                            "action": "inject_generated_slide",
                            "pain_point": category,
                            "slide": slide_generator.slide_payload(category, generated),
                        },
                    )

    async def close(self) -> None:
        await self._close_ws(self._pain_points_ws)
        await self._close_ws(self._slide_control_ws)
        self._pain_points_ws = None
        self._slide_control_ws = None

    def _channel_url(self, channel: str) -> str:
        return channel_ws_url(self._ws_config, channel)

    def _ensure_slide_generator(self) -> SlideGenerator:
        if self._slide_generator is None:
            self._slide_generator = SlideGenerator(llm_client=self._pipeline_llm_client)
        return self._slide_generator

    @staticmethod
    def _is_ws_open(ws: websockets.ClientConnection) -> bool:
        # websockets >=12 dropped the .closed bool attribute in favor of
        # ClientConnection.close_code (None while open). Support both.
        closed_attr = getattr(ws, "closed", None)
        if isinstance(closed_attr, bool):
            return not closed_attr
        return getattr(ws, "close_code", None) is None

    async def _ensure_pain_points_ws(self) -> websockets.ClientConnection:
        if self._pain_points_ws is None or not self._is_ws_open(self._pain_points_ws):
            self._pain_points_ws = await websockets.connect(self._channel_url("pain-points"))
        return self._pain_points_ws

    async def _ensure_slide_control_ws(self) -> websockets.ClientConnection:
        if self._slide_control_ws is None or not self._is_ws_open(self._slide_control_ws):
            self._slide_control_ws = await websockets.connect(self._channel_url("slide-control"))
        return self._slide_control_ws

    @staticmethod
    async def _close_ws(ws: websockets.ClientConnection | None) -> None:
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            pass

    @staticmethod
    async def _send_json(
        ws: websockets.ClientConnection,
        payload: dict[str, object],
    ) -> None:
        await ws.send(json.dumps(payload))
