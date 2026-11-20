"""scripts/smoke_app.sh against a tiny fake .app whose executable is a shell script."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_app.sh"

SERVER = """\
import sys, time
# Before the imports and the bind: app.log then shows how far a failed start got.
print(f"fake server starting at {time.time():.3f}", file=sys.stderr, flush=True)
import http.server, os, threading
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"phase": "idle"}' if self.path == "/status" else b"<html>recorder</html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
import socketserver
class Server(http.server.HTTPServer):
    # HTTPServer.server_bind() resolves the bound address with socket.getfqdn(), a
    # reverse DNS lookup. On a GitHub-hosted macOS runner that lookup stalls for
    # longer than the smoke timeout, while the socket is already bound but not yet
    # listening, so a connect times out instead of being refused. Nothing here uses
    # the resolved name, so bind and listen without the lookup.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
srv = Server(("127.0.0.1", int(os.environ["RECORDER_PORT"])), H)
print(f"fake server listening on 127.0.0.1:{os.environ['RECORDER_PORT']}", file=sys.stderr, flush=True)
threading.Timer(25, srv.shutdown).start()
srv.serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _fake_app(tmp_path: Path, *, detached: bool) -> Path:
    server = tmp_path / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    app = tmp_path / "Fake.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "fake"
    if detached:
        # The listener is a double-forked orphan: it is not a descendant of the app process.
        body = f'( "{sys.executable}" "{server}" >/dev/null 2>&1 & )\nsleep 20\n'
    else:
        body = f'exec "{sys.executable}" "{server}"\n'
    exe.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    exe.chmod(0o755)
    return app


def _run(app: Path, port: int, tmpdir: Path | None = None, keep: bool = False) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SMOKE_PORT": str(port), "SMOKE_TIMEOUT": "15"}
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    if keep:
        env["SMOKE_KEEP"] = "1"
    return subprocess.run(
        ["bash", str(SCRIPT), str(app), "15"], capture_output=True, text=True, env=env, timeout=60, check=False
    )


def test_fake_server_binds_without_resolving_the_bound_address(tmp_path: Path) -> None:
    """http.server.HTTPServer.server_bind() calls socket.getfqdn(), a reverse DNS
    lookup that on a GitHub-hosted macOS runner stalls past the smoke timeout: the
    socket is bound but not yet listening, so the connect times out (curl 28) instead
    of being refused. The fake server must bind without that lookup."""
    server = tmp_path / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import runpy, socket, sys\n"
        "def refuse(host):\n"
        "    sys.stderr.write(f'getfqdn called for {host!r}\\n')\n"
        "    raise SystemExit(3)\n"
        "socket.getfqdn = refuse\n"
        "runpy.run_path(sys.argv[1], run_name='__main__')\n",
        encoding="utf-8",
    )
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "RECORDER_PORT": str(_free_port())}
    try:
        result = subprocess.run(
            [sys.executable, str(probe), str(server)],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
    else:
        pytest.fail(f"the fake server stopped before serving (getfqdn?) instead of starting: {result.stderr}")
    assert "fake server listening" in stderr
    assert "getfqdn called" not in stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_normal_case_passes(tmp_path: Path) -> None:
    result = _run(_fake_app(tmp_path, detached=False), _free_port())
    assert result.returncode == 0, result.stderr
    assert "PASS:" in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_occupied_port_fails_before_launch(tmp_path: Path) -> None:
    app = _fake_app(tmp_path, detached=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        result = _run(app, port)
    assert result.returncode == 1
    assert f"FAIL: port {port} is already in use" in result.stderr
    assert "Starting" not in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_listener_that_is_not_the_child_fails(tmp_path: Path) -> None:
    result = _run(_fake_app(tmp_path, detached=True), _free_port())
    assert result.returncode == 1
    assert "not the launched app" in result.stderr
    assert "PASS:" not in result.stdout


def test_missing_bundle_fails(tmp_path: Path) -> None:
    result = _run(tmp_path / "absent.app", _free_port())
    assert result.returncode == 1
    assert "not an app bundle" in result.stderr


def _scratch(tmp_path: Path) -> Path:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return scratch


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_work_folder_is_removed_after_a_pass(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    result = _run(_fake_app(tmp_path, detached=False), _free_port(), scratch)
    assert result.returncode == 0, result.stderr
    assert list(scratch.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_work_folder_is_removed_after_a_failure_and_the_log_is_shown(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    result = _run(_fake_app(tmp_path, detached=True), _free_port(), scratch)
    assert result.returncode == 1
    assert list(scratch.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_work_folder_is_kept_with_smoke_keep(tmp_path: Path) -> None:
    scratch = _scratch(tmp_path)
    result = _run(_fake_app(tmp_path, detached=False), _free_port(), scratch, keep=True)
    assert result.returncode == 0, result.stderr
    kept = list(scratch.iterdir())
    assert len(kept) == 1
    assert str(kept[0].resolve()) in result.stderr or kept[0].name in result.stderr
    assert (kept[0] / "OnCue.app").is_dir()


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_failed_copy_leaves_no_temp_folder(tmp_path: Path) -> None:
    app = _fake_app(tmp_path, detached=False)
    unreadable = app / "Contents" / "unreadable"
    unreadable.write_text("x", encoding="utf-8")
    unreadable.chmod(0o000)
    tmpdir = tmp_path / "scratch"
    tmpdir.mkdir()
    try:
        result = _run(app, _free_port(), tmpdir=tmpdir)
    finally:
        unreadable.chmod(0o644)
    if os.geteuid() == 0:
        pytest.skip("root can read a mode 000 file, the copy does not fail")
    assert result.returncode != 0
    assert list(tmpdir.glob("oncue-smoke.*")) == []


def _app_with_bar(tmp_path: Path, *, orphan_bar: bool) -> Path:
    """A fake app that starts its bar like OnCue.app does and, normally, stops it on SIGTERM."""
    app = _fake_app(tmp_path, detached=False)
    bar_exe = app / "Contents" / "Helpers" / "OnCue Bar.app" / "Contents" / "MacOS" / "OnCueBar"
    bar_exe.parent.mkdir(parents=True)
    bar_exe.write_text("#!/bin/bash\nsleep 40\n", encoding="utf-8")
    bar_exe.chmod(0o755)
    exe = app / "Contents" / "MacOS" / "fake"
    server = f'"{sys.executable}" "{tmp_path / "server.py"}"'
    lines = [
        "#!/bin/bash",
        'C="$(cd "$(dirname "$0")/.." && pwd -P)"',
        '"$C/Helpers/OnCue Bar.app/Contents/MacOS/OnCueBar" --port "$RECORDER_PORT" &',
        "BAR=$!",
        f"{server} &",
        "SRV=$!",
    ]
    if not orphan_bar:
        lines.append("trap 'kill $BAR 2>/dev/null; kill $SRV 2>/dev/null; exit 0' TERM")
    exe.write_text("\n".join([*lines, "wait $SRV", ""]), encoding="utf-8")
    return app


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_bar_starts_and_is_gone_after_the_app_stops(tmp_path: Path) -> None:
    result = _run(_app_with_bar(tmp_path, orphan_bar=False), _free_port())
    assert result.returncode == 0, result.stderr
    assert "bar: started" in result.stdout and "gone after the app stopped" in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_bar_that_outlives_the_app_fails(tmp_path: Path) -> None:
    result = _run(_app_with_bar(tmp_path, orphan_bar=True), _free_port())
    assert result.returncode == 1
    assert "recording bar is still running" in result.stderr
    assert "PASS:" not in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_missing_bar_is_skipped_unless_required(tmp_path: Path) -> None:
    app = _app_with_python(tmp_path)
    modules = {"SMOKE_IMPORT_MODULES": "json"}
    assert "bar: not in the bundle" in _run_with_modules(app, "json").stdout
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SMOKE_PORT": str(_free_port()),
        "SMOKE_REQUIRE_BAR": "1",
        **modules,
    }
    strict = subprocess.run(
        ["bash", str(SCRIPT), str(app), "15"], capture_output=True, text=True, env=env, timeout=60, check=False
    )
    assert strict.returncode == 1 and "has no recording bar" in strict.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_app_ignoring_sigterm_is_killed_and_the_work_folder_removed(tmp_path: Path) -> None:
    import time

    server = tmp_path / "server.py"
    server.write_text(SERVER, encoding="utf-8")
    app = tmp_path / "Stubborn.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    exe = macos / "fake"
    loop = f'while true; do "{sys.executable}" "{server}"; done\n'
    exe.write_text("#!/bin/bash\ntrap '' TERM\n" + loop, encoding="utf-8")
    exe.chmod(0o755)
    scratch = _scratch(tmp_path)
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SMOKE_PORT": str(_free_port()),
        "SMOKE_TIMEOUT": "15",
        "SMOKE_STOP_TIMEOUT": "2",
        "TMPDIR": str(scratch),
    }
    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(SCRIPT), str(app), "15"], capture_output=True, text=True, env=env, timeout=60, check=False
    )
    assert time.monotonic() - started < 40
    assert "SIGKILL" in result.stderr
    assert list(scratch.glob("oncue-smoke.*")) == []


def _app_with_python(tmp_path: Path) -> Path:
    """A fake app whose bundle interpreter is the test interpreter (reads the code from -c)."""
    app = _fake_app(tmp_path, detached=False)
    py = app / "Contents" / "MacOS" / "python"
    py.write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    return app


def _run_with_modules(app: Path, modules: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SMOKE_PORT": str(_free_port()),
        "SMOKE_TIMEOUT": "15",
        "SMOKE_IMPORT_MODULES": modules,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), str(app), "15"], capture_output=True, text=True, env=env, timeout=60, check=False
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_import_check_passes_when_the_bundle_python_imports_the_modules(tmp_path: Path) -> None:
    result = _run_with_modules(_app_with_python(tmp_path), "json ssl")
    assert result.returncode == 0, result.stderr
    assert "imports ok: json ssl" in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_import_check_fails_with_the_module_name(tmp_path: Path) -> None:
    result = _run_with_modules(_app_with_python(tmp_path), "json no_such_module_xyz")
    assert result.returncode == 1
    assert "cannot import no_such_module_xyz" in result.stderr
    assert "PASS:" not in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_missing_bundle_python_is_skipped_unless_required(tmp_path: Path) -> None:
    app = _fake_app(tmp_path, detached=False)
    assert "no bundle interpreter (skipped)" in _run(app, _free_port()).stdout
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "SMOKE_PORT": str(_free_port()), "SMOKE_REQUIRE_BAR": "1"}
    strict = subprocess.run(
        ["bash", str(SCRIPT), str(app), "15"], capture_output=True, text=True, env=env, timeout=60, check=False
    )
    assert strict.returncode == 1 and "has no interpreter" in strict.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_import_check_reads_only_the_bundle_library(tmp_path: Path) -> None:
    app = _app_with_python(tmp_path)
    lib = app / "Contents" / "Resources" / "lib" / "python3.12"
    lib.mkdir(parents=True)
    (lib / "only_in_bundle_xyz.py").write_text("VALUE = 1\n", encoding="utf-8")
    assert _run_with_modules(app, "only_in_bundle_xyz").returncode == 0
    (lib / "only_in_bundle_xyz.py").unlink()
    assert _run_with_modules(app, "only_in_bundle_xyz").returncode == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="smoke_app.sh targets macOS")
def test_import_check_does_not_read_the_working_directory(tmp_path: Path) -> None:
    # python -c prepends the current directory to sys.path; the import check must import only from
    # the bundle, so a module sitting in the directory the script was started in must not satisfy it.
    app = _app_with_python(tmp_path)
    workdir = tmp_path / "cwd"
    workdir.mkdir()
    (workdir / "only_in_cwd_xyz.py").write_text("VALUE = 1\n", encoding="utf-8")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SMOKE_PORT": str(_free_port()),
        "SMOKE_TIMEOUT": "15",
        "SMOKE_IMPORT_MODULES": "only_in_cwd_xyz",
    }
    result = subprocess.run(
        ["bash", str(SCRIPT), str(app), "15"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
        cwd=workdir,
    )
    assert result.returncode == 1
    assert "cannot import only_in_cwd_xyz" in result.stderr


def test_default_import_modules_include_the_protobuf_extension() -> None:
    assert "google._upb._message" in SCRIPT.read_text(encoding="utf-8")
