"""Tests for the one-line Mac install: scripts/bootstrap.sh, scripts/install.sh,
scripts/oncue_preflight.sh and the icon fallback in scripts/launcher/build_app.sh.

These run the real shell scripts via subprocess (never a reimplementation in
Python). Every path the scripts could write to (OnCue checkout, logs,
~/.zprofile, apps, config) is redirected into pytest's tmp_path through the
environment variables the scripts expose, so nothing here touches the real
~/OnCue, ~/Applications, ~/.oncue or ~/.zprofile.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
BOOTSTRAP = SCRIPTS / "bootstrap.sh"
INSTALL = SCRIPTS / "install.sh"
PREFLIGHT = SCRIPTS / "oncue_preflight.sh"
BUILD_APP = SCRIPTS / "launcher" / "build_app.sh"
ICNS = SCRIPTS / "launcher" / "icon" / "sales-copilot.icns"

SHELL_FILES = [
    BOOTSTRAP,
    INSTALL,
    PREFLIGHT,
    BUILD_APP,
    SCRIPTS / "install_whisper_cpp.sh",
]

darwin_only = pytest.mark.skipif(sys.platform != "darwin", reason="the installer is macOS-only")


# ── helpers ──────────────────────────────────────────────────────────────────


def _base_env(tmp_path: Path) -> dict[str, str]:
    """Environment with every ambient ONCUE_* setting removed and all writable
    locations pointed into tmp_path."""
    # GIT_* is dropped too: a git hook (pre-push runs ci.sh) exports GIT_DIR and
    # friends, which would send every git call in these tests to the wrong repo.
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("ONCUE_", "GIT_"))
    }
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "ONCUE_DIR": str(tmp_path / "oncue"),
            "ONCUE_LOG_DIR": str(tmp_path / "logs"),
            "ONCUE_ZPROFILE": str(tmp_path / "zprofile"),
            # These tests cover the build route (git clone, command line tools,
            # Homebrew). The release route has its own file, test_install_release_route.py;
            # forcing the route here keeps this file off the network.
            "ONCUE_ROUTE": "build",
            "ONCUE_APPS_DIR": str(tmp_path / "apps"),
            "ONCUE_CONFIG_DIR": str(tmp_path / "oncue-config"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    env.pop("DEVELOPER_DIR", None)
    return env


def _run(args: list[str], env: dict[str, str], cwd: Path | None = None, timeout: int = 120):
    return subprocess.run(
        args,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _out(result: subprocess.CompletedProcess) -> str:
    """stdout and stderr together: a logging run merges both through tee."""
    return result.stdout + result.stderr


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    result = _run(["git", "-C", str(repo), *args], env)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def _make_source_repo(tmp_path: Path, env: dict[str, str]) -> Path:
    """A local git repo that has the markers of an OnCue checkout."""
    src = tmp_path / "source"
    (src / "src" / "sales_copilot").mkdir(parents=True)
    (src / "scripts").mkdir()
    (src / "src" / "sales_copilot" / "__main__.py").write_text("print('oncue')\n")
    (src / "scripts" / "install.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (src / ".gitignore").write_text(".env\ndata/\n")
    assert _run(["git", "init", "-q", "-b", "main", str(src)], env).returncode == 0
    _git(src, "add", "-A", env=env)
    _git(src, "commit", "-q", "-m", "initial", env=env)
    return src


def _add_commit(src: Path, name: str, env: dict[str, str]) -> None:
    (src / name).write_text(f"{name}\n")
    _git(src, "add", name, env=env)
    _git(src, "commit", "-q", "-m", f"add {name}", env=env)


def _fake_toolchain(root: Path, with_swift: bool = True) -> Path:
    dev = root / "fake-developer-dir"
    bin_dir = dev / "usr" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    tools = ["git", "swift"] if with_swift else ["git"]
    for tool in tools:
        path = bin_dir / tool
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return dev


def _install_xcode_select_stub(stub_dir: Path, dev: Path, creates_tools_on_install: bool) -> None:
    """A fake `xcode-select` first in PATH: -p reports `dev`, --install records
    the call and (optionally) makes the tools appear, like Apple's installer."""
    stub_dir.mkdir(parents=True, exist_ok=True)
    marker = stub_dir / "install-called"
    create = (
        f'mkdir -p "{dev}/usr/bin"; '
        f'for t in git swift; do printf "#!/bin/sh\\nexit 0\\n" > "{dev}/usr/bin/$t"; chmod +x "{dev}/usr/bin/$t"; done'
        if creates_tools_on_install
        else ":"
    )
    script = stub_dir / "xcode-select"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f'  -p) echo "{dev}"; exit 0 ;;\n'
        f'  --install) touch "{marker}"; {create}; exit 0 ;;\n'
        "esac\n"
        "exit 2\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _bash_function(script: Path, call: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Source a script (bootstrap in source-only mode) and run one call."""
    env = dict(env, ONCUE_BOOTSTRAP_SOURCE_ONLY="1")
    return _run(["bash", "-c", f'set -euo pipefail; source "{script}"; {call}'], env)


# ── syntax ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("script", SHELL_FILES, ids=lambda p: p.name)
def test_shell_syntax(script: Path) -> None:
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", [BOOTSTRAP, INSTALL, PREFLIGHT], ids=lambda p: p.name)
def test_shellcheck_when_installed(script: Path) -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed")
    result = subprocess.run(
        ["shellcheck", "-S", "warning", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout


# ── macOS version gate ───────────────────────────────────────────────────────


@pytest.mark.parametrize("version", ["", "abc", "14", "14.0", "14.1", "14.1.9", "13.6.1", "9", "14.x"])
def test_unsupported_or_malformed_macos_version_is_refused(version: str, tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    for script in (PREFLIGHT, BOOTSTRAP):
        result = _bash_function(script, f'oncue_macos_supported "{version}"', env)
        assert result.returncode != 0, f"{script.name} accepted macOS {version!r}"


@pytest.mark.parametrize("version", ["14.2", "14.2.1", "14.5", "15", "15.0.1", "26.0"])
def test_supported_macos_version_is_accepted(version: str, tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    for script in (PREFLIGHT, BOOTSTRAP):
        result = _bash_function(script, f'oncue_macos_supported "{version}"', env)
        assert result.returncode == 0, f"{script.name} refused macOS {version}: {result.stderr}"


def test_missing_macos_argument_is_refused(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    assert _bash_function(PREFLIGHT, "oncue_macos_supported", env).returncode != 0


@darwin_only
def test_bootstrap_names_the_macos_version_it_refuses(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["ONCUE_MACOS_VERSION"] = "13.6"
    result = _run(["bash", str(BOOTSTRAP)], env)
    assert result.returncode != 0
    assert "macOS 13.6" in _out(result)
    assert "14.2" in _out(result)
    assert not (tmp_path / "oncue").exists()
    # A failed run says where the log is.
    assert "send this file when you ask for help" in _out(result).lower()
    assert list((tmp_path / "logs").glob("install-*.log"))


@darwin_only
def test_install_sh_refuses_old_macos_before_changing_anything(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["ONCUE_MACOS_VERSION"] = "13.6"
    result = _run(["bash", str(INSTALL), "--auto-yes"], env)
    assert result.returncode != 0
    assert "macOS 13.6" in _out(result)
    assert "14.2" in _out(result)
    assert not (tmp_path / "apps").exists()
    assert not (tmp_path / "oncue-config").exists()


# ── command line tools detection ─────────────────────────────────────────────


@pytest.mark.parametrize("script", [PREFLIGHT, BOOTSTRAP], ids=lambda p: p.name)
def test_clt_detected_only_with_real_tools(script: Path, tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    stubs = tmp_path / "stubs"

    complete = _fake_toolchain(tmp_path / "complete")
    _install_xcode_select_stub(stubs, complete, creates_tools_on_install=False)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    assert _bash_function(script, "oncue_clt_present", env).returncode == 0

    # git alone is not enough: swift (AudioTee) has the same Apple shim.
    no_swift = _fake_toolchain(tmp_path / "no-swift", with_swift=False)
    _install_xcode_select_stub(stubs, no_swift, creates_tools_on_install=False)
    assert _bash_function(script, "oncue_clt_present", env).returncode != 0

    # xcode-select pointing at an empty directory: the shim case.
    empty = tmp_path / "empty-dev"
    empty.mkdir()
    _install_xcode_select_stub(stubs, empty, creates_tools_on_install=False)
    assert _bash_function(script, "oncue_clt_present", env).returncode != 0


@pytest.mark.parametrize("script", [PREFLIGHT, BOOTSTRAP], ids=lambda p: p.name)
def test_clt_detected_for_full_xcode_layout(script: Path, tmp_path: Path) -> None:
    """Xcode.app keeps swift in its toolchain, not in usr/bin (measured on the dev Mac)."""
    env = _base_env(tmp_path)
    dev = _fake_toolchain(tmp_path / "xcode", with_swift=False)
    swift = dev / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "bin" / "swift"
    swift.parent.mkdir(parents=True)
    swift.write_text("#!/bin/sh\nexit 0\n")
    swift.chmod(swift.stat().st_mode | stat.S_IEXEC)
    stubs = tmp_path / "stubs"
    _install_xcode_select_stub(stubs, dev, creates_tools_on_install=False)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    assert _bash_function(script, "oncue_clt_present", env).returncode == 0


@pytest.mark.parametrize("script", [PREFLIGHT, BOOTSTRAP], ids=lambda p: p.name)
def test_clt_missing_when_xcode_select_fails(script: Path, tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    failing = stubs / "xcode-select"
    failing.write_text("#!/bin/sh\nexit 2\n")
    failing.chmod(failing.stat().st_mode | stat.S_IEXEC)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    assert _bash_function(script, "oncue_clt_present", env).returncode != 0


@darwin_only
def test_bootstrap_installs_clt_and_waits_until_present(tmp_path: Path) -> None:
    src = _make_source_repo(tmp_path, _base_env(tmp_path))
    env = _base_env(tmp_path)
    stubs = tmp_path / "stubs"
    dev = tmp_path / "waited-dev"
    _install_xcode_select_stub(stubs, dev, creates_tools_on_install=True)
    env.update(
        PATH=f"{stubs}:{env['PATH']}",
        ONCUE_REPO_URL=str(src),
        ONCUE_SKIP_INSTALL="1",
        ONCUE_CLT_POLL="1",
        ONCUE_CLT_TIMEOUT="30",
    )
    result = _run(["bash", str(BOOTSTRAP)], env)
    assert result.returncode == 0, _out(result)
    assert (stubs / "install-called").exists()
    assert 'click "Install"' in result.stdout
    assert "Command line tools installed" in result.stdout


@darwin_only
def test_bootstrap_clt_wait_times_out_with_a_clear_message(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    stubs = tmp_path / "stubs"
    dev = tmp_path / "never-dev"
    dev.mkdir()
    _install_xcode_select_stub(stubs, dev, creates_tools_on_install=False)
    env.update(
        PATH=f"{stubs}:{env['PATH']}",
        ONCUE_SKIP_INSTALL="1",
        ONCUE_CLT_POLL="1",
        ONCUE_CLT_TIMEOUT="2",
    )
    result = _run(["bash", str(BOOTSTRAP)], env)
    assert result.returncode != 0
    assert "not installed within 2 seconds" in _out(result)
    assert not (tmp_path / "oncue").exists()


# ── Homebrew PATH persistence ────────────────────────────────────────────────


def test_brew_shellenv_is_persisted_once(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    profile = tmp_path / "zprofile"
    profile.write_text("export FOO=bar\n")
    for _ in range(3):
        result = _bash_function(BOOTSTRAP, "persist_brew_shellenv /opt/homebrew/bin/brew", env)
        assert result.returncode == 0, result.stderr
    text = profile.read_text()
    assert text.count("brew shellenv") == 1
    assert 'eval "$(/opt/homebrew/bin/brew shellenv)"' in text
    assert text.startswith("export FOO=bar\n")


def test_brew_shellenv_left_alone_when_already_configured(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    profile = tmp_path / "zprofile"
    original = 'eval "$(/usr/local/bin/brew shellenv)"\n'
    profile.write_text(original)
    result = _bash_function(BOOTSTRAP, "persist_brew_shellenv /opt/homebrew/bin/brew", env)
    assert result.returncode == 0
    assert profile.read_text() == original


def test_brew_shellenv_creates_missing_profile(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env["ONCUE_ZPROFILE"] = str(tmp_path / "nested" / "zprofile")
    result = _bash_function(BOOTSTRAP, "persist_brew_shellenv /opt/homebrew/bin/brew", env)
    assert result.returncode == 0
    assert "brew shellenv" in (tmp_path / "nested" / "zprofile").read_text()


# ── dry run ──────────────────────────────────────────────────────────────────


@darwin_only
def test_dry_run_prints_every_step_and_changes_nothing(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    home_before = sorted(p.name for p in (tmp_path / "home").iterdir())

    result = _run(["bash", str(BOOTSTRAP), "--dry-run"], env)

    assert result.returncode == 0, _out(result)
    out = result.stdout
    for n in range(1, 6):
        assert f"[{n}/5]" in out
    assert "dry run" in out.lower()
    assert "git clone" in out
    assert "install.sh --auto-yes" in out
    # Nothing was created: no checkout, no log directory, no zprofile.
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert sorted(p.name for p in (tmp_path / "home").iterdir()) == home_before
    assert not (tmp_path / "oncue").exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "zprofile").exists()


@darwin_only
def test_dry_run_describes_update_for_an_existing_checkout(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    assert _run(["git", "clone", "-q", str(src), str(tmp_path / "oncue")], env).returncode == 0
    result = _run(["bash", str(BOOTSTRAP), "--dry-run"], env)
    assert result.returncode == 0
    assert "fast-forward" in result.stdout
    assert "git clone" not in result.stdout


@darwin_only
def test_dry_run_reports_the_missing_clt_without_installing(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    stubs = tmp_path / "stubs"
    dev = tmp_path / "missing-dev"
    dev.mkdir()
    _install_xcode_select_stub(stubs, dev, creates_tools_on_install=True)
    env["PATH"] = f"{stubs}:{env['PATH']}"
    result = _run(["bash", str(BOOTSTRAP), "--dry-run"], env)
    assert result.returncode == 0
    assert "xcode-select --install" in result.stdout
    assert not (stubs / "install-called").exists()


def test_unknown_option_is_rejected(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    result = _run(["bash", str(BOOTSTRAP), "--frobnicate"], env)
    assert result.returncode != 0
    assert "Unknown option" in _out(result)


# ── clone / update ───────────────────────────────────────────────────────────


@darwin_only
def test_first_run_clones_and_logs(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    env.update(ONCUE_REPO_URL=str(src), ONCUE_SKIP_INSTALL="1")

    result = _run(["bash", str(BOOTSTRAP)], env)

    assert result.returncode == 0, _out(result)
    assert (tmp_path / "oncue" / "src" / "sales_copilot" / "__main__.py").is_file()
    logs = list((tmp_path / "logs").glob("install-*.log"))
    assert len(logs) == 1
    log_text = logs[0].read_text()
    assert "OnCue downloaded" in log_text
    assert "[4/5]" in log_text


@darwin_only
def test_second_run_fast_forwards_and_keeps_env_and_data(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    env.update(ONCUE_REPO_URL=str(src), ONCUE_SKIP_INSTALL="1")
    assert _run(["bash", str(BOOTSTRAP)], env).returncode == 0

    dest = tmp_path / "oncue"
    (dest / ".env").write_text("GEMINI_API_KEY=keep-me\n")
    (dest / "data" / "sessions").mkdir(parents=True)
    (dest / "data" / "sessions" / "call.json").write_text("{}")
    _add_commit(src, "new_in_upstream.txt", env)

    result = _run(["bash", str(BOOTSTRAP)], env)

    assert result.returncode == 0, _out(result)
    assert (dest / "new_in_upstream.txt").is_file()
    assert (dest / ".env").read_text() == "GEMINI_API_KEY=keep-me\n"
    assert (dest / "data" / "sessions" / "call.json").read_text() == "{}"


@darwin_only
def test_second_run_without_upstream_changes_is_a_noop(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    env.update(ONCUE_REPO_URL=str(src), ONCUE_SKIP_INSTALL="1")
    assert _run(["bash", str(BOOTSTRAP)], env).returncode == 0
    head = _git(tmp_path / "oncue", "rev-parse", "HEAD", env=env)
    assert _run(["bash", str(BOOTSTRAP)], env).returncode == 0
    assert _git(tmp_path / "oncue", "rev-parse", "HEAD", env=env) == head


@darwin_only
def test_non_oncue_directory_is_refused_and_left_alone(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    env.update(ONCUE_REPO_URL=str(src), ONCUE_SKIP_INSTALL="1")
    other = tmp_path / "oncue"
    other.mkdir()
    (other / "my-thesis.txt").write_text("precious\n")

    result = _run(["bash", str(BOOTSTRAP)], env)

    assert result.returncode != 0
    assert "not an OnCue folder" in _out(result)
    assert [p.name for p in other.iterdir()] == ["my-thesis.txt"]
    assert (other / "my-thesis.txt").read_text() == "precious\n"


@darwin_only
def test_a_plain_file_at_the_target_path_is_refused(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    env.update(ONCUE_REPO_URL=str(src), ONCUE_SKIP_INSTALL="1")
    target = tmp_path / "oncue"
    target.write_text("not a directory\n")
    result = _run(["bash", str(BOOTSTRAP)], env)
    assert result.returncode != 0
    assert target.read_text() == "not a directory\n"


@darwin_only
def test_git_repo_without_oncue_markers_is_refused(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    env.update(ONCUE_REPO_URL=str(src), ONCUE_SKIP_INSTALL="1")
    other = tmp_path / "oncue"
    assert _run(["git", "init", "-q", str(other)], env).returncode == 0
    result = _run(["bash", str(BOOTSTRAP)], env)
    assert result.returncode != 0
    assert "not an OnCue folder" in _out(result)


@darwin_only
def test_diverged_checkout_is_not_touched(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    env.update(ONCUE_REPO_URL=str(src), ONCUE_SKIP_INSTALL="1")
    assert _run(["bash", str(BOOTSTRAP)], env).returncode == 0
    dest = tmp_path / "oncue"
    _add_commit(dest, "local_only.txt", env)
    _add_commit(src, "upstream_only.txt", env)
    head = _git(dest, "rev-parse", "HEAD", env=env)

    result = _run(["bash", str(BOOTSTRAP)], env)

    assert result.returncode != 0
    assert "cannot be fast-forwarded" in _out(result)
    assert _git(dest, "rev-parse", "HEAD", env=env) == head
    assert not (dest / "upstream_only.txt").exists()


@darwin_only
def test_unreachable_repo_fails_with_a_plain_message(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    env.update(ONCUE_REPO_URL=str(tmp_path / "does-not-exist"), ONCUE_SKIP_INSTALL="1")
    result = _run(["bash", str(BOOTSTRAP)], env)
    assert result.returncode != 0
    assert "Could not download OnCue" in _out(result)


@darwin_only
def test_ref_selects_a_branch_on_clone_and_update(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    _git(src, "checkout", "-q", "-b", "feature", env=env)
    _add_commit(src, "on_feature.txt", env)
    _git(src, "checkout", "-q", "main", env=env)
    env.update(ONCUE_REPO_URL=str(src), ONCUE_REF="feature", ONCUE_SKIP_INSTALL="1")

    assert _run(["bash", str(BOOTSTRAP)], env).returncode == 0
    dest = tmp_path / "oncue"
    assert (dest / "on_feature.txt").is_file()
    assert _git(dest, "rev-parse", "--abbrev-ref", "HEAD", env=env) == "feature"

    _git(src, "checkout", "-q", "feature", env=env)
    _add_commit(src, "more_on_feature.txt", env)
    assert _run(["bash", str(BOOTSTRAP)], env).returncode == 0
    assert (dest / "more_on_feature.txt").is_file()


@darwin_only
def test_unknown_ref_fails_without_creating_the_directory(tmp_path: Path) -> None:
    env = _base_env(tmp_path)
    src = _make_source_repo(tmp_path, env)
    env.update(ONCUE_REPO_URL=str(src), ONCUE_REF="no-such-branch", ONCUE_SKIP_INSTALL="1")
    result = _run(["bash", str(BOOTSTRAP)], env)
    assert result.returncode != 0
    assert "Could not download OnCue" in _out(result)


# ── launcher icon ────────────────────────────────────────────────────────────


def test_icon_is_tracked_and_not_ignored() -> None:
    assert ICNS.is_file()
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    ignored = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", str(ICNS)], check=False
    )
    assert ignored.returncode == 1, "scripts/launcher/icon/sales-copilot.icns is gitignored"


@darwin_only
def test_build_app_without_icon_warns_and_still_builds(tmp_path: Path) -> None:
    if shutil.which("osacompile") is None:
        pytest.skip("osacompile not available")
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    shutil.copy(BUILD_APP, launcher / "build_app.sh")
    env = _base_env(tmp_path)

    result = _run(["bash", str(launcher / "build_app.sh")], env, timeout=180)

    assert result.returncode == 0, _out(result)
    assert "default applet icon" in _out(result)
    app = launcher / "Start OnCue.app"
    assert app.is_dir()
    assert (app / "Contents" / "Info.plist").is_file()
    verify = _run(["codesign", "--verify", "--strict", str(app)], env)
    assert verify.returncode == 0, verify.stderr
