from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import signal
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import websockets

from sales_copilot.audio.recorder import get_active_recorder
from sales_copilot.auth.feature_policy import get_feature_policy
from sales_copilot.core.config import SlidesConfig, TranscriberConfig, WebSocketConfig, load_env
from sales_copilot.core.conversion import ConversionCounterStore
from sales_copilot.core.logging import configure_logging
from sales_copilot.core.session_store import SessionStore
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.generator import (
    CallReport,
    PainPointEvent,
    PhaseEvent,
    SessionData,
    SpeechEvent,
    TranscriptSegment,
)
from sales_copilot.modules.reports.session import SessionData as TrackerSessionData
from sales_copilot.modules.reports.session import SessionTracker
from sales_copilot.modules.transcriber.backends import create_backend
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)


def _parse_iso(iso_ts: str) -> datetime:
    return datetime.fromisoformat(iso_ts)


def _call_duration_ms(session: TrackerSessionData) -> int:
    if session.started_at is None:
        return 0
    end_ts = session.ended_at or datetime.now(datetime.UTC).isoformat()
    start = _parse_iso(session.started_at)
    end = _parse_iso(end_ts)
    return max(0, int((end - start).total_seconds() * 1000))


def _speech_events_from_transcript(session: TrackerSessionData) -> list[SpeechEvent]:
    events: list[SpeechEvent] = []
    for entry in session.transcript:
        speaker = entry.get("speaker")
        start_ms = entry.get("start_ms")
        end_ms = entry.get("end_ms")
        if not isinstance(speaker, str):
            continue
        if not isinstance(start_ms, (int, float)) or not isinstance(end_ms, (int, float)):
            continue
        events.append(SpeechEvent(speaker=speaker, start_ms=int(start_ms), end_ms=int(end_ms)))
    return events


def _phase_events_from_transitions(
    session: TrackerSessionData, call_duration_ms: int
) -> list[PhaseEvent]:
    transitions = [
        entry
        for entry in session.phase_transitions
        if isinstance(entry.get("phase"), str) and isinstance(entry.get("timestamp_ms"), (int, float))
    ]
    transitions.sort(key=lambda item: item["timestamp_ms"])
    events: list[PhaseEvent] = []
    for idx, transition in enumerate(transitions):
        start_ms = int(transition["timestamp_ms"])
        end_ms = call_duration_ms
        if idx + 1 < len(transitions):
            end_ms = int(transitions[idx + 1]["timestamp_ms"])
        if end_ms < start_ms:
            end_ms = start_ms
        events.append(PhaseEvent(phase=transition["phase"], start_ms=start_ms, end_ms=end_ms))
    return events


def _pain_points_from_session(session: TrackerSessionData) -> list[PainPointEvent]:
    events: list[PainPointEvent] = []
    for entry in session.pain_points:
        category = entry.get("category")
        timestamp_ms = entry.get("timestamp_ms")
        if not isinstance(category, str) or not isinstance(timestamp_ms, (int, float)):
            continue
        events.append(
            PainPointEvent(
                category=category,
                timestamp_ms=int(timestamp_ms),
                case_id=entry.get("case_id") if isinstance(entry.get("case_id"), str) else None,
            )
        )
    return events


def _transcript_segments(session: TrackerSessionData) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    for entry in session.transcript:
        speaker = entry.get("speaker")
        text = entry.get("text")
        if not isinstance(speaker, str) or not isinstance(text, str):
            continue
        start_ms = entry.get("start_ms")
        end_ms = entry.get("end_ms")
        if not isinstance(start_ms, (int, float)):
            start_ms = 0
        if not isinstance(end_ms, (int, float)):
            end_ms = int(start_ms)
        segments.append(
            TranscriptSegment(
                speaker=speaker,
                text=text,
                start_ms=int(start_ms),
                end_ms=int(end_ms),
            )
        )
    # Sort by audio timestamp so post-call transcripts read chronologically
    # even when self-stream events arrive late under shared-queue priority.
    segments.sort(key=lambda s: (s.start_ms, s.end_ms))
    return segments


def _build_scorecard(session: TrackerSessionData) -> dict[str, object]:
    """Free post-call scorecard (design doc section 9 Phase 3).

    The count and the gap only: script coverage read from the persisted
    checkpoint snapshot plus the objection/opportunity detection counts.
    Deterministic, local, no LLM, no curated remedy text (that is Pro).
    `script_*` are None when no checkpoint exists for the session, so the UI
    omits the script line instead of showing a dishonest 0-of-N.
    """
    coverage = session.script_coverage
    return {
        "script_covered": coverage["covered"] if coverage else None,
        "script_total": coverage["total"] if coverage else None,
        "missing_required": list(coverage["missing_required"]) if coverage else [],
        "objection_count": session.objection_count,
        "objection_responded_count": session.objection_responded_count,
        "opportunity_count": session.opportunity_count,
    }


def _build_generator_session(session: TrackerSessionData) -> SessionData:
    call_duration_ms = _call_duration_ms(session)
    return SessionData(
        session_id=session.session_id,
        call_start_ms=0,
        call_end_ms=call_duration_ms,
        prospect_name=session.prospect_name,
        prospect_company=session.prospect_company,
        context_docs=session.context_docs,
        speech_events=_speech_events_from_transcript(session),
        phase_events=_phase_events_from_transitions(session, call_duration_ms),
        pain_points=_pain_points_from_session(session),
        monologues=[],
        transcript=_transcript_segments(session),
        scorecard=_build_scorecard(session),
    )


async def _batch_transcribe_self_wav(wav_path: Path) -> list[dict[str, object]]:
    """Chunk self.wav into ~30s segments and transcribe each with the configured backend."""
    tcfg = TranscriberConfig.from_env()
    backend = create_backend({"backend": tcfg.backend, "language": tcfg.language})
    entries: list[dict[str, object]] = []
    try:
        await backend.warmup()
        with wave.open(str(wav_path), "rb") as wf:
            sample_rate = wf.getframerate()
            total_frames = wf.getnframes()
            chunk_frames = max(1, int(30 * sample_rate))
            offset = 0
            while offset < total_frames:
                frames_to_read = min(chunk_frames, total_frames - offset)
                wf.setpos(offset)
                raw = wf.readframes(frames_to_read)
                int16 = np.frombuffer(raw, dtype=np.int16)
                audio = int16.astype(np.float32) / 32768.0
                start_ms = int(offset / sample_rate * 1000)
                end_ms = int((offset + frames_to_read) / sample_rate * 1000)
                text = await backend.transcribe(audio)
                if text.strip():
                    entries.append(
                        {
                            "speaker": "self",
                            "text": text.strip(),
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                            "is_final": True,
                        }
                    )
                offset += frames_to_read
    finally:
        await backend.stop()
    return entries


async def _run_self_batch_if_needed(
    session: TrackerSessionData,
    recorder_dir: Path | None,
) -> TrackerSessionData:
    """Run post-call batch transcription on self.wav when no live self transcripts exist."""
    if any(t.get("speaker") == "self" for t in session.transcript):
        return session
    if recorder_dir is None:
        return session
    self_wav = recorder_dir / "self.wav"
    if not self_wav.exists():
        return session
    try:
        batch_entries = await _batch_transcribe_self_wav(self_wav)
        if batch_entries:
            session = dataclasses.replace(
                session,
                transcript=list(session.transcript) + batch_entries,
            )
            logger.info(
                "Post-call self batch: added %d segments from %s",
                len(batch_entries),
                self_wav,
            )
    except Exception:
        logger.warning("Post-call self batch transcription failed", exc_info=True)
    return session


def _latest_report_path(session_id: str | None = None) -> Path | None:
    if not generator.REPORTS_DIR.exists():
        return None
    pattern = f"*_{session_id}_report.json" if session_id else "*_report.json"
    reports = list(generator.REPORTS_DIR.glob(pattern))
    if not reports:
        return None
    return max(reports, key=lambda path: path.stat().st_mtime)


def _report_payload(report: CallReport, report_path: Path | None) -> dict[str, object]:
    return {
        "type": "report_ready",
        "session_id": report.session_id,
        "call_duration_ms": report.call_duration_ms,
        "pain_point_count": len(report.pain_points_detected),
        "monologue_count": report.monologue_count,
        "report_path": str(report_path) if report_path else None,
        # Phase 3 Free scorecard: rendered by the dashboard when present;
        # hiding the section leaves all data stored (rollback invariant).
        "scorecard": report.scorecard,
    }


async def main(
    *,
    stop_event: asyncio.Event | None = None,
    register_signals: bool = True,
    prospect_name: str | None = None,
    prospect_company: str | None = None,
    context_docs: list[str] | None = None,
    session_id: str | None = None,
) -> None:
    load_env()
    configure_logging()
    ws_config = WebSocketConfig.from_env()
    slides_config = SlidesConfig.from_env()
    tracker = SessionTracker(
        ws_config=ws_config,
        db_path=slides_config.case_db_sqlite_path,
        prospect_name=prospect_name,
        prospect_company=prospect_company,
        context_docs=context_docs,
        session_id=session_id,
        session_store=SessionStore(),
    )
    stop_event = stop_event or asyncio.Event()

    # Q3 conversion instrumentation (design doc open question 5; decision
    # 2026-07-23): local-only counts, no nag, no network. A failure here must
    # never break the call, so every counter interaction is guarded.
    conversion_store: ConversionCounterStore | None = None
    try:
        conversion_store = ConversionCounterStore()
        conversion_store.note_tier(get_feature_policy().current_tier())
    except Exception:
        logger.warning("Conversion counter unavailable; continuing without it", exc_info=True)
        conversion_store = None

    def _request_shutdown() -> None:
        if not stop_event.is_set():
            logger.info("Shutdown requested. Stopping...")
            stop_event.set()

    if register_signals:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: _request_shutdown())

    await tracker.start_session()
    logger.info("Reports session started.")

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.25)
    finally:
        session = await tracker.end_session()
        recorder = get_active_recorder()
        session = await _run_self_batch_if_needed(
            session,
            recorder.directory if recorder is not None else None,
        )
        report_session = _build_generator_session(session)
        report = generator.generate_report(report_session)
        report_path = _latest_report_path(session.session_id)

        if (
            conversion_store is not None
            and report.scorecard is not None
            and report.scorecard.get("missing_required")
        ):
            try:
                conversion_store.record_gap_shown(session.session_id)
            except Exception:
                logger.warning("Conversion gap-shown count failed", exc_info=True)

        coaching_url = channel_ws_url(ws_config, "coaching")
        payload = _report_payload(report, report_path)
        if prospect_name is not None:
            payload["prospect_name"] = prospect_name
        if prospect_company is not None:
            payload["prospect_company"] = prospect_company
        try:
            async with websockets.connect(coaching_url) as ws:
                await ws.send(json.dumps(payload))
        except Exception:
            pass

    logger.info("Reports shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
