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
    assert all(job["runs-on"] == "macos-15" for job in jobs.values())
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


# ── recording bar ────────────────────────────────────────────────────────────

BAR_ID = "nl.vnxdigital.oncue.bar"
darwin_only = pytest.mark.skipif(not Path("/usr/libexec/PlistBuddy").exists(), reason="needs PlistBuddy (macOS)")


def _fake_bar(app: Path, version: str, *, bundle_id: str = BAR_ID, executable: bool = True) -> Path:
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict>\n'
        f"<key>CFBundleIdentifier</key><string>{bundle_id}</string>\n"
        f"<key>CFBundleVersion</key><string>{version}</string>\n"
        "</dict></plist>\n"
    )
    exe = app / "Contents" / "MacOS" / "OnCueBar"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755 if executable else 0o644)
    return app


def _check_bar(app: Path, expected: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{SCRIPT}"; check_bar_bundle "$1" "$2"', "_", str(app), expected],
        capture_output=True,
        text=True,
        check=False,
    )


@darwin_only
def test_bar_check_accepts_a_matching_bundle(tmp_path: Path) -> None:
    result = _check_bar(_fake_bar(tmp_path / "OnCue Bar.app", "1.2.3"), "1.2.3")
    assert result.returncode == 0, result.stderr


@darwin_only
@pytest.mark.parametrize(
    "case",
    ["wrong_version", "wrong_id", "not_executable", "no_plist", "empty_expected"],
)
def test_bar_check_refuses_anything_else(case: str, tmp_path: Path) -> None:
    app = tmp_path / "OnCue Bar.app"
    expected = "1.2.3"
    if case == "wrong_version":
        _fake_bar(app, "1.2.2")
    elif case == "wrong_id":
        _fake_bar(app, "1.2.3", bundle_id="nl.example.other")
    elif case == "not_executable":
        _fake_bar(app, "1.2.3", executable=False)
    elif case == "no_plist":
        _fake_bar(app, "1.2.3")
        (app / "Contents" / "Info.plist").unlink()
    else:
        _fake_bar(app, "1.2.3")
        expected = ""
    result = _check_bar(app, expected)
    assert result.returncode == 1
    assert "bar check:" in result.stderr


def test_bar_is_built_signed_deep_and_lands_in_the_tarball(tmp_path: Path) -> None:
    """Runs the real build_bar with a fake bar build script and a fake codesign."""
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "build_oncue_bar.sh").write_text(
        '#!/bin/bash\nmkdir -p "$1/OnCue Bar.app/Contents/MacOS"\n'
        'echo built > "$1/OnCue Bar.app/Contents/MacOS/OnCueBar"\n'
    )
    shims = tmp_path / "shims"
    shims.mkdir()
    calls = tmp_path / "calls.log"
    codesign = shims / "codesign"
    codesign.write_text(f'#!/bin/sh\necho "codesign $*" >> "{calls}"\n')
    codesign.chmod(0o755)
    stage = tmp_path / "stage"
    stage.mkdir()
    script = (
        f'source "{SCRIPT}"; ROOT_DIR="{root}"; LOG_FILE="{tmp_path / "build.log"}"; '
        f'build_bar "{stage}"'
    )
    env = {"PATH": f"{shims}:/usr/bin:/bin", "HOME": str(tmp_path)}
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert (stage / "OnCue Bar.app" / "Contents" / "MacOS" / "OnCueBar").is_file()
    assert "codesign --force --deep --sign - " + str(stage / "OnCue Bar.app") in calls.read_text()

    text = SCRIPT.read_text()
    assert 'codesign --verify --deep --strict "$dir/$BAR_APP_NAME"' in text
    assert 'check_bar_bundle "$dir/$BAR_APP_NAME" "$(oncue_version)"' in text
    assert "oncue bar:" in text and '"$BAR_BUNDLE_ID" "$(oncue_version)"' in text
    assert 'audiotee "$BAR_APP_NAME" BUILD_INFO.txt' in text


def test_bar_build_failure_stops_the_release_build(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "build_oncue_bar.sh").write_text("#!/bin/bash\nexit 1\n")
    stage = tmp_path / "stage"
    stage.mkdir()
    script = f'source "{SCRIPT}"; ROOT_DIR="{root}"; LOG_FILE="{tmp_path / "build.log"}"; build_bar "{stage}"'
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "oncue bar build failed" in result.stderr
