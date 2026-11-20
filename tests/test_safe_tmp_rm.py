"""scripts/safe_tmp_rm.sh: guarded removal of a temp folder, and the build_dmg.sh trap wiring."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
HELPER = SCRIPTS / "safe_tmp_rm.sh"


def _rm(
    target: str, tmpdir: Path | str | None, home: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home or "/nonexistent-home")}
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
    return subprocess.run(
        ["bash", "-c", f'source "{HELPER}"; safe_tmp_rm "$1"', "bash", target],
        capture_output=True, text=True, env=env, check=False,
    )


def test_removes_a_folder_under_tmpdir(tmp_path: Path) -> None:
    work = tmp_path / "tmp.abc"
    (work / "stage").mkdir(parents=True)
    (work / "stage" / "file").write_text("x", encoding="utf-8")
    result = _rm(str(work), tmp_path)
    assert result.returncode == 0, result.stderr
    assert not work.exists()


def test_refuses_a_path_outside_tmpdir(tmp_path: Path) -> None:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    outside = tmp_path / "keep-me"
    outside.mkdir()
    (outside / "file").write_text("x", encoding="utf-8")
    result = _rm(str(outside), tmpdir)
    assert result.returncode == 1
    assert "not under the temp directory" in result.stderr
    assert (outside / "file").exists()


def test_refuses_the_tmpdir_itself(tmp_path: Path) -> None:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = _rm(str(tmpdir), tmpdir)
    assert result.returncode == 1
    assert tmpdir.exists()


def test_refuses_the_tmpdir_itself_when_it_lies_beneath_another_root(tmp_path: Path) -> None:
    inner = tmp_path / "outer" / "inner"
    inner.mkdir(parents=True)
    (inner / "file").write_text("x", encoding="utf-8")
    result = _rm(str(inner), inner)
    assert result.returncode == 1
    assert inner.exists() and (inner / "file").exists()


def test_refuses_a_folder_under_tmp_when_tmpdir_points_elsewhere(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    victim = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        result = _rm(str(victim), other)
        assert result.returncode == 1
        assert "not under the temp directory" in result.stderr
        assert victim.exists()
    finally:
        shutil.rmtree(victim, ignore_errors=True)


@pytest.mark.parametrize("mode", ["unset", "empty"])
def test_without_tmpdir_only_strictly_below_tmp_is_removed(mode: str) -> None:
    tmpdir = None if mode == "unset" else ""
    victim = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        result = _rm(str(victim), tmpdir)
        assert result.returncode == 0, result.stderr
        assert not victim.exists()
        refused = _rm("/tmp", tmpdir)
        assert refused.returncode == 1
        assert Path("/tmp").is_dir()
    finally:
        shutil.rmtree(victim, ignore_errors=True)


@pytest.mark.parametrize("target", ["", "/"])
def test_refuses_empty_and_root(tmp_path: Path, target: str) -> None:
    result = _rm(target, tmp_path)
    assert result.returncode == 1
    assert "refusing" in result.stderr


def test_refuses_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = _rm(str(home), tmp_path, home=home)
    assert result.returncode == 1
    assert home.exists()


def test_refuses_a_symlink_that_points_outside(tmp_path: Path) -> None:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    link = tmpdir / "link"
    os.symlink(outside, link)
    result = _rm(str(link), tmpdir)
    assert result.returncode == 1
    assert outside.exists()


def test_refuses_a_missing_folder(tmp_path: Path) -> None:
    result = _rm(str(tmp_path / "absent"), tmp_path)
    assert result.returncode == 1
    assert "not a directory" in result.stderr


def test_build_dmg_wires_the_trap_and_keep_switch() -> None:
    text = (SCRIPTS / "build_dmg.sh").read_text(encoding="utf-8")
    assert "trap cleanup_work EXIT" in text
    assert "ONCUE_KEEP_BUILD_TMP" in text
    assert 'safe_tmp_rm "$WORK"' in text
    assert "rm -rf" not in text
