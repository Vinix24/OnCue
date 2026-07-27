"""Shared evaluation harness utilities (model specs, pricing, availability, latency).

This module holds the common building blocks used by both the cascade-flow eval
harness (``cascade_eval.py``) and the summarization/action-item shootout
(``summarization_eval.py``). Keeping them in one place guarantees that model
matrixes, cost estimates and availability checks are reported consistently
across tracks.
"""

from __future__ import annotations

import logging
import os
import random
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from sales_copilot.core.config import load_yaml
from sales_copilot.core.paths import resolve_app_path

try:
    import yaml
except ImportError:  # pragma: no cover - yaml is always present in this project
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_PRICES_PATH = resolve_app_path("config/eval_model_prices.yaml")


# ---------------------------------------------------------------------------
# Model specs and pricing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    """A single LLM entry in the shootout matrix."""

    provider: str
    model: str
    description: str = ""

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class PricingTable:
    """Simple lookup table for cloud token prices."""

    entries: list[dict[str, Any]]

    @classmethod
    def from_path(cls, path: Path | str | None = None) -> PricingTable:
        if path is None:
            path = DEFAULT_PRICES_PATH
        p = Path(path)
        if not p.exists():
            logger.warning("Pricing table not found at %s; costs will be unknown.", p)
            return cls(entries=[])
        data = load_yaml(p)
        return cls(entries=list(data.get("prices", [])))

    def lookup(self, provider: str, model: str) -> dict[str, float] | None:
        provider = provider.strip().lower()
        model = model.strip().lower()
        # Exact match first.
        for entry in self.entries:
            if (
                entry.get("provider", "").strip().lower() == provider
                and entry.get("model", "").strip().lower() == model
            ):
                return {
                    "input_per_1m": float(entry["input_per_1m"]),
                    "output_per_1m": float(entry["output_per_1m"]),
                }
        # Provider wildcard fallback.
        for entry in self.entries:
            if (
                entry.get("provider", "").strip().lower() == provider
                and entry.get("model", "").strip() == "*"
            ):
                return {
                    "input_per_1m": float(entry["input_per_1m"]),
                    "output_per_1m": float(entry["output_per_1m"]),
                }
        return None

    def estimate_cost(
        self,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float | None:
        rates = self.lookup(provider, model)
        if rates is None:
            return None
        return (
            input_tokens * rates["input_per_1m"] / 1_000_000
            + output_tokens * rates["output_per_1m"] / 1_000_000
        )


# ---------------------------------------------------------------------------
# Provider availability helpers
# ---------------------------------------------------------------------------


def env_key_for_provider(provider: str) -> str | None:
    """Return the API-key env var name for a cloud provider, if any."""
    provider = provider.strip().lower()
    mapping = {
        "gemini": "GEMINI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "groq": "GROQ_API_KEY",
        "azure": "AZURE_OPENAI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    return mapping.get(provider)


def is_provider_available(provider: str) -> tuple[bool, str]:
    """Return (available, reason) for a configured LLM provider.

    Cloud providers only require a non-empty key to be considered available for
    the eval harness; the actual call may still fail at runtime. Ollama is
    probed with a lightweight health request.
    """
    provider = provider.strip().lower()
    if provider in ("", "none"):
        return True, "provider disabled (none)"

    env_key = env_key_for_provider(provider)
    if env_key:
        if not os.getenv(env_key, "").strip():
            return False, f"{env_key} is empty"
        return True, f"{env_key} present"

    if provider == "ollama":
        base_url = (
            os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/").removesuffix("/v1")
        )
        try:
            response = httpx.get(f"{base_url}/api/tags", timeout=5.0)
            response.raise_for_status()
            return True, f"Ollama reachable at {base_url}"
        except Exception as exc:  # noqa: BLE001
            return False, f"Ollama not reachable at {base_url}: {exc}"

    if provider == "vertex":
        # Vertex relies on application-default credentials / GOOGLE_APPLICATION_CREDENTIALS.
        if not os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
            return False, "GOOGLE_APPLICATION_CREDENTIALS not set"
        return True, "GOOGLE_APPLICATION_CREDENTIALS present"

    return False, f"unknown provider '{provider}'"


# ---------------------------------------------------------------------------
# Latency aggregation helpers
# ---------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    """Return the ``p``-th percentile of ``values`` using nearest-rank interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round(p * (len(ordered) - 1)))
    return ordered[idx]


def latency_stats(latencies: list[float]) -> dict[str, float]:
    """Return n/p50/p95/mean for a list of latencies in milliseconds."""
    if not latencies:
        return {"n": 0, "p50": 0.0, "p95": 0.0, "mean": 0.0}
    return {
        "n": len(latencies),
        "p50": statistics.median(latencies),
        "p95": percentile(latencies, 0.95),
        "mean": statistics.mean(latencies),
    }


# ---------------------------------------------------------------------------
# Model-spec parsing helpers
# ---------------------------------------------------------------------------


def parse_model_specs(raw: str | None) -> list[ModelSpec] | None:
    """Parse a comma-separated list of ``provider:model`` strings."""
    if not raw:
        return None
    specs: list[ModelSpec] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"Model specificatie moet 'provider:model' zijn: {part!r}")
        provider, model = part.split(":", 1)
        specs.append(ModelSpec(provider=provider.strip(), model=model.strip()))
    return specs


def load_model_register(path: Path) -> list[ModelSpec]:
    """Load a YAML model register (list of {provider, model, description})."""
    if yaml is None:
        raise RuntimeError("PyYAML is required to load a model register")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    specs: list[ModelSpec] = []
    for entry in data.get("models", []):
        specs.append(
            ModelSpec(
                provider=str(entry["provider"]),
                model=str(entry["model"]),
                description=str(entry.get("description", "")),
            )
        )
    return specs


# ---------------------------------------------------------------------------
# Eval-only resilience: retry-with-backoff + inter-call pacing
#
# EVAL-SCOPED ONLY. This is deliberately kept out of ``core/llm_client.py`` and
# ``modules/detector/llm_confirm.py`` -- both are on the live copilot's path (a
# provisional pain-point event is already shown to the user before the LLM
# confirmation lands; see ``pipeline.aprocess``/``_confirm_async``), which runs
# on a hard ~3s budget. A multi-second exponential backoff there would blow
# that budget. Eval callers wrap their own LLM calls with these helpers
# instead: ``cascade_eval.build_pipeline_for_spec`` wraps the ``LLMClient``
# instance it builds in place (the cascade track goes through
# ``LLMConfirmClient``, the live seam, so wrapping happens one layer up rather
# than inside it); ``summarization_eval.extract_action_items`` and
# ``scripts/eval_suggestion.py``'s ``generate_rebuttal`` call ``client.create``
# directly and wrap that call site inline.
# ---------------------------------------------------------------------------

DEFAULT_MAX_RETRIES = 4
DEFAULT_BASE_DELAY_S = 1.0
DEFAULT_PACE_MS = 150


def is_rate_limit_error(exc: BaseException) -> bool:
    """Detect transient rate-limit errors across the Google genai / OpenAI-compatible SDKs.

    Covers Google's ``429 RESOURCE_EXHAUSTED`` (gemini/vertex, via ``google.genai.errors.
    APIError.code``) and OpenAI/OpenRouter/Groq's ``429`` / ``rate_limit_exceeded`` (via
    ``openai.APIStatusError.status_code``). Different SDK versions surface the code
    differently, so this checks both a numeric status attribute and the stringified message.
    """
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(exc, "code", None)
    if status_code == 429:
        return True
    message = str(exc).lower()
    return (
        "429" in message
        or "resource_exhausted" in message
        or "rate_limit" in message
        or "rate limit" in message
    )


def retry_with_backoff(
    fn: Callable[[], Any],
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    is_retryable: Callable[[BaseException], bool] = is_rate_limit_error,
    sleep: Callable[[float], None] | None = None,
    rand: Callable[[], float] | None = None,
) -> Any:
    """Call ``fn`` with exponential backoff + jitter on retryable errors. EVAL-SCOPED ONLY.

    Delays double each attempt (``base_delay_s`` x 1, 2, 4, 8 for the default
    ``max_retries=4``) plus up to ``base_delay_s`` of jitter. A non-retryable error, or a
    retryable error still failing after the last retry, propagates unchanged -- so existing
    eval failure-recording (``None`` + error string) keeps working exactly as today, just
    after a few backed-off attempts instead of none.
    """
    sleep_fn = sleep if sleep is not None else time.sleep
    rand_fn = rand if rand is not None else random.random
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if attempt >= max_retries or not is_retryable(exc):
                raise
            delay = base_delay_s * (2**attempt) + rand_fn() * base_delay_s
            logger.warning(
                "Eval LLM call hit a retryable error (attempt %d/%d): %s -- backing off %.1fs",
                attempt + 1,
                max_retries,
                exc,
                delay,
            )
            sleep_fn(delay)
            attempt += 1


def pace(pace_ms: int, *, sleep: Callable[[float], None] | None = None) -> None:
    """Sleep ``pace_ms`` milliseconds before an eval LLM call, when positive. EVAL-SCOPED ONLY."""
    if pace_ms > 0:
        (sleep if sleep is not None else time.sleep)(pace_ms / 1000.0)


def wrap_llm_client_for_eval(
    llm_client: Any,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    pace_ms: int = DEFAULT_PACE_MS,
) -> None:
    """Wrap ``llm_client.create`` in place with eval-only pacing + retry-with-backoff.

    Mutates the ``create`` attribute of this one ``LLMClient`` instance only -- the class
    definition in ``core/llm_client.py``, and every other instance (including the ones the
    live copilot builds), are untouched. Used by the cascade track, where the eval harness
    calls into ``LLMConfirmClient`` (the live seam) instead of ``LLMClient.create`` directly;
    compare ``summarization_eval.extract_action_items`` / ``scripts/eval_suggestion.py``,
    which call ``client.create`` themselves and wrap that call site inline instead.
    """
    original_create = llm_client.create

    def _create(**kwargs: Any) -> Any:
        pace(pace_ms)
        return retry_with_backoff(
            lambda: original_create(**kwargs),
            max_retries=max_retries,
            base_delay_s=base_delay_s,
        )

    llm_client.create = _create


def resilience_header_lines(
    *,
    effective_timeout_ms: int,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_BASE_DELAY_S,
    pace_ms: int = DEFAULT_PACE_MS,
    thinking_output_tokens: int | None = None,
) -> list[str]:
    """Markdown lines reporting the eval-run resilience config, for report headers.

    So a reader can see the run was clean without re-deriving it from CLI flags: the
    effective timeout (after any ``--timeout-ms`` override), the retry/backoff schedule, the
    inter-call pace, and -- when summarization ran -- the output-token floor used for
    thinking-model calls.
    """
    delays = ", ".join(f"{base_delay_s * (2**i):.0f}s" for i in range(max_retries))
    lines = [
        f"**Effective LLM_TIMEOUT_MS:** {effective_timeout_ms} ms",
        f"**Retry/backoff:** tot {max_retries} retries op rate-limit-fouten "
        f"(429/RESOURCE_EXHAUSTED), backoff {delays} + jitter",
        f"**Pace tussen calls:** {pace_ms} ms",
    ]
    if thinking_output_tokens is not None:
        lines.append(f"**Thinking output-token budget (summarization):** {thinking_output_tokens} tokens")
    return lines
