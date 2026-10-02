from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import signal
import stat
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import websockets

from sales_copilot.audio.recorder import get_active_recorder
from sales_copilot.auth.feature_policy import get_feature_policy
from sales_copilot.core.config import (
    DetectorConfig,
    InsightConfig,
    ReportDeliveryConfig,
    SlidesConfig,
    TranscriberConfig,
    WebSocketConfig,
    load_env,
)
from sales_copilot.core.context_docs import resolve_client_dir, write_dossier_transcript
from sales_copilot.core.conversion import ConversionCounterStore
from sales_copilot.core.logging import configure_logging
from sales_copilot.core.session_store import SessionStore
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.delivery import validate_report_delivery_config
from sales_copilot.modules.reports.enrichment import (
    ReportEnrichment,
    enrich_report,
    track_pending_report,
    wait_for_pending_reports,
)
from sales_copilot.modules.reports.generator import (
    CallReport,
    InsightEvent,
    PainPointEvent,
    PhaseEvent,
    SessionData,
    SpeechEvent,
    TranscriptSegment,
    WrittenReport,
)
from sales_copilot.modules.reports.junk import JunkVerdict, decide_junk
from sales_copilot.modules.reports.session import SessionData as TrackerSessionData
from sales_copilot.modules.reports.session import SessionTracker
from sales_copilot.modules.reports.term_corrections import load_report_terms
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


def _insights_from_session(session: TrackerSessionData) -> list[InsightEvent]:
    events: list[InsightEvent] = []
    for entry in session.insights:
        insight_type = entry.get("insight_type")
        text = entry.get("text")
        grounding = entry.get("grounding")
        speculation = entry.get("speculation")
        timestamp_ms = entry.get("timestamp_ms")
        if not isinstance(insight_type, str) or not isinstance(text, str):
            continue
        if not isinstance(timestamp_ms, (int, float)):
            continue
        question = entry.get("question")
        events.append(
            InsightEvent(
                insight_type=insight_type,
                text=text,
                grounding=grounding if isinstance(grounding, str) else "",
                speculation=speculation if isinstance(speculation, str) else "",
                timestamp_ms=int(timestamp_ms),
                question=question if isinstance(question, str) else None,
            )
        )
    return events


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
        insights=_insights_from_session(session),
        started_at=session.started_at,
        ended_at=session.ended_at,
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


def _report_payload(report: CallReport, report_path: Path | None) -> dict[str, object]:
    return {
        "type": "report_ready",
        "session_id": report.session_id,
        "call_duration_ms": report.call_duration_ms,
        "pain_point_count": len(report.pain_points_detected),
        "insight_count": len(report.insights),
        "monologue_count": report.monologue_count,
        "report_path": str(report_path) if report_path else None,
        # Phase 3 Free scorecard: rendered by the dashboard when present;
        # hiding the section leaves all data stored (rollback invariant).
        "scorecard": report.scorecard,
    }


def _dossier_transcript_markdown(report: CallReport) -> str:
    """Render a session's transcript as a readable markdown note for the client dossier."""
    lines = [f"# Sessie {report.session_id}"]
    if report.prospect_company:
        lines.append(f"Klant: {report.prospect_company}")
    if report.prospect_name:
        lines.append(f"Contact: {report.prospect_name}")
    lines.append("")
    for entry in report.full_transcript:
        text = entry.text.strip()
        if text:
            lines.append(f"{entry.speaker}: {text}")
    return "\n".join(lines)


def _maybe_save_dossier_transcript(report: CallReport, client_slug: str | None) -> None:
    """Post-call, opt-in: copy this session's transcript into the client's dossier folder.

    Both conditions are required: a client link for this session
    (``client_slug``, the per-session opt-in) AND ``DOSSIER_AUTO_SAVE=true``
    (the global auto-save toggle, default off). Never raises -- a write
    failure here must not affect the call or the already-persisted report.
    """
    if not client_slug or not InsightConfig.from_env().dossier_auto_save:
        return
    try:
        stub = f"{datetime.now():%Y-%m-%dT%H-%M-%S}_{report.session_id}_transcript"
        path = write_dossier_transcript(client_slug, stub, _dossier_transcript_markdown(report))
        logger.info("Dossier transcript saved for client=%s: %s", client_slug, path)
    except Exception:
        logger.warning("Could not save dossier transcript for client=%s", client_slug, exc_info=True)


def _archive_session_to_klantmap(
    report: CallReport, client_slug: str | None, report_path: Path | None
) -> None:
    """Archive a client-linked call into ``<klantmap>/gesprekken/<datum>-<session_id>/``.

    Unlike ``_maybe_save_dossier_transcript`` (opt-in via ``DOSSIER_AUTO_SAVE``),
    this runs whenever a client is linked to the session -- plan klantmap-als-
    eenheid: "Zonder klant gebeurt er niets extra", so a session with no
    ``client_slug`` is a no-op here too, but a linked session is always
    archived. The raw session data (audio, SQLite, the central report file)
    stays where it already lives; this is a readable copy for the client
    folder. Never raises -- an archive failure must not affect the call or the
    already-persisted central report.
    """
    if not client_slug:
        return
    try:
        klant_dir = resolve_client_dir(client_slug, create=True)
        if klant_dir is None:
            return
        archive_dir = klant_dir / "gesprekken" / f"{datetime.now():%Y-%m-%d}-{report.session_id}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_dir.chmod(stat.S_IRWXU)

        transcript_path = archive_dir / "transcript.md"
        transcript_path.write_text(_dossier_transcript_markdown(report), encoding="utf-8")
        transcript_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

        if report_path is not None and report_path.is_file():
            rapport_path = archive_dir / "rapport.json"
            rapport_path.write_bytes(report_path.read_bytes())
            rapport_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

        logger.info(
            "Session %s archived to klantmap for client=%s: %s",
            report.session_id,
            client_slug,
            archive_dir,
        )
    except Exception:
        logger.warning(
            "Could not archive session %s for client=%s", report.session_id, client_slug, exc_info=True
        )


def _enriched_payload(session_id: str, enrichment: ReportEnrichment, verdict: JunkVerdict) -> dict[str, object]:
    """The ``report_enriched`` event: the junk decision and nothing that identifies anyone.

    ``report_ready`` goes out before the enrichment, so the dashboard learns the decision
    from this second event. No transcript text, no names, no summary. Of the term corrections
    only the counts: their source, target and reason stay in the report on disk.
    """
    return {
        "type": "report_enriched",
        "session_id": session_id,
        "gesprek_gevoerd": enrichment.gesprek_gevoerd,
        "junk": verdict.junk,
        "junk_reason": verdict.reason,
        "term_correction_count": len(enrichment.term_corrections),
        "term_corrections_pii_limited": enrichment.term_corrections_pii_limited,
    }


async def _send_report_enriched(coaching_url: str | None, payload: dict[str, object]) -> None:
    if coaching_url is None:
        return
    try:
        async with websockets.connect(coaching_url) as ws:
            await ws.send(json.dumps(payload))
    except Exception:  # vnx-silent-except: the dashboard event is best-effort, the report is already on disk
        logger.warning("Could not send report_enriched for session=%s", payload.get("session_id"), exc_info=True)


def _rewrite_or_keep(written: WrittenReport, enrichment: ReportEnrichment, verdict: JunkVerdict) -> WrittenReport:
    """The report rewritten with its enrichment, or -- when the disk refuses -- the first copy."""
    try:
        return generator.rewrite_with_enrichment(written, enrichment, junk=verdict.junk, junk_reason=verdict.reason)
    except OSError:
        logger.error(
            "Could not rewrite the report for session=%s with its enrichment; the local copy at %s "
            "stays as first written and is delivered as is.",
            written.report.session_id,
            written.path,
            exc_info=True,
        )
        return written


async def _finish_report(
    written: WrittenReport,
    report_session: SessionData,
    *,
    detector_config: DetectorConfig | None,
    aflevering: str | None,
    client_slug: str | None,
    coaching_url: str | None = None,
) -> None:
    """The post-call steps that wait for the report model: enrich, decide, rewrite, deliver, archive.

    Runs as its own task, next to ``report_ready``: the model's minute or more holds up
    neither the dashboard, nor the end of the call, nor the next call. The local copy was
    written before this started. Delivery comes after the rewrite, so the sinks receive the
    enriched report and never a junk one. Junk is decided by the model's ``gesprek_gevoerd``
    (``junk.py``); a junk report stays local (and in the klantmap archive), marked as junk.
    The ``report_enriched`` event tells the dashboard, since ``report_ready`` went out earlier.
    The term list for the term correction (the client's ``klant.yaml`` and ``termenlijst.yaml``
    plus the fixed list) is read here, after the call, off the event loop.
    """
    try:
        terms = await asyncio.to_thread(load_report_terms, client_slug)
        enrichment = await enrich_report(report_session, detector_config, terms=terms)
        verdict = decide_junk(enrichment, written.report.full_transcript)
        written = _rewrite_or_keep(written, enrichment, verdict)
        await _send_report_enriched(coaching_url, _enriched_payload(written.report.session_id, enrichment, verdict))
        if verdict.junk:
            logger.info("Report delivery skipped for session=%s: junk (%s)", written.report.session_id, verdict.reason)
        else:
            generator.deliver_report(written, aflevering=aflevering)
    except Exception:  # vnx-silent-except: logged as ERROR; a detached task has no caller to raise to
        logger.error(
            "Post-call report steps failed for session=%s; the local copy at %s is unaffected.",
            written.report.session_id,
            written.path,
            exc_info=True,
        )
    _archive_session_to_klantmap(written.report, client_slug, written.path)


def _schedule_report_finish(
    written: WrittenReport,
    report_session: SessionData,
    *,
    detector_config: DetectorConfig | None,
    aflevering: str | None,
    client_slug: str | None,
    coaching_url: str | None = None,
) -> asyncio.Task[None]:
    task = asyncio.create_task(
        _finish_report(
            written,
            report_session,
            detector_config=detector_config,
            aflevering=aflevering,
            client_slug=client_slug,
            coaching_url=coaching_url,
        ),
        name=f"report-finish-{written.report.session_id}",
    )
    return track_pending_report(task)


async def main(
    *,
    stop_event: asyncio.Event | None = None,
    register_signals: bool = True,
    prospect_name: str | None = None,
    prospect_company: str | None = None,
    context_docs: list[str] | None = None,
    session_id: str | None = None,
    client_slug: str | None = None,
    aflevering: str | None = None,
    detector_config: DetectorConfig | None = None,
) -> None:
    """Track one call and, when it ends, write, announce and finish its report.

    ``detector_config`` is the conversation's config (provider, model and privacy ceiling)
    the post-call ``report`` LLM task resolves from; ``DetectorConfig.from_env()`` when
    omitted (the module run on its own).
    """
    load_env()
    configure_logging()
    # Fail fast on an unusable delivery destination -- at startup, with the
    # exact path and reason, rather than at the end of the first call.
    validate_report_delivery_config(ReportDeliveryConfig.from_env())
    ws_config = WebSocketConfig.from_env()
    slides_config = SlidesConfig.from_env()
    tracker = SessionTracker(
        ws_config=ws_config,
        db_path=slides_config.case_db_sqlite_path,
        prospect_name=prospect_name,
        prospect_company=prospect_company,
        context_docs=context_docs,
        session_id=session_id,
        client_slug=client_slug,
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
        # The local copy first, without any LLM step: it is on disk before a report model is
        # called, so neither a slow model nor a SIGTERM during the enrichment can cost it.
        # Enrichment, rewrite, delivery and the klantmap archive follow in their own task.
        written = generator.write_report(report_session)
        report = written.report
        coaching_url = channel_ws_url(ws_config, "coaching")
        _schedule_report_finish(
            written,
            report_session,
            detector_config=detector_config,
            aflevering=aflevering,
            client_slug=client_slug,
            coaching_url=coaching_url,
        )
        _maybe_save_dossier_transcript(report, client_slug)

        if (
            conversion_store is not None
            and report.scorecard is not None
            and report.scorecard.get("missing_required")
        ):
            try:
                conversion_store.record_gap_shown(session.session_id)
            except Exception:
                logger.warning("Conversion gap-shown count failed", exc_info=True)

        payload = _report_payload(report, written.path)
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


async def _run_standalone() -> None:
    await main()
    await wait_for_pending_reports()


if __name__ == "__main__":
    asyncio.run(_run_standalone())
