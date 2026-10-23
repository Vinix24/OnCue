"""constraints.txt must pin exactly what uv.lock locks.

CI and both installers install with `-c constraints.txt`. A lock bump that does
not carry the constraints along would make installs resolve to versions CI
never tested. These tests run the real scripts/constraints.py code against the
real files, plus against small fixtures for the failure paths.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "constraints.py"

_spec = importlib.util.spec_from_file_location("constraints_script", SCRIPT)
assert _spec is not None and _spec.loader is not None
constraints = importlib.util.module_from_spec(_spec)
sys.modules["constraints_script"] = constraints
_spec.loader.exec_module(constraints)

LOCK_FIXTURE = """\
version = 1

[[package]]
name = "Some_Pkg"
version = "1.2.3"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "other-pkg"
version = "4.5.6"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "live-sales-copilot"
version = "1.0.0rc3"
source = { editable = "." }
"""


def test_repo_constraints_match_repo_lock() -> None:
    assert constraints.diff(constraints.LOCK_PATH, constraints.CONSTRAINTS_PATH) == []


def test_check_mode_exits_zero_on_repo() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False, cwd=REPO_ROOT
    )
    assert result.returncode == 0, result.stderr


def test_lock_pins_skip_the_project_and_normalize_names(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK_FIXTURE, encoding="utf-8")
    assert constraints.lock_pins(lock) == {("some-pkg", "1.2.3"), ("other-pkg", "4.5.6")}


def test_diff_reports_a_version_bump_missing_from_constraints(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK_FIXTURE.replace("1.2.3", "2.0.0"), encoding="utf-8")
    pins = tmp_path / "constraints.txt"
    pins.write_text("# header\nsome-pkg==1.2.3\nother-pkg==4.5.6 ; sys_platform == 'win32'\n", encoding="utf-8")
    problems = constraints.diff(lock, pins)
    assert "in uv.lock but not in constraints.txt: some-pkg==2.0.0" in problems
    assert "in constraints.txt but not in uv.lock: some-pkg==1.2.3" in problems
    assert len(problems) == 2


def test_diff_is_empty_when_markers_and_name_spelling_differ(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK_FIXTURE, encoding="utf-8")
    pins = tmp_path / "constraints.txt"
    pins.write_text("Some_Pkg==1.2.3 ; python_full_version >= '3.13'\nother-pkg==4.5.6\n", encoding="utf-8")
    assert constraints.diff(lock, pins) == []


def test_constraints_rejects_a_line_that_is_not_a_pin(tmp_path: Path) -> None:
    pins = tmp_path / "constraints.txt"
    pins.write_text("some-pkg>=1.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a pinned requirement"):
        constraints.constraint_pins(pins)


def test_lock_package_without_version_is_an_error(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text('[[package]]\nname = "broken"\nsource = { registry = "x" }\n', encoding="utf-8")
    with pytest.raises(ValueError, match="has no version"):
        constraints.lock_pins(lock)


def test_check_mode_fails_when_constraints_file_is_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constraints, "CONSTRAINTS_PATH", tmp_path / "constraints.txt")
    assert constraints.main(["--check"]) == 1


INSTALLER_TOOLING = ("pip", "setuptools", "wheel")


def test_repo_constraints_do_not_pin_installer_tooling() -> None:
    pinned = {name for name, _ in constraints.constraint_pins(constraints.CONSTRAINTS_PATH)}
    assert pinned.isdisjoint(INSTALLER_TOOLING)


def test_lock_pins_leave_out_installer_tooling(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    extra = "".join(
        f'\n[[package]]\nname = "{name}"\nversion = "1.0.0"\nsource = {{ registry = "https://pypi.org/simple" }}\n'
        for name in ("pip", "Setuptools", "wheel")
    )
    lock.write_text(LOCK_FIXTURE + extra, encoding="utf-8")
    assert constraints.lock_pins(lock) == {("some-pkg", "1.2.3"), ("other-pkg", "4.5.6")}


def test_diff_flags_installer_tooling_pinned_in_constraints(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK_FIXTURE, encoding="utf-8")
    pins = tmp_path / "constraints.txt"
    pins.write_text("some-pkg==1.2.3\nother-pkg==4.5.6\nsetuptools==81.0.0\n", encoding="utf-8")
    assert constraints.diff(lock, pins) == ["in constraints.txt but not in uv.lock: setuptools==81.0.0"]


def test_export_drops_installer_tooling(monkeypatch: pytest.MonkeyPatch) -> None:
    exported = "pip==26.2.1\nsome-pkg==1.2.3\nSetuptools==81.0.0 ; python_full_version >= '3.12'\nwheel==0.45.0\n"

    class Result:
        returncode = 0
        stdout = exported
        stderr = ""

    monkeypatch.setattr(constraints.subprocess, "run", lambda *a, **k: Result())
    content = constraints.export_constraints()
    assert "some-pkg==1.2.3" in content
    pinned = {
        line.split("==")[0].strip().lower()
        for line in content.splitlines()
        if "==" in line and not line.startswith("#")
    }
    assert pinned == {"some-pkg"}
