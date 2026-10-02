"""LLM confirmation client for uncertain pain point matches."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from pydantic import BaseModel

from sales_copilot.core.config import DetectorConfig, load_yaml
from sales_copilot.core.context_docs import load_context_documents
from sales_copilot.core.llm_routing import resolve_llm_client
from sales_copilot.core.thinking_policy import ThinkingPolicy

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Je bent een sales-analysesysteem. Je classificeert fragmenten uit "
    "verkoopgesprekken in pain point categorieen."
)


class PainPointDetection(BaseModel):
    category: str
    confidence: float
    trigger_phrase: str


class LLMConfirmClient:
    def __init__(
        self,
        config: DetectorConfig | None = None,
        *,
        context_docs: list[str] | None = None,
        thinking: ThinkingPolicy | None = None,
    ) -> None:
        self.config = config or DetectorConfig.from_env()
        self.resolved, self._llm = resolve_llm_client(
            "detector_confirm", self.config, prompt_cache=self.config.llm_prompt_cache
        )
        self.provider = self.resolved.provider
        self.model = self.resolved.model
        self.categories = self._load_categories(self.config.pain_points_config)
        # Sanitized for the provider this task actually calls, not the global one.
        self._context_block = self._load_context_docs(context_docs or [], provider=self.provider)
        self.system_prompt = self._build_system_prompt()
        self._thinking = thinking

    @property
    def last_usage(self) -> dict[str, int] | None:
        """Token usage from the most recent LLM confirmation call."""
        return self._llm.last_usage if self._llm is not None else None

    def confirm(self, fragment: str) -> PainPointDetection | None:
        if not fragment.strip():
            return None
        timeout_s = self.resolved.timeout_ms / 1000
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._call_model, fragment)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout:
            return None
        except Exception as exc:
            logger.error(
                "LLMConfirmClient.confirm failed: provider=%s model=%s exc_type=%s msg=%s fragment_preview=%r",
                self.provider,
                self.model,
                type(exc).__name__,
                str(exc)[:300],
                fragment[:200],
            )
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    async def confirm_async(self, fragment: str) -> PainPointDetection | None:
        """Async variant used on the live path; does not block the event loop."""
        if not fragment.strip():
            return None
        if self._llm is None or self._llm._create is None:
            return None
        try:
            return await self._llm.acreate(
                model=self.model,
                system_prompt=self.system_prompt,
                user_text=self._user_prompt(fragment),
                response_model=PainPointDetection,
                temperature=self.config.llm_temperature,
                allow_local=True,
                thinking=self._thinking,
                max_tokens=self.resolved.output_limit(self._thinking),
            )
        except TimeoutError:
            return None
        except Exception as exc:
            logger.error(
                "LLMConfirmClient.confirm_async failed: provider=%s model=%s exc_type=%s msg=%s fragment_preview=%r",
                self.provider,
                self.model,
                type(exc).__name__,
                str(exc)[:300],
                fragment[:200],
            )
            return None

    def _call_model(self, fragment: str) -> PainPointDetection:
        return self._llm.create(
            model=self.model,
            system_prompt=self.system_prompt,
            user_text=self._user_prompt(fragment),
            response_model=PainPointDetection,
            temperature=self.config.llm_temperature,
            allow_local=True,
            thinking=self._thinking,
            max_tokens=self.resolved.output_limit(self._thinking),
        )

    def _user_prompt(self, fragment: str) -> str:
        # Assembled raw; the seam applies the outbound PII policy to the full user_text once.
        categories = ", ".join(self.categories) if self.categories else ""
        return (
            "Classificeer het fragment in een pain point categorie. "
            "Gebruik alleen de opgegeven categorieen. "
            "Geef confidence tussen 0 en 1 en de exacte trigger_phrase uit het fragment.\n\n"
            f"Categorieen: {categories}\n"
            f"Fragment: {fragment}"
        )

    def _build_system_prompt(self) -> str:
        if not self._context_block:
            return SYSTEM_PROMPT
        return f"{SYSTEM_PROMPT}\n\nContextdocumenten:\n{self._context_block}"

    @staticmethod
    def _load_categories(path: str) -> list[str]:
        data = load_yaml(path)
        routes = data.get("routes", [])
        categories: list[str] = []
        for route in routes:
            if isinstance(route, dict) and isinstance(route.get("name"), str):
                categories.append(route["name"])
        return categories

    @staticmethod
    def _load_context_docs(paths: list[str], *, provider: str | None = None) -> str:
        return load_context_documents(paths, provider=provider)
