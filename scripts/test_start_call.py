#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import queue
import socket
import threading
import time
from typing import Any

import requests
import uvicorn
import websockets

import sales_copilot.__main__ as app_main
from sales_copilot.core.config import build_module_configs
from sales_copilot.websocket import hub
from sales_copilot.websocket.hub_auth import authenticated_ws_url, get_hub_token

HOST = "127.0.0.1"
PORT = 8760
BASE_URL = f"http://{HOST}:{PORT}"
WS_BASE = f"ws://{HOST}:{PORT}"


def _result(ok: bool, label: str, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"[{status}] {label}{suffix}")
    return ok


def _is_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, timeout: float = 5.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        if _is_listening(host, port):
            return
        time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for {host}:{port}")


def _start_hub_if_needed() -> tuple[uvicorn.Server | None, threading.Thread | None, bool]:
    if _is_listening(HOST, PORT):
        return None, None, False
    config = uvicorn.Config(hub.app, host=HOST, port=PORT, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="diagnostic-hub")
    thread.start()
    _wait_for_port(HOST, PORT)
    return server, thread, True


async def _idle_module(**kwargs: Any) -> None:
    stop_event = kwargs["stop_event"]
    while not stop_event.is_set():
        await asyncio.sleep(0.1)


async def _mock_talk_time_main(**kwargs: Any) -> None:
    stop_event = kwargs["stop_event"]
    await hub.broadcast(
        "talk-time",
        {
            "type": "talk_time",
            "rolling_self_pct": 0.5,
            "rolling_prospect_pct": 0.5,
            "cumulative_self_pct": 0.5,
            "cumulative_prospect_pct": 0.5,
            "current_monologue_ms": 0,
            "monologue_speaker": None,
            "call_duration_ms": 1000,
            "phase": "discovery",
            "status": "green",
        },
    )
    while not stop_event.is_set():
        await asyncio.sleep(0.1)


async def _mock_transcriber_main(**kwargs: Any) -> None:
    stop_event = kwargs["stop_event"]
    await hub.broadcast(
        "transcript",
        {
            "type": "transcript",
            "text": "diagnostic transcript",
            "speaker": "prospect",
            "start_ms": 0,
            "end_ms": 500,
            "is_final": True,
        },
    )
    while not stop_event.is_set():
        await asyncio.sleep(0.1)


def _start_orchestrator_harness() -> tuple[threading.Event, threading.Thread]:
    stop_flag = threading.Event()

    async def _orchestrate() -> None:
        original_talk = app_main.talk_time_main
        original_transcriber = app_main.transcriber_main
        original_detector = app_main.detector_main
        original_reports = app_main.reports_main
        loop_stop = asyncio.Event()
        try:
            app_main.talk_time_main = _mock_talk_time_main
            app_main.transcriber_main = _mock_transcriber_main
            app_main.detector_main = _idle_module
            app_main.reports_main = _idle_module
            while not stop_flag.is_set():
                payload = await hub.wait_for_start_call(timeout=0.25)
                if payload is None:
                    continue
                call_config = app_main._parse_call_config(payload)
                configs = build_module_configs(call_config)
                await app_main._run_call(loop_stop, call_config, configs)
                hub.reset_config_state()
                break
        finally:
            app_main.talk_time_main = original_talk
            app_main.transcriber_main = original_transcriber
            app_main.detector_main = original_detector
            app_main.reports_main = original_reports

    def _runner() -> None:
        asyncio.run(_orchestrate())

    thread = threading.Thread(target=_runner, daemon=True, name="diagnostic-orchestrator")
    thread.start()
    return stop_flag, thread


def _wait_for_status(expected: str, timeout: float = 5.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = requests.get(f"{BASE_URL}/api/status", timeout=1.0)
            if response.ok and response.json().get("state") == expected:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.05)
    return False


def _listen_once(ws_url: str, result_queue: queue.Queue[tuple[bool, Any]]) -> None:
    async def _capture() -> None:
        try:
            async with websockets.connect(ws_url) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.5)
            result_queue.put((True, json.loads(raw)))
        except Exception as exc:  # pragma: no cover - diagnostic path
            result_queue.put((False, str(exc)))

    asyncio.run(_capture())


def _wait_for_subscribers(channel: str, minimum: int, timeout: float = 3.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if len(hub._subscribers.get(channel, set())) >= minimum:
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    hub.reset_config_state()
    server, thread, started_local = _start_hub_if_needed()
    stop_flag: threading.Event | None = None
    orchestrator_thread: threading.Thread | None = None
    if started_local:
        stop_flag, orchestrator_thread = _start_orchestrator_harness()
    results: list[bool] = []

    talk_queue: queue.Queue[tuple[bool, Any]] = queue.Queue()
    transcript_queue: queue.Queue[tuple[bool, Any]] = queue.Queue()
    talk_listener = threading.Thread(
        target=_listen_once,
        args=(authenticated_ws_url(f"{WS_BASE}/ws/talk-time"), talk_queue),
        daemon=True,
        name="listen-talk-time",
    )
    transcript_listener = threading.Thread(
        target=_listen_once,
        args=(authenticated_ws_url(f"{WS_BASE}/ws/transcript"), transcript_queue),
        daemon=True,
        name="listen-transcript",
    )
    talk_listener.start()
    transcript_listener.start()
    if started_local:
        _wait_for_subscribers("talk-time", 1, timeout=3.0)
        _wait_for_subscribers("transcript", 1, timeout=3.0)

    start_payload = {
        "config": {
            "screen_mode": "single",
            "modules": {
                "talk_time": True,
                "transcript": True,
                "pain_points": False,
                "presentation": False,
                "post_call_report": False,
            }
        }
    }
    auth_headers = {"X-Sales-Copilot-Token": get_hub_token()}

    try:
        response = requests.post(
            f"{BASE_URL}/api/start-call",
            json=start_payload,
            headers=auth_headers,
            timeout=3.0,
        )
        results.append(_result(response.status_code == 200, "POST /api/start-call", str(response.status_code)))
        results.append(_result(_wait_for_status("call_active"), "Hub state becomes call_active"))

        talk_listener.join(timeout=4.0)
        transcript_listener.join(timeout=4.0)

        talk_ok, talk_payload = talk_queue.get_nowait() if not talk_queue.empty() else (False, "no data")
        transcript_ok, transcript_payload = (
            transcript_queue.get_nowait() if not transcript_queue.empty() else (False, "no data")
        )
        results.append(
            _result(
                talk_ok and isinstance(talk_payload, dict) and talk_payload.get("type") == "talk_time",
                "Talk-time data flows on /ws/talk-time",
                str(talk_payload),
            )
        )
        results.append(
            _result(
                transcript_ok
                and isinstance(transcript_payload, dict)
                and transcript_payload.get("type") == "transcript",
                "Transcript data flows on /ws/transcript",
                str(transcript_payload),
            )
        )

        time.sleep(3.0)

        response = requests.post(
            f"{BASE_URL}/api/end-call",
            headers=auth_headers,
            timeout=3.0,
        )
        results.append(_result(response.status_code == 200, "POST /api/end-call", str(response.status_code)))
        results.append(
            _result(
                _wait_for_status("waiting_for_config"),
                "Hub state returns to waiting_for_config",
            )
        )
    finally:
        if stop_flag is not None and orchestrator_thread is not None:
            stop_flag.set()
            orchestrator_thread.join(timeout=2.0)
            hub.reset_config_state()
        if server is not None and thread is not None:
            server.should_exit = True
            thread.join(timeout=2.0)

    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
