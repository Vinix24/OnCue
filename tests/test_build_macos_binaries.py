"""Tests for the macOS release build script and its workflow."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_macos_binaries.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "release-macos-binaries.yml"


def _run_check(binary: Path) -> subprocess.CompletedProcess[str]:
    """Run the script's real check_no_external_deps function via bash."""
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; check_no_external_deps "$1"', "_", str(binary)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_syntax_is_valid() -> None:
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_script_requires_out_dir_argument() -> None:
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2
    assert "Usage" in result.stderr


def test_workflow_triggers_runner_and_permissions() -> None:
    text = WORKFLOW.read_text()
    doc = yaml.safe_load(text)
    # PyYAML reads the bare key `on` as boolean True.
    triggers = doc.get("on", doc.get(True))
    assert triggers["release"]["types"] == ["published"]
    assert "tag" in triggers["workflow_dispatch"]["inputs"]
    assert doc["permissions"] == {"contents": "write"}
    jobs = doc["jobs"]
    assert all(job["runs-on"] == "macos-14" for job in jobs.values())
    assert "self-hosted" not in text
    assert "CI_RUNNER" not in text


def test_workflow_pins_actions_to_commit_sha() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text())
    for job in doc["jobs"].values():
        for step in job["steps"]:
            uses = step.get("uses")
            if uses:
                ref = uses.split("@", 1)[1]
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), uses


def test_dependency_check_passes_on_system_only_binary() -> None:
    # /bin/ls links only /usr/lib/ libraries.
    result = _run_check(Path("/bin/ls"))
    assert result.returncode == 0, result.stderr


def test_dependency_check_fails_on_missing_binary(tmp_path: Path) -> None:
    result = _run_check(tmp_path / "does-not-exist")
    assert result.returncode == 1
    assert "does not exist" in result.stderr


def test_dependency_check_fails_when_no_dependencies_parse(tmp_path: Path) -> None:
    not_a_binary = tmp_path / "text.txt"
    not_a_binary.write_text("plain text")
    result = _run_check(not_a_binary)
    assert result.returncode == 1


def _build_rpath_binary(tmp_path: Path) -> Path:
    """Compile a tiny executable that links a dylib through @rpath."""
    (tmp_path / "lib.c").write_text("int answer(void) { return 42; }\n")
    (tmp_path / "main.c").write_text("int answer(void);\nint main(void) { return answer() == 42 ? 0 : 1; }\n")
    subprocess.run(
        ["clang", "-dynamiclib", "-install_name", "@rpath/libanswer.dylib",
         "-o", str(tmp_path / "libanswer.dylib"), str(tmp_path / "lib.c")],
        check=True, capture_output=True,
    )
    exe = tmp_path / "rpath-exe"
    subprocess.run(
        ["clang", "-o", str(exe), str(tmp_path / "main.c"), "-L", str(tmp_path), "-lanswer",
         "-Wl,-rpath,@executable_path"],
        check=True, capture_output=True,
    )
    return exe


@pytest.mark.skipif(
    shutil.which("otool") is None or shutil.which("clang") is None,
    reason="needs otool and clang (macOS command line tools)",
)
def test_dependency_check_fails_on_rpath_binary(tmp_path: Path) -> None:
    exe = _build_rpath_binary(tmp_path)
    listing = subprocess.run(["otool", "-L", str(exe)], capture_output=True, text=True, check=True).stdout
    assert "@rpath/libanswer.dylib" in listing
    result = _run_check(exe)
    assert result.returncode == 1
    assert "non-system library" in result.stderr
    assert "@rpath/libanswer.dylib" in result.stderr
