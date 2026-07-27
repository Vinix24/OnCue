"""Tests for the config-language routing surface of lang_router.

Covers coaching-prompt selection (backed by the i18n catalog) and
transcription-vocabulary config selection per configured LANGUAGE, plus the
end-to-end wiring into SuggestionLLMClient. Uses the real committed catalogs;
the loader-level fallback mechanics are unit-tested in test_i18n.py.
"""

from __future__ import annotations

import pytest

from sales_copilot.core import i18n, lang_router
from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.suggestions import (
    SUGGESTIONS_EMPTY_STATE,
    SuggestionLLMClient,
)


@pytest.fixture(autouse=True)
def _reset_i18n() -> None:
    i18n.clear_cache()
    yield
    i18n.set_catalog_dir(None)
    i18n.clear_cache()


# --- coaching prompts -------------------------------------------------------


def test_lang_router_coaching_prompts_nl() -> None:
    prompts = lang_router.route_coaching_prompts("nl")

    assert prompts.language == "nl"
    assert "salescoach" in prompts.system_prompt
    assert prompts.empty_state == "Wacht op gesprekscontext..."
    assert prompts.context_docs_label == "Contextdocumenten:"
    assert "{transcript}" in prompts.user_prompt


def test_lang_router_coaching_prompts_en() -> None:
    prompts = lang_router.route_coaching_prompts("en")

    assert prompts.language == "en"
    assert "sales coach" in prompts.system_prompt
    assert prompts.empty_state == "Waiting for conversation context..."
    assert prompts.context_docs_label == "Context documents:"
    assert "in English" in prompts.user_prompt


def test_lang_router_coaching_prompts_uses_configured_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(i18n.LANGUAGE_ENV_VAR, "en")
    prompts = lang_router.route_coaching_prompts()

    assert prompts.language == "en"
    assert prompts.empty_state == "Waiting for conversation context..."


def test_lang_router_coaching_prompts_fallback_for_unknown_language() -> None:
    # No catalog for "fr" -> every string falls back to the nl default.
    prompts = lang_router.route_coaching_prompts("fr")

    assert prompts.language == "fr"
    assert prompts.empty_state == "Wacht op gesprekscontext..."
    assert "salescoach" in prompts.system_prompt


# --- transcription vocabulary ----------------------------------------------


def test_lang_router_vocabulary_config_nl_is_base_file() -> None:
    assert lang_router.route_vocabulary_config("nl") == "config/transcription_vocabulary.yaml"


def test_lang_router_vocabulary_config_falls_back_when_language_file_absent() -> None:
    # No config/transcription_vocabulary_en.yaml ships -> base file is used.
    assert lang_router.route_vocabulary_config("en") == "config/transcription_vocabulary.yaml"


def test_lang_router_vocabulary_config_uses_configured_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(i18n.LANGUAGE_ENV_VAR, "nl")
    assert lang_router.route_vocabulary_config() == "config/transcription_vocabulary.yaml"


def test_lang_router_vocabulary_config_picks_language_file_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakePath:
        def exists(self) -> bool:
            return True

    monkeypatch.setattr(lang_router, "resolve_app_path", lambda _relative: _FakePath())
    assert lang_router.route_vocabulary_config("de") == "config/transcription_vocabulary_de.yaml"


# --- config integration -----------------------------------------------------


def test_transcriber_config_default_vocabulary_follows_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sales_copilot.core.config import TranscriberConfig

    monkeypatch.delenv("WHISPER_VOCABULARY_CONFIG", raising=False)
    monkeypatch.delenv(i18n.LANGUAGE_ENV_VAR, raising=False)

    config = TranscriberConfig.from_env()

    assert config.vocabulary_config == "config/transcription_vocabulary.yaml"


# --- suggestion client wiring ----------------------------------------------


def test_suggestion_client_prompts_follow_language_nl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    client = SuggestionLLMClient(DetectorConfig(llm_provider="openrouter", llm_model="m"), language="nl")

    prompt = client._user_prompt(["We lopen vast in onboarding."])

    assert "Return only actionable questions in Dutch." in prompt
    assert "- We lopen vast in onboarding." in prompt
    assert "salescoach" in client.system_prompt


def test_suggestion_client_prompts_follow_language_en(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    client = SuggestionLLMClient(DetectorConfig(llm_provider="openrouter", llm_model="m"), language="en")

    prompt = client._user_prompt(["We are stuck in onboarding."])

    assert "Return only actionable questions in English." in prompt
    assert "- We are stuck in onboarding." in prompt
    assert "sales coach" in client.system_prompt


def test_suggestions_empty_state_constant_is_a_catalog_string() -> None:
    assert isinstance(SUGGESTIONS_EMPTY_STATE, str)
    assert SUGGESTIONS_EMPTY_STATE
