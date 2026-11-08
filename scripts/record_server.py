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
import datetime as _dt
import ipaddress
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse  # noqa: E402
from starlette.middleware.trustedhost import TrustedHostMiddleware  # noqa: E402

from sales_copilot.audio.capture import (  # noqa: E402
    get_default_input_device,
    list_audio_devices,
)
from sales_copilot.audio.recorder_engine import EngineSnapshot, RecorderEngine  # noqa: E402
from sales_copilot.core.logging import configure_logging  # noqa: E402

logger = logging.getLogger("sales_copilot.record_server")

AUDIOTEE_PATH = str(REPO_ROOT / "bin" / "audiotee")
HTML_PATH = REPO_ROOT / "dashboard" / "recorder.html"
OUT_DIR = str(REPO_ROOT / "data" / "sessions")
TRANSCRIPT_NAME = "transcript.md"

# Notetaker labels (self/mic -> you, prospect/system -> the other side). Kept
# here so postcall.DEFAULT_LABELS, which other callers use, stays untouched.
NOTETAKER_LABELS = {
    "nl": {"prospect": "Ander", "self": "Jij", "system": "Ander", "mic": "Jij"},
    "en": {"prospect": "Other", "self": "You", "system": "Other", "mic": "You"},
}
_HEADER_TEXT = {
    "nl": {
        "title": "Transcript",
        "date": "Datum",
        "start": "Starttijd",
        "duration": "Duur",
        "empty": "(Geen spraak herkend.)",
    },
    "en": {
        "title": "Transcript",
        "date": "Date",
        "start": "Start time",
        "duration": "Duration",
        "empty": "(No speech detected.)",
    },
}
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SESSION_DIR_FORMAT = "%Y%m%d-%H%M%S"

# Locked to this computer. Browsers apply no CORS to websockets, so any open page
# could otherwise drive the recorder. The Host check stops DNS rebinding (a site
# resolving its own name to 127.0.0.1); the Origin checks stop cross-site pages.
_ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
_PORT = 8780  # the port this server was started with, set by main()

app = FastAPI()
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_ALLOWED_HOSTS)

# Everything below is shared between websocket handlers and the transcription
# worker thread. Mutations happen under _lock and never across an await.
_lock = threading.Lock()
_state: dict = {
    "engine": None,
    "starting": False,
    "broadcaster": None,
    # Latest finished transcription: {"session", "status": "ready"|"failed", "reason"}.
    "latest": None,
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


def _build_backend():
    """Backend from the normal env config, exactly like scripts/post_process_call.py."""
    from sales_copilot.core.config import TranscriberConfig, load_env
    from sales_copilot.modules.transcriber.backends import create_backend

    load_env()
    cfg = TranscriberConfig.from_env()
    return create_backend({
        "backend": cfg.backend,
        "language": cfg.language,
        "model_repo": f"mlx-community/whisper-{cfg.model}",
        "whisper_cpp_binary": cfg.whisper_cpp_binary,
        "whisper_cpp_model_path": cfg.whisper_cpp_model_path,
        "whisper_cpp_threads": cfg.whisper_cpp_threads,
    })


def _one_line_reason(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    detail = text[0] if text else ""
    reason = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
    return reason if len(reason) <= 200 else reason[:197] + "..."


def _resolve_session(session_id: str | None) -> Path:
    """Resolve a session id to a directory strictly inside the sessions dir."""
    if not session_id or not _SESSION_ID_RE.fullmatch(session_id):
        raise HTTPException(status_code=400, detail="Ongeldige sessie")
    base = _sessions_dir()
    candidate = (base / session_id).resolve()
    if candidate.parent != base or not candidate.is_dir():
        raise HTTPException(status_code=404, detail="Sessie niet gevonden")
    return candidate


def _require_local(request: Request) -> None:
    host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(status_code=403, detail="Alleen lokaal toegankelijk")


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


async def _transcribe_async(session_dir: Path, language: str) -> str:
    from sales_copilot.modules.transcriber.postcall import transcribe_session

    backend = _build_backend()
    try:
        return await transcribe_session(session_dir, backend, label_map=NOTETAKER_LABELS[language])
    finally:
        await backend.stop()


def transcribe_session_dir(session_dir: Path) -> Path:
    """Transcribe one session (blocking) and write transcript.md atomically."""
    if not list(session_dir.glob("*.wav")):
        raise RuntimeError("Geen opname gevonden in deze sessie")
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


def _run_job(session_dir: Path, loop: asyncio.AbstractEventLoop) -> None:
    session_id = session_dir.name
    _publish_from_thread(loop)
    try:
        path = transcribe_session_dir(session_dir)
        result = {"session": session_id, "status": "ready", "reason": None}
        logger.info("Transcript written: %s", path)
    except Exception as exc:  # vnx-silent-except: logged with traceback and reported to the page
        logger.exception("Transcription of %s failed", session_dir)
        result = {"session": session_id, "status": "failed", "reason": _one_line_reason(exc)}
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
    _ensure_worker()
    _jobs.put((session_dir, loop))


# --------------------------------------------------------------------------- #
# State and broadcast
# --------------------------------------------------------------------------- #


def _snapshot_dict(snap: EngineSnapshot) -> dict:
    return {
        "type": "level",
        "recording": snap.recording,
        "elapsed_s": round(snap.elapsed_s, 1),
        "directory": snap.directory,
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
        # unfinished_tasks counts a job from put() until task_done(), so there is
        # no gap between "queued" and "running" in which a page would see idle.
        pending = _jobs.unfinished_tasks
    elapsed_s = 0.0
    directory = None
    if engine is not None:
        snap = engine.snapshot()
        elapsed_s = round(snap.elapsed_s, 1)
        directory = snap.directory
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
        await _broadcast(_snapshot_dict(engine.snapshot()))
        await asyncio.sleep(0.1)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


@app.get("/")
def index() -> HTMLResponse:
    return HTMLResponse(HTML_PATH.read_text(encoding="utf-8"))


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
        raise HTTPException(status_code=404, detail="Nog geen transcript voor deze sessie")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@app.post("/reveal")
async def reveal(request: Request) -> JSONResponse:
    _require_local(request)
    if not _origin_allowed(request.headers.get("origin")):
        raise HTTPException(status_code=403, detail="Alleen lokaal toegankelijk")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type moet application/json zijn")
    if not _can_reveal():
        raise HTTPException(status_code=404, detail="Toon in Finder bestaat alleen op macOS")
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Ongeldige aanvraag") from exc
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
        raise HTTPException(status_code=500, detail=f"Finder openen mislukt: {_one_line_reason(exc)}") from exc
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
            if action == "start":
                await _handle_start(msg)
            elif action == "stop":
                await _handle_stop()
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)


async def _handle_start(msg: dict) -> None:
    with _lock:
        if _state["engine"] is not None or _state["starting"]:
            return
        _state["starting"] = True
    try:
        engine = RecorderEngine(out_dir=OUT_DIR, mic_device=msg.get("mic"), audiotee_path=AUDIOTEE_PATH)
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
    _state["broadcaster"] = asyncio.create_task(_broadcast_levels())
    await _publish_state()


async def _handle_stop() -> None:
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
    await _broadcast({"type": "stopped", "directory": directory})
    if directory is not None and Path(directory).is_dir():
        _enqueue_transcription(Path(directory), asyncio.get_running_loop())
    await _publish_state()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
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
