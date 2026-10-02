from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from sales_copilot.modules.transcriber import orphans
from sales_copilot.modules.transcriber.backends import whisper_cpp_backend as wcb
from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend
from sales_copilot.modules.transcriber.orphans import ProcInfo, find_orphans, stop_orphans

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX process semantics")

SRC = str(Path(__file__).resolve().parents[1] / "src")

FAKE_SERVER = textwrap.dedent(
    """
    import http.server, sys
    port = int(sys.argv[sys.argv.index("--port") + 1])
    http.server.HTTPServer(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler).serve_forever()
    """
)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def _guard_cmd(script: Path, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "sales_copilot.modules.transcriber.server_guard",
        "--",
        sys.executable,
        str(script),
        "--port",
        str(port),
    ]


def _child_pids_of(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
    return [int(a) for a, b in (ln.split() for ln in out.splitlines() if ln.strip()) if int(b) == pid]


def _wait_children(pid: int, timeout: float = 10.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        kids = _child_pids_of(pid)
        if kids:
            return kids
        time.sleep(0.1)
    return []


# --------------------------------------------------------------------------- #
# Guard: server dies with its parent
# --------------------------------------------------------------------------- #
@posix_only
def test_server_dies_when_parent_is_sigkilled(tmp_path: Path) -> None:
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys, time
            proc = subprocess.Popen({_guard_cmd(script, _free_port())!r}, stdin=subprocess.PIPE)
            print(proc.pid, flush=True)
            time.sleep(600)
            """
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": SRC}
    parent = subprocess.Popen([sys.executable, str(launcher)], stdout=subprocess.PIPE, text=True, env=env)
    try:
        assert parent.stdout is not None
        guard_pid = int(parent.stdout.readline())
        server_pids = _wait_children(guard_pid)
        assert server_pids, "guard never started the fake server"

        parent.kill()
        parent.wait()

        assert _wait_gone(guard_pid), "guard survived its parent"
        for pid in server_pids:
            assert _wait_gone(pid), "server survived its parent"
    finally:
        parent.kill()
        parent.wait()


def test_guard_without_command_fails() -> None:
    from sales_copilot.modules.transcriber import server_guard

    assert server_guard.main([]) == 2


# --------------------------------------------------------------------------- #
# Backend shutdown
# --------------------------------------------------------------------------- #
@posix_only
def test_shutdown_server_stops_guard_and_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", SRC)

    cli = tmp_path / "whisper-cli"
    cli.write_text("#!/bin/sh\n", encoding="utf-8")
    cli.chmod(0o755)
    model = tmp_path / "model.bin"
    model.write_text("m", encoding="utf-8")
    from sales_copilot.core.config import TranscriberConfig

    backend = WhisperCppBackend(
        TranscriberConfig(
            whisper_cpp_binary=str(cli),
            whisper_cpp_server_binary=str(cli),
            whisper_cpp_model_path=str(model),
        ),
        temp_dir=tmp_path / "chunks",
    )
    proc = subprocess.Popen(
        _guard_cmd(script, _free_port()),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    server_pids = _wait_children(proc.pid)
    assert server_pids
    backend._server_proc = proc  # noqa: SLF001
    backend._server_started = True  # noqa: SLF001

    backend._shutdown_server()  # noqa: SLF001

    assert proc.poll() is not None
    for pid in server_pids:
        assert _wait_gone(pid)


def test_start_server_launches_through_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.test_whisper_cpp_server import FakeClient, FakePopen, _make_backend, _ready_get

    captured: list[list[str]] = []

    def popen(cmd, **kwargs):
        captured.append(cmd)
        assert kwargs["stdin"] == subprocess.PIPE
        return FakePopen()

    backend = _make_backend(tmp_path)
    monkeypatch.setattr(wcb.subprocess, "Popen", popen)
    monkeypatch.setattr(wcb.httpx, "Client", FakeClient)
    monkeypatch.setattr(wcb, "stop_orphans", lambda _binary: [])
    _ready_get(monkeypatch)

    assert backend._ensure_server() is True  # noqa: SLF001
    cmd = captured[0]
    assert cmd[:4] == [sys.executable, "-m", "sales_copilot.modules.transcriber.server_guard", "--"]
    assert cmd[4] == str(backend._server_binary)  # noqa: SLF001


def test_orphan_detection_failure_does_not_block_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.test_whisper_cpp_server import FakeClient, FakePopen, _make_backend, _ready_get

    def boom() -> list[ProcInfo]:
        raise OSError("ps missing")

    monkeypatch.setattr(orphans, "list_processes", boom)
    backend = _make_backend(tmp_path)
    monkeypatch.setattr(wcb.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(wcb.httpx, "Client", FakeClient)
    _ready_get(monkeypatch)

    assert backend._ensure_server() is True  # noqa: SLF001


# --------------------------------------------------------------------------- #
# Orphan cleanup
# --------------------------------------------------------------------------- #
BINARY = Path("/opt/oncue/vendor/whisper.cpp/build/bin/whisper-server")
CMD = f"{BINARY} -m model.bin --port 61295"


@pytest.fixture
def posix_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orphans, "_IS_WINDOWS", False)


def test_find_orphans_only_own_binary_with_dead_parent(posix_rules: None) -> None:
    procs = [
        ProcInfo(10, 1, "Sun Sep 27 11:11:00 2026", CMD),  # orphan, own binary
        ProcInfo(11, 500, "Sun Sep 27 11:11:00 2026", CMD),  # living parent
        ProcInfo(500, 1, "Sun Sep 27 11:00:00 2026", "/usr/bin/python oncue"),
        ProcInfo(12, 1, "Sun Sep 27 11:11:00 2026", "/other/place/whisper-server --port 1"),  # other path
        ProcInfo(13, 1, "Sun Sep 27 11:11:00 2026", f"{BINARY}-extra --port 2"),  # prefix, not exact
    ]

    assert [p.pid for p in find_orphans(BINARY, procs)] == [10]


def test_find_orphans_windows_parent_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orphans, "_IS_WINDOWS", True)
    exe = str(BINARY)
    procs = [
        ProcInfo(10, 9999, "20260927111100", CMD, executable=exe),  # parent 9999 does not exist
        ProcInfo(11, 500, "20260927111100", CMD, executable=exe),
        ProcInfo(500, 4, "20260927110000", "oncue", executable="C:/py.exe"),
        ProcInfo(4, 0, "20260927100000", "system"),
    ]

    assert [p.pid for p in find_orphans(BINARY, procs)] == [10]


def test_stop_orphans_stops_only_orphans_and_logs(posix_rules: None, caplog: pytest.LogCaptureFixture) -> None:
    procs = [
        ProcInfo(10, 1, "Sun Sep 27 11:11:00 2026", CMD),
        ProcInfo(11, 500, "Sun Sep 27 11:11:00 2026", CMD),
        ProcInfo(500, 1, "Sun Sep 27 11:00:00 2026", "oncue"),
    ]
    stopped: list[int] = []

    with caplog.at_level("INFO", logger=orphans.logger.name):
        result = stop_orphans(BINARY, lister=lambda: procs, stopper=stopped.append)

    assert stopped == [10]
    assert [p.pid for p in result] == [10]
    assert "pid=10" in caplog.text and "Sun Sep 27 11:11:00 2026" in caplog.text


def test_stop_orphans_survives_listing_and_stopping_failures(posix_rules: None) -> None:
    def boom() -> list[ProcInfo]:
        raise subprocess.TimeoutExpired("ps", 1)

    assert stop_orphans(BINARY, lister=boom) == []

    def bad_stop(_pid: int) -> None:
        raise PermissionError

    procs = [ProcInfo(10, 1, "x", CMD)]
    assert stop_orphans(BINARY, lister=lambda: procs, stopper=bad_stop) == []


@posix_only
def test_real_orphan_is_stopped_and_live_sibling_is_kept(tmp_path: Path) -> None:
    """Real processes: a reparented orphan of our binary path dies, a live-parent one stays."""
    binary = tmp_path / "whisper-server"
    # `exec -a` makes a plain sleep report the fake server path as its command.
    sleeper = ["bash", "-c", f'exec -a "{binary}" sleep 120']

    spawner = tmp_path / "spawner.py"
    spawner.write_text(
        textwrap.dedent(
            f"""
            import subprocess
            p = subprocess.Popen({sleeper!r},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(p.pid, flush=True)
            """
        ),
        encoding="utf-8",
    )
    out = subprocess.run([sys.executable, str(spawner)], capture_output=True, text=True, check=True).stdout
    orphan_pid = int(out.strip())
    live = subprocess.Popen(sleeper)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if any(p.pid == orphan_pid and p.ppid == 1 for p in orphans.list_processes()):
                break
            time.sleep(0.1)

        stopped = stop_orphans(binary)

        assert [p.pid for p in stopped] == [orphan_pid]
        assert _wait_gone(orphan_pid)
        assert live.poll() is None
    finally:
        live.kill()
        live.wait()
        if _alive(orphan_pid):
            os.kill(orphan_pid, 9)
