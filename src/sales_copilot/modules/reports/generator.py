from __future__ import annotations

import contextlib
import logging
import os
import stat
import tempfile
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sales_copilot.core.config import ReportDeliveryConfig, env_bool
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.core.pii_filter import redact_pii
from sales_copilot.modules.reports import delivery
from sales_copilot.modules.reports.enrichment import ReportEnrichment

logger = logging.getLogger(__name__)

REPORTS_DIR = resolve_app_path("data/reports")


@dataclass(frozen=True)
class PhaseSegment:
    phase: str
    duration_ms: int


@dataclass(frozen=True)
class MinuteTalkTime:
    minute: int
    self_pct: float
    prospect_pct: float


@dataclass(frozen=True)
class PainPointDetected:
    category: str
    timestamp_ms: int
    case_id: str | None = None


@dataclass(frozen=True)
class TranscriptEntry:
    speaker: str
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class KeyMoment:
    type: str
    timestamp_ms: int
    description: str


@dataclass(frozen=True)
class KeyMomentEntry:
    type: str
    timestamp_ms: int
    description: str


@dataclass(frozen=True)
class InsightEntry:
    """Track 3 (deep insight lane) report row -- one `insight` channel payload."""

    insight_type: str
    text: str
    grounding: str
    speculation: str
    timestamp_ms: int
    question: str | None = None


@dataclass(frozen=True)
class TermCorrectionEntry:
    """One accepted term correction (termenlijst-in-uitwerking D2, ``term_corrections.py``).

    A layer on top of ``full_transcript``, which keeps the original: the
    ``occurrence``-th whole-word ``source`` in segment ``segment_index`` was meant as
    ``target``. Validated in code against the original segment; never a character offset.
    """

    segment_index: int
    source: str
    occurrence: int
    target: str
    reason: str


@dataclass(frozen=True)
class CallReport:
    session_id: str
    call_duration_ms: int
    prospect_name: str | None
    prospect_company: str | None
    context_docs: list[str]
    phase_timeline: list[PhaseSegment]
    per_minute_talk_time: list[MinuteTalkTime]
    pain_points_detected: list[PainPointDetected]
    conversation_summary: str | None
    key_moments: list[KeyMomentEntry]
    monologue_count: int
    total_self_pct: float
    total_prospect_pct: float
    full_transcript: list[TranscriptEntry]
    # Phase 3 Free post-call scorecard (counts + gaps); None on reports
    # generated before Phase 3 or without scorecard data.
    scorecard: dict[str, Any] | None = None
    # Track 3 (deep insight lane, PR-D2): the session's deep insights, in
    # publish order. Empty when the lane is off/not entitled.
    insights: list[InsightEntry] = field(default_factory=list)
    # Wall-clock ISO-8601 UTC timestamps (session.py's SessionData.started_at /
    # .ended_at). None only for reports built outside the normal session
    # lifecycle (e.g. direct construction in tests); the live pipeline always
    # sets both. Added for report-delivery consumers (docs/MODULE4.md,
    # "Report Delivery"): call_duration_ms alone has no wall-clock anchor, so a
    # downstream automation could not tell "when" a call happened.
    call_started_at: str | None = None
    call_ended_at: str | None = None
    # Post-call enrichment (belapp-junkfilter-rapportschema D1, ``enrichment.py``): written
    # after the LLM step, into the local copy that already exists. The defaults are the
    # fail-open result, so a report the step never reached (or could not enrich) reads as
    # "conversation held, nothing to add" -- never as hidden, never with invented text.
    gesprek_gevoerd: bool = True
    short_summary: str = ""
    overview: str = ""
    keywords: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    # Junk decision (D2+D3, ``junk.py``): the model said no conversation took place. The
    # local copy is kept and marked; delivery to the sinks is skipped. ``junk_reason`` is a
    # fixed category plus a count, never transcript text. Reports written before this
    # existed lack both keys and read as not junk.
    junk: bool = False
    junk_reason: str | None = None
    # Term correction (termenlijst-in-uitwerking D2): written with the enrichment. The
    # transcript above stays the original; these say where a list term was meant.
    # ``term_corrections_pii_limited`` counts the segments with candidate terms the PII policy
    # changed before the model saw them, where a stripped name could not be corrected.
    # Reports written before this existed lack both keys and read as "no corrections".
    term_corrections: list[TermCorrectionEntry] = field(default_factory=list)
    term_corrections_pii_limited: int = 0


@dataclass(frozen=True)
class WrittenReport:
    """A report plus its local copy: where it lives and the exact JSON written there.

    ``payload`` is what is on disk (redacted when ``REPORT_REDACT_PII`` is on), so the
    delivery sinks hand over exactly the local copy, never a second, differently-built one.
    """

    report: CallReport
    path: Path
    payload: str


try:  # pragma: no cover - prefer PR-36 definitions when available
    from sales_copilot.modules.reports.session import (  # type: ignore
        PainPointEvent,
        PhaseEvent,
        SessionData,
        SpeechEvent,
        TranscriptSegment,
    )
except Exception:  # pragma: no cover

    @dataclass(frozen=True)
    class SpeechEvent:
        speaker: str
        start_ms: int
        end_ms: int

    @dataclass(frozen=True)
    class PhaseEvent:
        phase: str
        start_ms: int
        end_ms: int

    @dataclass(frozen=True)
    class PainPointEvent:
        category: str
        timestamp_ms: int
        case_id: str | None = None

    @dataclass(frozen=True)
    class InsightEvent:
        insight_type: str
        text: str
        grounding: str
        speculation: str
        timestamp_ms: int
        question: str | None = None

    @dataclass(frozen=True)
    class TranscriptSegment:
        speaker: str
        text: str
        start_ms: int
        end_ms: int

    @dataclass(frozen=True)
    class SessionData:
        session_id: str
        call_start_ms: int
        call_end_ms: int
        prospect_name: str | None
        prospect_company: str | None
        context_docs: list[str]
        speech_events: list[SpeechEvent]
        phase_events: list[PhaseEvent]
        pain_points: list[PainPointEvent]
        monologues: list[Any]
        transcript: list[TranscriptSegment]
        conversation_summary: str | None = None
        key_moments: list[KeyMoment] = field(default_factory=list)
        scorecard: dict[str, Any] | None = None
        insights: list[InsightEvent] = field(default_factory=list)
        started_at: str | None = None
        ended_at: str | None = None


def _ensure_reports_dir() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        REPORTS_DIR.chmod(stat.S_IRWXU)
    except OSError:
        pass


def _bucket_overlap(start_ms: int, end_ms: int, bucket_start: int, bucket_end: int) -> int:
    overlap_start = max(start_ms, bucket_start)
    overlap_end = min(end_ms, bucket_end)
    return max(0, overlap_end - overlap_start)


def _compute_talk_totals(speech_events: list[Any]) -> tuple[int, int]:
    self_ms = 0
    prospect_ms = 0
    for event in speech_events:
        duration = max(0, event.end_ms - event.start_ms)
        if event.speaker == "self":
            self_ms += duration
        elif event.speaker == "prospect":
            prospect_ms += duration
    return self_ms, prospect_ms


def _per_minute_breakdown(call_duration_ms: int, speech_events: list[Any]) -> list[MinuteTalkTime]:
    minutes = max(1, (call_duration_ms + 59999) // 60000)
    breakdown: list[MinuteTalkTime] = []
    for minute in range(minutes):
        bucket_start = minute * 60000
        bucket_end = min(call_duration_ms, bucket_start + 60000)
        self_ms = 0
        prospect_ms = 0
        for event in speech_events:
            overlap = _bucket_overlap(event.start_ms, event.end_ms, bucket_start, bucket_end)
            if event.speaker == "self":
                self_ms += overlap
            elif event.speaker == "prospect":
                prospect_ms += overlap
        total = self_ms + prospect_ms
        if total > 0:
            self_pct = self_ms / total
            prospect_pct = prospect_ms / total
        else:
            self_pct = 0.0
            prospect_pct = 0.0
        breakdown.append(MinuteTalkTime(minute=minute, self_pct=self_pct, prospect_pct=prospect_pct))
    return breakdown


def _phase_timeline(phase_events: list[Any]) -> list[PhaseSegment]:
    timeline: list[PhaseSegment] = []
    for event in phase_events:
        duration = max(0, event.end_ms - event.start_ms)
        timeline.append(PhaseSegment(phase=event.phase, duration_ms=duration))
    return timeline


def _pain_points(pain_points: list[Any]) -> list[PainPointDetected]:
    detected: list[PainPointDetected] = []
    for event in pain_points:
        detected.append(
            PainPointDetected(
                category=event.category,
                timestamp_ms=event.timestamp_ms,
                case_id=getattr(event, "case_id", None),
            )
        )
    return detected


def _transcript(transcript: list[Any]) -> list[TranscriptEntry]:
    entries: list[TranscriptEntry] = []
    for segment in transcript:
        entries.append(
            TranscriptEntry(
                speaker=segment.speaker,
                text=segment.text,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
            )
        )
    return entries


def _key_moments(key_moments: list[Any]) -> list[KeyMomentEntry]:
    entries: list[KeyMomentEntry] = []
    for moment in key_moments:
        kind = getattr(moment, "type", None)
        timestamp_ms = getattr(moment, "timestamp_ms", None)
        description = getattr(moment, "description", None)
        if not isinstance(kind, str) or not isinstance(timestamp_ms, (int, float)):
            continue
        if not isinstance(description, str):
            description = ""
        entries.append(
            KeyMomentEntry(
                type=kind,
                timestamp_ms=int(timestamp_ms),
                description=description,
            )
        )
    return entries


def _insights(insights: list[Any]) -> list[InsightEntry]:
    entries: list[InsightEntry] = []
    for event in insights:
        entries.append(
            InsightEntry(
                insight_type=event.insight_type,
                text=event.text,
                grounding=event.grounding,
                speculation=event.speculation,
                timestamp_ms=event.timestamp_ms,
                question=getattr(event, "question", None),
            )
        )
    return entries


def _redact_field(value: str | None) -> str | None:
    """Redact PII in a single optional string field, preserving None."""
    if not value:
        return value
    return redact_pii(value)[0]


def _redact_report(report: CallReport) -> CallReport:
    """Return a copy of the report with PII redacted in the on-disk artifact.

    Redacts the prospect name/company, every transcript segment's text, the insights and
    the post-call enrichment text (summary, overview, keywords, action items, and each term
    correction's source, target and reason) via ``redact_pii``. The enrichment fields are
    redacted whatever provider wrote them: a local or trusted-tenant model receives the
    transcript raw (``apply_outbound_pii``), so its output can carry the same PII. Only the
    persisted file is affected; the returned in-memory report (used for the operator's own
    live coaching payload) stays verbatim.
    """
    redacted_transcript = [
        replace(entry, text=redact_pii(entry.text)[0]) for entry in report.full_transcript
    ]
    redacted_insights = [
        replace(
            entry,
            text=redact_pii(entry.text)[0],
            grounding=redact_pii(entry.grounding)[0],
            question=_redact_field(entry.question),
        )
        for entry in report.insights
    ]
    return replace(
        report,
        prospect_name=_redact_field(report.prospect_name),
        prospect_company=_redact_field(report.prospect_company),
        insights=redacted_insights,
        full_transcript=redacted_transcript,
        short_summary=redact_pii(report.short_summary)[0],
        overview=redact_pii(report.overview)[0],
        keywords=[redact_pii(keyword)[0] for keyword in report.keywords],
        action_items=[redact_pii(item)[0] for item in report.action_items],
        term_corrections=[
            replace(
                entry,
                source=redact_pii(entry.source)[0],
                target=redact_pii(entry.target)[0],
                reason=redact_pii(entry.reason)[0],
            )
            for entry in report.term_corrections
        ],
    )


def build_report(session: SessionData) -> CallReport:
    """The call report for ``session``, with the post-call enrichment fields at their defaults."""
    call_duration_ms = max(0, session.call_end_ms - session.call_start_ms)
    total_self_ms, total_prospect_ms = _compute_talk_totals(session.speech_events)
    total_ms = total_self_ms + total_prospect_ms
    total_self_pct = total_self_ms / total_ms if total_ms > 0 else 0.0
    total_prospect_pct = total_prospect_ms / total_ms if total_ms > 0 else 0.0

    return CallReport(
        session_id=session.session_id,
        call_duration_ms=call_duration_ms,
        prospect_name=session.prospect_name,
        prospect_company=session.prospect_company,
        context_docs=[Path(doc).name for doc in session.context_docs],
        phase_timeline=_phase_timeline(session.phase_events),
        per_minute_talk_time=_per_minute_breakdown(call_duration_ms, session.speech_events),
        pain_points_detected=_pain_points(session.pain_points),
        conversation_summary=session.conversation_summary,
        key_moments=_key_moments(session.key_moments),
        monologue_count=len(session.monologues),
        total_self_pct=total_self_pct,
        total_prospect_pct=total_prospect_pct,
        full_transcript=_transcript(session.transcript),
        scorecard=getattr(session, "scorecard", None),
        insights=_insights(getattr(session, "insights", [])),
        call_started_at=getattr(session, "started_at", None),
        call_ended_at=getattr(session, "ended_at", None),
    )


def _write_local_copy(path: Path, report: CallReport) -> str:
    """Atomically write ``report`` to ``path``, owner-only, and return the JSON written.

    A temp file in the same directory, fsynced, then ``os.replace``: a reader, a crash or a
    SIGTERM mid-write only ever sees the previous complete file or the new one. ``mkstemp``
    creates the temp file 0600, so the report is owner-only from its first byte.
    """
    # Default OFF: the local 0600 report is the operator's own owner-only record,
    # so unredacted PII is by-design. Set REPORT_REDACT_PII=true for compliance-strict
    # deployments. Consent-gating the write is the separate follow-up (backlog #12).
    disk_report = _redact_report(report) if env_bool("REPORT_REDACT_PII", False) else report
    payload = _to_json(disk_report)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise
    return payload


def write_report(session: SessionData) -> WrittenReport:
    """Build the call report and write its local copy, before any LLM step or delivery.

    The local copy exists from here on: a slow report model, a failed enrichment or a
    SIGTERM during it can no longer cost the operator the report.
    """
    report = build_report(session)
    _ensure_reports_dir()
    filename = f"{datetime.now():%Y-%m-%dT%H-%M-%S}_{session.session_id}_report.json"
    path = REPORTS_DIR / filename
    return WrittenReport(report=report, path=path, payload=_write_local_copy(path, report))


def rewrite_with_enrichment(
    written: WrittenReport,
    enrichment: ReportEnrichment,
    *,
    junk: bool = False,
    junk_reason: str | None = None,
) -> WrittenReport:
    """Put the post-call enrichment fields and the junk decision into the report and rewrite its local copy atomically.

    Same path, same redaction rule (``REPORT_REDACT_PII``) as the first write.
    """
    report = replace(
        written.report,
        gesprek_gevoerd=enrichment.gesprek_gevoerd,
        short_summary=enrichment.short_summary,
        overview=enrichment.overview,
        keywords=list(enrichment.keywords),
        action_items=list(enrichment.action_items),
        junk=junk,
        junk_reason=junk_reason,
        term_corrections=[
            TermCorrectionEntry(
                segment_index=correction.segment_index,
                source=correction.source,
                occurrence=correction.occurrence,
                target=correction.target,
                reason=correction.reason,
            )
            for correction in enrichment.term_corrections
        ],
        term_corrections_pii_limited=enrichment.term_corrections_pii_limited,
    )
    return WrittenReport(report=report, path=written.path, payload=_write_local_copy(written.path, report))


def deliver_report(written: WrittenReport, *, aflevering: str | None = None) -> None:
    """Hand the local copy to the customer-configured sinks (docs/MODULE4.md, "Report Delivery").

    Both sinks default off, so this is a no-op for every install that has not opted in.
    Delivers exactly ``written.payload`` -- the bytes on disk, same redaction state, same
    shape -- never a second, differently-built copy.

    ``aflevering`` is the selected client's ``klant.yaml`` delivery mode (klantmap-als-
    eenheid D2). ``"lokaal"`` opts this call OUT of the global sinks entirely, even when
    they are configured; ``None`` (no client, or no ``aflevering`` set) keeps the global
    behaviour. The local copy is written either way.
    """
    session_id = written.report.session_id
    if aflevering == "lokaal":
        logger.info(
            "Report delivery sinks skipped for session=%s: client aflevering=lokaal",
            session_id,
        )
        return
    delivery.deliver_report_in_background(
        written.payload.encode("utf-8"),
        written.path.name,
        ReportDeliveryConfig.from_env(),
        session_id=session_id,
        local_report_path=written.path,
    )


def _to_json(report: CallReport) -> str:
    data = asdict(report)
    return json_dumps(data)


def json_dumps(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)
