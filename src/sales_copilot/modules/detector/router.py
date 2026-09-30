from __future__ import annotations

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from semantic_router import Route, SemanticRouter
from semantic_router.encoders import HuggingFaceEncoder

from sales_copilot.core.config import DetectorConfig, load_yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteMatch:
    category: str
    confidence: float
    tier: Literal["high", "uncertain", "none"]
    source: Literal["keyword", "embedding"] = "embedding"


def _resolve_localized_config_path(config_path: str, language: str = "nl") -> str:
    language_code = (language or "nl").strip().lower()
    if language_code == "nl":
        return config_path

    base_path = Path(config_path)
    localized_path = base_path.with_name(f"{base_path.stem}_{language_code}{base_path.suffix}")
    if localized_path.exists():
        return str(localized_path)

    logger.warning(
        "Language config '%s' not found for %s. Falling back to Dutch routes.",
        localized_path,
        language_code,
    )
    return config_path


def routes_from_mapping(data: dict, *, source: str = "objections mapping") -> list[Route]:
    """Build semantic-router Routes from a ``{"routes": [{name, utterances, label?}]}`` mapping.

    Shared by the YAML file loader and by preset-driven construction, so the fast
    objection/pain-point router can read routes straight from a vakgebied-preset
    instead of only from the global config file.
    """
    raw_routes = data.get("routes", [])
    if not isinstance(raw_routes, list):
        raise ValueError(f"{source} must contain a 'routes' list")

    routes: list[Route] = []
    for entry in raw_routes:
        if not isinstance(entry, dict):
            raise ValueError("route entries must be mappings")
        name = entry.get("name")
        utterances = entry.get("utterances")
        if not name or not isinstance(name, str):
            raise ValueError("route entry missing name")
        if not utterances or not isinstance(utterances, list):
            raise ValueError(f"route '{name}' missing utterances")
        label = entry.get("label")
        metadata = {"label": label} if isinstance(label, str) else {}
        routes.append(Route(name=name, utterances=utterances, metadata=metadata))

    return routes


def _load_routes(config_path: str, language: str = "nl") -> list[Route]:
    resolved_path = _resolve_localized_config_path(config_path, language)
    return routes_from_mapping(load_yaml(resolved_path), source=resolved_path)


def _normalize_model_name(embedding_model: str) -> str:
    if "/" in embedding_model:
        return embedding_model
    return f"sentence-transformers/{embedding_model}"


def _build_router(routes: list[Route], embedding_model: str) -> SemanticRouter:
    encoder = HuggingFaceEncoder(name=_normalize_model_name(embedding_model))
    router = SemanticRouter(encoder=encoder, routes=[], aggregation="max")
    router.add(routes)
    return router


class PainPointRouter:
    # Hard bound on the offloaded classify() call (embedding model load +
    # inference). Subclasses (ObjectionRouter) inherit it.
    _CLASSIFY_TIMEOUT_S: float = 30.0

    # A single shared token is coincidence, not evidence that the utterance
    # was actually said -- the keyword fast path requires at least this many
    # independent overlapping content words before it will produce a match.
    # See _keyword_match: measured live over-match (2026-09-05, "een maand of
    # vijf, zes" -> rapportage at a hardcoded 0.95) traced to a single 7+ char
    # token clearing the old score>=2 bar on its own.
    _MIN_KEYWORD_OVERLAP: int = 2

    def __init__(
        self,
        config: DetectorConfig | None = None,
        language: str = "nl",
        *,
        include_negatives: bool | None = None,
    ) -> None:
        self.config = config or DetectorConfig.from_env()
        self.language = (language or self.config.call_language).strip().lower()
        self.routes = _load_routes(self.config.pain_points_config, self.language)
        self._configure_negative_class(include_negatives)
        self._router: SemanticRouter | None = None
        self._router_lock = threading.Lock()
        self._route_utterances: dict[str, list[str]] = {
            route.name: [utterance.strip().lower() for utterance in route.utterances if utterance.strip()]
            for route in self.routes
        }

    def _configure_negative_class(self, include_negatives: bool | None) -> None:
        """Load the shared negative/"none" class into ``self.routes``.

        Structural precision fix (mirrors ObjectionRouter's INCLUDE_NEGATIVES):
        generic non-topic prospect speech (rambling, questions, confirmations,
        numbers/time-spans) competes directly against the real routes for the
        best match instead of being forced into the closest category. A match
        that resolves to this class is filtered out in ``classify()``. Shared
        by PainPointRouter and ObjectionRouter so both get the same fix.
        """
        self._include_negatives = (
            include_negatives if include_negatives is not None else self.config.include_negatives
        )
        self._negative_categories: frozenset[str] = frozenset()
        if not self._include_negatives:
            return
        try:
            negative_routes = _load_routes(self.config.negatives_config, self.language)
        except FileNotFoundError:
            logger.warning(
                "Negatives config '%s' not found — continuing without negative class",
                self.config.negatives_config,
            )
            negative_routes = []
        self._negative_categories = frozenset(route.name for route in negative_routes)
        self.routes = self.routes + negative_routes

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) >= 4}

    def _ensure_router(self) -> SemanticRouter:
        if self._router is not None:
            return self._router
        with self._router_lock:
            if self._router is None:
                self._router = _build_router(self.routes, self.config.embedding_model)
        return self._router

    def _tier_for_score(self, score: float) -> Literal["high", "uncertain"]:
        return "high" if score >= self.config.confidence_threshold_high else "uncertain"

    def _keyword_match(self, text: str) -> RouteMatch | None:
        text_tokens = self._tokens(text)
        if not text_tokens:
            return None

        best_category: str | None = None
        best_confidence = 0.0
        for category, utterances in self._route_utterances.items():
            for utterance in utterances:
                if utterance in text:
                    # The full reference phrase was said verbatim -- an exact
                    # quote of *known* pain-point content is the strongest
                    # possible evidence, regardless of how long the utterance
                    # or the surrounding transcript is.
                    confidence = 1.0
                else:
                    # Deliberately NOT also treating `text in utterance` as an
                    # exact quote: that direction means the live transcript
                    # (often a single short/generic word or fragment) merely
                    # happens to appear somewhere inside a longer route
                    # utterance -- containment of a fragment says nothing
                    # about what the fragment itself carries as evidence. It
                    # falls through to the same token-overlap evidence as any
                    # other candidate below.
                    utterance_tokens = self._tokens(utterance)
                    if not utterance_tokens:
                        continue
                    overlap = utterance_tokens & text_tokens
                    if len(overlap) < self._MIN_KEYWORD_OVERLAP:
                        continue
                    # Confidence reflects how much of the utterance's content
                    # is actually present, scaled into the same [0, 1] space
                    # embedding scores live in so classify() can threshold
                    # both paths identically instead of hardcoding a constant.
                    coverage = len(overlap) / len(utterance_tokens)
                    confidence = min(0.97, 0.55 + 0.40 * coverage)
                if confidence > best_confidence:
                    best_category, best_confidence = category, confidence

        if best_category is None or best_confidence < self.config.confidence_threshold_low:
            logger.debug(
                "Keyword match below threshold: best_route=%s score=%.3f threshold=%.3f",
                best_category,
                best_confidence,
                self.config.confidence_threshold_low,
            )
            return None
        return RouteMatch(
            category=best_category,
            confidence=best_confidence,
            tier=self._tier_for_score(best_confidence),
            source="keyword",
        )

    def classify(self, text: str) -> RouteMatch | None:
        cleaned = text.strip()
        if not cleaned:
            return None

        # The keyword path only short-circuits the embedding layer on "high"
        # tier evidence -- confidence at or above the same
        # confidence_threshold_high the embedding layer itself is judged
        # against, which measured near-perfect precision on this fixture
        # (43/44). "uncertain" tier keyword evidence (partial word overlap)
        # measured far worse (31/54, ~57%) and is not worth trusting on its
        # own: it is discarded here and the embedding layer decides instead,
        # exactly as it would if the keyword path had found nothing at all.
        keyword_match = self._keyword_match(cleaned.lower())
        match = keyword_match if keyword_match is not None and keyword_match.tier == "high" else None
        if match is None:
            choice = self._ensure_router()(cleaned)
            if isinstance(choice, list):
                choice = choice[0] if choice else None
            if choice is None or choice.name is None or choice.similarity_score is None:
                logger.debug("Embedding router returned no route for this chunk")
                return None
            score = float(choice.similarity_score)
            if score < self.config.confidence_threshold_low:
                logger.debug(
                    "Embedding match below threshold: best_route=%s score=%.3f threshold=%.3f",
                    choice.name,
                    score,
                    self.config.confidence_threshold_low,
                )
                return None
            match = RouteMatch(category=choice.name, confidence=score, tier=self._tier_for_score(score))

        if match.category in self._negative_categories:
            logger.debug(
                "Match discarded as negative class: category=%s score=%.3f",
                match.category,
                match.confidence,
            )
            return None
        return match

    async def classify_async(self, text: str) -> RouteMatch | None:
        """Event-loop-safe entrypoint for ``classify``.

        ``classify`` is synchronous and blocking: the first call lazily builds the
        embedding model via ``_ensure_router`` (which can trigger a model download)
        and every call runs embedding inference. Calling it directly on the
        detector event loop would freeze all detection during a slow load. Offload
        it to a worker thread and bound it so a stalled load/inference degrades to
        "no match" instead of wedging the loop.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.classify, text),
                timeout=self._CLASSIFY_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warning(
                "semantic-router classify timed out after %.1fs "
                "(embedding model still loading?) — skipping classification",
                self._CLASSIFY_TIMEOUT_S,
            )
            return None
