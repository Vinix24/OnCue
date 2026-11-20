#!/usr/bin/env python3
"""Browser recorder UI: a REC button, live dB meters and an automatic transcript.

Serves a single vanilla HTML page that drives the shared RecorderEngine over a
websocket: click REC to start capturing both tracks (prospect = all system audio
via AudioTee, self = your mic) to data/sessions/<timestamp>/, watch the two
meters move so you know it is running, click again to stop and finalize. After
Stop the session is transcribed in the background with the backend that .env
selects (whisper.cpp by default) and written to <session>/transcript.md.

State is shared: every connected page receives the levels and the phase
(recording / transcribing / ready / failed), so a refreshed page picks up where
the recording or transcription is.

Localhost only.

Usage:
    .venv/bin/python scripts/record_server.py            # http://127.0.0.1:8780
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as _dt
import ipaddress
import json
import logging
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from sales_copilot.audio.capture import (  # noqa: E402
    get_default_input_device,
    list_audio_devices,
)
from sales_copilot.audio.recorder_engine import EngineSnapshot, RecorderEngine  # noqa: E402
from sales_copilot.core import i18n, route_runtime, update_check  # noqa: E402
from sales_copilot.core.config import AutoStopConfig  # noqa: E402
from sales_copilot.core.logging import configure_logging  # noqa: E402

logger = logging.getLogger("sales_copilot.record_server")

AUDIOTEE_PATH = str(REPO_ROOT / "bin" / "audiotee")
# The notetaker stops by itself after this long on both tracks (RECORDER_AUTOSTOP_SILENCE_SECONDS overrides; 0 = off).
# 120 s: measured, longer than the quiet stretches inside calls on both tracks, short enough to save hours.
NOTETAKER_AUTOSTOP_SECONDS = 120.0
HTML_PATH = REPO_ROOT / "dashboard" / "recorder.html"
LIBRARY_HTML_PATH = REPO_ROOT / "dashboard" / "library.html"
BAR_HTML_PATH = REPO_ROOT / "dashboard" / "bar.html"
I18N_JS_PATH = REPO_ROOT / "dashboard" / "js" / "i18n.js"
ONCUE_MARK_PATH = REPO_ROOT / "dashboard" / "assets" / "oncue-mark.svg"
ONCUE_LOADER_CSS_PATH = REPO_ROOT / "dashboard" / "css" / "oncue-loader.css"
OUT_DIR = str(REPO_ROOT / "data" / "sessions")
TRANSCRIPT_NAME = "transcript.md"
PENDING_MARKER = ".transcribe-pending"
RECORDING_MARKER = ".recording"
TRANSCRIPTION_FAILED_NAME = "transcription-failed.txt"
RECOVERY_FAILED_NAME = "recovery-failed.txt"
# A session without metadata.json whose newest WAV is younger than this may be a
# live recording by another process (a second notetaker instance that is still
# recording in the same directory).
RECOVERY_MIN_AGE_S = 60.0
START_WAIT_S = 10.0

# Notetaker labels: the report's neutral labels (self/mic -> Ik / Me, prospect/system ->
# Gesprekspartner / Other). Kept here so postcall.DEFAULT_LABELS, which other callers use,
# stays untouched.
NOTETAKER_LABELS = {
    language: {"self": own, "mic": own, "prospect": other, "system": other}
    for language, (own, other) in i18n.NEUTRAL_SPEAKER_LABELS.items()
}
_HEADER_TEXT = {
    "nl": {
        "title": "Transcript",
        "date": "Datum",
        "start": "Starttijd",
        "duration": "Duur",
        "empty": "(Geen spraak herkend.)",
        "marker": "Markering",
        "auto_stop": "Opname automatisch gestopt na {seconds} s stilte",
        "tap_lost": "Systeemgeluid weg, de ander is vanaf hier niet opgenomen",
        "tap_reattached": "Systeemgeluid weer opgenomen",
    },
    "en": {
        "title": "Transcript",
        "date": "Date",
        "start": "Start time",
        "duration": "Duration",
        "empty": "(No speech detected.)",
        "marker": "Marker",
        "auto_stop": "Recording stopped automatically after {seconds} s of silence",
        "tap_lost": "System audio lost, the other side is not recorded from here",
        "tap_reattached": "System audio recorded again",
    },
}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SESSION_DIR_FORMAT = "%Y%m%d-%H%M%S"

# Locked to this computer. Browsers apply no CORS to websockets, so any open page
# could otherwise drive the recorder. The Host check stops DNS rebinding (a site
# resolving its own name to 127.0.0.1); the Origin checks stop cross-site pages.
_ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
_PORT = 8780  # the port this server was started with, set by main()
# Set by the frozen app's launcher: recording waits until the speech model is downloaded.
MODEL_DOWNLOADER = None


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    _recover_sessions(asyncio.get_running_loop())
    route_runtime.write_runtime_file(route_runtime.NOTETAKER, _PORT)
    try:
        yield
        await _shutdown()
    finally:
        route_runtime.remove_runtime_file(route_runtime.NOTETAKER)


app = FastAPI(lifespan=_lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)

# Everything below is shared between websocket handlers and the transcription
# worker thread. Mutations happen under _lock and never across an await.
_lock = threading.Lock()
_state: dict = {
    "engine": None,
    "starting": False,
    "broadcaster": None,
    # Set once at shutdown: every websocket action is refused from then on.
    "closing": False,
    # Latest finished transcription: {"session", "status": "ready"|"failed", "reason"}.
    "latest": None,
    "auto_stop_s": None,
}
_clients: set[WebSocket] = set()
_jobs: queue.Queue = queue.Queue()
_worker: dict = {"thread": None}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _sessions_dir() -> Path:
    return Path(OUT_DIR).resolve()


def _language() -> str:
    from sales_copilot.core.config import I18nConfig, load_env

    load_env()
    language = I18nConfig.from_env().language
    return "en" if language == "en" else "nl"


def _msg(key: str, **params: object) -> str:
    """A recorder message from the i18n catalog, in the configured language."""
    return i18n.t(f"recorder.{key}", lang=_language(), **params)


def _build_backend():
    """Backend from the normal env config, exactly like scripts/post_process_call.py."""
    from sales_copilot.core.config import TranscriberConfig, load_env
    from sales_copilot.modules.transcriber.backends import create_backend
    from sales_copilot.modules.transcriber.backends.model_names import resolve_mlx_repo

    load_env()
    cfg = TranscriberConfig.from_env()
    return create_backend(
        {
            "backend": cfg.backend,
            "language": cfg.language,
            "model_repo": resolve_mlx_repo(cfg.model),
            "whisper_cpp_binary": cfg.whisper_cpp_binary,
            "whisper_cpp_model_path": cfg.whisper_cpp_model_path,
            "whisper_cpp_threads": cfg.whisper_cpp_threads,
        }
    )


def _one_line_reason(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    detail = text[0] if text else ""
    reason = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return reason if len(reason) <= 200 else reason[:197] + "..."


def _resolve_session(session_id: str | None) -> Path:
    """Resolve a session id to a directory strictly inside the sessions dir."""
    if not session_id or not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail=_msg("invalid_session"))
    base = _sessions_dir()
    candidate = (base / session_id).resolve()
    if candidate.parent != base or not candidate.is_dir():
        raise HTTPException(status_code=404, detail=_msg("session_not_found"))
    return candidate


def _require_local(request: Request) -> None:
    host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(status_code=403, detail=_msg("local_only"))


def _origin_allowed(origin: str | None) -> bool:
    """No Origin (local script, test client) passes: a browser always sends one."""
    if origin is None:
        return True
    return origin in {f"http://127.0.0.1:{_PORT}", f"http://localhost:{_PORT}"}


def _can_reveal() -> bool:
    return sys.platform == "darwin"


# --------------------------------------------------------------------------- #
# Transcript rendering and the transcription job
# --------------------------------------------------------------------------- #


def _session_header_facts(session_dir: Path) -> tuple[str | None, str | None, int | None]:
    """(date, start time, duration seconds) of a session. Missing parts are None."""
    date_s: str | None = None
    start_s: str | None = None
    try:
        started = _dt.datetime.strptime(session_dir.name, _SESSION_DIR_FORMAT)
        date_s = started.strftime("%Y-%m-%d")
        start_s = started.strftime("%H:%M")
    except ValueError:
        logger.warning("Session dir %s has no timestamp name; header has no date", session_dir.name)
    duration_s: int | None = None
    metadata_path = session_dir / "metadata.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        duration_s = int(round(float(metadata["duration_ms"]) / 1000))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("No usable duration in %s: %s", metadata_path, exc)
    return date_s, start_s, duration_s


def _fmt_duration(seconds: int) -> str:
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def render_transcript_md(session_dir: Path, body: str, language: str) -> str:
    text = _HEADER_TEXT[language]
    date_s, start_s, duration_s = _session_header_facts(session_dir)
    lines = [f"# {text['title']}", ""]
    if date_s:
        lines.append(f"{text['date']}: {date_s}")
    if start_s:
        lines.append(f"{text['start']}: {start_s}")
    if duration_s is not None:
        lines.append(f"{text['duration']}: {_fmt_duration(duration_s)}")
    lines.append("")
    lines.append(body.rstrip("\n") if body.strip() else text["empty"])
    return "\n".join(lines) + "\n"


def _session_markers(session_dir: Path) -> list[float]:
    """Marker times in seconds of recording time; none when the session has none."""
    try:
        metadata = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
        return [float(m["offset_ms"]) / 1000 for m in metadata.get("markers") or []]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.info("No markers read from %s: %s", session_dir, exc)
        return []


def _session_tap_notes(session_dir: Path, language: str) -> list[tuple[float, str]]:
    """``(seconds, text)`` for each lost and reattached tap event; other events add no line."""
    text = _HEADER_TEXT[language]
    try:
        metadata = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
        events = metadata.get("tap_events") or []
        return [
            (float(e["t"]), text["tap_lost" if e["event"] == "lost" else "tap_reattached"])
            for e in events
            if e["event"] in ("lost", "reattached")
        ]
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.info("No tap events read from %s: %s", session_dir, exc)
        return []


def _auto_stop_line(session_dir: Path, language: str) -> str:
    """The transcript line for a recording that silence stopped; empty when it was stopped by hand."""
    try:
        metadata = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
        stop = metadata.get("auto_stop")
        if not stop:
            return ""
        offset_s = float(stop["offset_ms"]) / 1000
        text = _HEADER_TEXT[language]["auto_stop"].format(seconds=int(stop["silence_s"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.info("No auto-stop marker read from %s: %s", session_dir, exc)
        return ""
    return f"[{int(offset_s) // 60:02d}:{int(offset_s) % 60:02d}] ({text})\n"


async def _transcribe_async(session_dir: Path, language: str) -> str:
    from sales_copilot.modules.transcriber.postcall import transcribe_session

    backend = _build_backend()
    try:
        body = await transcribe_session(
            session_dir,
            backend,
            label_map=NOTETAKER_LABELS[language],
            markers=_session_markers(session_dir),
            marker_label=_HEADER_TEXT[language]["marker"],
            notes=_session_tap_notes(session_dir, language),
        )
    finally:
        await backend.stop()
    # The stop is the last event of the recording, so its line closes the transcript.
    return body + _auto_stop_line(session_dir, language)


def transcribe_session_dir(session_dir: Path) -> Path:
    """Transcribe one session (blocking) and write transcript.md atomically."""
    if not list(session_dir.glob("*.wav")):
        raise RuntimeError(_msg("no_recording"))
    language = _language()
    body = asyncio.run(_transcribe_async(session_dir, language))
    target = session_dir / TRANSCRIPT_NAME
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render_transcript_md(session_dir, body, language), encoding="utf-8")
    os.replace(tmp, target)
    return target


def _worker_loop() -> None:
    while True:
        session_dir, loop = _jobs.get()
        _run_job(session_dir, loop)


def _write_marker(session_dir: Path, name: str = PENDING_MARKER) -> None:
    try:
        (session_dir / name).touch()
    except OSError as exc:
        logger.error("Could not write %s in %s: %s", name, session_dir, exc)


def _write_note(path: Path, reason: str) -> None:
    try:
        path.write_text(reason + "\n", encoding="utf-8")
    except OSError as exc:
        logger.error("Could not write %s: %s", path, exc)


def _clear_marker(session_dir: Path, name: str = PENDING_MARKER) -> None:
    try:
        (session_dir / name).unlink(missing_ok=True)
    except OSError as exc:
        logger.error("Could not remove %s in %s: %s", name, session_dir, exc)


def _run_job(session_dir: Path, loop: asyncio.AbstractEventLoop) -> None:
    session_id = session_dir.name
    _publish_from_thread(loop)
    try:
        path = transcribe_session_dir(session_dir)
        result = {"session": session_id, "status": "ready", "reason": None}
        logger.info("Transcript written: %s", path)
    except Exception as exc:  # vnx-silent-except: logged with traceback and reported to the page
        logger.exception("Transcription of %s failed", session_dir)
        reason = _one_line_reason(exc)
        result = {"session": session_id, "status": "failed", "reason": reason}
        _write_note(session_dir / TRANSCRIPTION_FAILED_NAME, reason)
    _clear_marker(session_dir)
    with _lock:
        _state["latest"] = result
    # task_done before publishing: the page must see the finished state, not a
    # job that still counts as pending.
    _jobs.task_done()
    _publish_from_thread(loop)


def _ensure_worker() -> None:
    with _lock:
        thread = _worker["thread"]
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=_worker_loop, name="recorder-transcriber", daemon=True)
        _worker["thread"] = thread
        thread.start()


def _enqueue_transcription(session_dir: Path, loop: asyncio.AbstractEventLoop) -> None:
    _write_marker(session_dir)
    _ensure_worker()
    _jobs.put((session_dir, loop))


# --------------------------------------------------------------------------- #
# Shutdown and recovery
# --------------------------------------------------------------------------- #


async def _shutdown() -> None:
    """Finish a running recording once; sessions waiting in the queue keep their marker."""
    with _lock:
        _state["closing"] = True
    logger.info("Shutdown: closing, websocket actions are refused")
    deadline = time.monotonic() + START_WAIT_S
    while _state["starting"] and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    with _lock:
        engine = _state["engine"]
        _state["engine"] = None
    broadcaster = _state["broadcaster"]
    _state["broadcaster"] = None
    if broadcaster is not None:
        broadcaster.cancel()
    if engine is None:
        logger.info("Shutdown: no recording running")
        return
    try:
        path = await asyncio.to_thread(engine.stop)
    except Exception:
        logger.exception("Shutdown: the recording could not be finished cleanly")
        return
    if path is None or not Path(path).is_dir():
        logger.error("Shutdown: the recording left no session folder")
        return
    session_dir = Path(path)
    _write_marker(session_dir)
    _clear_marker(session_dir, RECORDING_MARKER)
    logger.info("Shutdown: finished recording %s, it waits for transcription at the next start", session_dir.name)


def _repair_wav(path: Path) -> tuple[int, int, int]:
    """Fix the two header size fields to match the data (never the audio). Returns frames, rate, channels."""
    size = path.stat().st_size
    with path.open("r+b") as f:
        riff = f.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
            raise ValueError(f"{path.name} is geen WAV-bestand")
        riff_size = struct.unpack("<I", riff[4:8])[0]
        fmt: tuple[int, int, int] | None = None
        while True:
            head = f.read(8)
            if len(head) < 8:
                raise ValueError(_msg("wav_no_data", name=path.name))
            chunk_id, chunk_size = head[:4], struct.unpack("<I", head[4:])[0]
            if chunk_id == b"data":
                break
            body = f.read(chunk_size + (chunk_size & 1))
            if chunk_id == b"fmt ":
                if len(body) < 16:
                    raise ValueError(_msg("wav_bad_fmt", name=path.name))
                _tag, channels, rate, _byte_rate, align, _bits = struct.unpack("<HHIIHH", body[:16])
                fmt = (channels, rate, align)
        if fmt is None or min(fmt) <= 0:
            raise ValueError(_msg("wav_no_fmt", name=path.name))
        channels, rate, align = fmt
        data_start = f.tell()
        frames = (size - data_start) // align
        data_len = frames * align
        if chunk_size != data_len or riff_size != size - 8:
            logger.warning("Recovery: rewriting the header of %s to match its data", path)
            f.seek(data_start - 4)
            f.write(struct.pack("<I", data_len))
            f.seek(4)
            f.write(struct.pack("<I", size - 8))
    return frames, rate, channels


def _recover_metadata(session_dir: Path, wavs: list[Path]) -> None:
    frames_by_label: dict[str, int] = {}
    rate = channels = 0
    for wav in wavs:
        frames, rate, channels = _repair_wav(wav)
        frames_by_label[wav.stem] = frames
    per_stream = {label: int(round(n * 1000 / rate)) for label, n in frames_by_label.items()}
    metadata = {
        "session_id": session_dir.name,
        "duration_ms": max(per_stream.values(), default=0),
        "duration_ms_per_stream": per_stream,
        "end_padding_ms": {label: 0 for label in per_stream},
        "sample_rate": rate,
        "channels": channels,
        "stream_labels": list(per_stream),
        "recovered": True,
    }
    target = session_dir / "metadata.json"
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def _recover_sessions(loop: asyncio.AbstractEventLoop) -> None:
    """Requeue pending sessions and rebuild sessions a hard stop left without metadata."""
    base = _sessions_dir()
    if not base.is_dir():
        return
    for session_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        if (session_dir / TRANSCRIPT_NAME).exists():
            continue
        if (session_dir / PENDING_MARKER).exists():
            logger.info("Recovery: %s was waiting for transcription, queued again", session_dir.name)
            _enqueue_transcription(session_dir, loop)
            continue
        if not (session_dir / RECORDING_MARKER).exists():
            continue
        wavs = sorted(session_dir.glob("*.wav"))
        names = ("metadata.json", TRANSCRIPTION_FAILED_NAME, RECOVERY_FAILED_NAME)
        # A recovery-failed.txt stops the retry at every start; remove the note to retry at the next start.
        if not wavs or any((session_dir / n).exists() for n in names):
            continue
        try:
            age_s = time.time() - max(w.stat().st_mtime for w in wavs)
            if age_s <= RECOVERY_MIN_AGE_S:
                logger.info("Recovery: skipped %s, audio changed %.0f s ago (live recording?)", session_dir.name, age_s)
                continue
            _recover_metadata(session_dir, wavs)
        except (OSError, ValueError) as exc:
            logger.error("Recovery of %s failed: %s", session_dir.name, exc)
            _write_note(session_dir / RECOVERY_FAILED_NAME, _one_line_reason(exc))
            continue
        logger.info("Recovery: rebuilt metadata.json for %s, queued", session_dir.name)
        _clear_marker(session_dir, RECORDING_MARKER)
        _enqueue_transcription(session_dir, loop)


# --------------------------------------------------------------------------- #
# State and broadcast
# --------------------------------------------------------------------------- #


def _snapshot_dict(snap: EngineSnapshot) -> dict:
    return {
        "type": "level",
        "recording": snap.recording,
        "elapsed_s": round(snap.elapsed_s, 1),
        "directory": snap.directory,
        "tap": {"state": snap.tap_state, "attempts": snap.tap_attempts},
        "streams": [
            {
                "label": s.label,
                "db": None if s.display_db <= -120.0 else round(s.display_db, 1),
                "peak_db": None if s.peak_db <= -120.0 else round(s.peak_db, 1),
                "silent": s.silent,
                "started": s.started,
                "error": s.error,
            }
            for s in snap.streams
        ],
    }


def _state_message() -> dict:
    with _lock:
        engine = _state["engine"]
        latest = _state["latest"]
        auto_stop_s = _state["auto_stop_s"]
        # unfinished_tasks counts a job from put() until task_done(), so there is
        # no gap between "queued" and "running" in which a page would see idle.
        pending = _jobs.unfinished_tasks
    elapsed_s = 0.0
    directory = None
    paused = False
    markers: list[dict] = []
    tap = {"state": "ok", "attempts": 0}
    if engine is not None:
        snap = engine.snapshot()
        elapsed_s = round(snap.elapsed_s, 1)
        directory = snap.directory
        paused = snap.paused
        markers = [{"offset_ms": m} for m in engine.markers]
        tap = {"state": snap.tap_state, "attempts": snap.tap_attempts}
    if engine is not None:
        phase = "recording"
    elif pending:
        phase = "transcribing"
    elif latest is not None:
        phase = latest["status"]
    else:
        phase = "idle"
    return {
        "type": "state",
        "phase": phase,
        "elapsed_s": elapsed_s,
        "directory": directory,
        "transcribing": pending,
        "session": latest["session"] if latest else None,
        "reason": latest["reason"] if latest else None,
        "can_reveal": _can_reveal(),
        "paused": paused,
        "markers": markers,
        "tap": tap,
        "auto_stop_s": auto_stop_s if engine is None else None,
    }


async def _broadcast(message: dict) -> None:
    for client in list(_clients):
        try:
            await client.send_json(message)
        except Exception as exc:  # vnx-silent-except: a dead client is dropped, logged
            logger.info("Dropping websocket client after send failure: %s", exc)
            _clients.discard(client)


async def _publish_state() -> None:
    await _broadcast(_state_message())


def _publish_from_thread(loop: asyncio.AbstractEventLoop) -> None:
    def _log_failure(future) -> None:
        exc = future.exception()
        if exc is not None:
            logger.error("Publishing state to clients failed: %s", exc)

    try:
        asyncio.run_coroutine_threadsafe(_publish_state(), loop).add_done_callback(_log_failure)
    except RuntimeError as exc:  # event loop closed (server shutting down)
        logger.warning("Could not publish state, event loop is closed: %s", exc)


async def _broadcast_levels() -> None:
    while True:
        engine = _state["engine"]
        if engine is None:
            return
        snap = engine.snapshot()
        await _broadcast(_snapshot_dict(snap))
        if snap.auto_stop_s is not None:
            # Silence ended the call: finish exactly like the STOP button, from here
            # (the broadcaster is this task, so it must not cancel itself).
            _state["broadcaster"] = None
            await _handle_stop(auto_stop_s=snap.auto_stop_s)
            return
        await asyncio.sleep(0.1)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Library: a read-only view of the sessions dir
# --------------------------------------------------------------------------- #

_AUDIO_GLOB = "*.wav"
_REPORT_NAME_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d_(?P<id>.+)_report\.json$")
_TRANSCRIPT_LINE_RE = re.compile(r"^\[[\d:]+\] ([^:()\[\]]+): ")


def _read_json(path: Path) -> dict | None:
    """A JSON object from ``path``. A missing file is None quietly; a corrupt one is None with a warning."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _reports_index() -> dict[str, Path]:
    """Session id -> newest report file. The link rule lives here and in ``_report_for``:

    ``write_report`` names a report ``<timestamp>_<session_id>_report.json`` in data/reports/
    (next to the sessions dir) and the report carries the same ``session_id``. Both must agree
    with the directory name, exactly; no substring or prefix match.
    """
    index: dict[str, Path] = {}
    try:
        names = sorted(p for p in (_sessions_dir().parent / "reports").iterdir() if p.is_file())
    except FileNotFoundError:
        return index
    except OSError as exc:
        logger.warning("Cannot read the reports dir: %s", exc)
        return index
    for path in names:  # sorted by timestamp prefix, so a later report replaces an earlier one
        match = _REPORT_NAME_RE.match(path.name)
        if match:
            index[match.group("id")] = path
    return index


def _report_for(session_id: str, index: dict[str, Path]) -> dict | None:
    path = index.get(session_id)
    report = _read_json(path) if path else None
    if report is not None and report.get("session_id") != session_id:
        logger.warning("Report %s names another session; not linked", path)
        return None
    return report


def _has_summary(report: dict | None) -> bool:
    return bool(report) and any(str(report.get(key) or "").strip() for key in ("short_summary", "overview"))


def _session_status(session_dir: Path, report: dict | None) -> str | None:
    """The state of a session, derived from its markers and files (never assumed).

    In order: ``recording`` (.recording), ``transcribing`` (.transcribe-pending), ``failed``
    (transcription-failed.txt or recovery-failed.txt), ``ready`` (transcript.md, or a linked
    report), ``incomplete`` (audio but nothing else). None: no transcript, no report and no
    audio, so there is nothing to show and the directory is skipped.
    """
    if (session_dir / RECORDING_MARKER).exists():
        return "recording"
    if (session_dir / PENDING_MARKER).exists():
        return "transcribing"
    if (session_dir / TRANSCRIPTION_FAILED_NAME).exists() or (session_dir / RECOVERY_FAILED_NAME).exists():
        return "failed"
    if (session_dir / TRANSCRIPT_NAME).is_file() or report is not None:
        return "ready"
    if next(session_dir.glob(_AUDIO_GLOB), None) is not None:
        return "incomplete"
    return None


def _started_at(session_dir: Path, report: dict | None) -> _dt.datetime:
    try:
        return _dt.datetime.strptime(session_dir.name, _SESSION_DIR_FORMAT)
    except ValueError:
        pass
    try:
        return _dt.datetime.fromisoformat(str((report or {})["call_started_at"])).astimezone().replace(tzinfo=None)
    except (KeyError, ValueError):
        return _dt.datetime.fromtimestamp(session_dir.stat().st_mtime)


def _duration_s(session_dir: Path, report: dict | None) -> int | None:
    for data, key in ((_read_json(session_dir / "metadata.json"), "duration_ms"), (report, "call_duration_ms")):
        try:
            return int(round(float((data or {})[key]) / 1000))
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _session_summary(session_dir: Path, report: dict | None) -> dict | None:
    """The library row of one session, or None when the directory has nothing to show."""
    status = _session_status(session_dir, report)
    if status is None:
        return None
    started = _started_at(session_dir, report)
    title = str((_read_json(session_dir / "metadata.json") or {}).get("title") or "").strip()
    return {
        "id": session_dir.name,
        "title": title or started.strftime("%Y-%m-%d %H:%M"),
        "date": started.strftime("%Y-%m-%d"),
        "started": started.isoformat(timespec="seconds"),
        "duration_s": _duration_s(session_dir, report),
        "status": status,
        "has_summary": _has_summary(report),
    }


def _guard_local(request: Request) -> None:
    """Same checks as /status: loopback client and no foreign browser origin."""
    _require_local(request)
    if not _origin_allowed(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail=_msg("local_only"))


@app.get("/api/sessions")
def api_sessions(request: Request) -> JSONResponse:
    _guard_local(request)
    index = _reports_index()
    rows = []
    for entry in _sessions_dir().iterdir() if _sessions_dir().is_dir() else []:
        if not entry.is_dir() or entry.is_symlink() or not _SESSION_ID_RE.fullmatch(entry.name):
            continue
        row = _session_summary(entry, _report_for(entry.name, index))
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (r["started"], r["id"]), reverse=True)
    return JSONResponse({"sessions": rows})


@app.get("/api/sessions/{session_id}")
def api_session(request: Request, session_id: str) -> JSONResponse:
    _guard_local(request)
    session_dir = _resolve_session(session_id)
    report = _report_for(session_id, _reports_index())
    row = _session_summary(session_dir, report)
    if row is None:
        raise HTTPException(status_code=404, detail=_msg("session_not_found"))
    transcript_path = session_dir / TRANSCRIPT_NAME
    text = transcript_path.read_text(encoding="utf-8") if transcript_path.is_file() else None
    speakers: list[str] = []
    for line in (text or "").splitlines():
        match = _TRANSCRIPT_LINE_RE.match(line)
        if match and match.group(1) not in speakers:
            speakers.append(match.group(1))
    summary = None
    if _has_summary(report):
        summary = {
            key: report.get(key) or ([] if key not in ("short_summary", "overview") else "")
            for key in ("short_summary", "overview", "keywords", "action_items", "open_questions")
        }
    return JSONResponse({**row, "speakers": speakers, "transcript": text, "summary": summary})


def _page(path: Path) -> HTMLResponse:
    html = path.read_text(encoding="utf-8").replace('<html lang="en">', f'<html lang="{_language()}">', 1)
    return HTMLResponse(html)


@app.get("/")
def index() -> HTMLResponse:
    """The library. It shows the model download progress itself (first start), so no redirect is needed."""
    return _page(LIBRARY_HTML_PATH)


@app.get("/record")
def record_page() -> HTMLResponse:
    return _page(HTML_PATH)


@app.get("/api/config")
def page_config() -> JSONResponse:
    """The language the page follows: the same shape the dashboard reads from the hub."""
    return JSONResponse({"language": _language()})


@app.get("/api/update-check")
def api_update_check(request: Request) -> JSONResponse:
    """One GitHub release lookup, only when the page's button calls it (ADR-006 point 7)."""
    _guard_local(request)
    return JSONResponse(update_check.check_for_updates(lang=_language()))


@app.get("/js/i18n.js")
def i18n_script() -> PlainTextResponse:
    """The dashboard's own i18n loader, so the page uses one mechanism."""
    return PlainTextResponse(I18N_JS_PATH.read_text(encoding="utf-8"), media_type="text/javascript")


@app.get("/assets/oncue-mark.svg")
def oncue_mark() -> Response:
    """The same mark the dashboard serves, used as the notetaker's favicon."""
    return Response(content=ONCUE_MARK_PATH.read_text(encoding="utf-8"), media_type="image/svg+xml")


@app.get("/css/oncue-loader.css")
def oncue_loader_css() -> PlainTextResponse:
    """The shared conducting-mark loading animation, used by the notetaker pages."""
    return PlainTextResponse(ONCUE_LOADER_CSS_PATH.read_text(encoding="utf-8"), media_type="text/css")


@app.get("/i18n/{language}.json")
def i18n_catalog(language: str) -> JSONResponse:
    """The page sections (recorder, library) of a language catalog, in the file layout i18n.js fetches."""
    if language not in ("en", "nl"):
        raise HTTPException(status_code=404, detail=_msg("invalid_request"))
    catalog = i18n.load_catalog(language)
    return JSONResponse({section: catalog.get(section, {}) for section in ("recorder", "library")})


@app.get("/bar")
def bar(request: Request) -> HTMLResponse:
    _require_local(request)
    return HTMLResponse(BAR_HTML_PATH.read_text(encoding="utf-8"))


@app.get("/status")
def status(request: Request) -> JSONResponse:
    _require_local(request)
    if not _origin_allowed(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail=_msg("local_only"))
    state = _state_message()
    return JSONResponse(
        {
            "phase": state["phase"],
            "elapsed_s": state["elapsed_s"],
            "paused": state["paused"],
            "session": state["session"],
            "pid": os.getpid(),
        }
    )


@app.get("/model")
def model_status(request: Request) -> JSONResponse:
    _require_local(request)
    if MODEL_DOWNLOADER is None:
        return JSONResponse({"state": "ready", "done": 0, "total": 0, "error": None})
    state = MODEL_DOWNLOADER.status()
    if state.get("error"):
        # The downloader reports a language-neutral code; the page shows the catalog text.
        state["error"] = _msg(state["error"], detail=state.get("detail", ""))
    return JSONResponse(state)


@app.post("/model/retry")
def model_retry(request: Request) -> JSONResponse:
    _require_local(request)
    if not _origin_allowed(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail=_msg("local_only"))
    if MODEL_DOWNLOADER is not None:
        MODEL_DOWNLOADER.ensure()
    return model_status(request)


@app.get("/devices")
def devices() -> JSONResponse:
    mics = [
        {"index": d["index"], "name": d.get("name", str(d["index"]))}
        for d in list_audio_devices()
        if int(d.get("max_input_channels", 0) or 0) > 0
    ]
    default = get_default_input_device()
    return JSONResponse({"mics": mics, "default_index": default["index"] if default else None})


@app.get("/transcript")
def transcript(request: Request, session: str | None = None) -> PlainTextResponse:
    _require_local(request)
    path = _resolve_session(session) / TRANSCRIPT_NAME
    if not path.is_file():
        raise HTTPException(status_code=404, detail=_msg("no_transcript_yet"))
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.post("/reveal")
async def reveal(request: Request) -> JSONResponse:
    _require_local(request)
    if not _origin_allowed(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail=_msg("local_only"))
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail=_msg("content_type_json"))
    if not _can_reveal():
        raise HTTPException(status_code=404, detail=_msg("reveal_macos_only"))
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_msg("invalid_request")) from exc
    session_dir = _resolve_session(body.get("session") if isinstance(body, dict) else None)
    target = session_dir / TRANSCRIPT_NAME
    if not target.is_file():
        target = session_dir
    try:
        await asyncio.to_thread(
            subprocess.run, ["open", "-R", str(target)], check=True, capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.error("open -R %s failed: %s", target, exc)
        raise HTTPException(status_code=500, detail=_msg("reveal_failed_detail", reason=_one_line_reason(exc))) from exc
    return JSONResponse({"revealed": str(target)})


# --------------------------------------------------------------------------- #
# Websocket
# --------------------------------------------------------------------------- #


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    if not _origin_allowed(websocket.headers.get("origin")):
        logger.warning("Refused websocket from origin %s", websocket.headers.get("origin"))
        await websocket.close(code=1008)
        return
    await websocket.accept()
    _clients.add(websocket)
    try:
        await websocket.send_json(_state_message())
        engine = _state["engine"]
        if engine is not None:
            await websocket.send_json(_snapshot_dict(engine.snapshot()))
        while True:
            msg = await websocket.receive_json()
            action = msg.get("action")
            if _state["closing"]:
                logger.warning("Refused websocket action %r: the server is shutting down", action)
                await websocket.send_json({"type": "error", "reason": _msg("shutting_down")})
                continue
            if action == "start":
                await _handle_start(msg)
            elif action == "stop":
                await _handle_stop()
            elif action in _ENGINE_ACTIONS:
                await _handle_engine_action(action)
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)


async def _handle_start(msg: dict) -> None:
    if MODEL_DOWNLOADER is not None and MODEL_DOWNLOADER.status()["state"] != "ready":
        await _broadcast({"type": "error", "reason": _msg("model_downloading_block")})
        return
    with _lock:
        if _state["engine"] is not None or _state["starting"]:
            return
        _state["starting"] = True
    try:
        engine = RecorderEngine(
            out_dir=OUT_DIR,
            mic_device=msg.get("mic"),
            audiotee_path=AUDIOTEE_PATH,
            auto_stop=AutoStopConfig.from_env(default_seconds=NOTETAKER_AUTOSTOP_SECONDS),
        )
        await asyncio.to_thread(engine.start)
    except Exception as exc:
        logger.exception("Recorder failed to start")
        with _lock:
            _state["starting"] = False
        await _broadcast({"type": "error", "reason": _one_line_reason(exc)})
        return
    with _lock:
        _state["engine"] = engine
        _state["starting"] = False
        _state["auto_stop_s"] = None
    if engine.snapshot().directory:
        _write_marker(Path(engine.snapshot().directory), RECORDING_MARKER)
    _state["broadcaster"] = asyncio.create_task(_broadcast_levels())
    await _publish_state()


_ENGINE_ACTIONS = {"pause": "pause", "resume": "resume", "mark": "add_marker"}


async def _handle_engine_action(action: str) -> None:
    engine = _state["engine"]
    if engine is None:
        logger.info("Ignored '%s': no recording in progress", action)
        return
    changed = await asyncio.to_thread(getattr(engine, _ENGINE_ACTIONS[action]))
    if not changed:
        logger.info("Ignored '%s': does not fit the recording state", action)
        return
    await _publish_state()


async def _handle_stop(*, auto_stop_s: int | None = None) -> None:
    with _lock:
        engine = _state["engine"]
        _state["engine"] = None
    broadcaster = _state["broadcaster"]
    _state["broadcaster"] = None
    if broadcaster is not None:
        broadcaster.cancel()
    if engine is None:
        return
    try:
        path = await asyncio.to_thread(engine.stop)
    except Exception as exc:
        logger.exception("Recorder failed to stop cleanly")
        with _lock:
            _state["latest"] = {"session": None, "status": "failed", "reason": _one_line_reason(exc)}
        await _publish_state()
        return
    directory = str(path) if path is not None else None
    with _lock:
        _state["auto_stop_s"] = auto_stop_s
    await _broadcast({"type": "stopped", "directory": directory, "auto_stop_s": auto_stop_s})
    if directory is not None and Path(directory).is_dir():
        _clear_marker(Path(directory), RECORDING_MARKER)
        _enqueue_transcription(Path(directory), asyncio.get_running_loop())
    await _publish_state()


def _default_port() -> int:
    """RECORDER_PORT from .env, 8780 when it is absent or outside 1 to 65535."""
    from sales_copilot.core.config import load_env

    load_env()
    return route_runtime.configured_port(route_runtime.NOTETAKER, os.environ)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=_default_port())
    args = parser.parse_args()
    global _PORT
    _PORT = args.port
    log_path = configure_logging(file_name="recorder.log")
    print(f"Log: {log_path}")
    print(f"Recorder UI: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
