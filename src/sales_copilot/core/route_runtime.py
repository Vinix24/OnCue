"""Runtime file per route and the safety net that tells a route whether the
other route runs (ADR-004 decisions 7 and 8).

Each route (the copilot hub and the notetaker) writes ``data/run/<route>.json``
with its pid and port while it runs. ``other_route_running`` reads the file of
the other route, and falls back to the port from ``.env`` when the file is
absent, so a route started by an older build is still seen.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import tempfile
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values

from sales_copilot.core.paths import resolve_app_support

COPILOT = "copilot"
NOTETAKER = "notetaker"
ROUTES = (COPILOT, NOTETAKER)

DEFAULT_PORTS = {COPILOT: 8760, NOTETAKER: 8780}
PORT_ENV_KEYS = {COPILOT: "WS_HUB_PORT", NOTETAKER: "RECORDER_PORT"}

CONNECT_TIMEOUT_S = 0.3


def _check_route(route: str) -> str:
    if route not in ROUTES:
        raise ValueError(f"unknown route {route!r}, expected one of {ROUTES}")
    return route


def runtime_file(route: str, data_dir: Path | None = None) -> Path:
    base = Path(data_dir) if data_dir is not None else resolve_app_support() / "data"
    return base / "run" / f"{_check_route(route)}.json"


def write_runtime_file(route: str, port: int, data_dir: Path | None = None) -> Path:
    """Write ``{"pid", "port"}`` atomically: temp file in the same directory, then replace."""
    path = runtime_file(route, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{route}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"pid": os.getpid(), "port": int(port)}, handle)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return path


def read_runtime_file(route: str, data_dir: Path | None = None) -> dict[str, int] | None:
    """The file content, or ``None`` when it is absent or not a valid pid/port pair."""
    try:
        payload = json.loads(runtime_file(route, data_dir).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    pid, port = payload.get("pid"), payload.get("port")
    for value in (pid, port):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return None
    return {"pid": pid, "port": port}


def remove_runtime_file(route: str, data_dir: Path | None = None) -> bool:
    """Remove the file only when the pid in it is this process."""
    content = read_runtime_file(route, data_dir)
    if content is None or content["pid"] != os.getpid():
        return False
    try:
        runtime_file(route, data_dir).unlink()
    except OSError:
        return False
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _port_answers(port: int, timeout: float) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _env_port(route: str, environ: Mapping[str, str]) -> int:
    try:
        port = int(str(environ.get(PORT_ENV_KEYS[route], "")).strip())
    except ValueError:
        return DEFAULT_PORTS[route]
    return port if 0 < port < 65536 else DEFAULT_PORTS[route]


def configured_port(route: str, environ: Mapping[str, str] | None = None) -> int:
    """The port ``.env`` (or the environment) names for the route, else its default."""
    return _env_port(_check_route(route), _default_environ() if environ is None else environ)


def _default_environ() -> Mapping[str, str]:
    values = {k: v for k, v in dotenv_values(resolve_app_support() / ".env").items() if v is not None}
    return {**values, **os.environ}


def other_route_running(
    route: str,
    *,
    data_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    timeout: float = CONNECT_TIMEOUT_S,
) -> bool:
    """Whether the other route runs.

    A runtime file with a live pid whose port answers means running. A file
    with a dead pid, or one that cannot be parsed, counts as absent. Without a
    file the port from ``.env`` decides: an answering port counts as running.
    """
    other = NOTETAKER if _check_route(route) == COPILOT else COPILOT
    content = read_runtime_file(other, data_dir)
    if content is not None and _pid_alive(content["pid"]):
        return _port_answers(content["port"], timeout)
    return _port_answers(configured_port(other, environ), timeout)
