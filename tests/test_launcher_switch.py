"""One route at a time in the launcher: the real launcher against a temporary
repo root, with fake `osascript`, `curl` and `open` on PATH and fake servers
standing in for the running route."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.test_launcher_bar import LAUNCH, Env, _alive, _free_port, _wait_dead

OLD_SERVER = """\
import signal, socket, sys, time
port = int(sys.argv[1])
mode = sys.argv[2]
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(1)

def on_term(*_):
    if mode == "ignore":
        return
    s.close()
    if mode == "linger":
        time.sleep(3)
    sys.exit(0)

signal.signal(signal.SIGTERM, on_term)
while True:
    time.sleep(0.1)
"""

CLIENT = """\
import socket, sys, time
connect_port = int(sys.argv[1])
bind_port = int(sys.argv[2])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
if bind_port:
    s.bind(("127.0.0.1", bind_port))
s.connect(("127.0.0.1", connect_port))
print("connected", flush=True)
time.sleep(300)
"""


class SwitchEnv(Env):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.root = root
        self.children: list[subprocess.Popen] = []
        self.choice = root / "choice.txt"
        self.phase = root / "phase.txt"
        self.choice.write_text("Overschakelen")
        osascript = self.fakebin / "osascript"
        osascript.write_text(
            "#!/bin/bash\n"
            f'echo "$@" >> "{self.osascript_log}"\n'
            'if [[ "$*" == *"display dialog"* ]]; then\n'
            f'  [[ -s "{self.choice}" ]] || exit 1\n'
            f'  echo "button returned:$(cat "{self.choice}")"\n'
            "fi\n"
        )
        curl = self.fakebin / "curl"
        curl.write_text(
            "#!/bin/bash\n"
            f'if [[ -f "{self.phase}" ]]; then echo "{{\\"phase\\": \\"$(cat "{self.phase}")\\"}}"; exit 0; fi\n'
            "exit 7\n"
        )
        curl.chmod(0o755)
        (self.repo / ".env").write_text(f"WS_HUB_PORT={self.dash_port}\nLANGUAGE=nl\n")

    def start_old(self, port: int, mode: str = "term", cwd: Path | None = None) -> subprocess.Popen:
        script = self.root / "old_server.py"
        script.write_text(OLD_SERVER)
        proc = subprocess.Popen(
            [sys.executable, str(script), str(port), mode],
            start_new_session=True,
            cwd=cwd or self.repo,
        )
        # Reap in the background: a zombie would still answer kill -0.
        threading.Thread(target=proc.wait, daemon=True).start()
        self.children.append(proc)
        end = time.time() + 10
        while time.time() < end:
            if subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True).returncode == 0:
                return proc
            time.sleep(0.1)
        raise AssertionError("fake server did not start")

    def start_client(self, connect_port: int, cwd: Path, bind_port: int = 0) -> subprocess.Popen:
        """A process from another directory holding a client socket, like a browser tab."""
        script = self.root / "client.py"
        script.write_text(CLIENT)
        proc = subprocess.Popen(
            [sys.executable, str(script), str(connect_port), str(bind_port)],
            start_new_session=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            text=True,
        )
        threading.Thread(target=proc.wait, daemon=True).start()
        self.children.append(proc)
        assert proc.stdout.readline().strip() == "connected"
        return proc

    def write_run_file(self, route: str, pid: int, port: int) -> None:
        run = self.repo / "data" / "run"
        run.mkdir(parents=True, exist_ok=True)
        (run / f"{route}.json").write_text(json.dumps({"pid": pid, "port": port}))

    def dialogs(self) -> list[str]:
        if not self.osascript_log.exists():
            return []
        return [ln for ln in self.osascript_log.read_text().splitlines() if "display dialog" in ln]

    def started(self, name: str) -> bool:
        return (self.pids / f"oncue-launcher-{name}.pid").exists()


@pytest.fixture
def senv(tmp_path):
    e = SwitchEnv(tmp_path)
    yield e
    for proc in e.children:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    for pid_file in e.pids.glob("*.pid"):
        text = pid_file.read_text().strip()
        if text.isdigit() and _alive(int(text)):
            os.kill(int(text), signal.SIGTERM)


def test_switch_without_recording_stops_old_route_and_starts_new(senv):
    old = senv.start_old(senv.dash_port)
    senv.write_run_file("copilot", old.pid, senv.dash_port)
    result = senv.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    (dialog,) = senv.dialogs()
    assert "OnCue draait al als copilot. Overschakelen?" in dialog
    assert "opname" not in dialog
    assert old.wait(timeout=5) == 0
    assert senv.started("record")


def test_dialog_mentions_a_running_recording(senv):
    old = senv.start_old(senv.record_port)
    senv.write_run_file("notetaker", old.pid, senv.record_port)
    senv.phase.write_text("recording")
    result = senv.run(LAUNCH, "dashboard")
    assert result.returncode == 0, result.stderr
    (dialog,) = senv.dialogs()
    assert "OnCue draait al als notulist. Overschakelen?" in dialog
    assert "Er loopt een opname. Die wordt eerst afgerond." in dialog
    assert not _alive(old.pid)


def test_idle_or_unreachable_status_adds_no_recording_line(senv):
    old = senv.start_old(senv.record_port)
    senv.write_run_file("notetaker", old.pid, senv.record_port)
    senv.phase.write_text("idle")
    senv.choice.write_text("Nee")
    assert senv.run(LAUNCH, "dashboard").returncode == 0
    senv.phase.unlink()
    assert senv.run(LAUNCH, "dashboard").returncode == 0
    first, second = senv.dialogs()
    assert "opname" not in first
    assert "Overschakelen?" in second and "opname" not in second


def test_launcher_waits_for_pid_not_only_for_the_port(senv):
    old = senv.start_old(senv.dash_port, "linger")
    senv.write_run_file("copilot", old.pid, senv.dash_port)
    started = time.time()
    result = senv.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    assert time.time() - started >= 2.5
    assert not _alive(old.pid)
    assert senv.started("record")


def test_timeout_aborts_without_starting(senv):
    old = senv.start_old(senv.dash_port, "ignore")
    senv.write_run_file("copilot", old.pid, senv.dash_port)
    started = time.time()
    result = senv.run(LAUNCH, "record", env_extra={"ONCUE_SWITCH_TIMEOUT_S": "2"})
    assert time.time() - started < 6
    assert result.returncode == 1
    assert "De vorige route rondt nog af. Ik start notulist niet." in senv.osascript_log.read_text()
    assert _alive(old.pid)
    assert not senv.started("record")


def test_no_leaves_everything_alone(senv):
    old = senv.start_old(senv.dash_port)
    senv.write_run_file("copilot", old.pid, senv.dash_port)
    senv.choice.write_text("Nee")
    assert senv.run(LAUNCH, "record").returncode == 0
    assert _alive(old.pid)
    assert not senv.started("record")


def test_closed_dialog_counts_as_no(senv):
    old = senv.start_old(senv.dash_port)
    senv.write_run_file("copilot", old.pid, senv.dash_port)
    senv.choice.write_text("")
    assert senv.run(LAUNCH, "record").returncode == 0
    assert _alive(old.pid)
    assert not senv.started("record")


def test_dead_pid_in_run_file_and_no_listener_starts_normally(senv):
    gone = subprocess.Popen(["true"])
    gone.wait()
    assert _wait_dead(gone.pid)
    senv.write_run_file("copilot", gone.pid, senv.dash_port)
    result = senv.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    assert senv.dialogs() == []
    assert senv.started("record")


def test_listener_without_run_file_is_found_by_port(senv):
    old = senv.start_old(senv.dash_port)
    result = senv.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    assert len(senv.dialogs()) == 1
    assert old.wait(timeout=5) == 0
    assert senv.started("record")


def test_unreadable_run_file_falls_back_to_the_port(senv):
    old = senv.start_old(senv.dash_port)
    run = senv.repo / "data" / "run"
    run.mkdir(parents=True)
    (run / "copilot.json").write_text("{not json")
    assert senv.run(LAUNCH, "record").returncode == 0
    assert len(senv.dialogs()) == 1
    assert old.wait(timeout=5) == 0


def test_stopping_the_notetaker_also_stops_the_bar(senv):
    old = senv.start_old(senv.record_port)
    senv.write_run_file("notetaker", old.pid, senv.record_port)
    bar = subprocess.Popen(
        ["bash", "-c", "exec -a /x/OnCueBar sleep 300"], start_new_session=True
    )
    threading.Thread(target=bar.wait, daemon=True).start()
    senv.children.append(bar)
    (senv.pids / "oncue-launcher-bar.pid").write_text(f"{bar.pid}\n")
    assert senv.run(LAUNCH, "dashboard").returncode == 0
    assert _wait_dead(bar.pid)


def test_dry_run_exits_before_the_switch(senv):
    old = senv.start_old(senv.dash_port)
    senv.write_run_file("copilot", old.pid, senv.dash_port)
    result = senv.run(LAUNCH, "record", env_extra={"ONCUE_LAUNCH_DRY_RUN": "1"})
    assert result.returncode == 0, result.stderr
    assert senv.dialogs() == []
    assert _alive(old.pid)


def test_foreign_listener_on_the_other_port_is_left_alone(senv):
    other = senv.root / "elsewhere"
    other.mkdir()
    foreign = senv.start_old(senv.dash_port, cwd=other)
    result = senv.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    assert senv.dialogs() == []
    assert _alive(foreign.pid)
    assert "buiten deze map" in result.stderr
    assert str(senv.dash_port) in result.stderr
    assert senv.started("record")


def test_run_file_pid_that_does_not_own_the_port_stops_nothing(senv):
    other = senv.root / "elsewhere"
    other.mkdir()
    foreign = senv.start_old(senv.dash_port, cwd=other)
    # The test process is alive but owns no listener on that port.
    senv.write_run_file("copilot", os.getpid(), senv.dash_port)
    result = senv.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    assert senv.dialogs() == []
    assert _alive(foreign.pid)
    assert _alive(os.getpid())
    assert "buiten deze map" in result.stderr
    assert senv.started("record")


def test_client_connection_to_the_own_port_listener_does_not_make_it_foreign(senv):
    # The PORT_PIDS lookup of the launched route (`record`, RECORD_PORT) must
    # list only the listener, not the browser tab connected to it.
    other = senv.root / "elsewhere"
    other.mkdir()
    old = senv.start_old(senv.record_port)
    tab = senv.start_client(senv.record_port, other)
    senv.choice.write_text("Herstarten")
    result = senv.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    assert "buiten deze map" not in result.stderr
    assert "andere map" not in result.stderr
    assert len(senv.dialogs()) == 1
    assert old.wait(timeout=5) == 0
    assert _alive(tab.pid)
    assert senv.started("record")


def test_client_socket_on_the_own_port_number_is_not_a_listener(senv):
    # A foreign client socket bound to the launched route's port as its local
    # port, with nothing listening there, must not count as a port owner.
    other = senv.root / "elsewhere"
    other.mkdir()
    remote_port = _free_port()
    senv.start_old(remote_port, cwd=other)
    tab = senv.start_client(remote_port, other, bind_port=senv.record_port)
    result = senv.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    assert "buiten deze map" not in result.stderr
    assert "andere map" not in result.stderr
    assert senv.dialogs() == []
    assert _alive(tab.pid)
    assert senv.started("record")
