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

    def __init__(self, config: DetectorConfig | None = None, language: str = "nl") -> None:
        self.config = config or DetectorConfig.from_env()
        self.language = (language or self.config.call_language).strip().lower()
        self.routes = _load_routes(self.config.pain_points_config, self.language)
        self._router: SemanticRouter | None = None
        self._router_lock = threading.Lock()
        self._route_utterances: dict[str, list[str]] = {
            route.name: [utterance.strip().lower() for utterance in route.utterances if utterance.strip()]
            for route in self.routes
        }

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

    def _keyword_match(self, text: str) -> RouteMatch | None:
        best_category: str | None = None
        best_score = 0
        text_tokens = self._tokens(text)
        for category, utterances in self._route_utterances.items():
            for utterance in utterances:
                if utterance in text or text in utterance:
                    return RouteMatch(category=category, confidence=1.0, tier="high", source="keyword")
                utterance_tokens = self._tokens(utterance)
                overlap = utterance_tokens & text_tokens
                if not overlap:
                    continue
                long_token_overlap = any(len(token) >= 7 for token in overlap)
                score = len(overlap) + (1 if long_token_overlap else 0)
                if score > best_score:
                    best_category = category
                    best_score = score
        if best_category is None:
            return None
        if best_score < 2:
            return None
        return RouteMatch(category=best_category, confidence=0.95, tier="high", source="keyword")

    def classify(self, text: str) -> RouteMatch | None:
        cleaned = text.strip()
        if not cleaned:
            return None

        keyword_match = self._keyword_match(cleaned.lower())
        if keyword_match is not None:
            return keyword_match

        choice = self._ensure_router()(cleaned)
        if isinstance(choice, list):
            choice = choice[0] if choice else None
        if choice is None or choice.name is None or choice.similarity_score is None:
            return None
        score = float(choice.similarity_score)
        if score < self.config.confidence_threshold_low:
            return None
        tier: Literal["high", "uncertain", "none"]
        if score >= self.config.confidence_threshold_high:
            tier = "high"
        else:
            tier = "uncertain"
        return RouteMatch(category=choice.name, confidence=score, tier=tier)

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
