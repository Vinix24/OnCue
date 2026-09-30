"""Tests for the release version gate in scripts/build_release.sh.

The version in ``pyproject.toml`` is not decoration: pip decides whether to
reinstall by comparing it. Cut a release without bumping it and
``pipx upgrade live-sales-copilot`` prints "already at latest version" and
installs nothing -- no new code, and no wrapper in ``bin/`` for any command the
new version added under ``[project.scripts]``, because pip writes those at
install time only. That second half is completely silent: the command is simply
not there, forever.

The gate runs the real bash script against a throwaway repo skeleton (never a
reimplementation of its logic in Python), so nothing here can touch the real
``dist/`` directory or build an actual artifact.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_RELEASE = REPO_ROOT / "scripts" / "build_release.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _declared_version() -> str:
    with PYPROJECT.open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def _build_fake_repo(tmp_path: Path, *, pyproject_version: str, package_version: str) -> Path:
    """A skeleton with just the two files the version gate reads."""
    (tmp_path / "scripts").mkdir(parents=True)
    script_copy = tmp_path / "scripts" / "build_release.sh"
    script_copy.write_text(BUILD_RELEASE.read_text())

    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "live-sales-copilot"\nversion = "{pyproject_version}"\n'
    )
    package = tmp_path / "src" / "sales_copilot"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'__version__ = "{package_version}"\n')
    return script_copy


def _run(script_copy: Path, tag: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script_copy), tag],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_package_version_matches_pyproject() -> None:
    """Two hardcoded copies of the version exist; they must not drift.

    ``setup_app.py`` reads pyproject for the .app bundle version while the
    running app logs ``sales_copilot.__version__``. A release that bumps one and
    not the other ships a build that misreports which version it is.
    """
    from sales_copilot import __version__

    assert __version__ == _declared_version()


def test_build_release_refuses_a_tag_the_declared_version_does_not_match(tmp_path: Path) -> None:
    script_copy = _build_fake_repo(tmp_path, pyproject_version="1.2.3", package_version="1.2.3")

    result = _run(script_copy, "v9.9.9")

    assert result.returncode != 0
    assert "version mismatch" in result.stderr
    assert "1.2.3" in result.stderr and "9.9.9" in result.stderr
    # It stops at the gate, before it can create or remove anything.
    assert not (tmp_path / "dist").exists()


def test_build_release_refuses_when_the_two_version_copies_disagree(tmp_path: Path) -> None:
    script_copy = _build_fake_repo(tmp_path, pyproject_version="1.2.3", package_version="1.2.2")

    result = _run(script_copy, "v1.2.3")

    assert result.returncode != 0
    assert "version mismatch inside the repo" in result.stderr
    assert not (tmp_path / "dist").exists()


def test_build_release_passes_the_gate_when_tag_and_both_copies_agree(tmp_path: Path) -> None:
    """A matching tag clears the gate and the build proceeds to its own
    preflight, which fails on the absent launcher app -- so the gate is the only
    thing under test and no artifact is ever produced."""
    script_copy = _build_fake_repo(tmp_path, pyproject_version="1.2.3", package_version="1.2.3")

    result = _run(script_copy, "v1.2.3")

    assert "Version gate" in result.stdout
    assert "1.2.3" in result.stdout
    assert "version mismatch" not in result.stderr
    assert result.returncode != 0
    assert "Start OnCue.app not found" in result.stderr
    assert not (tmp_path / "dist").exists()


def test_build_release_accepts_a_tag_without_the_leading_v(tmp_path: Path) -> None:
    """``build_release.sh 1.2.3`` normalises to v1.2.3; the gate compares the
    version part, not the tag spelling."""
    script_copy = _build_fake_repo(tmp_path, pyproject_version="1.2.3", package_version="1.2.3")

    result = _run(script_copy, "1.2.3")

    assert "Version gate" in result.stdout
    assert "version mismatch" not in result.stderr
