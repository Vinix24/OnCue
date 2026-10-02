"""The term correction as its own resolver task, ``report_terms``, next to ``report``.

Every part of the post-call report can run on its own model. ``report_terms`` inherits what it
does not set from ``report`` and only then from the conversation, so the default stays exactly
one call. Covers:

- the fallback chain ``REPORT_TERMS_*`` -> ``REPORT_*`` -> the conversation (provider, model,
  timeout, output limit) and ``TaskModelMissingError`` when a level's provider would get a
  model that belongs to another provider;
- the start-call poort and the start-call health check knowing ``report_terms``;
- ``enrich_report``: the same model gives one call with both schema parts, a different model
  two calls (terms only with candidates), side by side; a refusal by the ceiling, an error, a
  timeout or a failing review in one part never costs the other part.

The provider SDK behind ``LLMClient`` is a local fake; the seam, the resolver, the PII policy
and the validation stay real.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sales_copilot import __main__ as app_main
from sales_copilot.core import context_docs
from sales_copilot.core import llm_client as llm_client_mod
from sales_copilot.core.config import CallConfig, DetectorConfig, InsightConfig, WebSocketConfig
from sales_copilot.core.llm_routing import (
    TASKS,
    TaskModelMissingError,
    active_tasks,
    resolve_llm,
    task_env_keys,
    task_max_output_tokens_key,
)
from sales_copilot.core.privacy_gate import PrivacyGateError
from sales_copilot.modules.reports import enrichment as enrichment_mod
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.enrichment import ReportEnrichment, ReportTermCorrections, enrich_report
from sales_copilot.modules.reports.term_corrections import TERM_CORRECTION_PROMPT, TermCorrection
from sales_copilot.websocket import hub_core

TERMS = ("ROI", "Roy", "Teamleader", "Sentrix")

SEGMENTS = (
    ("self", "We plannen alles in Team Leader."),
    ("prospect", "Het draait bij Zentrix sinds maart."),
    ("prospect", "Prima, tot dan."),
)

_SUMMARY = "Planning besproken."


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for task in TASKS:
        for key in (*task_env_keys(task), task_max_output_tokens_key(task)):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_TIMEOUT_MS",
        "TRUST_OWN_TENANT",
        "ALLOW_RAW_LLM_PII",
        "INSIGHT_ENABLED",
        "ENABLE_SUMMARY",
        "ENABLE_SUGGESTIONS",
        "ENABLE_SCRIPT_TRACKING",
        "AUTO_PHASE_DETECTION",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PII_REDACTION", "off")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def _config(**kwargs: Any) -> DetectorConfig:
    base: dict[str, Any] = {"llm_provider": "openrouter", "llm_model": "global-model", "llm_timeout_ms": 7000}
    base.update(kwargs)
    return DetectorConfig(**base)


def _correction() -> TermCorrection:
    return TermCorrection(segment_index=0, source="Team Leader", occurrence=1, target="Teamleader", reason="pakket")


class _Sdk:
    """Answers per response model, the way the report call or the term call would."""

    def __init__(self) -> None:
        self.built: list[tuple[str, int]] = []
        self.calls: list[dict[str, Any]] = []
        self.on_report: Callable[[dict[str, Any]], Any] = lambda kwargs: ReportEnrichment(
            gesprek_gevoerd=True,
            short_summary=_SUMMARY,
            overview="Het ging over de planning.",
            keywords=["planning"],
            action_items=[],
            term_corrections=[_correction()],
        )
        self.on_terms: Callable[[dict[str, Any]], Any] = lambda kwargs: ReportTermCorrections(
            term_corrections=[_correction()]
        )

    def build_client(self, provider: str, *, timeout_ms: int) -> object:
        self.built.append((provider, timeout_ms))
        return object()

    def build_create(self, client: object, provider: str):
        def _create(**kwargs: Any) -> Any:
            self.calls.append(kwargs)
            if kwargs["response_model"] is ReportTermCorrections:
                return self.on_terms(kwargs)
            return self.on_report(kwargs)

        return _create

    def call_for(self, response_model: type) -> dict[str, Any]:
        [call] = [call for call in self.calls if call["response_model"] is response_model]
        return call


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _Sdk:
    fake = _Sdk()
    monkeypatch.setattr(llm_client_mod, "build_client", fake.build_client)
    monkeypatch.setattr(llm_client_mod, "build_create", fake.build_create)
    monkeypatch.setattr(llm_client_mod, "_ensure_ollama_num_ctx_model", lambda base_url, model, num_ctx: model)
    return fake


def _session(segments: tuple[tuple[str, str], ...] = SEGMENTS) -> generator.SessionData:
    return generator.SessionData(
        session_id="session-terms",
        call_start_ms=0,
        call_end_ms=60000,
        prospect_name=None,
        prospect_company="Acme BV",
        context_docs=[],
        speech_events=[],
        phase_events=[],
        pain_points=[],
        monologues=[],
        transcript=[
            generator.TranscriptSegment(speaker, text, index * 1000, index * 1000 + 900)
            for index, (speaker, text) in enumerate(segments)
        ],
    )


def _own_terms_model(monkeypatch: pytest.MonkeyPatch, model: str = "fast-terms-model") -> None:
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", model)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == enrichment_mod.__name__ and record.levelno == logging.WARNING
    ]


def _system_text(call: dict[str, Any]) -> str:
    """The system prompt sent; a cacheable prompt (anthropic/... on OpenRouter) is a list of parts."""
    content = call["messages"][0]["content"]
    if isinstance(content, list):
        return "".join(part["text"] for part in content)
    return content


def _accepted(result: ReportEnrichment) -> list[tuple[int, str, str]]:
    return [(c.segment_index, c.source, c.target) for c in result.term_corrections]


# ---------------------------------------------------------------------------
# The fallback chain: REPORT_TERMS_* -> REPORT_* -> the conversation
# ---------------------------------------------------------------------------


def test_report_terms_is_a_task_with_its_own_keys() -> None:
    assert "report_terms" in TASKS
    assert task_env_keys("report_terms") == (
        "REPORT_TERMS_LLM_PROVIDER",
        "REPORT_TERMS_LLM_MODEL",
        "REPORT_TERMS_LLM_TIMEOUT_MS",
    )
    assert task_max_output_tokens_key("report_terms") == "REPORT_TERMS_MAX_OUTPUT_TOKENS"


def test_without_any_report_values_terms_resolve_to_the_conversation() -> None:
    resolved = resolve_llm("report_terms", _config(llm_timeout_ms=3000))

    assert (resolved.provider, resolved.model, resolved.timeout_ms, resolved.max_output_tokens) == (
        "openrouter",
        "global-model",
        300_000,
        16_384,
    )


def test_without_own_values_terms_take_the_report_values_not_the_global_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "azure")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    monkeypatch.setenv("REPORT_LLM_TIMEOUT_MS", "120000")
    monkeypatch.setenv("REPORT_MAX_OUTPUT_TOKENS", "9000")

    terms = resolve_llm("report_terms", _config())
    report = resolve_llm("report", _config())

    assert (terms.provider, terms.model, terms.timeout_ms, terms.max_output_tokens) == (
        "azure",
        "report-model",
        120_000,
        9000,
    )
    assert (terms.provider, terms.model) == (report.provider, report.model)


def test_own_terms_values_win_over_the_report_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "azure")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    monkeypatch.setenv("REPORT_LLM_TIMEOUT_MS", "120000")
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "terms-model")
    monkeypatch.setenv("REPORT_TERMS_LLM_TIMEOUT_MS", "90000")
    monkeypatch.setenv("REPORT_TERMS_MAX_OUTPUT_TOKENS", "4000")

    resolved = resolve_llm("report_terms", _config())

    assert (resolved.task, resolved.provider, resolved.model, resolved.timeout_ms, resolved.max_output_tokens) == (
        "report_terms",
        "openrouter",
        "terms-model",
        90_000,
        4000,
    )
    # The report keeps its own route.
    assert resolve_llm("report", _config()).model == "report-model"


def test_a_terms_model_alone_runs_on_the_report_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "azure")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    _own_terms_model(monkeypatch, "azure-fast")

    resolved = resolve_llm("report_terms", _config())

    assert (resolved.provider, resolved.model) == ("azure", "azure-fast")


def test_a_terms_provider_equal_to_the_report_provider_takes_the_report_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "azure")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "Azure")

    resolved = resolve_llm("report_terms", _config())

    assert (resolved.provider, resolved.model) == ("azure", "report-model")


def test_a_terms_provider_needs_its_own_model_when_the_report_model_belongs_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "azure")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "openrouter")

    with pytest.raises(TaskModelMissingError) as exc_info:
        resolve_llm("report_terms", _config())

    message = str(exc_info.value)
    assert "REPORT_TERMS_LLM_PROVIDER=openrouter" in message
    assert "REPORT_LLM_PROVIDER=azure" in message
    assert "REPORT_TERMS_LLM_MODEL" in message
    assert "report-model" in message


def test_a_report_model_alone_belongs_to_the_conversation_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """REPORT_LLM_MODEL without REPORT_LLM_PROVIDER is a model for the conversation's provider."""
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "azure")

    with pytest.raises(TaskModelMissingError, match="LLM_PROVIDER=openrouter"):
        resolve_llm("report_terms", _config())


def test_a_report_provider_without_its_model_fails_the_terms_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "azure")

    with pytest.raises(TaskModelMissingError, match="REPORT_LLM_MODEL"):
        resolve_llm("report_terms", _config())


def test_terms_switched_off_leave_the_report_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "none")

    assert resolve_llm("report_terms", _config()).provider == "none"
    assert resolve_llm("report", _config()).provider == "openrouter"


def test_terms_can_run_while_the_report_is_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "none")
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "openrouter")

    # The conversation's model belongs to openrouter, so no own model is needed.
    resolved = resolve_llm("report_terms", _config())

    assert (resolved.provider, resolved.model) == ("openrouter", "global-model")


def test_a_terms_provider_resolving_to_ollama_gets_the_ollama_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_TIMEOUT_MS", "1000")
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "llama3")

    assert resolve_llm("report_terms", _config()).timeout_ms == 90_000


def test_a_non_integer_report_timeout_is_refused_for_the_terms_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_TIMEOUT_MS", "soon")

    with pytest.raises(ValueError, match="REPORT_LLM_TIMEOUT_MS"):
        resolve_llm("report_terms", _config())


@pytest.mark.parametrize("profile", ["local", "tenant"])
def test_the_ceiling_applies_to_the_terms_provider(profile: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "terms-model")

    with pytest.raises(PrivacyGateError, match="report_terms"):
        resolve_llm("report_terms", _config(llm_provider="ollama", llm_model="llama3", privacy=profile))


# ---------------------------------------------------------------------------
# The start-call poort and the health check
# ---------------------------------------------------------------------------


def test_active_tasks_carry_report_terms_with_the_report() -> None:
    assert active_tasks(_config(), insight_active=False, report_active=True)[-2:] == ("report", "report_terms")
    assert "report_terms" not in active_tasks(_config(), insight_active=False, report_active=False)


def _write_klant_yaml(root: Path, slug: str, content: str) -> None:
    client_dir = root / slug
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "klant.yaml").write_text(content, encoding="utf-8")


def _local_start_call(**extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"client_slug": "acme-corp", "llm": {"provider": "ollama", "model": "llama3"}}
    config.update(extra)
    return {"config": config}


def test_start_call_refuses_a_terms_provider_above_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "terms-model")
    _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

    with pytest.raises(PrivacyGateError) as exc_info:
        hub_core.extract_start_call_config(_local_start_call())

    assert "report_terms" in str(exc_info.value)
    assert "acme-corp" in str(exc_info.value)


def test_start_call_refuses_a_terms_provider_without_its_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "openrouter")

    with pytest.raises(TaskModelMissingError, match="REPORT_TERMS_LLM_MODEL"):
        hub_core.extract_start_call_config({"config": {"llm": {"provider": "ollama", "model": "llama3"}}})


def test_start_call_ignores_the_terms_provider_when_reports_are_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "terms-model")
    _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

    config = hub_core.extract_start_call_config(_local_start_call(modules={"post_call_report": False}))

    assert config["privacy"] == "local"


async def test_health_check_covers_the_terms_provider_under_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[tuple[str, str]] = []

    async def _record(provider: str, module: str, ws_config: WebSocketConfig) -> None:
        checked.append((provider, module))

    monkeypatch.setattr(app_main, "_health_check_one_provider", _record)
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "azure")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "terms-model")

    await app_main._health_check_providers(
        CallConfig(enable_detector=False), _config(), InsightConfig(), WebSocketConfig()
    )

    assert checked == [("openrouter", "reports"), ("azure", "reports")]


# ---------------------------------------------------------------------------
# enrich_report: one call or two
# ---------------------------------------------------------------------------


async def test_the_same_model_is_one_call_with_both_schema_parts(sdk: _Sdk) -> None:
    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert len(sdk.calls) == 1
    call = sdk.calls[0]
    assert call["response_model"] is ReportEnrichment
    assert "term_corrections" in ReportEnrichment.model_json_schema()["properties"]
    assert _system_text(call).endswith(TERM_CORRECTION_PROMPT)
    assert "candidates for segment 0: Teamleader" in call["messages"][1]["content"]
    assert result.short_summary == _SUMMARY
    assert _accepted(result) == [(0, "Team Leader", "Teamleader")]


async def test_explicit_terms_values_equal_to_the_report_route_stay_one_call(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_LLM_MODEL", "anthropic/claude-sonnet-5.5")
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "anthropic/claude-sonnet-5.5")

    await enrich_report(_session(), _config(), terms=TERMS)

    assert len(sdk.calls) == 1
    assert sdk.calls[0]["model"] == "anthropic/claude-sonnet-5.5"


async def test_one_call_takes_the_larger_timeout_and_output_limit(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_LLM_TIMEOUT_MS", "100000")
    monkeypatch.setenv("REPORT_TERMS_LLM_TIMEOUT_MS", "200000")
    monkeypatch.setenv("REPORT_TERMS_MAX_OUTPUT_TOKENS", "24000")

    await enrich_report(_session(), _config(), terms=TERMS)

    assert sdk.built == [("openrouter", 200_000)]
    assert sdk.calls[0]["max_tokens"] == 24_000


async def test_one_call_cut_off_names_the_key_whose_limit_it_was(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from instructor.core.exceptions import IncompleteOutputException

    monkeypatch.setenv("REPORT_TERMS_MAX_OUTPUT_TOKENS", "24000")

    def _cut(kwargs: dict[str, Any]) -> Any:
        raise IncompleteOutputException(last_completion=None)

    sdk.on_report = _cut
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert result == ReportEnrichment.fail_open()
    [cut] = [message for message in _warnings(caplog) if "output limit" in message]
    assert "24000" in cut and "REPORT_TERMS_MAX_OUTPUT_TOKENS" in cut


async def test_a_different_model_is_two_calls(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_MODEL", "google/gemini-2.5-flash")
    _own_terms_model(monkeypatch, "anthropic/claude-sonnet-5.5")
    monkeypatch.setenv("REPORT_MAX_OUTPUT_TOKENS", "4000")
    monkeypatch.setenv("REPORT_TERMS_MAX_OUTPUT_TOKENS", "12000")

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert len(sdk.calls) == 2
    report_call = sdk.call_for(ReportEnrichment)
    terms_call = sdk.call_for(ReportTermCorrections)
    # The report call carries the plain transcript and no term section.
    assert report_call["model"] == "google/gemini-2.5-flash"
    assert _system_text(report_call) == enrichment_mod._SYSTEM_PROMPT
    assert "candidates" not in report_call["messages"][1]["content"]
    assert report_call["max_tokens"] == 4000
    # The term call carries only the term part: numbered segments with their candidates.
    assert terms_call["model"] == "anthropic/claude-sonnet-5.5"
    assert _system_text(terms_call).endswith(TERM_CORRECTION_PROMPT)
    assert "gesprek_gevoerd" not in _system_text(terms_call)
    assert "candidates for segment 0: Teamleader" in terms_call["messages"][1]["content"]
    assert terms_call["max_tokens"] == 12_000
    # The report model's own term corrections never count: only the term call decides.
    assert result.short_summary == _SUMMARY
    assert _accepted(result) == [(0, "Team Leader", "Teamleader")]


async def test_a_different_provider_is_two_calls_each_on_its_own_client(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "llama3")

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert sorted(provider for provider, _ in sdk.built) == ["ollama", "openrouter"]
    assert len(sdk.calls) == 2
    assert _accepted(result) == [(0, "Team Leader", "Teamleader")]


@pytest.mark.parametrize("terms", [(), ("Wattelaar",)])
async def test_a_different_model_without_candidates_makes_no_term_call(
    terms: tuple[str, ...], sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    _own_terms_model(monkeypatch)

    result = await enrich_report(_session(), _config(), terms=terms)

    assert [call["response_model"] for call in sdk.calls] == [ReportEnrichment]
    assert sdk.built == [("openrouter", 300_000)]
    assert result.short_summary == _SUMMARY
    assert result.term_corrections == []


async def test_the_two_calls_run_side_by_side(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each call waits until the other one has started: one after the other would never meet."""
    _own_terms_model(monkeypatch)
    both_started = threading.Barrier(2, timeout=5)
    report_answer, terms_answer = sdk.on_report, sdk.on_terms

    def _report(kwargs: dict[str, Any]) -> Any:
        both_started.wait()
        return report_answer(kwargs)

    def _terms(kwargs: dict[str, Any]) -> Any:
        both_started.wait()
        return terms_answer(kwargs)

    sdk.on_report, sdk.on_terms = _report, _terms

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert result.short_summary == _SUMMARY
    assert _accepted(result) == [(0, "Team Leader", "Teamleader")]


# ---------------------------------------------------------------------------
# One part failing never costs the other
# ---------------------------------------------------------------------------


async def test_a_terms_provider_above_the_ceiling_leaves_the_report(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "terms-model")
    sdk.on_report = lambda kwargs: ReportEnrichment(
        gesprek_gevoerd=True, short_summary=_SUMMARY, overview="", keywords=[], action_items=[]
    )
    caplog.set_level(logging.WARNING)

    result = await enrich_report(
        _session(), _config(llm_provider="ollama", llm_model="llama3", privacy="local"), terms=TERMS
    )

    assert [provider for provider, _ in sdk.built] == ["ollama"]
    assert [call["response_model"] for call in sdk.calls] == [ReportEnrichment]
    assert "candidates" not in sdk.calls[0]["messages"][1]["content"]
    assert result.short_summary == _SUMMARY
    assert result.term_corrections == []
    [warning] = _warnings(caplog)
    assert warning.startswith("Report term correction refused") and "privacy ceiling" in warning


async def test_a_report_provider_above_the_ceiling_leaves_the_terms(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("REPORT_TERMS_LLM_MODEL", "llama3")
    caplog.set_level(logging.WARNING)

    result = await enrich_report(
        _session(), _config(llm_provider="ollama", llm_model="llama3", privacy="local"), terms=TERMS
    )

    assert [provider for provider, _ in sdk.built] == ["ollama"]
    assert [call["response_model"] for call in sdk.calls] == [ReportTermCorrections]
    assert result.short_summary == ""
    assert result.gesprek_gevoerd is True
    assert _accepted(result) == [(0, "Team Leader", "Teamleader")]
    [warning] = _warnings(caplog)
    assert warning.startswith("Report enrichment refused") and "privacy ceiling" in warning


async def test_a_terms_provider_without_its_model_leaves_the_report(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "azure")
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert result.short_summary == _SUMMARY
    assert result.term_corrections == []
    [warning] = _warnings(caplog)
    assert "TaskModelMissingError" in warning and "REPORT_TERMS_LLM_MODEL" in warning


async def test_a_failing_term_call_leaves_the_report(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _own_terms_model(monkeypatch)

    def _boom(kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("terms provider down")

    sdk.on_terms = _boom
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert result.short_summary == _SUMMARY
    assert result.term_corrections == []
    assert any(message.startswith("Report term correction failed") for message in _warnings(caplog))


async def test_a_failing_report_call_leaves_the_terms(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _own_terms_model(monkeypatch)

    def _boom(kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("report provider down")

    sdk.on_report = _boom
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert (result.gesprek_gevoerd, result.short_summary, result.keywords) == (True, "", [])
    assert _accepted(result) == [(0, "Team Leader", "Teamleader")]
    assert any(message.startswith("Report enrichment failed") for message in _warnings(caplog))


async def test_a_slow_term_call_times_out_without_holding_the_report(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _own_terms_model(monkeypatch)
    monkeypatch.setenv("REPORT_TERMS_LLM_TIMEOUT_MS", "50")
    release = threading.Event()
    terms_answer = sdk.on_terms

    def _hang(kwargs: dict[str, Any]) -> Any:
        release.wait(5)
        return terms_answer(kwargs)

    sdk.on_terms = _hang
    caplog.set_level(logging.WARNING)
    start = time.monotonic()
    try:
        result = await enrich_report(_session(), _config(), terms=TERMS)
    finally:
        release.set()

    assert time.monotonic() - start < 2
    assert result.short_summary == _SUMMARY
    assert result.term_corrections == []
    assert any("Report term correction timed out" in message for message in _warnings(caplog))


async def test_a_term_call_without_a_result_leaves_the_report(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
    _own_terms_model(monkeypatch)
    sdk.on_terms = lambda kwargs: None

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert result.short_summary == _SUMMARY
    assert result.term_corrections == []


@pytest.mark.parametrize("own_terms_model", [False, True])
async def test_a_failing_review_costs_only_the_term_corrections(
    own_terms_model: bool, sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if own_terms_model:
        _own_terms_model(monkeypatch)

    def _broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("review broke")

    monkeypatch.setattr(enrichment_mod, "review_term_corrections", _broken)
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert result.short_summary == _SUMMARY
    assert result.term_corrections == []
    assert any("review failed" in message for message in _warnings(caplog))


async def test_both_parts_switched_off_make_no_call(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "none")

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert result == ReportEnrichment.fail_open()
    assert sdk.calls == []


async def test_terms_switched_off_leave_one_plain_report_call(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_TERMS_LLM_PROVIDER", "none")

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert len(sdk.calls) == 1
    assert "candidates" not in sdk.calls[0]["messages"][1]["content"]
    assert result.short_summary == _SUMMARY
    assert result.term_corrections == []


async def test_the_split_does_not_block_the_event_loop(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
    _own_terms_model(monkeypatch)
    report_answer, terms_answer = sdk.on_report, sdk.on_terms

    def _slow_report(kwargs: dict[str, Any]) -> Any:
        time.sleep(0.3)
        return report_answer(kwargs)

    def _slow_terms(kwargs: dict[str, Any]) -> Any:
        time.sleep(0.3)
        return terms_answer(kwargs)

    sdk.on_report, sdk.on_terms = _slow_report, _slow_terms
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        await enrich_report(_session(), _config(), terms=TERMS)
    finally:
        ticker.cancel()

    assert ticks >= 10
