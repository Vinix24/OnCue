from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.llm_client import LLMClient
from sales_copilot.core.llm_routing import ResolvedLLM, build_llm_client, resolve_llm

logger = logging.getLogger(__name__)

SLIDE_GENERATION_TIMEOUT_SECONDS = 5.0
SLIDE_PROMPT_TEMPLATE = (
    "Generate a case study slide for pain point '{category}'. "
    "Include title, description, and 1-2 metrics."
)


class GeneratedSlide(BaseModel):
    title: str
    description: str
    metrics: list[str]


def _same_route(client: LLMClient, resolved: ResolvedLLM) -> bool:
    return (
        client.provider == resolved.provider
        and client.timeout_ms == resolved.timeout_ms
        and client.privacy == resolved.privacy
    )


class SlideGenerator:
    def __init__(
        self,
        config: DetectorConfig | None = None,
        *,
        llm_client: object | None = None,
    ) -> None:
        inherited_config = getattr(llm_client, "config", None)
        if config is None and isinstance(inherited_config, DetectorConfig):
            config = inherited_config
        self.config = config or DetectorConfig.from_env()

        # Slides resolve their OWN route (SLIDES_LLM_* plus the conversation's privacy
        # ceiling) instead of silently inheriting whatever the detector's confirm client
        # happens to call.
        self.resolved = resolve_llm("slides", self.config)
        self.provider = self.resolved.provider
        self._model = self.resolved.model

        # Reuse the sibling site's already-built LLMClient (the pipeline's LLMConfirmClient)
        # only when it is exactly the route slides resolved to -- same provider, timeout and
        # ceiling -- so we do not rebuild the provider client and instructor patch for nothing.
        inherited = getattr(llm_client, "_llm", None)
        if isinstance(inherited, LLMClient) and _same_route(inherited, self.resolved):
            self._llm = inherited
        else:
            self._llm = build_llm_client(self.resolved, prompt_cache=self.config.llm_prompt_cache)

    async def generate(
        self,
        category: str,
        *,
        on_partial: Callable[[GeneratedSlide], Awaitable[None]] | None = None,
    ) -> GeneratedSlide | None:
        category_value = category.strip()
        if not category_value:
            return None

        # Assembled raw; the seam applies the outbound PII policy to the full user_text once.
        prompt = SLIDE_PROMPT_TEMPLATE.format(category=category_value)
        system_prompt = "You are a B2B sales case-study writer."

        if not self.config.llm_streaming:
            try:
                return await asyncio.wait_for(
                    self._llm.acreate(
                        model=self._model,
                        system_prompt=system_prompt,
                        user_text=prompt,
                        response_model=GeneratedSlide,
                        temperature=self.config.llm_temperature,
                        allow_local=True,
                        max_tokens=self.resolved.output_limit(),
                    ),
                    timeout=SLIDE_GENERATION_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.warning(
                    "Dynamic slide generation timed out after %.2fs for pain point '%s'.",
                    SLIDE_GENERATION_TIMEOUT_SECONDS,
                    category_value,
                )
                return None
            except Exception:
                logger.warning(
                    "Dynamic slide generation failed for pain point '%s'.",
                    category_value,
                    exc_info=True,
                )
                return None

        async def _drain() -> GeneratedSlide | None:
            last: GeneratedSlide | None = None
            async for partial in self._llm.astream(
                model=self._model,
                system_prompt=system_prompt,
                user_text=prompt,
                response_model=GeneratedSlide,
                temperature=self.config.llm_temperature,
                allow_local=True,
                max_tokens=self.resolved.output_limit(),
            ):
                last = partial
                if on_partial is not None:
                    await on_partial(partial)
            return last

        try:
            return await asyncio.wait_for(_drain(), timeout=SLIDE_GENERATION_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.warning(
                "Dynamic slide generation stream timed out after %.2fs for pain point '%s'.",
                SLIDE_GENERATION_TIMEOUT_SECONDS,
                category_value,
            )
            return None
        except Exception:
            logger.warning(
                "Dynamic slide generation stream failed for pain point '%s'.",
                category_value,
                exc_info=True,
            )
            return None

    def slide_payload(self, category: str, generated: GeneratedSlide) -> dict[str, object]:
        """Return bounded structured slide content for browser-side rendering."""

        return {
            "title": (generated.title.strip() or f"Case: {category}")[:160],
            "description": generated.description.strip()[:1_000],
            "metrics": [metric[:200] for metric in self._normalize_metrics(generated.metrics)],
        }

    def partial_slide_payload(self, category: str, partial: GeneratedSlide) -> dict[str, object]:
        """Best-effort payload for a still-streaming ``GeneratedSlide``.

        Unlike the fully-formed object ``slide_payload`` expects, a partial-streamed instance
        has every field optional (``None``/absent until that part of the JSON has arrived), so
        every field is guarded here instead of assumed present.
        """
        title = (partial.title or "").strip()
        description = (partial.description or "").strip()
        metrics = [metric for metric in (partial.metrics or []) if isinstance(metric, str)]
        return {
            "title": (title or f"Case: {category}")[:160],
            "description": description[:1_000],
            "metrics": [metric[:200] for metric in self._normalize_metrics(metrics)],
        }

    @staticmethod
    def _normalize_metrics(metrics: list[str]) -> list[str]:
        normalized: list[str] = []
        for metric in metrics:
            cleaned = metric.strip()
            if not cleaned or cleaned in normalized:
                continue
            normalized.append(cleaned)
            if len(normalized) == 2:
                break
        return normalized
