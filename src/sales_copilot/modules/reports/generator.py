from __future__ import annotations

import stat
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from sales_copilot.core.config import env_bool
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.core.pii_filter import redact_pii

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
    return replace(
        report,
        prospect_name=_redact_field(report.prospect_name),
        prospect_company=_redact_field(report.prospect_company),
        full_transcript=redacted_transcript,
    )


def generate_report(session: SessionData) -> CallReport:
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
    )

    _ensure_reports_dir()
    filename = f"{datetime.now():%Y-%m-%dT%H-%M-%S}_{session.session_id}_report.json"
    output_path = REPORTS_DIR / filename
    # Default OFF: the local 0600 report is the operator's own owner-only record,
    # so unredacted PII is by-design. Set REPORT_REDACT_PII=true for compliance-strict
    # deployments. Consent-gating the write is the separate follow-up (backlog #12).
    disk_report = _redact_report(report) if env_bool("REPORT_REDACT_PII", False) else report
    output_path.write_text(_to_json(disk_report), encoding="utf-8")
    output_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return report


def _to_json(report: CallReport) -> str:
    data = asdict(report)
    return json_dumps(data)


def json_dumps(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2)
