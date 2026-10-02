"""Per-task LLM routing: one resolver, one privacy ceiling.

Every production place that talks to an LLM names its *task* and gets its provider, model
and timeout from ``resolve_llm(task, ...)`` -- never from ``DetectorConfig`` directly, and
never by constructing ``LLMClient`` itself (``tests/test_llm_routing.py`` scans the tree for
that). The resolver is also where the conversation's privacy ceiling is enforced, so a
per-task provider can never exist without a plafond above it.

Sources, per field, first hit wins:

  provider: ``<TAAK>_LLM_PROVIDER``  -> the conversation's ``llm_provider`` (``LLM_PROVIDER``,
            or the start-call ``llm.provider`` override)
  model:    ``<TAAK>_LLM_MODEL``     -> the conversation's ``llm_model``
  timeout:  ``<TAAK>_LLM_TIMEOUT_MS`` -> ``TASK_DEFAULT_TIMEOUT_MS[task]`` -> ``LLM_TIMEOUT_MS``
  output:   ``<TAAK>_LLM_MAX_OUTPUT_TOKENS`` -> ``TASK_DEFAULT_MAX_OUTPUT_TOKENS[task]``

The ``insight`` task keeps its existing variables (``INSIGHT_PROVIDER``, ``INSIGHT_MODEL``,
``INSIGHT_LLM_TIMEOUT_MS``, read by ``InsightConfig``) instead of ``INSIGHT_LLM_*``: they
were there first, and its 60 s timeout must never collapse to the live ``LLM_TIMEOUT_MS``.
The ``report`` task's output limit keeps its key from #275, ``REPORT_MAX_OUTPUT_TOKENS``, and
``report_terms`` has ``REPORT_TERMS_MAX_OUTPUT_TOKENS`` next to it.

``report_terms`` (the post-call term correction) inherits from ``report`` (``_TASK_PARENT``):
every field it does not set itself comes from the ``REPORT_*`` value first and only then from
the conversation's, so without ``REPORT_TERMS_*`` values it resolves exactly like ``report``
and the report step stays one call. A model always has to belong to the provider it is sent
to: a level that names its own provider, different from the one the inherited model came
with, needs its own model too (``TaskModelMissingError``).

Every call passes its task's output limit as ``max_tokens`` (``ResolvedLLM.output_limit``).
Without one, OpenRouter reserves the model's maximum output for each call and refuses it with
a 402 as soon as the credit drops below what that maximum would cost, also in the middle of a
conversation. A reasoning budget comes on top of the limit, never out of it.

No provider or model is hardcoded here: a task without its own value falls back to the
global one. The Ollama timeout floor that ``DetectorConfig`` applies to the global provider
applies to every task that resolves to Ollama, whatever the global provider is.

Privacy ceiling: ``DetectorConfig.privacy`` (from ``klant.yaml`` via ``CallConfig``) is
checked with ``privacy_gate.enforce_privacy`` for the resolved provider. A provider above
the ceiling raises ``PrivacyGateError`` -- no client is built and no call is made. The
built ``LLMClient`` carries the same ceiling and checks it again on every call, because
``tier_of()`` reads ``TRUST_OWN_TENANT``/``OLLAMA_BASE_URL`` from the environment, which can
change while a client is alive.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sales_copilot.core.config import (
    _OLLAMA_TIMEOUT_FLOOR_MS,
    DetectorConfig,
    InsightConfig,
    env,
    env_int,
)
from sales_copilot.core.llm_client import LLMClient
from sales_copilot.core.privacy_gate import enforce_privacy
from sales_copilot.core.thinking_policy import ThinkingPolicy

logger = logging.getLogger(__name__)

#: Every task the resolver knows. A name outside this tuple is a programming error.
TASKS: tuple[str, ...] = (
    "detector_confirm",
    "window_classifier",
    "phase",
    "suggestions",
    "summary",
    "script_tracking",
    "slides",
    "insight",
    "report",
    "report_terms",
)

#: A task whose unset values come from another task before the conversation's. The term
#: correction runs on the report's route unless it is given one of its own.
_TASK_PARENT: dict[str, str] = {"report_terms": "report"}

#: Timeout default per task when its own ``*_TIMEOUT_MS`` variable is unset.
#: ``None`` means "the conversation's ``LLM_TIMEOUT_MS``" -- right for the live tasks,
#: whose latency budget is the live one. ``summary`` runs post-segment, not on the
#: 5-second path, and timed out at 3 s on 2026-09-27; ``insight`` is the minutes-latency
#: deep lane (its value is what ``InsightConfig`` defaults ``INSIGHT_LLM_TIMEOUT_MS`` to).
#: ``report`` is the post-call enrichment of the whole transcript
#: (``modules/reports/enrichment.py``): it runs after the call, off the live path, and nobody
#: waits for it. Measured 2026-10-01 on a one-hour call (9,019 words, 200 terms): 54 s with
#: Sonnet 5.5 and 77 s with Sonnet 5, so the former 60 s cut a slower model off. 300 s
#: leaves room for a longer call or a slower provider.
TASK_DEFAULT_TIMEOUT_MS: dict[str, int | None] = {
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

#: Output limit (``max_tokens``) per task when its own variable is unset: the task's
#: structured answer with room to spare. The short live tasks get a tight limit, so a
#: provider never reserves the model's maximum output for a one-word answer.
TASK_DEFAULT_MAX_OUTPUT_TOKENS: dict[str, int] = {
    # PainPointDetection: one category, one confidence and one quoted trigger phrase,
    # about 60 tokens with the tool-call structure.
    "detector_confirm": 256,
    # WindowAnalysis: zero to a few detections of five short fields each (one quote, one
    # line of reasoning), about 100 tokens per detection.
    "window_classifier": 1024,
    # PhaseClassification: one of three words.
    "phase": 128,
    # SuggestionResponse: two or three short follow-up questions.
    "suggestions": 512,
    # ConversationSummary: two or three sentences plus the key moments it restates; the
    # objections among those moments add up over the whole call, so this one gets room.
    "summary": 2048,
    # _CoverageConfirmResponse: point ids plus one short hint per missing script point.
    "script_tracking": 1024,
    # GeneratedSlide: a title, a description and one or two metrics.
    "slides": 512,
    # InsightBatch: zero to three insights with their grounding.
    "insight": 2048,
    # ReportEnrichment over a whole call. Measured 2026-10-01: a one-hour call (9,019 words,
    # 193 term corrections) answers in about 9,000-10,000 tokens; 16,384 leaves ~60% headroom.
    "report": 16_384,
    # The term corrections of that same call are nearly all of that answer.
    "report_terms": 16_384,
}

_INSIGHT_ENV_KEYS = ("INSIGHT_PROVIDER", "INSIGHT_MODEL", "INSIGHT_LLM_TIMEOUT_MS")

#: Output-limit keys that predate the ``<TAAK>_LLM_MAX_OUTPUT_TOKENS`` rule (#275).
_MAX_OUTPUT_TOKENS_KEYS = {
    "report": "REPORT_MAX_OUTPUT_TOKENS",
    "report_terms": "REPORT_TERMS_MAX_OUTPUT_TOKENS",
}


@dataclass(frozen=True)
class ResolvedLLM:
    """What one task will call: provider, model, timeout and output limit, under which ceiling."""

    task: str
    provider: str
    model: str
    timeout_ms: int
    max_output_tokens: int
    privacy: str | None
    client_slug: str | None = None

    def output_limit(self, thinking: ThinkingPolicy | None = None) -> int:
        """The ``max_tokens`` of one call: the task's limit, with a reasoning budget on top.

        The reasoning trace is spent from the same ``max_tokens``, so with a budget the limit
        is never below that budget plus the task's default room for the answer; a cap under it
        would cut the answer off.
        """
        if thinking is not None and thinking.thinking_on and thinking.reasoning_budget:
            return max(
                self.max_output_tokens,
                thinking.reasoning_budget + TASK_DEFAULT_MAX_OUTPUT_TOKENS[self.task],
            )
        return self.max_output_tokens


class TaskModelMissingError(ValueError):
    """A task names its own provider, differing from the conversation's, but no model.

    The conversation's model belongs to the conversation's provider (and a ``REPORT_LLM_MODEL``
    to the report's); sending it to another provider fails mid-call. A ``ValueError`` subclass so the start-call poort
    (``hub_core.extract_start_call_config``) returns it as a 400 before any audio runs.
    """


def task_env_keys(task: str) -> tuple[str, str, str]:
    """The ``(provider, model, timeout)`` environment variables for ``task``."""
    _require_task(task)
    if task == "insight":
        return _INSIGHT_ENV_KEYS
    prefix = task.upper()
    return (f"{prefix}_LLM_PROVIDER", f"{prefix}_LLM_MODEL", f"{prefix}_LLM_TIMEOUT_MS")


def task_max_output_tokens_key(task: str) -> str:
    """The environment variable of ``task``'s output limit (``<TAAK>_LLM_MAX_OUTPUT_TOKENS``)."""
    _require_task(task)
    return _MAX_OUTPUT_TOKENS_KEYS.get(task, f"{task.upper()}_LLM_MAX_OUTPUT_TOKENS")


def task_max_output_tokens(task: str) -> int:
    """``task``'s output limit: its own variable, its parent's, else its default.

    The parent is ``_TASK_PARENT[task]`` (``REPORT_MAX_OUTPUT_TOKENS`` for ``report_terms``).
    A blank variable counts as unset. A value that is not a positive integer is ignored with a
    WARNING naming the key: a typo in a limit must never stop a call, and the default is a
    safe limit.
    """
    _require_task(task)
    for level in _task_chain(task):
        key = task_max_output_tokens_key(level)
        raw = (env(key) or "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
        logger.warning("%s is not a positive integer; it is ignored.", key)
    return TASK_DEFAULT_MAX_OUTPUT_TOKENS[task]


def _task_chain(task: str) -> tuple[str, ...]:
    """``task`` and the tasks it inherits from, most specific first."""
    chain = [task]
    while chain[-1] in _TASK_PARENT:
        chain.append(_TASK_PARENT[chain[-1]])
    return tuple(chain)


def _require_task(task: str) -> None:
    if task not in TASKS:
        raise ValueError(f"unknown LLM task {task!r}; known tasks: {list(TASKS)}")


def _env_timeout_ms(name: str) -> int | None:
    """``name`` as an int, ``None`` when unset or blank; a non-integer raises (like ``env_int``)."""
    raw = (env(name) or "").strip()
    return env_int(name) if raw else None


def _positive_int(value: int | None) -> int | None:
    return value if value is not None and value > 0 else None


def _first_positive(values: Iterable[int | None]) -> int | None:
    """The first positive value, read lazily: a later level is only read when needed."""
    for value in values:
        if value is not None and value > 0:
            return value
    return None


def resolve_llm(
    task: str,
    detector_config: DetectorConfig | None = None,
    *,
    insight_config: InsightConfig | None = None,
) -> ResolvedLLM:
    """Resolve provider, model and timeout for ``task`` and enforce the privacy ceiling.

    ``detector_config`` is the conversation's config (global values plus start-call
    overrides plus ``privacy``); ``DetectorConfig.from_env()`` when omitted.
    ``insight_config`` is only read for the ``insight`` task.

    Raises:
        ValueError: for an unknown ``task``.
        TaskModelMissingError: when a level's own provider differs from the provider the
            inherited model belongs to and that level has no model of its own (``provider``
            ``none`` is exempt).
        PrivacyGateError: when the resolved provider is above ``detector_config.privacy``.
    """
    _require_task(task)
    config = detector_config or DetectorConfig.from_env()

    # One (provider key, model key, provider, model) per level the task reads, most specific
    # first: the task itself and, for report_terms, the report task.
    levels: list[tuple[str, str, str, str]] = []
    if task == "insight":
        insight = insight_config or InsightConfig.from_env()
        provider_key, model_key, _ = _INSIGHT_ENV_KEYS
        insight_provider = (insight.llm_provider or "").strip()
        levels.append((provider_key, model_key, insight_provider, (insight.llm_model or "").strip()))
        timeout_ms = _positive_int(insight.llm_timeout_ms) or TASK_DEFAULT_TIMEOUT_MS["insight"]
    else:
        chain = _task_chain(task)
        for level in chain:
            provider_key, model_key, _ = task_env_keys(level)
            levels.append((provider_key, model_key, (env(provider_key) or "").strip(), (env(model_key) or "").strip()))
        timeout_ms = (
            _first_positive(_env_timeout_ms(task_env_keys(level)[2]) for level in chain)
            or _first_positive(TASK_DEFAULT_TIMEOUT_MS[level] for level in chain)
            or config.llm_timeout_ms
        )

    # From the conversation up to the task: a level's provider replaces the one below it, and a
    # level's model replaces the model below it and belongs to the provider in force there.
    provider = (config.llm_provider or "").strip().lower()
    model = config.llm_model
    model_owner, model_owner_key = provider, "LLM_PROVIDER"
    provider_keys: tuple[str, str] | None = None
    for provider_key, model_key, level_provider, level_model in reversed(levels):
        if level_provider:
            provider = level_provider.lower()
            provider_keys = (provider_key, model_key)
        if level_model:
            model = level_model
            model_owner = provider
            model_owner_key = provider_keys[0] if provider_keys is not None else "LLM_PROVIDER"
    if provider == "ollama":
        timeout_ms = max(timeout_ms, _OLLAMA_TIMEOUT_FLOOR_MS)

    enforce_privacy(config.privacy, provider, client_slug=config.client_slug, lane=task)
    if provider_keys is not None and provider not in ("none", model_owner):
        provider_key, model_key = provider_keys
        model_name = "het gespreksmodel" if model_owner_key == "LLM_PROVIDER" else "het model"
        raise TaskModelMissingError(
            f"{provider_key}={provider} wijkt af van {model_owner_key}={model_owner}; "
            f"zet ook {model_key}: {model_name} {model!r} hoort bij een andere provider"
        )
    return ResolvedLLM(
        task=task,
        provider=provider,
        model=model,
        timeout_ms=int(timeout_ms),
        max_output_tokens=task_max_output_tokens(task),
        privacy=config.privacy,
        client_slug=config.client_slug,
    )


def build_llm_client(resolved: ResolvedLLM, *, prompt_cache: bool = True) -> LLMClient:
    """The one production constructor of ``LLMClient``: bound to ``resolved``'s ceiling."""
    return LLMClient(
        resolved.provider,
        timeout_ms=resolved.timeout_ms,
        prompt_cache=prompt_cache,
        privacy=resolved.privacy,
        task=resolved.task,
        client_slug=resolved.client_slug,
    )


def resolve_llm_client(
    task: str,
    detector_config: DetectorConfig | None = None,
    *,
    insight_config: InsightConfig | None = None,
    prompt_cache: bool = True,
) -> tuple[ResolvedLLM, LLMClient]:
    """``resolve_llm`` plus ``build_llm_client`` in one step, for the call sites."""
    resolved = resolve_llm(task, detector_config, insight_config=insight_config)
    return resolved, build_llm_client(resolved, prompt_cache=prompt_cache)


def active_tasks(
    detector_config: DetectorConfig, *, insight_active: bool, report_active: bool
) -> tuple[str, ...]:
    """The tasks a conversation with ``detector_config`` will actually run.

    Mirrors ``modules/detector/__main__.py``: confirm, window classification and slides
    always run; the others only when their switch is on. ``insight_active`` is the caller's
    verdict on the deep lane (``INSIGHT_ENABLED`` plus its license entitlement), which this
    module cannot see. The start-call privacy-poort checks exactly these tasks, so a
    leftover ``<TAAK>_LLM_PROVIDER`` for a task that is switched off never blocks a call.

    ``report_active`` is whether the reports module runs for this call
    (``CallConfig.enable_reports``, the start-call ``modules.post_call_report``). When it
    does, the post-call enrichment and its term correction run after every conversation, so
    both providers are checked at the poort like any live task: a ``REPORT_LLM_PROVIDER`` or
    ``REPORT_TERMS_LLM_PROVIDER`` above the ceiling refuses the call up front instead of
    leaving an unenriched report at the end.
    """
    enabled = {
        "detector_confirm": True,
        "window_classifier": True,
        "phase": detector_config.auto_phase_detection,
        "suggestions": detector_config.enable_suggestions,
        "summary": detector_config.enable_summary,
        "script_tracking": detector_config.enable_script_tracking,
        "slides": True,
        "insight": insight_active,
        "report": report_active,
        "report_terms": report_active,
    }
    return tuple(task for task in TASKS if enabled[task])
