from __future__ import annotations

import logging
import time
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from sales_copilot.core.config import DetectorConfig, load_yaml
from sales_copilot.core.llm_client import LLMClient
from sales_copilot.core.preset import Preset, load_preset
from sales_copilot.modules.detector.sliding_window import TranscriptChunk

logger = logging.getLogger(__name__)

DetectionCategory = Literal[
    "pain_point",
    "objection",
    "buying_signal",
    "doubt",
    "none",
]

PHASE_FOCUS = {
    "discovery": (
        "Tijdens discovery let je vooral op pijnpunten en open vragen "
        "die de prospect stelt over hun situatie."
    ),
    "pitch": (
        "Tijdens pitch let je vooral op bezwaren ('te duur', 'geen tijd', 'past niet'), "
        "twijfel-signalen, en koopsignalen."
    ),
    "closing": (
        "Tijdens closing let je vooral op koopsignalen ('hoe snel', 'wat is de volgende stap'), "
        "last-minute autoriteit-bezwaren, en betreurde toezeggingen."
    ),
}

_SYSTEM_PROMPT_TEMPLATE = """\
Je bent een sales-analysesysteem dat fragmenten uit verkoopgesprekken analyseert.

Detecteer: pijnpunten (pain_point), bezwaren (objection), koopsignalen (buying_signal), \
en twijfelsignalen (doubt). Geef alleen detections terug als er een duidelijk herkenbaar signaal is.

Geldige subcategorieen voor pain_point: {pain_categories}
Geldige subcategorieen voor objection: {objection_categories}
Subcategorieen voor buying_signal: {buying_categories}
Subcategorieen voor doubt: {doubt_categories}

NIET als detection markeren:
- "Ja precies" — bevestiging, geen pijnpunt of bezwaar
- "Even snel" — taalgebruik, geen tijd-bezwaar
- "Ik heb nu daar ook" — incomplete zin, niet classificeren
- "Oh la la" — emotionele uiting, geen detectie
- "Ja, ja, ja" — backchanneling, geen detectie
- "Interessant" — algemene reactie, geen koopsignaal
- "Ik begrijp het" — begrip, geen actie
- "Oké" / "Okay" — akkoord, geen detectie
- "Laten we kijken" — vaag, geen koopsignaal
- "We gaan het bespreken" — intern overleg zonder urgentie, niet automatisch autoriteit-bezwaar
- "Goed idee" — instemming zonder concrete volgende stap
- "We denken erover na" — zonder urgentie, geen koopsignaal

Bij twijfel: detections=[].
"""


class WindowDetection(BaseModel):
    category: DetectionCategory
    subcategory: str | None = Field(
        default=None,
        description=(
            "Voor pain_point: bijv. offerteproces, kosten, capaciteit. "
            "Voor objection: timing, prijs, fit, autoriteit, anders. "
            "Voor buying_signal: vraag-naar-prijs, vraag-naar-implementatie, etc. "
            "Leeg voor category=none."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = Field(description="Verbatim quote uit het window die de detectie triggerde")
    reasoning: str = Field(description="Eenregelige uitleg in het Nederlands")


class WindowAnalysis(BaseModel):
    detections: list[WindowDetection]
    """Nul of meer detections. Lege lijst als er geen actionabele signalen zijn."""


class WindowClassifierHooks(Protocol):
    """Opt-in instrumentation callbacks for ``WindowClassifier.classify()``.

    Mirrors ``DetectionHooks`` in ``pipeline.py``. Implementations must not raise:
    ``classify()`` catches and logs any hook exception so a faulty harness cannot
    break live detection.
    """

    def on_classify_start(self) -> None: ...
    def on_classify_end(
        self,
        analysis: WindowAnalysis,
        latency_ms: float,
        ttft_ms: float | None,
    ) -> None: ...


class _NoOpWindowClassifierHooks:
    def on_classify_start(self) -> None: pass
    def on_classify_end(
        self,
        analysis: WindowAnalysis,
        latency_ms: float,
        ttft_ms: float | None,
    ) -> None: pass


class WindowClassifier:
    def __init__(
        self,
        config: DetectorConfig,
        provider: str,
        *,
        pain_categories: list[str] | None = None,
        objection_categories: list[str] | None = None,
        preset: Preset | None = None,
        hooks: WindowClassifierHooks | None = None,
    ) -> None:
        self.config = config
        self.provider = provider.lower()
        self._hooks = hooks or _NoOpWindowClassifierHooks()
        if self.provider in {"none", ""}:
            self._llm = None
            self.system_prompt = ""
            return
        self.preset = preset if preset is not None else load_preset(config.preset_name)
        self._pain_categories = (
            pain_categories
            if pain_categories is not None
            else self._extract_route_names(self.preset.pain_points)
        )
        self._objection_categories = (
            objection_categories
            if objection_categories is not None
            else self._extract_route_names(self.preset.objections)
        )
        self._buying_categories = self._extract_category_names(self.preset.buying_signals)
        self._doubt_categories = self._extract_category_names(self.preset.doubts)
        self._llm = LLMClient(self.provider, timeout_ms=config.llm_timeout_ms)
        self.system_prompt = self._build_system_prompt()

    async def classify(
        self,
        window_text: str,
        latest_chunk: TranscriptChunk | None,
        phase: str = "discovery",
        *,
        use_streaming: bool = False,
    ) -> WindowAnalysis:
        """Classify the sliding window's context text.

        ``use_streaming`` is opt-in and defaults to ``False``, so every existing
        (production) call site is unaffected: it keeps using the non-streaming
        ``LLMClient.acreate()`` call it always has. When a caller (e.g. the
        e2e-latency harness) passes ``use_streaming=True``, the call instead
        drains ``LLMClient.astream()`` to completion -- same final result, but
        ``LLMClient.last_ttft_ms`` is populated afterward and forwarded to
        ``on_classify_end`` as ``ttft_ms``.
        """
        if self._llm is None:
            logger.debug("WindowClassifier skipped: LLM provider is none")
            return WindowAnalysis(detections=[])
        if not window_text.strip():
            logger.debug("WindowClassifier skipped empty window_text")
            return WindowAnalysis(detections=[])
        logger.debug(
            "WindowClassifier classify start: provider=%s model=%s phase=%s latest_chunk=%r window_text=%r",
            self.provider,
            self.config.llm_model,
            phase,
            (latest_chunk.text[:200] if latest_chunk is not None else None),
            window_text[:1200],
        )
        self._safe_hook("on_classify_start")
        classify_start = time.perf_counter()
        analysis = await self._call_model_sync(window_text, phase, use_streaming=use_streaming)
        latency_ms = (time.perf_counter() - classify_start) * 1000
        ttft_ms = self._llm.last_ttft_ms if use_streaming else None
        self._safe_hook("on_classify_end", analysis, latency_ms, ttft_ms)
        logger.debug(
            "WindowClassifier classify result: detections=%s",
            [
                {
                    "category": det.category,
                    "subcategory": det.subcategory,
                    "confidence": det.confidence,
                    "evidence_quote": det.evidence_quote[:160],
                    "reasoning": det.reasoning[:160],
                }
                for det in analysis.detections
            ],
        )
        return analysis

    def _safe_hook(self, name: str, *args: object) -> None:
        """Invoke a hook by name and swallow any exception to protect live detection."""
        try:
            getattr(self._hooks, name)(*args)
        except Exception:  # noqa: BLE001
            logger.exception("WindowClassifier hook %r raised; ignoring", name)

    def _build_user_prompt(self, window_text: str, phase: str) -> str:
        # Preset-level redaction stays as the first DPIA layer (recruitment preset); the seam
        # then applies the outbound PII policy to the assembled user_text before it leaves.
        redacted_text, _ = self.preset.redact(window_text)
        phase_focus = PHASE_FOCUS.get(phase, PHASE_FOCUS["discovery"])
        return (
            f"Huidige fase: {phase}. {phase_focus}\n\n"
            f"Laatste prospect-uitspraken samen:\n"
            f'"{redacted_text}"\n\n'
            "Detecteer alle pijnpunten, bezwaren, koopsignalen en twijfel. "
            "Geef per detection categorie, subcategorie, confidence (0-1), "
            "evidence_quote en korte uitleg. "
            "Als niets relevant: detections=[]."
        )

    async def _call_model_sync(self, window_text: str, phase: str, *, use_streaming: bool = False) -> WindowAnalysis:
        user_prompt = self._build_user_prompt(window_text, phase)
        try:
            if use_streaming:
                last: WindowAnalysis | None = None
                async for partial in self._llm.astream(
                    model=self.config.llm_model,
                    system_prompt=self.system_prompt,
                    user_text=user_prompt,
                    response_model=WindowAnalysis,
                    temperature=self.config.llm_temperature,
                    allow_local=True,
                ):
                    last = partial
                return last if last is not None else WindowAnalysis(detections=[])
            return await self._llm.acreate(
                model=self.config.llm_model,
                system_prompt=self.system_prompt,
                user_text=user_prompt,
                response_model=WindowAnalysis,
                temperature=self.config.llm_temperature,
                allow_local=True,
            )
        except TimeoutError:
            logger.warning(
                "WindowClassifier classify timed out after %.1fs: provider=%s model=%s",
                self._llm.live_timeout_s,
                self.provider,
                self.config.llm_model,
            )
            return WindowAnalysis(detections=[])
        except Exception as exc:
            logger.error(
                "WindowClassifier LLM call failed: provider=%s model=%s exc_type=%s msg=%s window_preview=%r",
                self.provider,
                self.config.llm_model,
                type(exc).__name__,
                str(exc)[:300],
                window_text[:200],
            )
            return WindowAnalysis(detections=[])

    def _build_system_prompt(self) -> str:
        pain_cats = ", ".join(self._pain_categories) if self._pain_categories else "(geen)"
        obj_cats = ", ".join(self._objection_categories) if self._objection_categories else "(geen)"
        buying_cats = (
            ", ".join(self._buying_categories)
            if self._buying_categories
            else "vraag-naar-prijs, vraag-naar-implementatie, timeline-vraag, referentie-vraag, vergelijking-vraag"
        )
        doubt_cats = (
            ", ".join(self._doubt_categories) if self._doubt_categories else "sceptisch, twijfel, bedenktijd"
        )
        prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            pain_categories=pain_cats,
            objection_categories=obj_cats,
            buying_categories=buying_cats,
            doubt_categories=doubt_cats,
        )
        addendum = (self.preset.system_prompt_addendum or "").strip()
        if addendum:
            prompt += f"\n\n{addendum}"
        return prompt

    @staticmethod
    def _extract_route_names(routes_dict: dict) -> list[str]:
        routes = (routes_dict or {}).get("routes", [])
        return [r["name"] for r in routes if isinstance(r, dict) and isinstance(r.get("name"), str)]

    @staticmethod
    def _extract_category_names(section: dict) -> list[str]:
        """Extract category names from either a 'routes' list or a 'subcategories' list."""
        if not section:
            return []
        routes = section.get("routes", [])
        if routes:
            return [r["name"] for r in routes if isinstance(r, dict) and isinstance(r.get("name"), str)]
        return [s for s in section.get("subcategories", []) if isinstance(s, str)]

    @staticmethod
    def _load_names(path: str) -> list[str]:
        try:
            data = load_yaml(path)
        except Exception:
            return []
        routes = data.get("routes", [])
        return [r["name"] for r in routes if isinstance(r, dict) and isinstance(r.get("name"), str)]
