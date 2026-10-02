"""Post-call report enrichment: the structured LLM step once the conversation is over.

``enrich_report`` reads the whole transcript and returns the five report fields the live
pipeline cannot produce: ``gesprek_gevoerd`` (did two people actually talk, or was it a
voicemail, a phone menu or a line nobody answered), ``short_summary``, ``overview``,
``keywords`` and ``action_items``, plus the term corrections.

Term correction (termenlijst-in-uitwerking D2, ``term_corrections.py``): with a term list, the
prefilter picks per segment the terms that resemble something in it; when at least one segment
has candidates, the transcript goes out numbered with a candidates line under those segments,
the system prompt gets the term section, and the model's ``term_corrections`` are validated in
code against the original segments and against what the model saw after the PII policy.
Without candidates there is no term request at all. The transcript in the report is never
changed: the accepted corrections are a layer on top of it.

Routing, one model per part: the ``report`` task of ``core.llm_routing`` writes the five
fields (``REPORT_LLM_PROVIDER`` / ``REPORT_LLM_MODEL`` / ``REPORT_LLM_TIMEOUT_MS``, output limit
``REPORT_MAX_OUTPUT_TOKENS``), the ``report_terms`` task the term corrections
(``REPORT_TERMS_LLM_*``, ``REPORT_TERMS_MAX_OUTPUT_TOKENS``). ``report_terms`` inherits every
value it does not set from ``report``, which falls back to the conversation's provider and
model, with a 300 s default timeout. When both resolve to the same provider and model the
step is ONE call that carries both parts, at the larger of the two timeouts and output limits:
the default costs nothing extra. When they differ, the step is two calls side by side, and the
term call only runs when there are candidates. The conversation's privacy ceiling applies to
each call exactly as it does to the live tasks.

PII: the transcript is handed to ``LLMClient`` raw; the client applies ``apply_outbound_pii``
with the resolved provider to the whole user text, once. ``PII_REDACTION`` and
``TRUST_OWN_TENANT`` therefore decide per resolved provider, and a local provider receives the
text without anything leaving the machine.

Fail-open, per part: an error, a timeout, a refusal by the ceiling, a task without its model or
a missing LLM stack leave that part empty with a WARNING naming the reason. For the report part
that is ``ReportEnrichment.fail_open()`` -- ``gesprek_gevoerd`` true and every text field
empty; for the term part it is no term corrections. A failing term correction never costs the
summary, and a failing summary never costs the term corrections. An answer cut off at the
output limit is one of those reasons; its WARNING names the limit and the key. Nothing is ever
made up: a field the model did not fill stays empty.

Each call runs off the event loop (``LLMClient.acreate`` offloads it to a thread) under an
``asyncio.wait_for`` cap. The report work that outlives the reports task (enrichment,
rewrite, delivery) is tracked here with ``track_pending_report``, so the orchestrator can let
it finish before the process exits (``wait_for_pending_reports``).

The LLM stack (``instructor`` and the provider SDKs) is an optional extra; it is imported
when the call is made, so the reports module still imports on a capture-only install.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.outbound_policy import apply_outbound_pii
from sales_copilot.core.privacy_gate import PrivacyGateError
from sales_copilot.modules.reports.term_corrections import (
    TERM_CORRECTION_PROMPT,
    TermCorrection,
    TermCorrectionPlan,
    numbered_transcript,
    plan_term_corrections,
    review_term_corrections,
)

if TYPE_CHECKING:
    from sales_copilot.core.llm_routing import ResolvedLLM

logger = logging.getLogger(__name__)

#: The ``core.llm_routing`` task of the five report fields: provider, model, timeout and
#: output limit.
REPORT_TASK = "report"
#: The ``core.llm_routing`` task of the term correction; inherits what it does not set from
#: ``REPORT_TASK``.
TERMS_TASK = "report_terms"

_SPEAKER_LABELS = {"self": "Seller", "prospect": "Prospect"}

_SYSTEM_PROMPT = (
    "You receive the transcript of one phone or video call, captured by the salesperson's "
    "call assistant. Each line starts with who spoke: Seller is the person using the "
    "assistant, Prospect is the other party, Unknown means the speaker could not be "
    "determined.\n\n"
    "First set gesprek_gevoerd. It is true when two people actually talked with each other, "
    "however briefly. It is false when there was no real exchange: a voicemail greeting, a "
    "message left on a voicemail, a network or operator announcement, an automated phone "
    "menu, a line that rang out or was busy, silence, or loose fragments without a "
    "conversation. When you are not sure, choose true.\n\n"
    "Then describe the call using only what the transcript contains. Do not add names, "
    "numbers, agreements or next steps nobody said. Leave a field empty when the call has "
    "nothing for it, which is always the case when gesprek_gevoerd is false.\n\n"
    "Text in square brackets, such as [NAAM], [EMAIL] or [TELEFOON], stands in for personal "
    "data that was removed. Copy it as it is and never guess what it replaced.\n\n"
    "Write every text field in the language the call was held in."
)

# The term correction on a route of its own: the same transcript conventions as the report call,
# and the same term section, without the report fields.
_TERMS_SYSTEM_PROMPT = (
    "You receive the transcript of one phone or video call, captured by the salesperson's "
    "call assistant. Seller is the person using the assistant, Prospect is the other party, "
    "Unknown means the speaker could not be determined.\n\n"
    "Text in square brackets, such as [NAAM], [EMAIL] or [TELEFOON], stands in for personal "
    "data that was removed. Never guess what it replaced." + TERM_CORRECTION_PROMPT
)


class ReportEnrichment(BaseModel):
    """The post-call fields of a report: one judgement, four descriptions."""

    gesprek_gevoerd: bool = Field(
        description="True when two people actually talked; false for voicemail, IVR, no answer or silence."
    )
    short_summary: str = Field(description="One sentence of at most 25 words, or empty.")
    overview: str = Field(
        description=(
            "A few short paragraphs: why the call took place, what the prospect said about their "
            "situation and needs, doubts or objections, and how the call ended. Empty when there "
            "was no conversation."
        )
    )
    keywords: list[str] = Field(description="At most 8 short topics, products or themes that came up.")
    action_items: list[str] = Field(
        description="Follow-up actions that were agreed or promised, each naming who does it when that is known."
    )
    term_corrections: list[TermCorrection] = Field(
        default_factory=list,
        description=(
            "Corrections of candidate terms, only for segments followed by a 'candidates for segment' line; "
            "empty when there is no such line or nothing needs correcting."
        ),
    )
    # Set by the code, never by the model (left out of the schema the model sees): the
    # segments with candidates whose text or candidates the PII policy changed before the
    # model saw them, so a name stripped there could not be corrected.
    term_corrections_pii_limited: SkipJsonSchema[int] = 0

    @classmethod
    def fail_open(cls) -> ReportEnrichment:
        """The result when the step could not run: never hide the call, never invent text."""
        return cls(gesprek_gevoerd=True, short_summary="", overview="", keywords=[], action_items=[])

    def cleaned(self) -> ReportEnrichment:
        """Surrounding whitespace trimmed and blank list entries dropped; nothing added.

        The model's term corrections are not carried over: only ``review_term_corrections``
        decides which of them reach the report.
        """
        return ReportEnrichment(
            gesprek_gevoerd=self.gesprek_gevoerd,
            short_summary=self.short_summary.strip(),
            overview=self.overview.strip(),
            keywords=_clean_items(self.keywords),
            action_items=_clean_items(self.action_items),
        )


class ReportTermCorrections(BaseModel):
    """The answer of the term-correction call when it runs apart from the report call."""

    term_corrections: list[TermCorrection] = Field(
        default_factory=list,
        description=(
            "Corrections of candidate terms, only for segments followed by a 'candidates for segment' line; "
            "empty when nothing needs correcting."
        ),
    )


@dataclasses.dataclass(frozen=True)
class _Part:
    """One part of the enrichment as the log names it, and what its failure leaves out."""

    name: str
    consequence: str


_REPORT_PART = _Part("Report enrichment", "gesprek_gevoerd stays true and the report fields stay empty")
_TERMS_PART = _Part("Report term correction", "the report gets no term corrections")


def _hit_output_limit(exc: BaseException) -> bool:
    """Whether ``exc`` (or what it wraps) is an answer cut off at ``max_tokens``.

    instructor raises ``IncompleteOutputException`` for ``finish_reason='length'``, and wraps it
    in a retry error once its retries are used up, so the cause chain is walked too.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if any(cls.__name__ == "IncompleteOutputException" for cls in type(current).__mro__):
            return True
        choices = getattr(getattr(current, "last_completion", None), "choices", None) or []
        if any(getattr(choice, "finish_reason", None) == "length" for choice in choices):
            return True
        current = current.__cause__ or current.__context__
    return False


def _clean_items(items: Iterable[str]) -> list[str]:
    return [item.strip() for item in items if item and item.strip()]


def _transcript_lines(transcript: Sequence[Any]) -> list[tuple[int, str, str]]:
    """``(index, speaker label, text)`` per segment with text; the index is its place in the report."""
    lines: list[tuple[int, str, str]] = []
    for index, segment in enumerate(transcript):
        text = " ".join((segment.text or "").split())
        if text:
            lines.append((index, _SPEAKER_LABELS.get(segment.speaker, "Unknown"), text))
    return lines


def _transcript_text(lines: Iterable[tuple[int, str, str]]) -> str:
    """One ``<Speaker>: <text>`` line per segment with text, in the report's order."""
    return "\n".join(f"{speaker}: {text}" for _, speaker, text in lines)


async def enrich_report(
    session: Any, detector_config: DetectorConfig | None = None, *, terms: Sequence[str] = ()
) -> ReportEnrichment:
    """Enrich the report of ``session`` through the ``report`` and ``report_terms`` tasks.

    ``session`` is the generator's ``SessionData`` (its ``session_id`` and ``transcript`` are
    read). ``detector_config`` is the conversation's config -- provider, model, temperature
    and privacy ceiling -- and ``DetectorConfig.from_env()`` when omitted. ``terms`` is the
    call's term list (``term_corrections.load_report_terms``); empty means no term correction.

    One call when both tasks resolve to the same provider and model, two when they differ;
    see the module docstring. Never raises for a model or routing problem: each part fails
    open on its own.
    """
    session_id = session.session_id
    transcript = list(session.transcript)
    lines = _transcript_lines(transcript)
    if not lines:
        logger.info("Report enrichment skipped for session=%s: the transcript has no text.", session_id)
        return ReportEnrichment.fail_open()

    try:
        from sales_copilot.core.llm_routing import resolve_llm
    except ImportError as exc:
        logger.warning(
            "Report enrichment skipped for session=%s: the LLM dependencies are not installed (%s); "
            "gesprek_gevoerd stays true and the report fields stay empty.",
            session_id,
            exc,
        )
        return ReportEnrichment.fail_open()

    config = detector_config or DetectorConfig.from_env()
    report_route = _resolve_part(resolve_llm, _REPORT_PART, REPORT_TASK, config, session_id)
    terms_route = _resolve_part(resolve_llm, _TERMS_PART, TERMS_TASK, config, session_id) if terms else None
    plan = await _plan_terms(lines, terms, session_id) if terms_route is not None else None
    if plan is not None and not plan.has_candidates:
        plan = None

    if plan is not None and terms_route is not None and _same_model(report_route, terms_route):
        return await _combined_call(report_route, terms_route, plan, transcript, config, session_id)

    report_call = (
        _call(
            _REPORT_PART,
            report_route,
            system_prompt=_SYSTEM_PROMPT,
            user_text=_transcript_text(lines),
            response_model=ReportEnrichment,
            temperature=config.llm_temperature,
            session_id=session_id,
        )
        if report_route is not None
        else _nothing()
    )
    terms_user_text = numbered_transcript(plan.segments) if plan is not None else ""
    terms_call = (
        _call(
            _TERMS_PART,
            terms_route,
            system_prompt=_TERMS_SYSTEM_PROMPT,
            user_text=terms_user_text,
            response_model=ReportTermCorrections,
            temperature=config.llm_temperature,
            session_id=session_id,
        )
        if plan is not None and terms_route is not None
        else _nothing()
    )
    report_result, terms_result = await asyncio.gather(report_call, terms_call)

    enrichment = (
        report_result.cleaned() if isinstance(report_result, ReportEnrichment) else ReportEnrichment.fail_open()
    )
    if not isinstance(terms_result, ReportTermCorrections) or plan is None or terms_route is None:
        return enrichment
    return _with_term_corrections(
        enrichment, terms_result.term_corrections, plan, transcript, terms_user_text, terms_route.provider, session_id
    )


async def _nothing() -> None:
    """The result of a part that has nothing to call: no route, or no candidates."""
    return None


def _same_model(first: ResolvedLLM | None, second: ResolvedLLM) -> bool:
    """Whether one call serves both parts: the same provider and the same model."""
    return first is not None and (first.provider, first.model) == (second.provider, second.model)


def _resolve_part(
    resolve_llm: Callable[[str, DetectorConfig], ResolvedLLM],
    part: _Part,
    task: str,
    config: DetectorConfig,
    session_id: str,
) -> ResolvedLLM | None:
    """``task``'s route for ``part``, or ``None`` -- with the reason logged -- when it cannot run."""
    try:
        resolved = resolve_llm(task, config)
    except PrivacyGateError as exc:
        logger.warning(
            "%s refused for session=%s by the privacy ceiling: %s -- %s.",
            part.name,
            session_id,
            exc,
            part.consequence,
        )
        return None
    except Exception as exc:  # vnx-silent-except: fail-open, the reason is logged as a WARNING
        logger.warning(
            "%s skipped for session=%s: the %s LLM could not be set up (%s: %s); %s.",
            part.name,
            session_id,
            task,
            type(exc).__name__,
            exc,
            part.consequence,
        )
        return None
    if resolved.provider == "none":
        logger.info("%s switched off for session=%s: the %s provider is 'none'.", part.name, session_id, task)
        return None
    return resolved


async def _combined_call(
    report_route: ResolvedLLM,
    terms_route: ResolvedLLM,
    plan: TermCorrectionPlan,
    transcript: Sequence[Any],
    config: DetectorConfig,
    session_id: str,
) -> ReportEnrichment:
    """Both parts in one call: the same provider and model, the larger timeout and output limit.

    The answer carries the term corrections next to the report fields, so the limit is never
    below what either task allows; the WARNING on a cut-off names the key whose value it was.
    """
    from sales_copilot.core.llm_routing import task_max_output_tokens_key

    terms_limit_wins = terms_route.output_limit() > report_route.output_limit()
    route = dataclasses.replace(
        report_route,
        timeout_ms=max(report_route.timeout_ms, terms_route.timeout_ms),
        max_output_tokens=max(report_route.max_output_tokens, terms_route.max_output_tokens),
    )
    user_text = numbered_transcript(plan.segments)
    result = await _call(
        _REPORT_PART,
        route,
        system_prompt=_SYSTEM_PROMPT + TERM_CORRECTION_PROMPT,
        user_text=user_text,
        response_model=ReportEnrichment,
        temperature=config.llm_temperature,
        session_id=session_id,
        limit_key=task_max_output_tokens_key(TERMS_TASK if terms_limit_wins else REPORT_TASK),
    )
    if not isinstance(result, ReportEnrichment):
        return ReportEnrichment.fail_open()
    return _with_term_corrections(
        result.cleaned(), result.term_corrections, plan, transcript, user_text, route.provider, session_id
    )


async def _call(
    part: _Part,
    route: ResolvedLLM,
    *,
    system_prompt: str,
    user_text: str,
    response_model: type[BaseModel],
    temperature: float | None,
    session_id: str,
    limit_key: str | None = None,
) -> BaseModel | None:
    """One structured call on ``route``; ``None`` -- with the reason logged -- when it fails."""
    from sales_copilot.core.llm_routing import build_llm_client, task_max_output_tokens_key

    try:
        llm = build_llm_client(route)
    except Exception as exc:  # vnx-silent-except: fail-open, the reason is logged as a WARNING
        logger.warning(
            "%s skipped for session=%s: the %s LLM could not be set up (%s: %s); %s.",
            part.name,
            session_id,
            route.task,
            type(exc).__name__,
            exc,
            part.consequence,
        )
        return None

    timeout_s = route.timeout_ms / 1000
    max_tokens = route.output_limit()
    try:
        result = await asyncio.wait_for(
            llm.acreate(
                model=route.model,
                system_prompt=system_prompt,
                user_text=user_text,
                response_model=response_model,
                temperature=temperature,
                allow_local=True,
                max_tokens=max_tokens,
            ),
            timeout=timeout_s,
        )
    except TimeoutError:
        logger.warning(
            "%s timed out after %.1fs for session=%s (provider=%s); %s.",
            part.name,
            timeout_s,
            session_id,
            route.provider,
            part.consequence,
        )
        return None
    except PrivacyGateError as exc:
        logger.warning(
            "%s refused for session=%s by the privacy ceiling at call time: %s -- %s.",
            part.name,
            session_id,
            exc,
            part.consequence,
        )
        return None
    except Exception as exc:  # vnx-silent-except: fail-open, the traceback is logged as a WARNING
        if _hit_output_limit(exc):
            logger.warning(
                "%s for session=%s (provider=%s) was cut off at the output limit of %d tokens; "
                "raise %s if the answer needs more room; %s.",
                part.name,
                session_id,
                route.provider,
                max_tokens,
                limit_key or task_max_output_tokens_key(route.task),
                part.consequence,
            )
            return None
        logger.warning(
            "%s failed for session=%s (provider=%s); %s.",
            part.name,
            session_id,
            route.provider,
            part.consequence,
            exc_info=True,
        )
        return None

    if not isinstance(result, response_model):
        logger.warning(
            "%s returned no result for session=%s (provider=%s); %s.",
            part.name,
            session_id,
            route.provider,
            part.consequence,
        )
        return None
    return result


async def _plan_terms(
    lines: Sequence[tuple[int, str, str]], terms: Sequence[str], session_id: str
) -> TermCorrectionPlan | None:
    """The prefilter over the whole transcript, off the event loop; ``None`` when it fails.

    A failing prefilter costs the term correction only, never the rest of the enrichment.
    """
    try:
        return await asyncio.to_thread(plan_term_corrections, lines, terms)
    except Exception:  # vnx-silent-except: fail-open, the traceback is logged as a WARNING
        logger.warning(
            "Report term prefilter failed for session=%s; the report gets no term corrections.",
            session_id,
            exc_info=True,
        )
        return None


def _with_term_corrections(
    enrichment: ReportEnrichment,
    corrections: Sequence[TermCorrection],
    plan: TermCorrectionPlan,
    transcript: Sequence[Any],
    user_text: str,
    provider: str,
    session_id: str,
) -> ReportEnrichment:
    """``enrichment`` with the model's term corrections that survive validation.

    What the model saw is rebuilt with the same ``apply_outbound_pii`` call ``LLMClient``
    made on ``user_text``, for the same provider. Positions are checked on the report's
    original segment texts. The log names counts and fixed refusal categories, never text.
    A review that fails costs the term corrections only, never the report fields.
    """
    try:
        visible_text = apply_outbound_pii(user_text, provider=provider, allow_local=True)
        review = review_term_corrections(
            corrections, plan, [segment.text or "" for segment in transcript], visible_text
        )
    except Exception:  # vnx-silent-except: fail-open, the traceback is logged as a WARNING
        logger.warning(
            "Report term correction review failed for session=%s; the report gets no term corrections.",
            session_id,
            exc_info=True,
        )
        return enrichment
    logger.info(
        "Report term corrections for session=%s: %d accepted, refused %s, %d segment(s) with candidates "
        "changed by the PII policy.",
        session_id,
        len(review.accepted),
        dict(sorted(review.rejected.items())),
        review.pii_limited,
    )
    return enrichment.model_copy(
        update={"term_corrections": list(review.accepted), "term_corrections_pii_limited": review.pii_limited}
    )


# Post-call report work that outlives the reports task: the enrichment, the rewrite of the
# local copy and the delivery. Held here so a task is never garbage-collected mid-flight and
# so the orchestrator can wait for it at shutdown.
_pending_reports: set[asyncio.Task[Any]] = set()


def track_pending_report(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    """Keep ``task`` until it is done, so ``wait_for_pending_reports`` can wait for it."""
    _pending_reports.add(task)
    task.add_done_callback(_pending_reports.discard)
    return task


async def wait_for_pending_reports() -> None:
    """Wait until every tracked report task on the running loop has finished.

    Each task catches and logs its own failures, so this only waits; it never raises one.
    """
    loop = asyncio.get_running_loop()
    while True:
        pending = [task for task in _pending_reports if task.get_loop() is loop and not task.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
