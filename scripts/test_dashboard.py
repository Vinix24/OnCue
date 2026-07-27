#!/usr/bin/env python3

from __future__ import annotations

import socket
import threading
import time

import requests
import uvicorn

from sales_copilot.websocket import hub

HOST = "127.0.0.1"
PORT = 8760
BASE_URL = f"http://{HOST}:{PORT}"


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


def _start_hub_if_needed() -> tuple[uvicorn.Server | None, threading.Thread | None]:
    if _is_listening(HOST, PORT):
        return None, None
    config = uvicorn.Config(hub.app, host=HOST, port=PORT, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="dashboard-diagnostic-hub")
    thread.start()
    _wait_for_port(HOST, PORT)
    return server, thread


def main() -> int:
    hub.reset_config_state()
    server, thread = _start_hub_if_needed()
    results: list[bool] = []

    try:
        response = requests.get(f"{BASE_URL}/api/v1/auth/session-token", timeout=3.0)
        token = response.json().get("token", "")
        auth_headers = {"X-Sales-Copilot-Token": token}
        results.append(
            _result(
                response.status_code == 200 and bool(token),
                "GET /api/v1/auth/session-token",
                str(response.status_code),
            )
        )

        response = requests.get(f"{BASE_URL}/dashboard/", timeout=3.0)
        results.append(_result(response.status_code == 200, "GET /dashboard/", str(response.status_code)))

        response = requests.get(f"{BASE_URL}/dashboard/js/setup.js", timeout=3.0)
        body = response.text
        results.append(
            _result(
                response.status_code == 200 and "initStartCall" in body,
                "GET /dashboard/js/setup.js contains initStartCall",
                str(response.status_code),
            )
        )

        response = requests.get(f"{BASE_URL}/api/presets", timeout=3.0)
        presets = response.json()
        results.append(
            _result(
                response.status_code == 200 and isinstance(presets, list),
                "GET /api/presets returns JSON array",
                str(response.status_code),
            )
        )

        response = requests.get(f"{BASE_URL}/api/config", timeout=3.0)
        config_payload = response.json()
        results.append(
            _result(
                response.status_code == 200
                and "ws_host" in config_payload
                and "ws_port" in config_payload,
                "GET /api/config returns ws_host/ws_port",
                str(config_payload),
            )
        )

        response = requests.post(
            f"{BASE_URL}/api/start-call",
            json={"config": {"screen_mode": "single", "modules": {"talk_time": True}}},
            headers=auth_headers,
            timeout=3.0,
        )
        results.append(_result(response.status_code == 200, "POST /api/start-call", str(response.status_code)))
        requests.post(f"{BASE_URL}/api/end-call", headers=auth_headers, timeout=3.0)
        hub.reset_config_state()
    finally:
        if server is not None and thread is not None:
            server.should_exit = True
            thread.join(timeout=2.0)

    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
