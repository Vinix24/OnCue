"""Output limit per LLM task: ``TASK_DEFAULT_MAX_OUTPUT_TOKENS`` in ``core.llm_routing``.

Measured in #275: the live tasks passed no ``max_tokens``, so OpenRouter reserved the model's
maximum output for every call and refused it with a 402 once the credit dropped below that,
also in the middle of a conversation. Every task now gets its limit from one table, can
override it through ``<TAAK>_LLM_MAX_OUTPUT_TOKENS`` (``REPORT_MAX_OUTPUT_TOKENS`` for the
report), and a reasoning budget comes on top of it.

Covers, per task: the default, the variable name, the override, a bad value, and that the
call site really sends the limit to the SDK seam. The suggestions and slides call sites keep
their own tests in ``test_suggestions.py`` and ``test_slide_generator.py``; the report call in
``test_report_enrichment.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pydantic import BaseModel

from sales_copilot.core import llm_client as llm_client_mod
from sales_copilot.core import llm_routing
from sales_copilot.core.config import DetectorConfig, InsightConfig
from sales_copilot.core.llm_routing import (
    TASK_DEFAULT_MAX_OUTPUT_TOKENS,
    TASKS,
    resolve_llm,
    task_env_keys,
    task_max_output_tokens,
    task_max_output_tokens_key,
)
from sales_copilot.core.thinking_policy import THINKING_PROFILES
from sales_copilot.modules.coaching.script_tracker import (
    ScriptCoverageLLMClient,
    ScriptPoint,
    _CoverageConfirmResponse,
)
from sales_copilot.modules.detector.llm_confirm import LLMConfirmClient, PainPointDetection
from sales_copilot.modules.detector.phase_detector import PhaseClassification, PhaseLLMClient
from sales_copilot.modules.detector.summary import ConversationSummary, SummaryLLMClient
from sales_copilot.modules.detector.window_classifier import WindowAnalysis, WindowClassifier


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for task in TASKS:
        for key in (*task_env_keys(task), task_max_output_tokens_key(task)):
            monkeypatch.delenv(key, raising=False)
    for key in ("LLM_PROVIDER", "LLM_MODEL", "LLM_TIMEOUT_MS", "TRUST_OWN_TENANT", "PII_REDACTION"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


_REPLIES: dict[type[BaseModel], BaseModel] = {
    PainPointDetection: PainPointDetection(category="kosten", confidence=0.9, trigger_phrase="te duur"),
    WindowAnalysis: WindowAnalysis(detections=[]),
    PhaseClassification: PhaseClassification(phase="pitch"),
    ConversationSummary: ConversationSummary(text="Een korte samenvatting."),
    _CoverageConfirmResponse: _CoverageConfirmResponse(),
}


class _Sdk:
    """The provider SDK behind ``LLMClient``; the seam itself stays real."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build_client(self, provider: str, *, timeout_ms: int) -> object:
        return object()

    def build_create(self, client: object, provider: str):
        def _create(**kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return _REPLIES[kwargs["response_model"]]

        return _create


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _Sdk:
    fake = _Sdk()
    monkeypatch.setattr(llm_client_mod, "build_client", fake.build_client)
    monkeypatch.setattr(llm_client_mod, "build_create", fake.build_create)
    return fake


def _config(**kwargs: Any) -> DetectorConfig:
    base: dict[str, Any] = {"llm_provider": "openrouter", "llm_model": "global-model", "llm_timeout_ms": 7000}
    base.update(kwargs)
    return DetectorConfig(**base)


_POINT = ScriptPoint(id="budget", title="Budget", phase="discovery", required=True)


async def _confirm(config: DetectorConfig) -> None:
    await LLMConfirmClient(config).confirm_async("Het offerteproces kost ons te veel tijd.")


async def _window(config: DetectorConfig) -> None:
    await WindowClassifier(config, config.llm_provider).classify("Dat is ons te duur.", None)


async def _phase(config: DetectorConfig) -> None:
    await PhaseLLMClient(config).aclassify_phase(["prospect: wat kost het?"])


async def _summary(config: DetectorConfig) -> None:
    await SummaryLLMClient(config).summarize(["prospect: wat kost het?"], [])


async def _script(config: DetectorConfig) -> None:
    await ScriptCoverageLLMClient(config).confirm([_POINT], ["prospect: ons budget is krap"])


#: The five live call sites that sent no limit before this change.
_LIVE_SITES: tuple[tuple[str, Callable[[DetectorConfig], Awaitable[None]]], ...] = (
    ("detector_confirm", _confirm),
    ("window_classifier", _window),
    ("phase", _phase),
    ("summary", _summary),
    ("script_tracking", _script),
)


# ---------------------------------------------------------------------------
# The table and the variables
# ---------------------------------------------------------------------------


def test_default_table_covers_every_task_with_the_agreed_values() -> None:
    assert TASK_DEFAULT_MAX_OUTPUT_TOKENS == {
        "detector_confirm": 256,
        "window_classifier": 1024,
        "phase": 128,
        "suggestions": 512,
        "summary": 2048,
        "script_tracking": 1024,
        "slides": 512,
        "insight": 2048,
        "report": 16_384,
        "report_terms": 16_384,
    }
    assert set(TASK_DEFAULT_MAX_OUTPUT_TOKENS) == set(TASKS)


def test_the_short_live_tasks_stay_below_the_summary() -> None:
    short = ("detector_confirm", "window_classifier", "phase", "script_tracking")
    assert all(TASK_DEFAULT_MAX_OUTPUT_TOKENS[task] < TASK_DEFAULT_MAX_OUTPUT_TOKENS["summary"] for task in short)


@pytest.mark.parametrize("task", [task for task in TASKS if task not in ("report", "report_terms")])
def test_the_variable_follows_taak_llm_max_output_tokens(task: str) -> None:
    assert task_max_output_tokens_key(task) == f"{task.upper()}_LLM_MAX_OUTPUT_TOKENS"


def test_the_report_keeps_its_variable_from_275_and_the_terms_get_one_next_to_it() -> None:
    assert task_max_output_tokens_key("report") == "REPORT_MAX_OUTPUT_TOKENS"
    assert task_max_output_tokens_key("report_terms") == "REPORT_TERMS_MAX_OUTPUT_TOKENS"


def test_the_terms_limit_falls_back_to_the_report_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_MAX_OUTPUT_TOKENS", "9000")
    assert task_max_output_tokens("report_terms") == 9000

    monkeypatch.setenv("REPORT_TERMS_MAX_OUTPUT_TOKENS", "12000")
    assert task_max_output_tokens("report_terms") == 12000
    assert task_max_output_tokens("report") == 9000


def test_a_bad_terms_limit_falls_through_to_the_report_limit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("REPORT_TERMS_MAX_OUTPUT_TOKENS", "veel")
    monkeypatch.setenv("REPORT_MAX_OUTPUT_TOKENS", "9000")
    caplog.set_level(logging.WARNING, logger=llm_routing.__name__)

    assert task_max_output_tokens("report_terms") == 9000
    assert any("REPORT_TERMS_MAX_OUTPUT_TOKENS" in record.getMessage() for record in caplog.records)


def test_an_unknown_task_has_no_output_limit() -> None:
    with pytest.raises(ValueError, match="unknown LLM task"):
        task_max_output_tokens("smalltalk")


@pytest.mark.parametrize("task", TASKS)
def test_each_task_resolves_to_its_default(task: str) -> None:
    resolved = resolve_llm(task, _config(), insight_config=InsightConfig())

    assert resolved.max_output_tokens == TASK_DEFAULT_MAX_OUTPUT_TOKENS[task]
    assert resolved.output_limit() == TASK_DEFAULT_MAX_OUTPUT_TOKENS[task]


@pytest.mark.parametrize("task", TASKS)
def test_each_task_follows_its_own_variable(task: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(task_max_output_tokens_key(task), "321")

    resolved = resolve_llm(task, _config(), insight_config=InsightConfig())

    assert resolved.max_output_tokens == 321
    # One task's variable never moves another task's limit.
    other = "phase" if task != "phase" else "summary"
    assert resolve_llm(other, _config()).max_output_tokens == TASK_DEFAULT_MAX_OUTPUT_TOKENS[other]


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "1.5"])
def test_a_bad_value_falls_back_to_the_default_with_a_warning(
    raw: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PHASE_LLM_MAX_OUTPUT_TOKENS", raw)
    caplog.set_level(logging.WARNING, logger=llm_routing.__name__)

    assert task_max_output_tokens("phase") == 128
    assert [record.getMessage() for record in caplog.records if record.name == llm_routing.__name__] == [
        "PHASE_LLM_MAX_OUTPUT_TOKENS is not a positive integer; it is ignored."
    ]


def test_a_blank_value_counts_as_unset_without_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SUMMARY_LLM_MAX_OUTPUT_TOKENS", "  ")
    caplog.set_level(logging.WARNING, logger=llm_routing.__name__)

    assert task_max_output_tokens("summary") == 2048
    assert not [record for record in caplog.records if record.name == llm_routing.__name__]


# ---------------------------------------------------------------------------
# A reasoning budget comes on top
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task", TASKS)
def test_a_reasoning_budget_lifts_the_limit_by_the_answer_room(task: str) -> None:
    resolved = resolve_llm(task, _config(), insight_config=InsightConfig())
    budget = THINKING_PROFILES["think-2048"]

    assert resolved.output_limit(budget) == 2048 + TASK_DEFAULT_MAX_OUTPUT_TOKENS[task]
    assert resolved.output_limit(THINKING_PROFILES["no-think"]) == TASK_DEFAULT_MAX_OUTPUT_TOKENS[task]


def test_a_configured_limit_above_budget_plus_answer_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHASE_LLM_MAX_OUTPUT_TOKENS", "5000")
    resolved = resolve_llm("phase", _config())

    assert resolved.output_limit(THINKING_PROFILES["think-512"]) == 5000
    assert resolved.output_limit(THINKING_PROFILES["think-8192"]) == 8192 + 128


# ---------------------------------------------------------------------------
# The call sites send it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("task", "call"), _LIVE_SITES)
async def test_each_live_call_site_sends_its_task_limit(
    task: str, call: Callable[[DetectorConfig], Awaitable[None]], sdk: _Sdk
) -> None:
    await call(_config())

    assert len(sdk.calls) == 1
    assert sdk.calls[0]["max_tokens"] == TASK_DEFAULT_MAX_OUTPUT_TOKENS[task]


@pytest.mark.parametrize(("task", "call"), _LIVE_SITES)
async def test_each_live_call_site_follows_its_variable(
    task: str, call: Callable[[DetectorConfig], Awaitable[None]], sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(task_max_output_tokens_key(task), "77")

    await call(_config())

    assert sdk.calls[0]["max_tokens"] == 77


async def test_the_confirm_call_puts_its_reasoning_budget_on_top(sdk: _Sdk) -> None:
    client = LLMConfirmClient(_config(), thinking=THINKING_PROFILES["think-512"])

    await client.confirm_async("Het offerteproces kost ons te veel tijd.")

    assert sdk.calls[0]["max_tokens"] == 512 + 256
    assert sdk.calls[0]["extra_body"]["reasoning"] == {"max_tokens": 512}


def test_the_synchronous_confirm_path_sends_the_limit_too(sdk: _Sdk) -> None:
    LLMConfirmClient(_config()).confirm("Het offerteproces kost ons te veel tijd.")

    assert sdk.calls[0]["max_tokens"] == 256
