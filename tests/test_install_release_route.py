"""Tests for the no-compiler install route: scripts/bootstrap.sh (release route),
the --prebuilt mode of scripts/install.sh and the choice between the release and
the build route.

The scripts run for real via subprocess. The "release" is a set of file://
fixtures: a source archive made with `git archive` of a minimal tree, a fake
binaries archive (with a correct and a wrong checksum), the GitHub API answers and
a fake uv installer. Every path the scripts write to (install dir, logs, apps,
config, HOME, TMPDIR) is redirected into pytest's tmp_path, so nothing here
touches the real ~/OnCue, ~/Applications, ~/.oncue, ~/Library/Logs/OnCue or the
user's uv and Python.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.test_install_scripts import (
    BOOTSTRAP,
    INSTALL,
    SCRIPTS,
    _base_env,
    _git,
    _make_source_repo,
    _out,
    _run,
    darwin_only,
)

TAG = "v9.0.0"
ARCHIVE = "oncue-macos-arm64.tar.gz"
BUILD_APP = SCRIPTS / "launcher" / "build_app.sh"

# Commands that are shims on a Mac without Apple's command line tools: calling one
# opens Apple's install window and breaks the promise of the release route.
FORBIDDEN_COMMANDS = (
    "git",
    "swift",
    "swiftc",
    "cmake",
    "make",
    "cc",
    "c++",
    "gcc",
    "clang",
    "xcrun",
    "xcode-select",
    "brew",
    "python3",
)


# ── fixtures ─────────────────────────────────────────────────────────────────


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _api_json(tag: str, assets: tuple[str, ...]) -> str:
    items = ",\n".join(f'    {{\n      "name": "{a}",\n      "size": 1\n    }}' for a in assets)
    return f'{{\n  "tag_name": "{tag}",\n  "name": "OnCue {tag}",\n  "assets": [\n{items}\n  ]\n}}\n'


class Release:
    """A fake OnCue release laid out as file:// URL bases."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "fixture"
        self.api = self.root / "api"
        self.sources = self.root / "sources"
        self.assets = self.root / "assets"
        self.uv_calls = tmp_path / "uv-calls.log"
        self.repo = tmp_path / "release-source"
        self.env = _base_env(tmp_path)
        for d in (self.api / "releases" / "tags", self.sources, self.assets):
            d.mkdir(parents=True, exist_ok=True)
        _make_source_repo_tree(self.repo, self.env)
        self.fake_uv = self.root / "fake-uv"
        _executable(
            self.fake_uv,
            "#!/bin/sh\n"
            f'LOG="{self.uv_calls}"\n'
            'printf "uv %s | cache=%s pyinstall=%s pref=%s config=%s\\n" "$*" "$UV_CACHE_DIR" '
            '"$UV_PYTHON_INSTALL_DIR" "$UV_PYTHON_PREFERENCE" "$XDG_CONFIG_HOME" >> "$LOG"\n'
            'case "$1" in\n'
            '  --version) echo "uv 9.9.9 (fake)"; exit 0 ;;\n'
            "  python) exit 0 ;;\n"
            '  venv) for last; do :; done; mkdir -p "$last/bin"; '
            'printf \'#!/bin/sh\\nexit 0\\n\' > "$last/bin/python"; chmod +x "$last/bin/python"; exit 0 ;;\n'
            "  --directory) exit 0 ;;\n"
            "esac\n"
            "exit 3\n",
        )
        _executable(
            self.root / "uv-install.sh",
            "#!/bin/sh\n"
            'printf "installer no_modify_path=%s install_dir=%s\\n" "$UV_NO_MODIFY_PATH" '
            f'"$UV_INSTALL_DIR" >> "{self.uv_calls}"\n'
            'mkdir -p "$UV_INSTALL_DIR"\n'
            f'cp "{self.fake_uv}" "$UV_INSTALL_DIR/uv"\n'
            'chmod +x "$UV_INSTALL_DIR/uv"\n',
        )

    def publish(
        self,
        tag: str = TAG,
        *,
        with_assets: bool = True,
        good_checksum: bool = True,
        omit: str | None = None,
        latest: bool = True,
    ) -> None:
        """Make release `tag` available: API answer, source archive, programs."""
        (self.repo / "VERSION").write_text(f"{tag}\n")
        _git(self.repo, "add", "-A", env=self.env)
        _git(self.repo, "commit", "-q", "--allow-empty", "-m", f"release {tag}", env=self.env)
        source = self.sources / f"{tag}.tar.gz"
        assert (
            _run(
                [
                    "git",
                    "-C",
                    str(self.repo),
                    "archive",
                    "--format=tar.gz",
                    f"--prefix=OnCue-{tag}/",
                    "-o",
                    str(source),
                    "HEAD",
                ],
                self.env,
            ).returncode
            == 0
        )
        names = (ARCHIVE, f"{ARCHIVE}.sha256") if with_assets else ()
        answer = _api_json(tag, names)
        (self.api / "releases" / "tags" / tag).write_text(answer)
        if latest:
            (self.api / "releases" / "latest").write_text(answer)
        if not with_assets:
            return
        stage = self.root / f"stage-{tag}"
        stage.mkdir(exist_ok=True)
        for name in ("whisper-cli", "whisper-server", "audiotee"):
            _executable(stage / name, f"#!/bin/sh\n# fake {name} {tag}\nexit 0\n")
        (stage / "BUILD_INFO.txt").write_text(f"fake build for {tag}\n")
        asset_dir = self.assets / tag
        asset_dir.mkdir(exist_ok=True)
        archive = asset_dir / ARCHIVE
        with tarfile.open(archive, "w:gz") as tar:
            for name in ("whisper-cli", "whisper-server", "audiotee", "BUILD_INFO.txt"):
                if name == omit:
                    continue
                tar.add(stage / name, arcname=name)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if not good_checksum:
            digest = "0" * 64
        (asset_dir / f"{ARCHIVE}.sha256").write_text(f"{digest}  {ARCHIVE}\n")

    def unpublish_downloads(self, tag: str = TAG) -> None:
        (self.sources / f"{tag}.tar.gz").unlink()
        shutil.rmtree(self.assets / tag)
        (self.root / "uv-install.sh").unlink()


def _make_source_repo_tree(repo: Path, env: dict[str, str]) -> None:
    (repo / "src" / "sales_copilot").mkdir(parents=True)
    (repo / "scripts").mkdir()
    (repo / "src" / "sales_copilot" / "__main__.py").write_text("print('oncue')\n")
    (repo / "scripts" / "install.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    (repo / "constraints.txt").write_text("# pinned\n")
    (repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    (repo / ".env.example").write_text("EXAMPLE=1\n")
    (repo / "VERSION").write_text("seed\n")
    assert _run(["git", "init", "-q", "-b", "main", str(repo)], env).returncode == 0
    _git(repo, "add", "-A", env=env)
    _git(repo, "commit", "-q", "-m", "seed", env=env)


@pytest.fixture
def release(tmp_path: Path) -> Release:
    rel = Release(tmp_path)
    rel.publish()
    return rel


def _release_env(rel: Release, tmp_path: Path, **extra: str) -> dict[str, str]:
    """Environment for a release-route run: no forced route, file:// bases, and
    logging shims for every command that needs Apple's command line tools."""
    env = dict(rel.env)
    env.pop("ONCUE_ROUTE", None)
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    shims = tmp_path / "shims"
    for name in FORBIDDEN_COMMANDS:
        _executable(shims / name, f'#!/bin/sh\necho "{name} $*" >> "{tmp_path / "shim-calls.log"}"\nexit 1\n')
    env.update(
        PATH=f"{shims}:{env['PATH']}",
        TMPDIR=str(tmp),
        ONCUE_API_BASE=f"file://{rel.api}",
        ONCUE_SOURCE_URL_BASE=f"file://{rel.sources}",
        ONCUE_ASSET_URL_BASE=f"file://{rel.assets}",
        ONCUE_UV_INSTALLER_URL=f"file://{rel.root / 'uv-install.sh'}",
        ONCUE_UV_VERSION="9.9.9",
        ONCUE_SKIP_INSTALL="1",
    )
    env.update(extra)
    return env


def _shim_calls(tmp_path: Path) -> str:
    log = tmp_path / "shim-calls.log"
    return log.read_text() if log.exists() else ""


def _bootstrap(env: dict[str, str], *args: str):
    return _run(["bash", str(BOOTSTRAP), *args], env)


def _marker(install: Path) -> str:
    return (install / ".oncue-release").read_text().strip()


# ── route choice ─────────────────────────────────────────────────────────────


@darwin_only
def test_git_checkout_keeps_the_build_route(release: Release, tmp_path: Path) -> None:
    env = _release_env(release, tmp_path)
    checkout = _make_source_repo(tmp_path / "dev", release.env)
    env["ONCUE_DIR"] = str(checkout)

    result = _bootstrap(env, "--dry-run")

    assert result.returncode == 0, _out(result)
    assert "fast-forward pull" in result.stdout
    assert "[2/5] Apple command line tools" in result.stdout
    assert "Installing release" not in result.stdout


@darwin_only
def test_empty_dir_takes_the_release_route(release: Release, tmp_path: Path) -> None:
    env = _release_env(release, tmp_path)
    (tmp_path / "oncue").mkdir()

    result = _bootstrap(env, "--dry-run")

    assert result.returncode == 0, _out(result)
    out = result.stdout
    assert f"Installing release {TAG} without compiling" in out
    for n in range(1, 5):
        assert f"[{n}/4]" in out
    assert "Apple command line tools" not in out
    assert "Homebrew" not in out
    assert "git clone" not in out
    assert "install.sh --auto-yes --prebuilt" in out
    assert not (tmp_path / "logs").exists()


@darwin_only
def test_release_without_the_programs_falls_back_with_one_line(release: Release, tmp_path: Path) -> None:
    release.publish("v9.1.0", with_assets=False)
    env = _release_env(release, tmp_path)

    result = _bootstrap(env, "--dry-run")

    assert result.returncode == 0, _out(result)
    notes = [line for line in _out(result).splitlines() if "No prebuilt OnCue available" in line]
    assert len(notes) == 1
    assert "release v9.1.0 has no prebuilt programs" in notes[0]
    assert "git clone" in result.stdout
    assert "[5/5]" in result.stdout


@darwin_only
def test_unreachable_release_information_falls_back(release: Release, tmp_path: Path) -> None:
    env = _release_env(release, tmp_path, ONCUE_API_BASE=f"file://{tmp_path}/nowhere")

    result = _bootstrap(env, "--dry-run")

    assert result.returncode == 0, _out(result)
    assert "No prebuilt OnCue available (the release information could not be read)" in _out(result)
    assert "git clone" in result.stdout


_ARCHIVE_ASSET = '{"name": "oncue-macos-arm64.tar.gz"}'
_SHA_ASSET = '{"name": "oncue-macos-arm64.tar.gz.sha256"}'


def _answer(tag_field: str, *assets: str) -> str:
    fields = [f for f in (tag_field, f'"assets": [{", ".join(assets)}]') if f]
    return "{" + ", ".join(fields) + "}"


@darwin_only
@pytest.mark.parametrize(
    "answer",
    [
        "",
        "not json at all",
        _answer('"tag_name": "../../etc"', _ARCHIVE_ASSET, _SHA_ASSET),
        _answer('"tag_name": "v1"', _ARCHIVE_ASSET),
        _answer('"tag_name": "v1"', _SHA_ASSET),
        _answer("", _ARCHIVE_ASSET, _SHA_ASSET),
        _answer('"tag_name": "v1"', '{"name": "oncue-macos-arm64xtar.gz"}', _SHA_ASSET),
    ],
)
def test_unusable_release_answers_are_never_a_release_route(release: Release, tmp_path: Path, answer: str) -> None:
    (release.api / "releases" / "latest").write_text(answer)
    env = _release_env(release, tmp_path)

    result = _bootstrap(env, "--dry-run")

    assert result.returncode == 0, _out(result)
    assert "No prebuilt OnCue available" in _out(result)
    assert "Installing release" not in result.stdout


@darwin_only
def test_route_override(release: Release, tmp_path: Path) -> None:
    env = _release_env(release, tmp_path, ONCUE_ROUTE="build")
    forced_build = _bootstrap(env, "--dry-run")
    assert forced_build.returncode == 0, _out(forced_build)
    assert "git clone" in forced_build.stdout
    assert "Installing release" not in forced_build.stdout
    assert "No prebuilt OnCue available" not in _out(forced_build)

    env = _release_env(release, tmp_path, ONCUE_ROUTE="release")
    forced_release = _bootstrap(env, "--dry-run")
    assert forced_release.returncode == 0, _out(forced_release)
    assert f"Installing release {TAG}" in forced_release.stdout

    # Forcing the release route when there is no release must fail, not fall back.
    release.publish("v9.1.0", with_assets=False)
    refused = _bootstrap(env, "--dry-run")
    assert refused.returncode != 0
    assert "ONCUE_ROUTE=release, but release v9.1.0 has no prebuilt programs" in _out(refused)
    assert "git clone" not in refused.stdout

    bogus = _bootstrap(_release_env(release, tmp_path, ONCUE_ROUTE="fast"), "--dry-run")
    assert bogus.returncode != 0
    assert "ONCUE_ROUTE must be 'release' or 'build'" in _out(bogus)


@darwin_only
def test_oncue_release_selects_a_tag(release: Release, tmp_path: Path) -> None:
    release.publish("v8.0.0", latest=False)
    env = _release_env(release, tmp_path, ONCUE_RELEASE="v8.0.0")

    result = _bootstrap(env, "--dry-run")

    assert result.returncode == 0, _out(result)
    assert "Installing release v8.0.0" in result.stdout


# ── release route: first install ─────────────────────────────────────────────


@darwin_only
def test_first_install_needs_no_command_line_tools(release: Release, tmp_path: Path) -> None:
    env = _release_env(release, tmp_path)
    install = tmp_path / "oncue"

    result = _bootstrap(env)

    out = _out(result)
    assert result.returncode == 0, out
    # The step list names no Apple tools, no Homebrew and no password.
    for n in range(1, 5):
        assert f"[{n}/4]" in result.stdout
    assert "Apple command line tools" not in result.stdout
    assert "Installing Homebrew" not in out
    assert "Homebrew" not in out
    assert _marker(install) == TAG
    assert (install / "VERSION").read_text().strip() == TAG
    # Programs sit where the code looks for them.
    for rel_path in (
        "vendor/whisper.cpp/build/bin/whisper-cli",
        "vendor/whisper.cpp/build/bin/whisper-server",
        "bin/audiotee",
    ):
        path = install / rel_path
        assert path.is_file() and path.stat().st_mode & stat.S_IEXEC, rel_path
    assert (install / ".oncue-binaries").read_text().strip() == TAG
    assert (install / "bin" / "BUILD_INFO.txt").is_file()
    # No staging leftovers next to the install.
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("oncue")) == ["oncue"]
    assert list((tmp_path / "tmp").iterdir()) == []
    # Nothing in the call list needed the command line tools.
    assert _shim_calls(tmp_path) == ""
    # The user's shell profile and home stay untouched.
    assert not (tmp_path / "zprofile").exists()
    assert list((tmp_path / "home").iterdir()) == []
    log = next((tmp_path / "logs").glob("install-*.log")).read_text()
    assert "[4/4]" in log


@darwin_only
def test_uv_and_python_stay_inside_the_install_dir(release: Release, tmp_path: Path) -> None:
    env = _release_env(release, tmp_path)
    install = tmp_path / "oncue"

    result = _bootstrap(env)

    assert result.returncode == 0, _out(result)
    assert (install / ".tools" / "bin" / "uv").is_file()
    assert (install / ".venv" / "bin" / "python").is_file()
    calls = release.uv_calls.read_text().splitlines()
    assert any(
        line.startswith("installer no_modify_path=1 ") and f"install_dir={install}/.tools/bin" in line for line in calls
    )
    managed = [line for line in calls if not line.startswith(("installer", "uv --version"))]
    assert any("uv python install 3.11" in line for line in managed)
    assert any("uv venv --seed --python 3.11 " in line and f"{install}/.venv" in line for line in managed)
    assert any(
        "pip install --python" in line and "-e .[smart] -c constraints.txt" in line and f"--directory {install}" in line
        for line in managed
    )
    assert managed, calls
    for line in managed:
        assert f"cache={install}/.tools/cache" in line, line
        assert f"pyinstall={install}/.tools/python" in line, line
        assert "pref=only-managed" in line, line
        assert f"config={install}/.tools/config" in line, line


@darwin_only
def test_skip_install_stops_before_install_sh_and_the_real_handover_is_prebuilt(
    release: Release, tmp_path: Path
) -> None:
    marker = tmp_path / "handover-args"
    (release.repo / "scripts" / "install.sh").write_text(f'#!/usr/bin/env bash\necho "$*" > "{marker}"\n')
    release.publish("v9.2.0")
    env = _release_env(release, tmp_path)
    env.pop("ONCUE_SKIP_INSTALL")

    result = _bootstrap(env)

    assert result.returncode == 0, _out(result)
    assert marker.read_text().strip() == "--auto-yes --prebuilt"


# ── release route: update, same tag, damaged downloads ───────────────────────


@darwin_only
def test_update_keeps_env_data_and_tools_and_keeps_the_old_version(release: Release, tmp_path: Path) -> None:
    install = tmp_path / "oncue"
    assert _bootstrap(_release_env(release, tmp_path)).returncode == 0
    (install / ".env").write_text("SECRET=mine\n")
    (install / "data" / "sessions").mkdir(parents=True)
    (install / "data" / "sessions" / "call.txt").write_text("my recording\n")
    release.publish("v9.0.1", latest=False)
    # The fake uv installer is gone: the carried-over tools must be enough.
    (release.root / "uv-install.sh").unlink()
    env = _release_env(release, tmp_path, ONCUE_RELEASE="v9.0.1")

    result = _bootstrap(env)

    assert result.returncode == 0, _out(result)
    assert _marker(install) == "v9.0.1"
    assert (install / "VERSION").read_text().strip() == "v9.0.1"
    assert (install / ".env").read_text() == "SECRET=mine\n"
    assert (install / "data" / "sessions" / "call.txt").read_text() == "my recording\n"
    assert (install / ".tools" / "bin" / "uv").is_file()
    assert (install / ".oncue-binaries").read_text().strip() == "v9.0.1"
    previous = tmp_path / "oncue.previous"
    assert _marker(previous) == TAG
    assert (previous / "VERSION").read_text().strip() == TAG
    assert _shim_calls(tmp_path) == ""
    assert not (tmp_path / "oncue.staging").exists()

    # A third version replaces .previous instead of piling up.
    (previous / "sentinel").write_text("old\n")
    release.publish("v9.0.2", latest=False)
    third = _bootstrap(_release_env(release, tmp_path, ONCUE_RELEASE="v9.0.2"))
    assert third.returncode == 0, _out(third)
    assert _marker(install) == "v9.0.2"
    assert _marker(previous) == "v9.0.1"
    assert not (previous / "sentinel").exists()
    assert (install / ".env").read_text() == "SECRET=mine\n"


@darwin_only
def test_same_tag_skips_the_download(release: Release, tmp_path: Path) -> None:
    env = _release_env(release, tmp_path)
    install = tmp_path / "oncue"
    assert _bootstrap(env).returncode == 0
    (install / ".env").write_text("SECRET=mine\n")
    release.unpublish_downloads()
    calls_before = release.uv_calls.read_text()

    result = _bootstrap(env)

    out = _out(result)
    assert result.returncode == 0, out
    assert f"OnCue {TAG} is already downloaded" in out
    assert _marker(install) == TAG
    assert (install / ".env").read_text() == "SECRET=mine\n"
    assert not (tmp_path / "oncue.previous").exists()
    assert "uv python install" in release.uv_calls.read_text()
    assert release.uv_calls.read_text().count("installer ") == calls_before.count("installer ")


@darwin_only
def test_missing_programs_are_fetched_again_for_the_same_tag(release: Release, tmp_path: Path) -> None:
    env = _release_env(release, tmp_path)
    install = tmp_path / "oncue"
    assert _bootstrap(env).returncode == 0
    (install / "bin" / "audiotee").unlink()
    (release.sources / f"{TAG}.tar.gz").unlink()

    result = _bootstrap(env)

    assert result.returncode == 0, _out(result)
    assert (install / "bin" / "audiotee").is_file()
    assert _marker(install) == TAG


@darwin_only
def test_a_failed_write_while_placing_the_programs_stops_and_never_reports_success(
    release: Release, tmp_path: Path
) -> None:
    env = _release_env(release, tmp_path)
    install = tmp_path / "oncue"
    assert _bootstrap(env).returncode == 0
    # A regular file where the bin directory should be: placing audiotee cannot work.
    shutil.rmtree(install / "bin")
    (install / "bin").write_text("in the way\n")
    (release.sources / f"{TAG}.tar.gz").unlink()

    result = _bootstrap(env)

    out = _out(result)
    assert result.returncode != 0, out
    assert "✗" in out
    assert "Prebuilt programs of" not in out
    assert "[3/4]" not in out
    assert (install / "bin").is_file()


@darwin_only
def test_checksum_mismatch_refuses_and_changes_nothing_on_first_install(tmp_path: Path) -> None:
    rel = Release(tmp_path)
    rel.publish(good_checksum=False)
    env = _release_env(rel, tmp_path)

    result = _bootstrap(env)

    out = _out(result)
    assert result.returncode != 0
    assert "do not match their checksum" in out
    assert "Nothing was changed" in out
    assert "No prebuilt" not in out and "build route" not in out
    assert not (tmp_path / "oncue").exists()
    assert not (tmp_path / "oncue.staging").exists()
    assert list((tmp_path / "tmp").iterdir()) == []
    assert _shim_calls(tmp_path) == ""


@darwin_only
def test_checksum_mismatch_leaves_an_existing_install_untouched(release: Release, tmp_path: Path) -> None:
    install = tmp_path / "oncue"
    assert _bootstrap(_release_env(release, tmp_path)).returncode == 0
    (install / ".env").write_text("SECRET=mine\n")
    release.publish("v9.0.1", good_checksum=False, latest=False)
    before = sorted(str(p.relative_to(install)) for p in install.rglob("*"))

    result = _bootstrap(_release_env(release, tmp_path, ONCUE_RELEASE="v9.0.1"))

    assert result.returncode != 0
    assert "do not match their checksum" in _out(result)
    assert _marker(install) == TAG
    assert (install / ".env").read_text() == "SECRET=mine\n"
    assert sorted(str(p.relative_to(install)) for p in install.rglob("*")) == before
    assert not (tmp_path / "oncue.previous").exists()
    assert not (tmp_path / "oncue.staging").exists()


@darwin_only
def test_malformed_checksum_file_refuses(release: Release, tmp_path: Path) -> None:
    (release.assets / TAG / f"{ARCHIVE}.sha256").write_text("garbage\n")

    result = _bootstrap(_release_env(release, tmp_path))

    assert result.returncode != 0
    assert "do not match their checksum" in _out(result)
    assert not (tmp_path / "oncue").exists()


@darwin_only
def test_incomplete_programs_archive_refuses(tmp_path: Path) -> None:
    rel = Release(tmp_path)
    rel.publish(omit="audiotee")

    result = _bootstrap(_release_env(rel, tmp_path))

    assert result.returncode != 0
    assert "audiotee is missing" in _out(result)
    assert not (tmp_path / "oncue").exists()


@darwin_only
def test_failed_download_falls_back_to_the_build_route(release: Release, tmp_path: Path) -> None:
    (release.sources / f"{TAG}.tar.gz").unlink()
    env = _release_env(release, tmp_path)
    src = _make_source_repo(tmp_path / "build-source", release.env)
    env["ONCUE_REPO_URL"] = str(src)
    # The build route does need the real tools: no logging shims on this one.
    env["PATH"] = release.env["PATH"]

    result = _bootstrap(env)

    out = _out(result)
    assert result.returncode == 0, out
    assert "Prebuilt download failed" in out
    assert "the OnCue source of v9.0.0 could not be downloaded" in out
    assert (tmp_path / "oncue" / ".git").is_dir()
    assert "[5/5]" in result.stdout
    assert not (tmp_path / "oncue" / ".oncue-release").exists()


@darwin_only
def test_failed_download_with_forced_release_route_does_not_fall_back(release: Release, tmp_path: Path) -> None:
    (release.sources / f"{TAG}.tar.gz").unlink()
    env = _release_env(release, tmp_path, ONCUE_ROUTE="release")

    result = _bootstrap(env)

    assert result.returncode != 0
    assert "ONCUE_ROUTE=release, but the OnCue source" in _out(result)
    assert not (tmp_path / "oncue").exists()


@darwin_only
def test_release_route_refuses_foreign_dirs_and_git_checkouts(release: Release, tmp_path: Path) -> None:
    env = _release_env(release, tmp_path, ONCUE_ROUTE="release")
    foreign = tmp_path / "oncue"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("not oncue\n")

    result = _bootstrap(env)

    assert result.returncode != 0
    assert "is not an OnCue folder" in _out(result)
    assert (foreign / "notes.txt").read_text() == "not oncue\n"
    assert sorted(p.name for p in foreign.iterdir()) == ["notes.txt"]

    checkout = _make_source_repo(tmp_path / "dev", release.env)
    env["ONCUE_DIR"] = str(checkout)
    refused = _bootstrap(env)
    assert refused.returncode != 0
    assert "git checkout of OnCue" in _out(refused)
    assert not (checkout / ".oncue-release").exists()


@darwin_only
def test_build_route_explains_a_release_install_it_cannot_update(release: Release, tmp_path: Path) -> None:
    assert _bootstrap(_release_env(release, tmp_path)).returncode == 0
    env = _release_env(release, tmp_path, ONCUE_ROUTE="build")
    # The build route needs the real tools (xcode-select, brew): no logging shims here.
    env["PATH"] = release.env["PATH"]

    result = _bootstrap(env)

    assert result.returncode != 0
    assert "was installed from a release" in _out(result)


# ── install.sh --prebuilt ────────────────────────────────────────────────────


def _prebuilt_tree(tmp_path: Path) -> Path:
    """A copy of the scripts plus what bootstrap's release route leaves behind."""
    tree = tmp_path / "tree"
    shutil.copytree(SCRIPTS, tree / "scripts", ignore=shutil.ignore_patterns("*.app", "__pycache__"))
    (tree / ".env.example").write_text("EXAMPLE=1\n")
    _executable(tree / ".venv" / "bin" / "python", "#!/bin/sh\nexit 0\n")
    for rel_path in (
        "vendor/whisper.cpp/build/bin/whisper-cli",
        "vendor/whisper.cpp/build/bin/whisper-server",
        "bin/audiotee",
    ):
        _executable(tree / rel_path, "#!/bin/sh\nexit 0\n")
    return tree


def _prebuilt_env(tmp_path: Path, model: Path | None) -> dict[str, str]:
    env = _base_env(tmp_path)
    shims = tmp_path / "shims"
    for name in FORBIDDEN_COMMANDS:
        _executable(shims / name, f'#!/bin/sh\necho "{name} $*" >> "{tmp_path / "shim-calls.log"}"\nexit 1\n')
    env["PATH"] = f"{shims}:{env['PATH']}"
    env["ONCUE_MODEL_URL"] = f"file://{model}" if model else f"file://{tmp_path}/no-such-model.bin"
    return env


@darwin_only
def test_prebuilt_install_verifies_and_finishes_without_command_line_tools(tmp_path: Path) -> None:
    tree = _prebuilt_tree(tmp_path)
    model = tmp_path / "model.bin"
    model.write_bytes(b"ggml" * 1000)
    env = _prebuilt_env(tmp_path, model)

    result = _run(["bash", str(tree / "scripts" / "install.sh"), "--auto-yes", "--prebuilt"], env, timeout=180)

    out = _out(result)
    assert result.returncode == 0, out
    for n in range(1, 6):
        assert f"[{n}/5]" in out
    assert "[6/" not in out and "/9]" not in out
    assert "Command line tools" not in out and "Homebrew" not in out
    assert (tree / "vendor/whisper.cpp/models/ggml-large-v3-turbo.bin").read_bytes() == model.read_bytes()
    assert not list((tree / "vendor/whisper.cpp/models").glob("*.part"))
    assert (tree / ".env").read_text() == "EXAMPLE=1\n"
    assert (tree / "data" / "sessions").is_dir()
    assert (tmp_path / "apps" / "OnCue.app").is_dir()
    assert (tmp_path / "oncue-config" / "repo-path").read_text() == str(tree)
    for label in ("Transcriber", "Audio capture", "App"):
        assert re.search(rf"{label}\s+OK", out), out
    assert "OnCue is installed." in out
    assert _shim_calls(tmp_path) == ""


@darwin_only
def test_prebuilt_install_reports_a_broken_program_and_a_failed_model(tmp_path: Path) -> None:
    tree = _prebuilt_tree(tmp_path)
    _executable(tree / "vendor/whisper.cpp/build/bin/whisper-server", "#!/bin/sh\nexit 1\n")
    _executable(tree / "bin" / "audiotee", "#!/bin/sh\nexit 1\n")
    env = _prebuilt_env(tmp_path, None)

    result = _run(["bash", str(tree / "scripts" / "install.sh"), "--auto-yes", "--prebuilt"], env, timeout=180)

    out = _out(result)
    assert result.returncode == 0, out
    assert re.search(r"Transcriber\s+NOT OK", out), out
    assert re.search(r"Audio capture\s+NOT OK", out), out
    assert "run the one-line install command again" in out
    assert not (tree / "vendor/whisper.cpp/models/ggml-large-v3-turbo.bin").exists()
    assert _shim_calls(tmp_path) == ""


@darwin_only
def test_prebuilt_install_without_a_python_environment_stops(tmp_path: Path) -> None:
    tree = _prebuilt_tree(tmp_path)
    (tree / ".venv" / "bin" / "python").unlink()
    env = _prebuilt_env(tmp_path, None)

    result = _run(["bash", str(tree / "scripts" / "install.sh"), "--auto-yes", "--prebuilt"], env, timeout=60)

    assert result.returncode != 0
    assert "Python environment" in _out(result)
    assert not (tree / ".env").exists()
    assert not (tmp_path / "apps").exists()


@darwin_only
def test_prebuilt_install_keeps_an_existing_model(tmp_path: Path) -> None:
    tree = _prebuilt_tree(tmp_path)
    model_dir = tree / "vendor/whisper.cpp/models"
    model_dir.mkdir(parents=True)
    (model_dir / "ggml-large-v3-turbo.bin").write_bytes(b"mine")
    env = _prebuilt_env(tmp_path, None)

    result = _run(["bash", str(tree / "scripts" / "install.sh"), "--auto-yes", "--prebuilt"], env, timeout=180)

    assert result.returncode == 0, _out(result)
    assert (model_dir / "ggml-large-v3-turbo.bin").read_bytes() == b"mine"
    assert "Speech model already present" in _out(result)


# ── no command line tools on the release route ───────────────────────────────

_FUNCTION_START = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{")
_DOUBLE_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
_SINGLE_QUOTED = re.compile(r"'[^']*'")
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)")
# A command word: at the start of a line, after a separator, a substitution, or a
# prefix command such as env or _run_logged that executes its argument.
_COMMAND_POSITION = re.compile(
    r"(?:^|[;&|(`!{]|\$\(|\b(?:then|do|else|env|command|exec|sudo|time|nohup|xargs|if|elif|while|until|_run_logged|release_fetch)\b)\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:/usr/bin/|/usr/local/bin/|/opt/homebrew/bin/)?"
    r"(" + "|".join(re.escape(c) for c in FORBIDDEN_COMMANDS) + r")(?![\w./-])"
)
_ABSOLUTE_SHIM = re.compile(r"/usr/bin/(?:" + "|".join(re.escape(c) for c in FORBIDDEN_COMMANDS) + r")\b")


def _functions(path: Path, selector) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in path.read_text().splitlines():
        if current is None:
            match = _FUNCTION_START.match(line)
            if match and selector(match.group(1)):
                current = match.group(1)
                found[current] = [line]
        else:
            found[current].append(line)
            if line == "}":
                current = None
    return found


def _command_words(source_lines: list[str]) -> list[str]:
    """Forbidden commands that are actually invoked: comments and the text of
    messages are ignored, commands hidden in $(...) inside a string are not."""
    hits: list[str] = []
    for raw in source_lines:
        line = raw.strip()
        if line.startswith("#"):
            continue
        # Look at substitutions inside double quotes before blanking the string.
        inner = "; ".join(m for s in _DOUBLE_QUOTED.findall(line) for m in _SUBSTITUTION.findall(s))
        line = _DOUBLE_QUOTED.sub('""', line)
        line = _SINGLE_QUOTED.sub("''", line)
        line = re.sub(r"\s#.*$", "", line)
        # A case label such as `git)` names a value, it does not run anything.
        line = re.sub(r"^[\w|*]+\)\s*", "", line)
        for text in (line, inner):
            hits.extend(m.group(1) for m in _COMMAND_POSITION.finditer(text))
            hits.extend(m.group(0) for m in _ABSOLUTE_SHIM.finditer(text))
    return hits


def test_the_checker_flags_real_calls_and_ignores_messages() -> None:
    assert _command_words(["    git clone x y"]) == ["git"]
    assert _command_words(["x=$(swift build -c release)"]) == ["swift"]
    assert _command_words(['echo "$(cmake --version)"']) == ["cmake"]
    assert _command_words(["    _run_logged brew install cmake"]) == ["brew"]
    assert _command_words(["    env FOO=1 make all"]) == ["make"]
    assert _command_words(["    if git status; then"]) == ["git"]
    assert "/usr/bin/xcrun" in _command_words(["    /usr/bin/xcrun --find clang"])
    assert _command_words(["    foo | cc -o x"]) == ["cc"]
    assert _command_words(['    _fail "this is a git checkout, run make later"']) == []
    assert _command_words(["    # git is not used here"]) == []
    assert _command_words(["        git) _fail oops ;;"]) == []
    assert _command_words(["    curl -fsSL https://example.com/git/make.tar.gz -o out"]) == []
    assert _command_words(['    release_json_has_asset "$json" x']) == []


@darwin_only
def test_release_route_code_calls_no_command_line_tool() -> None:
    bootstrap = _functions(
        BOOTSTRAP,
        lambda n: n.startswith(("release_", "step_release_")) or n == "choose_route" or n == "_run_logged",
    )
    install = _functions(INSTALL, lambda n: n.startswith("prebuilt_") or n == "_run_logged")
    shared = _functions(INSTALL, lambda n: n in ("_is_python_311",))
    # The route is made of these pieces: a rename must not silently empty the check.
    assert {
        "release_resolve",
        "release_download",
        "release_install_code",
        "release_place_binaries",
        "release_uv",
        "release_install_uv",
        "step_release_download",
        "step_release_python",
        "step_release_install",
        "choose_route",
    } <= set(bootstrap)
    assert {"prebuilt_verify_environment", "prebuilt_fetch_model"} <= set(install)

    offenders: dict[str, list[str]] = {}
    for source, functions in (("bootstrap.sh", bootstrap), ("install.sh", {**install, **shared})):
        for name, lines in functions.items():
            hits = _command_words(lines)
            if hits:
                offenders[f"{source}:{name}"] = hits
    assert offenders == {}

    # The steps both routes share in install.sh (settings, app) and the app builder
    # also run after the release route.
    app_hits = _command_words(BUILD_APP.read_text().splitlines())
    assert app_hits == []
    install_text = INSTALL.read_text()
    shared_part = install_text[install_text.index('_step "$STEP_SETTINGS"') :]
    assert _command_words(shared_part.splitlines()) == []


@darwin_only
def test_the_prebuilt_branch_of_install_sh_skips_every_compiler_step() -> None:
    text = INSTALL.read_text()
    start = text.index('if [[ "$PREBUILT" == true ]]; then\n    _step 2')
    branch = text[start : text.index("\nelse\n", start)]
    assert "prebuilt_verify_environment" in branch and "prebuilt_fetch_model" in branch
    assert _command_words(branch.splitlines()) == []
    for tool_step in ("brew", "cmake", "swift", "xcode-select", "oncue_clt_present", "install_whisper_cpp"):
        assert tool_step not in branch


@darwin_only
def test_shell_syntax_of_the_touched_scripts() -> None:
    for script in (BOOTSTRAP, INSTALL):
        result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
