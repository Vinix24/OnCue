"""Tests for scripts/release_dmg.sh with xcrun, spctl and shasum mocked on PATH."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "release_dmg.sh"
VERSION = re.search(r'^version\s*=\s*"([^"]+)"', (REPO / "pyproject.toml").read_text(), re.M).group(1)
GOOD = f"OnCue-{VERSION}-arm64.dmg"
HASH = "a" * 64


def _fake(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake(bindir, "xcrun", 'exit "${FAKE_STAPLER_RC:-0}"')
    _fake(bindir, "spctl", 'exit "${FAKE_SPCTL_RC:-0}"')
    _fake(
        bindir,
        "shasum",
        f'if [ -n "${{FAKE_SHASUM_OUT:-}}" ]; then echo "$FAKE_SHASUM_OUT"; else echo "{HASH}  $3"; fi; '
        'exit "${FAKE_SHASUM_RC:-0}"',
    )
    e = dict(os.environ)
    e["PATH"] = "{}:{}".format(bindir, e["PATH"])
    return e


def _run(dmg: Path, env: dict, **extra):
    return subprocess.run(["bash", str(SCRIPT), str(dmg)], env={**env, **extra}, capture_output=True, text=True)


def _dmg(tmp_path: Path, name: str = GOOD) -> Path:
    d = tmp_path / "out"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_bytes(b"dmg")
    return p


def _sums(tmp_path: Path):
    return list((tmp_path / "out").glob("*.sha256"))


def test_happy_path_writes_checksum_and_prints_upload(tmp_path, env):
    r = _run(_dmg(tmp_path), env)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "out" / (GOOD + ".sha256")).read_text() == f"{HASH}  {GOOD}\n"
    assert f"gh release upload v{VERSION}" in r.stdout
    assert "-R Vinix24/OnCue" in r.stdout


def test_wrong_name_refused(tmp_path, env):
    r = _run(_dmg(tmp_path, "OnCue-latest.dmg"), env)
    assert r.returncode != 0
    assert GOOD in r.stderr
    assert not _sums(tmp_path)


def test_version_mismatch_refused(tmp_path, env):
    r = _run(_dmg(tmp_path, "OnCue-9.9.9-arm64.dmg"), env)
    assert r.returncode != 0
    assert f"version {VERSION}" in r.stderr


def test_missing_file_refused(tmp_path, env):
    r = _run(tmp_path / GOOD, env)
    assert r.returncode != 0
    assert "not a file" in r.stderr


@pytest.mark.parametrize("var,msg", [("FAKE_STAPLER_RC", "stapler validate"), ("FAKE_SPCTL_RC", "spctl --assess")])
def test_failing_check_refused(tmp_path, env, var, msg):
    r = _run(_dmg(tmp_path), env, **{var: "1"})
    assert r.returncode != 0
    assert msg in r.stderr
    assert not _sums(tmp_path)


def test_malformed_shasum_output_refused(tmp_path, env):
    r = _run(_dmg(tmp_path), env, FAKE_SHASUM_OUT="garbage")
    assert r.returncode != 0
    assert "unexpected shasum output" in r.stderr
    assert not _sums(tmp_path)


def test_shasum_failure_refused(tmp_path, env):
    r = _run(_dmg(tmp_path), env, FAKE_SHASUM_RC="1")
    assert r.returncode != 0
    assert not _sums(tmp_path)


def test_upload_command_is_shell_escaped(tmp_path, env):
    d = tmp_path / "my dir $HOME `id` \"q\""
    d.mkdir()
    dmg = d / GOOD
    dmg.write_bytes(b"dmg")
    r = _run(dmg, env)
    assert r.returncode == 0, r.stderr
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("gh release upload"))
    out = subprocess.run(
        ["bash", "-c", 'printf "%s\\n" ' + line[len("gh release upload ") :]],
        capture_output=True,
        text=True,
    )
    args = out.stdout.splitlines()
    assert args == [f"v{VERSION}", str(dmg.resolve()), str(dmg.resolve()) + ".sha256", "-R", "Vinix24/OnCue"]
