"""llm-routering-per-taak D2+D3: the per-task resolver and the privacy ceiling above it.

Covers the plan's verplichte tests and the plan-gate acceptatiepunten:

- per task: provider, model and timeout, the fallback chain, and insight keeping its own
  INSIGHT_* variables and 60 s default (never LLM_TIMEOUT_MS);
- the matrix privacy-profile x task-provider: no combination lightens the tier, above the
  ceiling is an error with no client built and no call made, context docs sanitized per
  resolved provider;
- the ceiling applied at call time and per conversation (two conversations in a row);
- ``sanitize_for_outbound`` deciding on the resolved provider, not the global one;
- slides going through their own resolve instead of inheriting the detector's client;
- the PII modes ``off`` and ``cloud_only`` on the resolved provider;
- the start-call poort checking every active task (400 for a task above the ceiling or an
  unknown profile);
- the tree guard: no production ``LLMClient(`` outside the resolver, with an explicit
  allowlist for eval/tooling.
"""

from __future__ import annotations

import re
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from sales_copilot.core import context_docs
from sales_copilot.core import llm_client as llm_client_mod
from sales_copilot.core.config import (
    CallConfig,
    DetectorConfig,
    InsightConfig,
    SlidesConfig,
    WebSocketConfig,
    build_module_configs,
)
from sales_copilot.core.llm_routing import (
    TASK_DEFAULT_TIMEOUT_MS,
    TASKS,
    TaskModelMissingError,
    active_tasks,
    build_llm_client,
    resolve_llm,
    task_env_keys,
)
from sales_copilot.core.outbound_policy import sanitize_for_outbound, tier_of
from sales_copilot.core.privacy_gate import PrivacyGateError, privacy_allows, validate_privacy
from sales_copilot.modules.copilot import slide_generator as slide_generator_mod
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.copilot.slide_generator import SlideGenerator
from sales_copilot.modules.detector.llm_confirm import LLMConfirmClient
from sales_copilot.modules.detector.suggestions import SuggestionLLMClient
from sales_copilot.modules.detector.summary import SummaryLLMClient
from sales_copilot.websocket import hub_core

ROOT = Path(__file__).resolve().parents[1]

_LIVE_TASKS = tuple(task for task in TASKS if task not in {"summary", "insight", "report", "report_terms"})
_NON_INSIGHT_TASKS = tuple(task for task in TASKS if task != "insight")
_PROFILES = ("local", "tenant", "public")
_RANK = {"local": 0, "tenant": 1, "public": 2}

_EMAIL = "jan@example.com"
_IBAN = "NL91ABNA0417164300"
_PII_TEXT = f"Contact: {_EMAIL}, IBAN {_IBAN}"

# (label, provider, extra env) -> the tier tier_of() must classify it as.
_PROVIDER_SPECS: tuple[tuple[str, str, dict[str, str], str], ...] = (
    ("ollama-loopback", "ollama", {"OLLAMA_BASE_URL": "http://localhost:11434/v1"}, "local"),
    ("ollama-remote", "ollama", {"OLLAMA_BASE_URL": "http://10.0.0.5:11434/v1"}, "public"),
    ("azure-trusted", "azure", {"TRUST_OWN_TENANT": "true"}, "tenant"),
    ("vertex-trusted", "vertex", {"TRUST_OWN_TENANT": "true"}, "tenant"),
    ("vertex-untrusted", "vertex", {}, "public"),
    ("openrouter", "openrouter", {}, "public"),
)


@pytest.fixture(autouse=True)
def _clean_routing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts from the same environment: no per-task values, no ceiling flags."""
    for task in TASKS:
        for key in task_env_keys(task):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_TIMEOUT_MS",
        "TRUST_OWN_TENANT",
        "PII_REDACTION",
        "ALLOW_RAW_LLM_PII",
        "INSIGHT_ENABLED",
        "ENABLE_SUMMARY",
        "ENABLE_SUGGESTIONS",
        "ENABLE_SCRIPT_TRACKING",
        "AUTO_PHASE_DETECTION",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://tenant.openai.azure.com")


class _SdkRecorder:
    """Stands in for the SDK seam: records every client build and every call."""

    def __init__(self) -> None:
        self.built: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def build_client(self, provider: str, *, timeout_ms: int) -> object:
        self.built.append(provider)
        return object()

    def build_create(self, client: object, provider: str):
        def _create(**kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return kwargs["response_model"](answer="ok")

        return _create


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _SdkRecorder:
    recorder = _SdkRecorder()
    monkeypatch.setattr(llm_client_mod, "build_client", recorder.build_client)
    monkeypatch.setattr(llm_client_mod, "build_create", recorder.build_create)
    monkeypatch.setattr(
        llm_client_mod, "_ensure_ollama_num_ctx_model", lambda base_url, model, num_ctx: model
    )
    return recorder


class _Answer(BaseModel):
    answer: str


def _config(**kwargs: Any) -> DetectorConfig:
    base: dict[str, Any] = {"llm_provider": "openrouter", "llm_model": "global-model", "llm_timeout_ms": 7000}
    base.update(kwargs)
    return DetectorConfig(**base)


# ---------------------------------------------------------------------------
# Per task: provider, model, timeout and the fallback chain
# ---------------------------------------------------------------------------


def test_timeout_default_table_is_exactly_the_agreed_one() -> None:
    assert TASK_DEFAULT_TIMEOUT_MS == {
        "detector_confirm": None,
        "window_classifier": None,
        "phase": None,
        "suggestions": None,
        "summary": 30_000,
        "script_tracking": None,
        "slides": None,
        "insight": 60_000,
        "report": 300_000,
        "report_terms": 300_000,
    }
    assert set(TASK_DEFAULT_TIMEOUT_MS) == set(TASKS)


@pytest.mark.parametrize("task", _NON_INSIGHT_TASKS)
def test_task_env_keys_follow_taak_llm_naming(task: str) -> None:
    prefix = task.upper()
    assert task_env_keys(task) == (
        f"{prefix}_LLM_PROVIDER",
        f"{prefix}_LLM_MODEL",
        f"{prefix}_LLM_TIMEOUT_MS",
    )


def test_insight_keeps_its_existing_variable_names() -> None:
    assert task_env_keys("insight") == ("INSIGHT_PROVIDER", "INSIGHT_MODEL", "INSIGHT_LLM_TIMEOUT_MS")


@pytest.mark.parametrize("task", _NON_INSIGHT_TASKS)
def test_task_without_own_values_falls_back_to_the_global_ones(task: str) -> None:
    resolved = resolve_llm(task, _config(llm_timeout_ms=3000))

    assert resolved.provider == "openrouter"
    assert resolved.model == "global-model"
    expected_timeout = TASK_DEFAULT_TIMEOUT_MS[task] or 3000
    assert resolved.timeout_ms == expected_timeout


@pytest.mark.parametrize("task", _NON_INSIGHT_TASKS)
def test_task_own_values_win_over_global(task: str, monkeypatch: pytest.MonkeyPatch) -> None:
    provider_key, model_key, timeout_key = task_env_keys(task)
    monkeypatch.setenv(provider_key, "Azure")
    monkeypatch.setenv(model_key, f"{task}-model")
    monkeypatch.setenv(timeout_key, "12345")

    resolved = resolve_llm(task, _config())

    assert (resolved.task, resolved.provider, resolved.model, resolved.timeout_ms) == (
        task,
        "azure",
        f"{task}-model",
        12345,
    )


@pytest.mark.parametrize("task", _LIVE_TASKS)
def test_live_tasks_fall_back_to_llm_timeout_ms(task: str) -> None:
    assert resolve_llm(task, _config(llm_timeout_ms=3000)).timeout_ms == 3000


def test_summary_has_its_own_30s_default_not_the_live_timeout() -> None:
    """2026-09-27: the summary timed out after 3.00s under LLM_TIMEOUT_MS=3000."""
    assert resolve_llm("summary", _config(llm_timeout_ms=3000)).timeout_ms == 30_000


def test_summary_client_uses_the_resolved_timeout(sdk: _SdkRecorder) -> None:
    client = SummaryLLMClient(_config(llm_timeout_ms=3000))

    assert client.resolved.timeout_ms == 30_000
    assert client._llm.timeout_ms == 30_000


def test_blank_task_values_fall_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "  ")
    monkeypatch.setenv("SUMMARY_LLM_MODEL", "")
    monkeypatch.setenv("SUMMARY_LLM_TIMEOUT_MS", " ")

    resolved = resolve_llm("summary", _config())

    assert (resolved.provider, resolved.model, resolved.timeout_ms) == ("openrouter", "global-model", 30_000)


def test_non_integer_task_timeout_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHASE_LLM_TIMEOUT_MS", "soon")

    with pytest.raises(ValueError, match="PHASE_LLM_TIMEOUT_MS"):
        resolve_llm("phase", _config())


@pytest.mark.parametrize("task", _NON_INSIGHT_TASKS)
def test_task_provider_other_than_the_conversation_needs_its_own_model(
    task: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_key, model_key, _ = task_env_keys(task)
    monkeypatch.setenv(provider_key, "azure")

    with pytest.raises(TaskModelMissingError) as exc_info:
        resolve_llm(task, _config(llm_provider="vertex"))

    assert isinstance(exc_info.value, ValueError)
    assert provider_key in str(exc_info.value)
    assert model_key in str(exc_info.value)
    assert "LLM_PROVIDER=vertex" in str(exc_info.value)


def test_insight_provider_other_than_the_conversation_needs_its_own_model() -> None:
    insight = InsightConfig(llm_provider="azure")

    with pytest.raises(TaskModelMissingError) as exc_info:
        resolve_llm("insight", _config(llm_provider="vertex"), insight_config=insight)

    assert "INSIGHT_PROVIDER" in str(exc_info.value)
    assert "INSIGHT_MODEL" in str(exc_info.value)


@pytest.mark.parametrize("task", _NON_INSIGHT_TASKS)
def test_task_provider_equal_to_the_conversation_falls_back_to_its_model(
    task: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(task_env_keys(task)[0], "Vertex")

    resolved = resolve_llm(task, _config(llm_provider="vertex", llm_model="conversation-model"))

    assert (resolved.provider, resolved.model) == ("vertex", "conversation-model")


def test_insight_provider_equal_to_the_conversation_falls_back_to_its_model() -> None:
    resolved = resolve_llm(
        "insight",
        _config(llm_provider="vertex", llm_model="conversation-model"),
        insight_config=InsightConfig(llm_provider="vertex"),
    )

    assert (resolved.provider, resolved.model) == ("vertex", "conversation-model")


@pytest.mark.parametrize("task", TASKS)
def test_task_switched_off_with_none_needs_no_model(task: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(task_env_keys(task)[0], "none")

    resolved = resolve_llm(task, _config(llm_provider="vertex"), insight_config=InsightConfig.from_env())

    assert resolved.provider == "none"


def test_unknown_task_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown LLM task"):
        resolve_llm("smalltalk", _config())


def test_task_resolving_to_ollama_gets_the_ollama_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUGGESTIONS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SUGGESTIONS_LLM_MODEL", "local-model")

    assert resolve_llm("suggestions", _config(llm_timeout_ms=3000)).timeout_ms == 90_000


def test_insight_reads_insight_variables_and_keeps_60s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSIGHT_PROVIDER", "azure")
    monkeypatch.setenv("INSIGHT_MODEL", "deep-model")
    monkeypatch.setenv("LLM_TIMEOUT_MS", "3000")

    resolved = resolve_llm("insight", DetectorConfig.from_env(), insight_config=InsightConfig.from_env())

    assert (resolved.provider, resolved.model, resolved.timeout_ms) == ("azure", "deep-model", 60_000)


def test_insight_does_not_drop_to_the_live_timeout_when_unset() -> None:
    resolved = resolve_llm("insight", _config(llm_timeout_ms=3000), insight_config=InsightConfig())

    assert resolved.timeout_ms == 60_000
    assert resolved.provider == "openrouter"
    assert resolved.model == "global-model"


def test_insight_timeout_override_still_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INSIGHT_LLM_TIMEOUT_MS", "45000")

    resolved = resolve_llm("insight", _config(), insight_config=InsightConfig.from_env())

    assert resolved.timeout_ms == 45_000


def test_insight_ignores_insight_llm_prefixed_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tiebreaker: no rename to INSIGHT_LLM_PROVIDER/MODEL."""
    monkeypatch.setenv("INSIGHT_LLM_PROVIDER", "azure")
    monkeypatch.setenv("INSIGHT_LLM_MODEL", "renamed-model")

    resolved = resolve_llm("insight", _config(), insight_config=InsightConfig.from_env())

    assert (resolved.provider, resolved.model) == ("openrouter", "global-model")


def test_start_call_override_is_the_global_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The start-call ``llm.provider`` override is what a task without its own value gets."""
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    configs = build_module_configs(CallConfig(llm_provider="azure", llm_model="call-model"))

    resolved = resolve_llm("phase", configs["detector"])

    assert (resolved.provider, resolved.model) == ("azure", "call-model")


# ---------------------------------------------------------------------------
# Matrix: privacy profile x task provider
# ---------------------------------------------------------------------------


def _matrix() -> list[tuple[str, str, str, dict[str, str], str]]:
    return [
        (profile, label, provider, env, tier)
        for profile in _PROFILES
        for label, provider, env, tier in _PROVIDER_SPECS
    ]


@pytest.mark.parametrize("task", TASKS)
@pytest.mark.parametrize(("profile", "label", "provider", "env", "tier"), _matrix())
def test_no_profile_task_provider_combination_lightens_the_tier(
    task: str,
    profile: str,
    label: str,
    provider: str,
    env: dict[str, str],
    tier: str,
    sdk: _SdkRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    provider_key, model_key, _ = task_env_keys(task)
    monkeypatch.setenv(provider_key, provider)
    monkeypatch.setenv(model_key, "task-model")
    # The global provider is always the strictest (local) one: only the task deviates.
    config = _config(llm_provider="ollama", privacy=profile)
    insight = InsightConfig.from_env()

    assert tier_of(provider) == tier, label
    allowed = _RANK[tier] <= _RANK[profile]

    if not allowed:
        with pytest.raises(PrivacyGateError, match=task):
            resolve_llm(task, config, insight_config=insight)
        assert sdk.built == []
        assert sdk.calls == []
        return

    resolved = resolve_llm(task, config, insight_config=insight)
    client = build_llm_client(resolved)
    client.create(model=resolved.model, system_prompt="s", user_text="u", response_model=_Answer)

    assert resolved.provider == provider
    assert _RANK[tier_of(resolved.provider)] <= _RANK[profile]
    assert client.privacy == profile
    assert sdk.built == [provider]
    assert len(sdk.calls) == 1


_CONTEXT_DOC_SITES = (
    ("suggestions", lambda config, docs: SuggestionLLMClient(config, context_docs=docs)),
    ("detector_confirm", lambda config, docs: LLMConfirmClient(config, context_docs=docs)),
)


@pytest.mark.parametrize(("task", "build_site"), _CONTEXT_DOC_SITES)
@pytest.mark.parametrize(("profile", "label", "provider", "env", "tier"), _matrix())
def test_context_docs_are_sanitized_for_the_resolved_provider(
    task: str,
    build_site: Any,
    profile: str,
    label: str,
    provider: str,
    env: dict[str, str],
    tier: str,
    sdk: _SdkRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptatiepunt opus: context docs follow the task's resolved provider, inside the matrix."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    monkeypatch.setenv("LLM_PROVIDER", "ollama")  # the global provider is local
    monkeypatch.setenv(task_env_keys(task)[0], provider)
    monkeypatch.setenv(task_env_keys(task)[1], "task-model")
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    doc = tmp_path / "prep.txt"
    doc.write_text(_PII_TEXT, encoding="utf-8")
    config = _config(llm_provider="ollama", privacy=profile)

    if _RANK[tier] > _RANK[profile]:
        with pytest.raises(PrivacyGateError):
            build_site(config, [str(doc)])
        assert sdk.built == []
        return

    site = build_site(config, [str(doc)])

    assert site.provider == provider
    if tier == "public":
        assert _EMAIL not in site.system_prompt
        assert _IBAN not in site.system_prompt
    else:
        assert _EMAIL in site.system_prompt


# ---------------------------------------------------------------------------
# Ceiling at call time, per conversation
# ---------------------------------------------------------------------------


def test_ceiling_is_checked_again_at_call_time(sdk: _SdkRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clients live for a whole call; tier_of() reads TRUST_OWN_TENANT at call time."""
    monkeypatch.setenv("TRUST_OWN_TENANT", "true")
    resolved = resolve_llm("phase", _config(llm_provider="azure", privacy="tenant"))
    client = build_llm_client(resolved)
    monkeypatch.delenv("TRUST_OWN_TENANT")

    with pytest.raises(PrivacyGateError, match="phase"):
        client.create(model="m", system_prompt="s", user_text="u", response_model=_Answer)

    assert sdk.calls == []


@pytest.mark.asyncio
async def test_ceiling_is_checked_on_acreate_and_astream(sdk: _SdkRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUST_OWN_TENANT", "true")
    client = build_llm_client(resolve_llm("suggestions", _config(llm_provider="azure", privacy="tenant")))
    monkeypatch.delenv("TRUST_OWN_TENANT")

    with pytest.raises(PrivacyGateError):
        await client.acreate(model="m", system_prompt="s", user_text="u", response_model=_Answer)
    with pytest.raises(PrivacyGateError):
        async for _ in client.astream(model="m", system_prompt="s", user_text="u", response_model=_Answer):
            pass

    assert sdk.calls == []


def test_two_conversations_in_a_row_each_get_their_own_ceiling(
    sdk: _SdkRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("SUMMARY_LLM_MODEL", "summary-model")

    # Conversation 1: klant with privacy local -> the public summary provider is refused.
    first = build_module_configs(CallConfig(llm_provider="ollama", privacy="local", client_slug="acme"))
    with pytest.raises(PrivacyGateError, match="acme"):
        SummaryLLMClient(first["detector"])
    assert sdk.built == []

    # Conversation 2: no ceiling -> the same summary provider runs.
    second = build_module_configs(CallConfig(llm_provider="ollama"))
    summary = SummaryLLMClient(second["detector"])
    assert summary.resolved.privacy is None
    assert summary.provider == "openrouter"

    # Conversation 3: public ceiling -> allowed, and it carries that ceiling.
    third = build_module_configs(CallConfig(llm_provider="ollama", privacy="public"))
    assert SummaryLLMClient(third["detector"])._llm.privacy == "public"

    # Conversation 4: local again -> refused again; nothing leaked from 2 and 3.
    fourth = build_module_configs(CallConfig(llm_provider="ollama", privacy="local"))
    with pytest.raises(PrivacyGateError):
        SummaryLLMClient(fourth["detector"])
    assert sdk.built == ["openrouter", "openrouter"]


def test_call_config_privacy_reaches_the_detector_config() -> None:
    configs = build_module_configs(CallConfig(privacy="tenant", client_slug="acme"))

    assert configs["detector"].privacy == "tenant"
    assert configs["detector"].client_slug == "acme"


@pytest.mark.parametrize("bad", ["secret", "LOCAL-ish", 3])
def test_unknown_privacy_profile_is_refused(bad: object) -> None:
    with pytest.raises(PrivacyGateError):
        CallConfig(privacy=bad)  # type: ignore[arg-type]
    with pytest.raises(PrivacyGateError):
        DetectorConfig(privacy=bad)  # type: ignore[arg-type]
    with pytest.raises(PrivacyGateError):
        validate_privacy(bad)


def test_none_provider_is_off_and_never_crosses_a_ceiling() -> None:
    assert privacy_allows("local", "none") is True
    assert privacy_allows("local", "") is False


# ---------------------------------------------------------------------------
# PII decisions on the resolved provider
# ---------------------------------------------------------------------------


def test_sanitize_for_outbound_strips_for_task_openrouter_under_global_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    monkeypatch.setenv("SUGGESTIONS_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("SUGGESTIONS_LLM_MODEL", "suggestions-model")
    resolved = resolve_llm("suggestions", DetectorConfig.from_env())

    stripped = sanitize_for_outbound(_PII_TEXT, provider=resolved.provider, allow_local=True)
    # The pre-fix behaviour (decide on the global provider) would have passed it raw:
    global_decision = sanitize_for_outbound(_PII_TEXT, provider=None, allow_local=True)

    assert resolved.provider == "openrouter"
    assert _EMAIL not in stripped and _IBAN not in stripped
    assert _EMAIL in global_decision


def test_sanitize_for_outbound_requires_a_provider_argument() -> None:
    with pytest.raises(TypeError):
        sanitize_for_outbound(_PII_TEXT)  # type: ignore[call-arg]


def _user_text_sent(sdk: _SdkRecorder) -> str:
    return sdk.calls[-1]["messages"][1]["content"]


@pytest.mark.parametrize("mode", ["off", "cloud_only"])
def test_pii_modes_apply_to_the_resolved_provider(
    mode: str, sdk: _SdkRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PII_REDACTION", mode)
    monkeypatch.setenv("TRUST_OWN_TENANT", "true")
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("SUMMARY_LLM_MODEL", "summary-model")
    config = _config(llm_provider="azure")

    public = build_llm_client(resolve_llm("summary", config))
    tenant = build_llm_client(resolve_llm("phase", config))
    public.create(model="m", system_prompt="s", user_text=_PII_TEXT, response_model=_Answer)
    to_public = _user_text_sent(sdk)
    tenant.create(model="m", system_prompt="s", user_text=_PII_TEXT, response_model=_Answer)
    to_tenant = _user_text_sent(sdk)

    # A trusted tenant passes raw in both modes; nothing forces a strip.
    assert _EMAIL in to_tenant
    if mode == "off":
        assert _EMAIL in to_public
    else:
        assert _EMAIL not in to_public and _IBAN not in to_public


# ---------------------------------------------------------------------------
# Slides resolve their own route
# ---------------------------------------------------------------------------


def _confirm_client(config: DetectorConfig) -> LLMConfirmClient:
    return LLMConfirmClient(config)


def test_slides_go_through_their_own_resolve(sdk: _SdkRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real_resolve = slide_generator_mod.resolve_llm

    def _spy(task: str, *args: Any, **kwargs: Any):
        seen.append(task)
        return real_resolve(task, *args, **kwargs)

    monkeypatch.setattr(slide_generator_mod, "resolve_llm", _spy)
    confirm = _confirm_client(_config())

    SlideGenerator(llm_client=confirm)

    assert seen == ["slides"]


def test_slides_reuse_the_detector_client_only_on_the_same_route(sdk: _SdkRecorder) -> None:
    confirm = _confirm_client(_config())

    generator = SlideGenerator(llm_client=confirm)

    assert generator._llm is confirm._llm


def test_slides_with_own_provider_do_not_inherit_the_detector_client(
    sdk: _SdkRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLIDES_LLM_PROVIDER", "azure")
    monkeypatch.setenv("SLIDES_LLM_MODEL", "slides-model")
    confirm = _confirm_client(_config())

    generator = SlideGenerator(llm_client=confirm)

    assert generator._llm is not confirm._llm
    assert generator.provider == "azure"
    assert generator._llm.provider == "azure"
    assert generator._model == "slides-model"


def test_slides_above_the_ceiling_are_refused_even_with_a_compliant_detector(
    sdk: _SdkRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLIDES_LLM_PROVIDER", "openrouter")
    config = _config(llm_provider="ollama", privacy="local")
    confirm = _confirm_client(config)
    injector = SlideInjector(
        types.SimpleNamespace(llm_client=confirm, config=config),
        object(),  # type: ignore[arg-type]
        WebSocketConfig(),
        SlidesConfig(),
        feature_policy=types.SimpleNamespace(allows=lambda _feature: True),  # type: ignore[arg-type]
    )

    with pytest.raises(PrivacyGateError, match="slides"):
        injector._ensure_slide_generator()
    assert sdk.built == ["ollama"]


# ---------------------------------------------------------------------------
# Start-call poort: every active task
# ---------------------------------------------------------------------------


def _write_klant_yaml(root: Path, slug: str, content: str) -> None:
    client_dir = root / slug
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "klant.yaml").write_text(content, encoding="utf-8")


def _ollama_start_call() -> dict[str, Any]:
    return {"config": {"client_slug": "acme-corp", "llm": {"provider": "ollama", "model": "llama3"}}}


def test_start_call_refuses_a_task_provider_above_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "openrouter")
    _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

    with pytest.raises(ValueError) as exc_info:
        hub_core.extract_start_call_config(_ollama_start_call())

    assert "acme-corp" in str(exc_info.value)
    assert "summary" in str(exc_info.value)


def test_start_call_ignores_a_task_that_is_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("ENABLE_SUMMARY", "false")
    _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

    config = hub_core.extract_start_call_config(_ollama_start_call())

    assert config["privacy"] == "local"


def test_start_call_refuses_a_task_provider_without_its_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "openrouter")

    with pytest.raises(TaskModelMissingError) as exc_info:
        hub_core.extract_start_call_config(_ollama_start_call())

    assert "SUMMARY_LLM_PROVIDER" in str(exc_info.value)
    assert "SUMMARY_LLM_MODEL" in str(exc_info.value)


def test_start_call_accepts_a_task_provider_with_its_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("SUMMARY_LLM_MODEL", "summary-model")

    config = hub_core.extract_start_call_config(_ollama_start_call())

    assert config["llm"]["provider"] == "ollama"


def test_task_provider_without_model_is_a_400_before_capture(
    authed_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "openrouter")
    hub_core.reset_config_state()
    try:
        response = authed_client.post("/api/start-call", json=_ollama_start_call())
        assert response.status_code == 400
        assert "SUMMARY_LLM_MODEL" in response.json()["detail"]
        assert hub_core.status_state() == "waiting_for_config"
    finally:
        hub_core.reset_config_state()


def test_start_call_refuses_an_unknown_privacy_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)

    with pytest.raises(ValueError, match="privacy"):
        hub_core.extract_start_call_config({"config": {"privacy": "somewhat"}})


def test_start_call_enforces_a_caller_supplied_profile_without_klant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)

    with pytest.raises(ValueError, match="dit gesprek"):
        hub_core.extract_start_call_config({"config": {"privacy": "local", "llm": {"provider": "openrouter"}}})


def test_unknown_privacy_profile_is_a_400(authed_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    hub_core.reset_config_state()
    try:
        response = authed_client.post("/api/start-call", json={"config": {"privacy": "somewhat"}})
        assert response.status_code == 400
        assert hub_core.status_state() == "waiting_for_config"
    finally:
        hub_core.reset_config_state()


def test_active_tasks_follow_the_module_switches() -> None:
    config = _config(
        enable_suggestions=False, enable_summary=True, enable_script_tracking=False, auto_phase_detection=True
    )

    assert active_tasks(config, insight_active=False, report_active=False) == (
        "detector_confirm",
        "window_classifier",
        "phase",
        "summary",
        "slides",
    )
    assert active_tasks(config, insight_active=True, report_active=False)[-1] == "insight"
    assert active_tasks(config, insight_active=True, report_active=True)[-3:] == ("insight", "report", "report_terms")


# ---------------------------------------------------------------------------
# Tree guard: no production LLMClient( outside the resolver
# ---------------------------------------------------------------------------

#: The only place production code may construct an LLMClient.
_RESOLVER = "src/sales_copilot/core/llm_routing.py"

#: Eval and tooling that pick their provider per run on purpose and never carry a
#: conversation's privacy ceiling. Adding a path here is a reviewed decision.
_EVAL_TOOLING_ALLOWLIST = frozenset(
    {
        "src/sales_copilot/modules/detector/card_selection_eval.py",
        "src/sales_copilot/modules/detector/summarization_eval.py",
        "scripts/eval_suggestion.py",
        "scripts/label_and_summarize.py",
        "scripts/spark_benchmark.py",
    }
)

_CONSTRUCTOR = re.compile(r"(?<![A-Za-z0-9_])LLMClient\(")


def _unrouted_llm_clients(root: Path) -> list[str]:
    offenders: list[str] = []
    for base in ("src", "scripts"):
        for path in sorted((root / base).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel == _RESOLVER or rel in _EVAL_TOOLING_ALLOWLIST:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if _CONSTRUCTOR.search(line):
                    offenders.append(f"{rel}:{lineno}")
    return offenders


def test_no_llm_client_is_built_outside_the_resolver() -> None:
    offenders = _unrouted_llm_clients(ROOT)

    assert offenders == [], (
        "LLMClient( outside core/llm_routing.py; route it through resolve_llm_client() "
        f"so it gets a task, a timeout and the privacy ceiling: {offenders}"
    )


def test_allowlist_entries_exist_and_still_need_it() -> None:
    for rel in sorted(_EVAL_TOOLING_ALLOWLIST | {_RESOLVER}):
        path = ROOT / rel
        assert path.is_file(), f"stale allowlist entry: {rel}"
        assert _CONSTRUCTOR.search(path.read_text(encoding="utf-8")), f"{rel} no longer builds an LLMClient"


def test_tree_guard_catches_a_new_place(tmp_path: Path) -> None:
    module = tmp_path / "src" / "sales_copilot" / "modules" / "new_feature.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from sales_copilot.core.llm_client import LLMClient\n"
        "client = LLMClient('openrouter', timeout_ms=1)\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()
    subclass_site = tmp_path / "scripts" / "fine.py"
    subclass_site.write_text("x = SummaryLLMClient(config)\n", encoding="utf-8")

    assert _unrouted_llm_clients(tmp_path) == ["src/sales_copilot/modules/new_feature.py:2"]


# ---------------------------------------------------------------------------
# Start-call health check covers every distinct task provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_covers_each_distinct_task_provider_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from sales_copilot import __main__ as app_main

    checked: list[tuple[str, str]] = []

    async def _record(provider: str, module: str, ws_config: WebSocketConfig) -> None:
        checked.append((provider, module))

    monkeypatch.setattr(app_main, "_health_check_one_provider", _record)
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "azure")
    monkeypatch.setenv("SUMMARY_LLM_MODEL", "summary-model")
    monkeypatch.setenv("INSIGHT_PROVIDER", "vertex")

    await app_main._health_check_providers(
        CallConfig(),
        _config(),
        InsightConfig(enabled=True, llm_provider="vertex", llm_model="insight-model"),
        WebSocketConfig(),
    )

    assert checked == [("openrouter", "detector"), ("azure", "detector"), ("vertex", "insight")]


@pytest.mark.asyncio
async def test_health_check_publishes_a_refused_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from sales_copilot import __main__ as app_main

    warnings: list[tuple[str, str]] = []

    async def _warn(module: str, warning: str, ws_config: WebSocketConfig) -> None:
        warnings.append((module, warning))

    async def _noop(provider: str, module: str, ws_config: WebSocketConfig) -> None:
        return None

    monkeypatch.setattr(app_main, "_publish_module_warning", _warn)
    monkeypatch.setattr(app_main, "_health_check_one_provider", _noop)
    monkeypatch.setenv("SUMMARY_LLM_PROVIDER", "openrouter")

    await app_main._health_check_providers(
        CallConfig(), _config(llm_provider="ollama", privacy="local"), InsightConfig(), WebSocketConfig()
    )

    assert [module for module, _ in warnings] == ["detector"]
    assert "summary" in warnings[0][1]
