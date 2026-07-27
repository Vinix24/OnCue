#!/usr/bin/env python3
"""Browser recorder UI: a REC button and live dB meters — no hub, no LLM.

Serves a single vanilla HTML page that drives the shared RecorderEngine over a
websocket: click REC to start capturing both tracks (prospect = all system audio
via AudioTee, self = your mic) to data/sessions/<timestamp>/, watch the two
meters move so you know it is running, click again to stop and finalize.

Localhost only.

Usage:
    .venv/bin/python scripts/record_server.py            # http://127.0.0.1:8780
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from sales_copilot.audio.capture import (  # noqa: E402
    get_default_input_device,
    list_audio_devices,
)
from sales_copilot.audio.recorder_engine import EngineSnapshot, RecorderEngine  # noqa: E402

AUDIOTEE_PATH = str(REPO_ROOT / "bin" / "audiotee")
HTML_PATH = REPO_ROOT / "dashboard" / "recorder.html"
OUT_DIR = str(REPO_ROOT / "data" / "sessions")

app = FastAPI()
_state: dict[str, RecorderEngine | None] = {"engine": None}
_state_lock = asyncio.Lock()


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
            }
            for s in snap.streams
        ],
    }


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


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    pusher: asyncio.Task | None = None
    try:
        while True:
            msg = await websocket.receive_json()
            action = msg.get("action")
            if action == "start":
                pusher = await _handle_start(websocket, msg, pusher)
            elif action == "stop":
                await _handle_stop(websocket, pusher)
                pusher = None
    except WebSocketDisconnect:
        pass
    finally:
        if pusher is not None:
            pusher.cancel()


async def _handle_start(websocket: WebSocket, msg: dict, pusher: asyncio.Task | None) -> asyncio.Task | None:
    async with _state_lock:
        if _state["engine"] is not None:
            return pusher
        mic = msg.get("mic")
        engine = RecorderEngine(out_dir=OUT_DIR, mic_device=mic, audiotee_path=AUDIOTEE_PATH)
        await asyncio.to_thread(engine.start)
        _state["engine"] = engine
    return asyncio.create_task(_push_levels(websocket))


async def _handle_stop(websocket: WebSocket, pusher: asyncio.Task | None) -> None:
    if pusher is not None:
        pusher.cancel()
    async with _state_lock:
        engine = _state["engine"]
        _state["engine"] = None
    directory = None
    if engine is not None:
        path = await asyncio.to_thread(engine.stop)
        directory = str(path) if path is not None else None
    try:
        await websocket.send_json({"type": "stopped", "directory": directory})
    except Exception:  # noqa: BLE001
        pass


async def _push_levels(websocket: WebSocket) -> None:
    try:
        while True:
            engine = _state["engine"]
            if engine is None:
                break
            try:
                await websocket.send_json(_snapshot_dict(engine.snapshot()))
            except Exception:  # noqa: BLE001
                break
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    args = parser.parse_args()
    print(f"Recorder UI: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
