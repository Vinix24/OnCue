"""Per-provider thinking-policy request kwargs for the LLM client seam.

Providers expose "thinking" (extended reasoning emitted before the final
structured answer) through incompatible knobs:

- **gemini / vertex** (google-genai): a ``types.ThinkingConfig(thinking_budget=...)``
  passed as a top-level ``thinking_config`` kwarg to ``generate_content``.
  ``thinking_budget=0`` disables thinking; a positive integer caps it.
- **openrouter** (Qwen and Qwen-like models served over vLLM/SGLang): thinking
  is toggled via ``extra_body={"chat_template_kwargs": {"enable_thinking": ...}}``
  and capped via OpenRouter's unified ``extra_body={"reasoning": {"max_tokens": ...}}``.
  Qwen3.6-35B-A3B has thinking ON by default, so leaving this unset is itself a
  policy choice, not a neutral one.
- **ollama** (local models): there is no request-level thinking kwarg. A larger
  reasoning trace instead needs a larger context window, so the budget maps
  onto the existing ``num_ctx`` derived-model seam in ``llm_client.py``.
- **openai / azure** (GPT-4o family): not thinking models at all. The helper
  is a structural no-op for them.

This module is the single place that translates a provider-agnostic
``(thinking_on, reasoning_budget)`` policy into the concrete per-provider
request shape, so the fase-B thinking-sweep (``scripts/run_fase_b.py
--profiles``) can compare models under an explicit, controlled thinking
setting instead of each provider's undocumented default.

This module stays provider-SDK-free by architectural rule (see
``scripts/check_architecture_boundaries.py``'s ``provider-sdk-confinement``
check): every provider SDK import must live in ``core/llm_client.py``. For
gemini/vertex this returns a plain ``thinking_budget`` int under that key
instead of a ``google.genai.types.ThinkingConfig`` instance; ``LLMClient.create()``
is the one place that wraps it into the real SDK type.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Providers with no "thinking" concept at all -- gpt-4o/gpt-4o-mini emit a
# single-pass completion with no reasoning-trace knob, so sweeping a thinking
# policy over them is structurally meaningless (see ``thinking_applicable``).
_NON_THINKING_PROVIDERS = frozenset({"openai", "azure"})

# google-genai's ThinkingConfig is shared by gemini (public API key) and
# vertex (BYO-tenant ADC) -- both route through instructor's from_genai
# adapter, which accepts the same top-level ``thinking_config`` kwarg.
_GENAI_PROVIDERS = frozenset({"gemini", "vertex"})


@dataclass(frozen=True)
class ThinkingPolicy:
    """One point in the thinking-mode x reasoning-budget sweep matrix."""

    name: str
    thinking_on: bool
    reasoning_budget: int | None = None


# Named profiles swept by ``scripts/run_fase_b.py --profiles``.
THINKING_PROFILES: dict[str, ThinkingPolicy] = {
    "no-think": ThinkingPolicy(name="no-think", thinking_on=False, reasoning_budget=None),
    "think-512": ThinkingPolicy(name="think-512", thinking_on=True, reasoning_budget=512),
    "think-2048": ThinkingPolicy(name="think-2048", thinking_on=True, reasoning_budget=2048),
    "think-8192": ThinkingPolicy(name="think-8192", thinking_on=True, reasoning_budget=8192),
}


def thinking_applicable(provider: str) -> bool:
    """Whether ``provider`` has a thinking concept worth sweeping at all.

    ``False`` for openai/azure (GPT-4o family): callers should report these
    once, marked "thinking n/a", rather than running the same no-op call
    once per profile.
    """
    return provider.strip().lower() not in _NON_THINKING_PROVIDERS


def thinking_request_kwargs(
    provider: str,
    *,
    thinking_on: bool,
    reasoning_budget: int | None = None,
) -> dict[str, Any]:
    """Return the extra ``LLMClient.create()`` kwargs that implement a thinking policy.

    For gemini/vertex this returns a plain ``{"thinking_budget": <int>}`` -- NOT a
    ``google.genai.types.ThinkingConfig`` instance, since this module must stay
    provider-SDK-free (see the module docstring). ``LLMClient.create()`` is the one
    place, inside ``core/llm_client.py``, that wraps the int into the real SDK type.

    Ollama has no request-level thinking kwarg -- its budget maps onto the
    ``num_ctx`` derived-model seam in ``llm_client.py`` instead, so this
    returns ``{}`` for it; ``LLMClient.create()`` reads ``reasoning_budget``
    directly off the ``ThinkingPolicy`` for that mapping. Same for openai/azure
    (not thinking models) and any unrecognized provider.
    """
    provider = provider.strip().lower()

    if provider in _GENAI_PROVIDERS:
        # Off always means budget 0 (thinking disabled), regardless of any
        # budget value a caller passed alongside thinking_on=False.
        budget = reasoning_budget if thinking_on else 0
        return {"thinking_budget": budget}

    if provider == "openrouter":
        extra_body: dict[str, Any] = {"chat_template_kwargs": {"enable_thinking": thinking_on}}
        if thinking_on and reasoning_budget is not None:
            extra_body["reasoning"] = {"max_tokens": reasoning_budget}
        return {"extra_body": extra_body}

    # ollama (budget maps onto num_ctx, not a request kwarg), openai/azure
    # (not thinking models), groq and any unrecognized provider: no-op.
    return {}
