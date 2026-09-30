"""Tests for scripts/oncue_chain.sh -- the one-command OnCue preflight/launch script.

These run the actual bash script via subprocess (never reimplement its logic in
Python) against a throwaway fake repo skeleton, so nothing here touches the
developer's real .venv, MCP registration, or a real `claude` binary. The fake
repo's `.venv/bin/python` is a thin wrapper around *this* interpreter with
PYTHONPATH pointed at this repo's real `src/`, so checks that shell out to
Python still exercise the real `sales_copilot` package (e.g. the real
MEETING_APP_CANDIDATES tuple) instead of a duplicated stub.
"""

from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "oncue_chain.sh"
REAL_SRC = REPO_ROOT / "src"


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _write_python_wrapper(venv_python: Path) -> None:
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text(
        "#!/bin/sh\n"
        f'export PYTHONPATH="{REAL_SRC}"\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    _make_executable(venv_python)


def _write_claude_stub(bindir: Path, exit_code: int) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "claude"
    stub.write_text(f"#!/bin/sh\nexit {exit_code}\n")
    _make_executable(stub)


def _build_fake_repo(tmp_path: Path, *, with_venv: bool) -> Path:
    """A minimal repo skeleton oncue_chain.sh can run against unattended."""
    (tmp_path / "scripts" / "launcher").mkdir(parents=True)
    script_copy = tmp_path / "scripts" / "oncue_chain.sh"
    script_copy.write_text(SCRIPT_PATH.read_text())
    _make_executable(script_copy)

    # A start-oncue.command that just proves it was (not) invoked -- `check`
    # must never reach it, and `up` must not reach it when it aborts early.
    start_command = tmp_path / "scripts" / "launcher" / "start-oncue.command"
    start_command.write_text("#!/bin/sh\necho STUB-START-SHOULD-NOT-RUN\n")
    _make_executable(start_command)

    if with_venv:
        _write_python_wrapper(tmp_path / ".venv" / "bin" / "python")

    return script_copy


def _run(
    script_path: Path,
    args: list[str],
    *,
    cwd: Path,
    extra_path: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{extra_path}:{env.get('PATH', '')}"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(script_path), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_bash_syntax_is_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT_PATH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_script_never_kills_or_removes_anything() -> None:
    """Hard rule: no kill -9 of anything the script did not start, and no
    killing at all as part of `check` -- enforced here as "not at all", since
    `up` never needs to kill anything either (it skips starting/registering
    whatever is already healthy instead of restarting it)."""
    content = SCRIPT_PATH.read_text()
    assert not re.search(r"\bkill\b", content), "found a 'kill' in oncue_chain.sh"
    assert not re.search(r"\brm\b", content), "found an 'rm' in oncue_chain.sh"


def test_meeting_app_candidates_are_read_from_capture_py_not_duplicated() -> None:
    content = SCRIPT_PATH.read_text()
    assert "from sales_copilot.audio.capture import MEETING_APP_CANDIDATES" in content
    # No second, hand-rolled candidate list living alongside the import.
    assert "CANDIDATES=(" not in content


def test_check_exits_nonzero_when_hub_is_down(tmp_path: Path) -> None:
    script_copy = _build_fake_repo(tmp_path, with_venv=True)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=1)
    (tmp_path / ".env").write_text(f"WS_HUB_PORT={_free_port()}\n")

    result = _run(script_copy, ["check"], cwd=tmp_path, extra_path=bindir)

    assert result.returncode != 0
    assert "[FAIL]" in result.stdout
    assert "Hub" in result.stdout
    assert "STUB-START-SHOULD-NOT-RUN" not in result.stdout


def test_check_reports_ok_venv_when_venv_present(tmp_path: Path) -> None:
    script_copy = _build_fake_repo(tmp_path, with_venv=True)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=0)
    (tmp_path / ".env").write_text(f"WS_HUB_PORT={_free_port()}\n")

    result = _run(script_copy, ["check"], cwd=tmp_path, extra_path=bindir)

    assert "[OK]   1. Repo + venv" in result.stdout
    assert "[OK]   4. MCP-registratie" in result.stdout


def test_check_fails_venv_step_when_venv_missing(tmp_path: Path) -> None:
    script_copy = _build_fake_repo(tmp_path, with_venv=False)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=1)

    result = _run(script_copy, ["check"], cwd=tmp_path, extra_path=bindir)

    assert result.returncode != 0
    assert "[FAIL] 1. Repo + venv" in result.stdout
    assert "setup.sh" in result.stdout


def test_up_aborts_before_starting_anything_when_venv_missing(tmp_path: Path) -> None:
    """`up` must never touch the launcher (open -a Terminal / MCP registration)
    when the venv itself is broken -- there is nothing to launch yet."""
    script_copy = _build_fake_repo(tmp_path, with_venv=False)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=1)

    result = _run(script_copy, ["up"], cwd=tmp_path, extra_path=bindir)

    assert result.returncode != 0
    assert "setup.sh" in (result.stdout + result.stderr)
    assert "STUB-START-SHOULD-NOT-RUN" not in result.stdout


def test_unknown_subcommand_prints_usage_and_exits_nonzero() -> None:
    result = subprocess.run(["bash", str(SCRIPT_PATH), "bogus"], capture_output=True, text=True)
    assert result.returncode == 2
    assert "check|up" in result.stderr


def test_no_subcommand_prints_usage_and_exits_nonzero() -> None:
    result = subprocess.run(["bash", str(SCRIPT_PATH)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "check|up" in result.stderr


def test_log_names_are_read_from_their_writers_not_hardcoded() -> None:
    """7a and 7b must resolve their filenames from the modules that write them.

    The first version of check 7a looked for `launcher-dashboard.log`, a file
    the app never writes, and therefore reported "does not exist" while a
    6.8 MB `copilot.log` sat next to it. A preflight that cannot see the log it
    vouches for is worse than no preflight.
    """
    content = SCRIPT_PATH.read_text()
    assert "sales_copilot.core.logging LOG_FILE_NAME" in content
    assert "sales_copilot.mcp_bridge.server _LOG_FILE_NAME" in content
    assert "launcher-dashboard.log" not in content


def test_check_finds_the_app_log_that_actually_exists(tmp_path: Path) -> None:
    from sales_copilot.core.logging import LOG_FILE_NAME

    script_copy = _build_fake_repo(tmp_path, with_venv=True)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=1)
    logs_dir = tmp_path / "data" / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / LOG_FILE_NAME).write_text("een regel\n")

    result = _run(
        script_copy,
        ["check"],
        cwd=tmp_path,
        extra_path=bindir,
        extra_env={"LOG_DIR": str(logs_dir)},
    )

    assert "7a. App-log" in result.stdout
    app_line = next(line for line in result.stdout.splitlines() if "7a. App-log" in line)
    assert "[OK]" in app_line
    assert LOG_FILE_NAME in app_line
    assert "bestaat nog niet" not in app_line


def test_check_finds_the_mcp_bridge_log_in_the_repo_not_the_host_cache(tmp_path: Path) -> None:
    # mcp_bridge is Pro-only and excluded from the public export, while this
    # test file ships with it. Skip rather than fail where the package is absent
    # — the same degradation the script itself performs for check 7b.
    server = pytest.importorskip("sales_copilot.mcp_bridge.server")
    bridge_log_name = server._LOG_FILE_NAME

    script_copy = _build_fake_repo(tmp_path, with_venv=True)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=1)
    logs_dir = tmp_path / "data" / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / bridge_log_name).write_text("MCP bridge starting\n")

    result = _run(
        script_copy,
        ["check"],
        cwd=tmp_path,
        extra_path=bindir,
        extra_env={"LOG_DIR": str(logs_dir)},
    )

    bridge_line = next(line for line in result.stdout.splitlines() if "7b. MCP-bridge-log" in line)
    assert "[OK]" in bridge_line
    assert bridge_log_name in bridge_line
    assert "claude-cli-nodejs" not in bridge_line


def test_bridge_check_degrades_when_the_pro_package_is_absent(tmp_path: Path) -> None:
    """`src/sales_copilot/mcp_bridge/` is excluded from the public export, so an
    OSS checkout cannot import it. The check must say so calmly, not fail."""
    script_copy = _build_fake_repo(tmp_path, with_venv=False)
    # A venv whose python has no sales_copilot at all: the import fails exactly
    # the way it would in an export that carries no mcp_bridge package.
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    _make_executable(venv_python)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=1)

    result = _run(script_copy, ["check"], cwd=tmp_path, extra_path=bindir)

    bridge_line = next(line for line in result.stdout.splitlines() if "7b. MCP-bridge-log" in line)
    assert "[FAIL]" not in bridge_line


def _write_pyproject(root: Path, commands: list[str]) -> None:
    """A pyproject.toml declaring exactly `commands` under [project.scripts]."""
    lines = ["[project]", 'name = "live-sales-copilot"', 'version = "0.0.0"', "", "[project.scripts]"]
    lines += [f'{name} = "sales_copilot.__main__:main"' for name in commands]
    (root / "pyproject.toml").write_text("\n".join(lines) + "\n")


def _write_console_script(root: Path, name: str) -> None:
    """A stub wrapper in the fake venv's bin/, as pip writes at install time."""
    script = root / ".venv" / "bin" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n")
    _make_executable(script)


def test_declared_entrypoints_are_read_from_pyproject_not_duplicated() -> None:
    """Same rule as check 5: the declared set comes from the source of truth."""
    content = SCRIPT_PATH.read_text()
    assert "tomllib" in content
    assert '(data.get("project") or {}).get("scripts")' in content
    # No second, hand-rolled command list living alongside the read.
    assert "sales-copilot talk-time" not in content


def test_entrypoint_check_flags_a_command_the_install_never_got(tmp_path: Path) -> None:
    """The upgrade bug itself: pyproject declares a command, the venv's bin/ has
    no wrapper for it because nothing reinstalled the package after the pull.
    Nothing errors anywhere, so the preflight has to be the one to say it."""
    script_copy = _build_fake_repo(tmp_path, with_venv=True)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=1)
    _write_pyproject(tmp_path, ["sales-copilot", "oncue-brand-new"])
    _write_console_script(tmp_path, "sales-copilot")

    result = _run(script_copy, ["check"], cwd=tmp_path, extra_path=bindir)

    line = next(line for line in result.stdout.splitlines() if "1b. Entrypoints" in line)
    assert "[FAIL]" in line
    assert "oncue-brand-new" in line
    assert "sales-copilot," not in line, "a command that IS installed must not be reported missing"
    assert "install.sh" in result.stdout
    assert result.returncode != 0


def test_entrypoint_check_is_ok_when_every_declared_command_is_present(tmp_path: Path) -> None:
    script_copy = _build_fake_repo(tmp_path, with_venv=True)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=1)
    _write_pyproject(tmp_path, ["sales-copilot", "talk-time"])
    _write_console_script(tmp_path, "sales-copilot")
    _write_console_script(tmp_path, "talk-time")

    result = _run(script_copy, ["check"], cwd=tmp_path, extra_path=bindir)

    line = next(line for line in result.stdout.splitlines() if "1b. Entrypoints" in line)
    assert "[OK]" in line
    assert "sales-copilot" in line and "talk-time" in line


def test_entrypoint_check_degrades_without_a_pyproject(tmp_path: Path) -> None:
    """A packaged build ships no pyproject.toml. Say so calmly, never FAIL."""
    script_copy = _build_fake_repo(tmp_path, with_venv=True)
    bindir = tmp_path / "bin"
    _write_claude_stub(bindir, exit_code=1)

    result = _run(script_copy, ["check"], cwd=tmp_path, extra_path=bindir)

    line = next(line for line in result.stdout.splitlines() if "1b. Entrypoints" in line)
    assert "[OK]" in line
    assert "verpakte build" in line


def test_script_is_executable() -> None:
    assert os.access(SCRIPT_PATH, os.X_OK), (
        "scripts/oncue_chain.sh must be executable so ./scripts/oncue_chain.sh works"
    )


def test_detector_check_message_matches_post_206_behaviour() -> None:
    """The WARN copy described the pre-#206 world, where setup.js re-applied the
    first preset on every page load. It now applies once on a fresh browser and
    says so on screen."""
    content = SCRIPT_PATH.read_text()
    assert "applyPreset(presets[0])" not in content
    assert "verse browser" in content
