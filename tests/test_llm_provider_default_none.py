"""Privacy by default in code: without any LLM key in the environment the provider is ``none``.

``.env.example`` already shipped ``LLM_PROVIDER=none``; these tests pin the same default for
a checkout without a ``.env``: no provider, no client, nothing outbound. An explicit
``LLM_PROVIDER=openrouter`` keeps working as before.
"""

from __future__ import annotations

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.llm_routing import TASKS, resolve_llm, resolve_llm_client
from sales_copilot.core.outbound_policy import outbound_is_local, tier_of

_PROVIDER_KEYS = ("LLM_PROVIDER", "INSIGHT_PROVIDER", "REPORT_TERMS_LLM_PROVIDER") + tuple(
    f"{task.upper()}_LLM_PROVIDER" for task in TASKS
)


@pytest.fixture
def empty_llm_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for key in _PROVIDER_KEYS + ("LLM_MODEL",):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_detector_config_defaults_to_none(empty_llm_env: pytest.MonkeyPatch) -> None:
    assert DetectorConfig().llm_provider == "none"
    assert DetectorConfig.from_env().llm_provider == "none"


def test_empty_provider_value_still_means_none(empty_llm_env: pytest.MonkeyPatch) -> None:
    empty_llm_env.setenv("LLM_PROVIDER", "")
    assert DetectorConfig.from_env().llm_provider == "none"


def test_outbound_policy_classifies_unset_env_as_not_public(empty_llm_env: pytest.MonkeyPatch) -> None:
    assert tier_of("none") == "local"
    assert outbound_is_local() is False
    assert tier_of(DetectorConfig.from_env().llm_provider) != "public"


@pytest.mark.parametrize("task", [t for t in TASKS if t != "insight"])
def test_no_task_resolves_a_provider_or_builds_a_client(empty_llm_env: pytest.MonkeyPatch, task: str) -> None:
    resolved, client = resolve_llm_client(task, DetectorConfig.from_env())
    assert resolved.provider == "none"
    assert client._client is None


def test_openrouter_in_env_works_as_before(empty_llm_env: pytest.MonkeyPatch) -> None:
    empty_llm_env.setenv("LLM_PROVIDER", "openrouter")
    empty_llm_env.setenv("OPENROUTER_API_KEY", "test-key")
    config = DetectorConfig.from_env()
    assert config.llm_provider == "openrouter"
    resolved, client = resolve_llm_client("detector_confirm", config)
    assert resolved.provider == "openrouter"
    assert client._client is not None
    assert tier_of("openrouter") == "public"
    assert resolve_llm("summary", config).provider == "openrouter"


def test_none_provider_passes_every_privacy_ceiling(empty_llm_env: pytest.MonkeyPatch) -> None:
    config = DetectorConfig.from_env()
    for privacy in ("local", "tenant", "public"):
        resolved = resolve_llm("summary", DetectorConfig(llm_provider=config.llm_provider, privacy=privacy))
        assert resolved.provider == "none"
