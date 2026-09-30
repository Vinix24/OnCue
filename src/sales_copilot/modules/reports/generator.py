from __future__ import annotations

import logging
import stat
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sales_copilot.core.config import ReportDeliveryConfig, env_bool
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.core.pii_filter import redact_pii
from sales_copilot.modules.reports import delivery

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

    Redacts the prospect name/company and every transcript segment's text via
    ``redact_pii``. Only the persisted file is affected; the returned in-memory
    report (used for the operator's own live coaching payload) stays verbatim.
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
    )


def generate_report(session: SessionData, *, aflevering: str | None = None) -> CallReport:
    """Build the call report and write it to disk.

    ``aflevering`` is the selected client's ``klant.yaml`` delivery mode (klantmap-als-
    eenheid D2). ``"lokaal"`` means the global ``ReportDeliveryConfig`` sinks (directory /
    endpoint) are skipped entirely for this report, even when they are configured --
    ``None`` (no client, or no ``aflevering`` set) keeps the existing global behaviour.
    """
    call_duration_ms = max(0, session.call_end_ms - session.call_start_ms)
    total_self_ms, total_prospect_ms = _compute_talk_totals(session.speech_events)
    total_ms = total_self_ms + total_prospect_ms
    total_self_pct = total_self_ms / total_ms if total_ms > 0 else 0.0
    total_prospect_pct = total_prospect_ms / total_ms if total_ms > 0 else 0.0

    report = CallReport(
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

    _ensure_reports_dir()
    filename = f"{datetime.now():%Y-%m-%dT%H-%M-%S}_{session.session_id}_report.json"
    output_path = REPORTS_DIR / filename
    # Default OFF: the local 0600 report is the operator's own owner-only record,
    # so unredacted PII is by-design. Set REPORT_REDACT_PII=true for compliance-strict
    # deployments. Consent-gating the write is the separate follow-up (backlog #12).
    disk_report = _redact_report(report) if env_bool("REPORT_REDACT_PII", False) else report
    payload = _to_json(disk_report)
    output_path.write_text(payload, encoding="utf-8")
    output_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    # Customer-configured trigger delivery (docs/MODULE4.md, "Report Delivery"):
    # both sinks default off, so this is a no-op for every install that has not
    # opted in. Delivers exactly the payload just written to disk -- same
    # redaction state, same shape -- never a second, differently-built copy.
    # klantmap-als-eenheid D2: a client with ``aflevering: lokaal`` in klant.yaml opts
    # this call OUT of the global sinks entirely, even when they are configured --
    # the local disk write above already happened unconditionally.
    if aflevering == "lokaal":
        logger.info(
            "Report delivery sinks skipped for session=%s: client aflevering=lokaal",
            session.session_id,
        )
    else:
        delivery_config = ReportDeliveryConfig.from_env()
        delivery.deliver_report_in_background(
            payload.encode("utf-8"),
            filename,
            delivery_config,
            session_id=session.session_id,
            local_report_path=output_path,
        )

    return report


def _to_json(report: CallReport) -> str:
    data = asdict(report)
    return json_dumps(data)


def json_dumps(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)
