"""The stop script asks before it signals anything, and only when a question is
worth asking. It runs for real from a temporary repo root with temporary pid
files. Every command that could touch real processes or the screen (`pkill`,
`osascript`, `curl`, `rm`) is a fake on PATH; signals only reach processes the
test started itself."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STOP_SOURCE = REPO / "scripts" / "launcher" / "stop-oncue.command"

FAKE_SERVER = "import time\ntime.sleep(300)\n"

FAKE_CURL = """\
#!/bin/bash
if [[ -f "$FAKE_STATUS_FILE" ]]; then
    cat "$FAKE_STATUS_FILE"
    exit 0
fi
exit 7
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class Env:
    def __init__(self, root: Path) -> None:
        self.repo = root / "repo"
        self.pids = root / "pids"
        self.fakebin = root / "fakebin"
        self.status_file = root / "status.json"
        self.calls = root / "calls.log"
        self.procs: list[subprocess.Popen] = []
        launcher = self.repo / "scripts" / "launcher"
        for d in (self.pids, self.fakebin, launcher, self.repo / ".venv" / "bin"):
            d.mkdir(parents=True)
        self.script = launcher / "stop-oncue.command"
        shutil.copy(STOP_SOURCE, self.script)
        (self.repo / "scripts" / "record_server.py").write_text(FAKE_SERVER)
        python = self.repo / ".venv" / "bin" / "python"
        python.write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
        python.chmod(0o755)
        recorder = f'echo "$(basename "$0") $*" >> "{self.calls}"'
        for name in ("pkill", "osascript", "rm"):
            self._fake(name, recorder)
        self._fake("curl", FAKE_CURL.split("\n", 1)[1])

    def _fake(self, name: str, body: str) -> None:
        f = self.fakebin / name
        f.write_text(f"#!/bin/bash\n{body}\n")
        f.chmod(0o755)

    def start(self, argv: list[str]) -> subprocess.Popen:
        proc = subprocess.Popen(argv, cwd=self.repo)
        # Reap on exit, or a finished process stays a zombie and looks alive.
        threading.Thread(target=proc.wait, daemon=True).start()
        self.procs.append(proc)
        return proc

    def record_server(self) -> subprocess.Popen:
        proc = self.start([sys.executable, "scripts/record_server.py"])
        (self.pids / "oncue-launcher-record.pid").write_text(f"{proc.pid}\n")
        return proc

    def answer_status(self, body: str) -> None:
        self.status_file.write_text(body)

    def run(self, stdin: str = "") -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ['PATH']}",
            "ONCUE_PID_DIR": str(self.pids),
            "ONCUE_RECORD_PORT": str(_free_port()),
            "FAKE_STATUS_FILE": str(self.status_file),
        }
        return subprocess.run(
            ["bash", str(self.script)], env=env, input=stdin, capture_output=True, text=True, timeout=90
        )

    def fake_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def pkill_calls(self) -> list[str]:
        return [c for c in self.fake_calls() if c.startswith("pkill ")]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    for proc in e.procs:
        if proc.poll() is None:
            proc.terminate()


def _wait_exit(proc: subprocess.Popen, timeout: float = 10) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return True
        time.sleep(0.1)
    return False


def _status(phase: str, pid: int) -> str:
    return json.dumps({"phase": phase, "pid": pid, "elapsed_s": 65})


def test_nothing_running_asks_nothing_and_signals_nothing(env):
    bystander = env.start(["sleep", "300"])
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert "[j/N]" not in result.stdout
    assert "Geen opname-server actief" in result.stdout
    assert "OnCue is gestopt." in result.stdout
    assert bystander.poll() is None


def test_live_server_without_status_answer_asks_and_n_signals_nothing(env):
    server = env.record_server()
    result = env.run(stdin="n\n")
    assert result.returncode == 0, result.stderr
    assert "Toch stoppen? [j/N]" in result.stdout
    assert "Niets gestopt" in result.stdout
    time.sleep(0.5)
    assert server.poll() is None
    assert env.pkill_calls() == []
    assert (env.pids / "oncue-launcher-record.pid").exists()


def test_live_server_with_unusable_status_asks(env):
    server = env.record_server()
    env.answer_status("not json")
    result = env.run(stdin="n\n")
    assert "Toch stoppen? [j/N]" in result.stdout
    assert server.poll() is None


def test_unknown_phase_with_live_server_asks(env):
    server = env.record_server()
    env.answer_status(_status("mystery", server.pid))
    result = env.run(stdin="n\n")
    assert "Toch stoppen? [j/N]" in result.stdout
    assert server.poll() is None


def test_unknown_phase_without_server_asks_nothing(env):
    env.answer_status(_status("mystery", 0))
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert "[j/N]" not in result.stdout


def test_missing_venv_python_counts_as_no_answer(env):
    server = env.record_server()
    (env.repo / ".venv" / "bin" / "python").chmod(0o644)
    env.answer_status(_status("idle", server.pid))
    result = env.run(stdin="n\n")
    assert "Toch stoppen? [j/N]" in result.stdout
    assert server.poll() is None


def test_recording_declined_signals_nothing(env):
    server = env.record_server()
    env.answer_status(_status("recording", server.pid))
    result = env.run(stdin="n\n")
    assert result.returncode == 0, result.stderr
    assert "Er loopt een opname (01:05)." in result.stdout
    assert "Niets gestopt" in result.stdout
    time.sleep(0.5)
    assert server.poll() is None
    assert env.pkill_calls() == []


def test_recording_confirmed_sends_sigterm_and_waits_for_exit(env):
    server = env.record_server()
    env.answer_status(_status("recording", server.pid))
    result = env.run(stdin="j\n")
    assert result.returncode == 0, result.stderr
    assert "Opname-server gestopt" in result.stdout
    assert _wait_exit(server)
    assert server.returncode == -15
    assert f"rm -f {env.pids}/oncue-launcher-record.pid" in env.fake_calls()


@pytest.mark.parametrize("phase", ["ready", "failed", "idle"])
def test_finished_phases_ask_nothing_and_stop_the_server(env, phase):
    server = env.record_server()
    env.answer_status(_status(phase, server.pid))
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert "[j/N]" not in result.stdout
    assert _wait_exit(server)
    assert server.returncode == -15


def test_transcribing_asks_nothing_and_stops_the_server(env):
    server = env.record_server()
    env.answer_status(_status("transcribing", server.pid))
    result = env.run()
    assert "[j/N]" not in result.stdout
    assert "De transcriptie loopt door bij de volgende start." in result.stdout
    assert _wait_exit(server)


def test_pid_file_naming_another_process_is_not_signalled(env):
    other = env.start(["sleep", "300"])
    (env.pids / "oncue-launcher-record.pid").write_text(f"{other.pid}\n")
    env.answer_status(_status("idle", 0))
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert "Geen opname-server actief" in result.stdout
    time.sleep(0.5)
    assert other.poll() is None


def test_garbage_pid_file_is_not_signalled(env):
    (env.pids / "oncue-launcher-record.pid").write_text("not-a-pid\n")
    env.answer_status(_status("idle", 0))
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert "Geen opname-server actief" in result.stdout
