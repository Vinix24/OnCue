"""Find and stop ``whisper-server`` orphans left behind by a hard-killed parent."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_LIST_TIMEOUT_S = 10.0
_STOP_GRACE_S = 3.0


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    started: str
    command: str
    executable: str | None = None


def list_processes() -> list[ProcInfo]:
    return _list_processes_windows() if _IS_WINDOWS else _list_processes_posix()


def _list_processes_posix() -> list[ProcInfo]:
    output = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,lstart=,command="],
        capture_output=True,
        text=True,
        check=True,
        timeout=_LIST_TIMEOUT_S,
    ).stdout
    procs: list[ProcInfo] = []
    for line in output.splitlines():
        parts = line.split(None, 7)
        # pid ppid + lstart (weekday month day time year = 5 tokens) + command
        if len(parts) < 8 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        procs.append(
            ProcInfo(
                pid=int(parts[0]),
                ppid=int(parts[1]),
                started=" ".join(parts[2:7]),
                command=parts[7],
            )
        )
    return procs


def _list_processes_windows() -> list[ProcInfo]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    output = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=_LIST_TIMEOUT_S,
    ).stdout
    rows = json.loads(output or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    return [
        ProcInfo(
            pid=int(row["ProcessId"]),
            ppid=int(row["ParentProcessId"]),
            started=str(row.get("CreationDate") or ""),
            command=str(row.get("CommandLine") or ""),
            executable=row.get("ExecutablePath"),
        )
        for row in rows
    ]


def _binary_candidates(binary: Path) -> set[str]:
    return {str(binary), str(binary.resolve())}


def _runs_binary(proc: ProcInfo, binary: Path) -> bool:
    if proc.executable:
        return os.path.normcase(os.path.realpath(proc.executable)) == os.path.normcase(os.path.realpath(binary))
    command = proc.command
    return any(command == c or command.startswith(c + " ") for c in _binary_candidates(binary))


def find_orphans(binary: Path, procs: list[ProcInfo]) -> list[ProcInfo]:
    """Processes running exactly ``binary`` whose parent is gone.

    POSIX: parent is init (PPID 1). Windows: the parent PID is not in ``procs``.
    A server with a living parent (a second OnCue instance) is never returned.
    """
    live_pids = {p.pid for p in procs}
    own_pid = os.getpid()
    orphans = []
    for proc in procs:
        if proc.pid == own_pid or not _runs_binary(proc, binary):
            continue
        parent_gone = proc.ppid not in live_pids if _IS_WINDOWS else proc.ppid == 1
        if parent_gone:
            orphans.append(proc)
    return orphans


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _stop_pid(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _STOP_GRACE_S
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def stop_orphans(
    binary: Path,
    *,
    lister: Callable[[], list[ProcInfo]] | None = None,
    stopper: Callable[[int], None] = _stop_pid,
) -> list[ProcInfo]:
    """Stop orphaned servers of ``binary``. Never raises: failure only logs at DEBUG."""
    stopped: list[ProcInfo] = []
    try:
        orphans = find_orphans(binary, (lister or list_processes)())
    except Exception as exc:
        logger.debug("whisper-server orphan detection failed: %s", exc)
        return stopped
    for proc in orphans:
        try:
            stopper(proc.pid)
        except Exception as exc:
            logger.debug("could not stop orphaned whisper-server pid=%s: %s", proc.pid, exc)
            continue
        logger.info("Stopped orphaned whisper-server pid=%s started=%s", proc.pid, proc.started)
        stopped.append(proc)
    return stopped
