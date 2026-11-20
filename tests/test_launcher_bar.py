"""The launcher starts the recording bar next to the notetaker server. It runs
for real against a temporary repo root and temporary pid files, with fake
`open`/`osascript` on PATH and a fake server standing in for the real one."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LAUNCH = REPO / "scripts" / "launcher" / "oncue-launch.sh"

FAKE_SERVER = """\
import socket, sys, time
port = int(sys.argv[sys.argv.index("--port") + 1])
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(1)
time.sleep(300)
"""

FAKE_BAR = """\
#!/bin/bash
echo "$@" > "$(dirname "$0")/args.txt"
trap 'kill $! 2>/dev/null; exit 0' TERM
sleep 300 &
wait
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


def _wait_dead(pid: int, timeout: float = 10) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


class Env:
    def __init__(self, root: Path) -> None:
        self.repo = root / "repo"
        self.pids = root / "pids"
        self.fakebin = root / "fakebin"
        self.home = root / "home"
        self.record_port = _free_port()
        self.dash_port = _free_port()
        self.bar_dir = self.repo / "bin" / "OnCue Bar.app" / "Contents" / "MacOS"
        self.osascript_log = root / "osascript.log"
        for d in (self.pids, self.fakebin, self.home, self.repo / "scripts", self.repo / "src" / "sales_copilot"):
            d.mkdir(parents=True)
        (self.repo / "src" / "sales_copilot" / "__main__.py").write_text("")
        (self.repo / "scripts" / "record_server.py").write_text(FAKE_SERVER)
        (self.repo / ".env").write_text("LANGUAGE=nl\n")
        venv = self.repo / ".venv" / "bin"
        venv.mkdir(parents=True)
        python = venv / "python"
        python.write_text(
            f"#!/bin/bash\n"
            f'if [[ "$1" == "-m" ]]; then\n'
            f'  exec "{sys.executable}" scripts/record_server.py --port {self.dash_port}\n'
            f"fi\n"
            f'if [[ "$1" == "-c" ]]; then echo /dashboard/wizard/; exit 0; fi\n'
            f'exec "{sys.executable}" "$@"\n'
        )
        python.chmod(0o755)
        for name, body in (("open", "exit 0"), ("osascript", f'echo "$@" >> "{self.osascript_log}"')):
            f = self.fakebin / name
            f.write_text(f"#!/bin/bash\n{body}\n")
            f.chmod(0o755)

    def install_bar(self) -> Path:
        self.bar_dir.mkdir(parents=True)
        bar = self.bar_dir / "OnCueBar"
        bar.write_text(FAKE_BAR)
        bar.chmod(0o755)
        return self.bar_dir / "args.txt"

    def run(
        self, script: Path, *args: str, stdin: str = "", env_extra: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ['PATH']}",
            "HOME": str(self.home),
            "ONCUE_REPO": str(self.repo),
            "ONCUE_PID_DIR": str(self.pids),
            "ONCUE_RECORD_PORT": str(self.record_port),
            **(env_extra or {}),
        }
        return subprocess.run(
            ["bash", str(script), *args], env=env, input=stdin, capture_output=True, text=True, timeout=90
        )

    def pid(self, name: str) -> int:
        return int((self.pids / f"oncue-launcher-{name}.pid").read_text())

    def stop_server(self) -> None:
        pid = self.pid("record")
        os.kill(pid, signal.SIGTERM)
        assert _wait_dead(pid)

    def notifications(self) -> int:
        if not self.osascript_log.exists():
            return 0
        return sum("opnamebalk" in line for line in self.osascript_log.read_text().splitlines())


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    # Only processes this test started: the pid files live in its own temp dir.
    for pid_file in e.pids.glob("*.pid"):
        text = pid_file.read_text().strip()
        if text.isdigit() and _alive(int(text)):
            os.kill(int(text), signal.SIGTERM)


def test_missing_bar_does_not_stop_oncue_and_notifies_once(env):
    first = env.run(LAUNCH, "record")
    assert first.returncode == 0, first.stderr
    assert "OnCue record gestart" in first.stdout
    log = env.repo / "data" / "logs" / "launcher-bar.log"
    marker = env.repo / "data" / ".bar-missing-notified"
    assert marker.exists()
    assert len(log.read_text().splitlines()) == 1
    assert env.notifications() == 1
    assert not (env.pids / "oncue-launcher-bar.pid").exists()

    env.stop_server()
    second = env.run(LAUNCH, "record")
    assert second.returncode == 0, second.stderr
    assert len(log.read_text().splitlines()) == 2
    assert env.notifications() == 1


def test_non_executable_bar_counts_as_missing(env):
    args = env.install_bar()
    (env.bar_dir / "OnCueBar").chmod(0o644)
    result = env.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    assert not args.exists()
    assert (env.repo / "data" / ".bar-missing-notified").exists()


def test_bar_started_with_port_and_hotkey_and_marker_removed(env):
    args = env.install_bar()
    (env.repo / "data").mkdir()
    (env.repo / "data" / ".bar-missing-notified").write_text("")
    (env.repo / ".env").write_text("BAR_HOTKEY=ctrl+shift+b  # my key\n")
    result = env.run(LAUNCH, "record")
    assert result.returncode == 0, result.stderr
    end = time.time() + 10
    while not args.exists() and time.time() < end:
        time.sleep(0.1)
    assert args.read_text().strip() == f"--port {env.record_port} --hotkey ctrl+shift+b"
    assert _alive(env.pid("bar"))
    assert not (env.repo / "data" / ".bar-missing-notified").exists()


def test_empty_hotkey_means_default_and_second_start_replaces_bar(env):
    args = env.install_bar()
    (env.repo / ".env").write_text("BAR_HOTKEY=\n")
    assert env.run(LAUNCH, "record").returncode == 0
    old_bar = env.pid("bar")
    end = time.time() + 10
    while not args.exists() and time.time() < end:
        time.sleep(0.1)
    assert args.read_text().strip().endswith("--hotkey ctrl+option+r")

    env.stop_server()
    assert env.run(LAUNCH, "record").returncode == 0
    assert _wait_dead(old_bar)
    assert env.pid("bar") != old_bar
    assert _alive(env.pid("bar"))


def test_foreign_pid_in_bar_pid_file_is_ignored_and_overwritten(env):
    env.install_bar()
    foreign = subprocess.Popen(["sleep", "300"])
    try:
        (env.pids / "oncue-launcher-bar.pid").write_text(f"{foreign.pid}\n")
        assert env.run(LAUNCH, "record").returncode == 0
        assert foreign.poll() is None
        assert env.pid("bar") != foreign.pid
    finally:
        foreign.terminate()
        foreign.wait()


def test_dashboard_mode_starts_no_bar(env):
    env.install_bar()
    (env.repo / ".env").write_text(f"WS_HUB_PORT={env.dash_port}\n")
    result = env.run(LAUNCH, "dashboard")
    assert result.returncode == 0, result.stderr
    assert not (env.pids / "oncue-launcher-bar.pid").exists()
    assert not (env.bar_dir / "args.txt").exists()


def _dry_run(env: Env, *, record_port_seam: str | None) -> dict[str, str]:
    run_env = {
        **os.environ,
        "HOME": str(env.home),
        "ONCUE_REPO": str(env.repo),
        "ONCUE_PID_DIR": str(env.pids),
        "ONCUE_LAUNCH_DRY_RUN": "1",
    }
    run_env.pop("ONCUE_RECORD_PORT", None)
    if record_port_seam is not None:
        run_env["ONCUE_RECORD_PORT"] = record_port_seam
    out = subprocess.run(
        ["bash", str(LAUNCH), "record"], env=run_env, capture_output=True, text=True, timeout=30, check=True
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def test_dry_run_record_port_follows_recorder_port_from_env_file(env) -> None:
    (env.repo / ".env").write_text("RECORDER_PORT=8791  # notetaker\n")
    assert _dry_run(env, record_port_seam=None)["PORT"] == "8791"
    assert _dry_run(env, record_port_seam="8802")["PORT"] == "8802"


def test_dry_run_record_port_falls_back_to_8780(env) -> None:
    assert _dry_run(env, record_port_seam=None)["PORT"] == "8780"
    (env.repo / ".env").write_text("RECORDER_PORT=abc\n")
    assert _dry_run(env, record_port_seam=None)["PORT"] == "8780"
