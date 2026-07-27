"""Unit tests for the per-provider thinking-policy helper.

Covers the profile registry, ``thinking_applicable`` (which providers are worth
sweeping at all), and ``thinking_request_kwargs`` (the exact per-provider request
shape) for every provider named in the fase-B thinking-sweep spec: gemini/vertex
(google-genai ThinkingConfig), openrouter (chat_template_kwargs + reasoning cap),
openai/azure (no-op, not thinking models), and ollama (no-op at the request-kwarg
level -- its budget maps onto num_ctx in llm_client.py instead).
"""

from __future__ import annotations

import pytest

from sales_copilot.core.thinking_policy import (
    THINKING_PROFILES,
    ThinkingPolicy,
    thinking_applicable,
    thinking_request_kwargs,
)

# ---------------------------------------------------------------------------
# Profile registry
# ---------------------------------------------------------------------------


def test_profile_registry_has_the_four_named_profiles() -> None:
    assert set(THINKING_PROFILES) == {"no-think", "think-512", "think-2048", "think-8192"}


def test_no_think_profile_is_thinking_off() -> None:
    profile = THINKING_PROFILES["no-think"]
    assert profile.thinking_on is False
    assert profile.reasoning_budget is None


@pytest.mark.parametrize(
    "name,budget",
    [("think-512", 512), ("think-2048", 2048), ("think-8192", 8192)],
)
def test_think_profiles_are_thinking_on_with_the_named_budget(name: str, budget: int) -> None:
    profile = THINKING_PROFILES[name]
    assert profile.thinking_on is True
    assert profile.reasoning_budget == budget


# ---------------------------------------------------------------------------
# thinking_applicable — which providers are worth sweeping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["gemini", "vertex", "openrouter", "ollama", "groq"])
def test_thinking_applicable_true_for_thinking_capable_providers(provider: str) -> None:
    assert thinking_applicable(provider) is True


@pytest.mark.parametrize("provider", ["openai", "azure"])
def test_thinking_applicable_false_for_gpt4o_family(provider: str) -> None:
    """GPT-4o/GPT-4o-mini are not thinking models -- sweep them once, not per profile."""
    assert thinking_applicable(provider) is False


def test_thinking_applicable_is_case_insensitive() -> None:
    assert thinking_applicable("OpenAI") is False
    assert thinking_applicable("Gemini") is True


# ---------------------------------------------------------------------------
# thinking_request_kwargs — gemini / vertex
#
# Returns a plain ``thinking_budget`` int, NOT a google.genai.types.ThinkingConfig
# instance: this module must stay provider-SDK-free (provider-sdk-confinement
# architecture rule). LLMClient.create() in core/llm_client.py is the one place
# that wraps the int into the real SDK type -- covered by tests/test_llm_client.py.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
def test_genai_no_think_sets_budget_zero(provider: str) -> None:
    kwargs = thinking_request_kwargs(provider, thinking_on=False, reasoning_budget=None)

    assert kwargs == {"thinking_budget": 0}


@pytest.mark.parametrize("provider", ["gemini", "vertex"])
@pytest.mark.parametrize("budget", [512, 2048, 8192])
def test_genai_think_sets_the_requested_budget(provider: str, budget: int) -> None:
    kwargs = thinking_request_kwargs(provider, thinking_on=True, reasoning_budget=budget)

    assert kwargs == {"thinking_budget": budget}


def test_genai_off_ignores_a_stray_budget() -> None:
    """thinking_on=False always means budget 0, even if a caller passed a budget anyway."""
    kwargs = thinking_request_kwargs("gemini", thinking_on=False, reasoning_budget=2048)

    assert kwargs == {"thinking_budget": 0}


# ---------------------------------------------------------------------------
# thinking_request_kwargs — openrouter (chat_template_kwargs + reasoning cap)
# ---------------------------------------------------------------------------


def test_openrouter_no_think_disables_via_chat_template_kwargs() -> None:
    kwargs = thinking_request_kwargs("openrouter", thinking_on=False, reasoning_budget=None)

    assert kwargs == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def test_openrouter_think_without_budget_enables_thinking_only() -> None:
    kwargs = thinking_request_kwargs("openrouter", thinking_on=True, reasoning_budget=None)

    assert kwargs == {"extra_body": {"chat_template_kwargs": {"enable_thinking": True}}}
    assert "reasoning" not in kwargs["extra_body"]


def test_openrouter_think_with_budget_adds_reasoning_cap() -> None:
    kwargs = thinking_request_kwargs("openrouter", thinking_on=True, reasoning_budget=512)

    assert kwargs == {
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning": {"max_tokens": 512},
        }
    }


# ---------------------------------------------------------------------------
# thinking_request_kwargs — openai/azure (no-op, not thinking models)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "azure"])
@pytest.mark.parametrize("thinking_on,budget", [(False, None), (True, 2048)])
def test_openai_family_is_always_a_noop(provider: str, thinking_on: bool, budget: int | None) -> None:
    kwargs = thinking_request_kwargs(provider, thinking_on=thinking_on, reasoning_budget=budget)

    assert kwargs == {}


# ---------------------------------------------------------------------------
# thinking_request_kwargs — ollama (no request kwarg; budget maps onto num_ctx)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("thinking_on,budget", [(False, None), (True, 8192)])
def test_ollama_is_a_noop_at_the_request_kwarg_level(thinking_on: bool, budget: int | None) -> None:
    kwargs = thinking_request_kwargs("ollama", thinking_on=thinking_on, reasoning_budget=budget)

    assert kwargs == {}


# ---------------------------------------------------------------------------
# All four named profiles, end to end, per provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", list(THINKING_PROFILES))
def test_every_named_profile_resolves_to_kwargs_for_every_provider(profile_name: str) -> None:
    profile: ThinkingPolicy = THINKING_PROFILES[profile_name]
    for provider in ("gemini", "vertex", "openrouter", "openai", "azure", "ollama", "groq"):
        # Must not raise for any (provider, profile) combination in the sweep matrix.
        thinking_request_kwargs(provider, thinking_on=profile.thinking_on, reasoning_budget=profile.reasoning_budget)
