"""Bundle gate tests: real files in a temp dir, the real script as a subprocess."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts" / "check_bundle_gate.py"
EXPORT_SCRIPT = """\
FORBIDDEN_PATHS=(
    ".vnx"
    # a comment
    "bin"
    "config/objection_responses.yaml"
    "src/sales_copilot/auth/_dev_keys.py"
)
"""


@pytest.fixture()
def export_script(tmp_path: Path) -> Path:
    path = tmp_path / "export_public.sh"
    path.write_text(EXPORT_SCRIPT, encoding="utf-8")
    return path


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "OnCue.app"
    res = root / "Contents" / "Resources"
    (res / "bin").mkdir(parents=True)
    (res / "bin" / "audiotee").write_bytes(b"\xcf\xfa")
    (res / "config").mkdir()
    (res / "config" / "pain_points.yaml").write_text("x: 1\n")
    pkg = res / "lib" / "python3.12" / "sales_copilot"
    pkg.mkdir(parents=True)
    (pkg / "__init__.pyc").write_bytes(b"\x00")
    return root


def run(bundle: Path, export_script: Path) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(GATE), str(bundle), "--export-script", str(export_script)]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def test_clean_bundle_passes_and_audiotee_is_allowed(bundle: Path, export_script: Path) -> None:
    result = run(bundle, export_script)
    assert result.returncode == 0, result.stderr
    assert "BUNDLE GATE OK" in result.stdout


def test_forbidden_file_fails_and_is_named(bundle: Path, export_script: Path) -> None:
    (bundle / "Contents" / "Resources" / "config" / "objection_responses.yaml").write_text("secret: pro\n")
    result = run(bundle, export_script)
    assert result.returncode == 1
    assert "config/objection_responses.yaml" in result.stderr


def test_deep_lane_pyc_fails_even_if_export_list_omits_it(bundle: Path, export_script: Path) -> None:
    lane = bundle / "Contents" / "Resources" / "lib" / "python3.12" / "sales_copilot" / "modules" / "insight"
    lane.mkdir(parents=True)
    (lane / "engine.pyc").write_bytes(b"\x00")
    result = run(bundle, export_script)
    assert result.returncode == 1
    assert "modules/insight/engine.pyc" in result.stderr


def test_forbidden_py_entry_matches_its_pyc(bundle: Path, export_script: Path) -> None:
    auth = bundle / "Contents" / "Resources" / "lib" / "python3.12" / "sales_copilot" / "auth"
    auth.mkdir()
    (auth / "_dev_keys.pyc").write_bytes(b"\x00")
    assert run(bundle, export_script).returncode == 1


def test_forbidden_member_inside_zip_fails(bundle: Path, export_script: Path) -> None:
    archive = bundle / "Contents" / "Resources" / "lib" / "python312.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("sales_copilot/mcp_bridge/host.pyc", b"\x00")
    result = run(bundle, export_script)
    assert result.returncode == 1
    assert "python312.zip/sales_copilot/mcp_bridge/host.pyc" in result.stderr


def test_bin_is_only_forbidden_where_the_export_root_lands(bundle: Path, export_script: Path) -> None:
    res = bundle / "Contents" / "Resources"
    (res / "lib" / "torch" / "bin").mkdir(parents=True)
    (res / "lib" / "torch" / "bin" / "protoc").write_text("x")
    assert run(bundle, export_script).returncode == 0
    (res / "bin" / "private-tool").write_text("x")
    result = run(bundle, export_script)
    assert result.returncode == 1
    assert "Contents/Resources/bin/private-tool" in result.stderr


def test_gate_that_cannot_run_is_not_a_pass(bundle: Path, tmp_path: Path, export_script: Path) -> None:
    assert run(bundle, tmp_path / "nope.sh").returncode == 2
    empty = tmp_path / "empty.sh"
    empty.write_text("FORBIDDEN_PATHS=(\n)\n")
    assert run(bundle, empty).returncode == 2
    assert run(tmp_path / "no-such.app", export_script).returncode == 2


def test_pyc_that_merely_contains_a_zip_signature_is_not_a_zip(bundle: Path, export_script: Path) -> None:
    pyc = bundle / "Contents" / "Resources" / "lib" / "python3.12" / "sales_copilot" / "upload.pyc"
    pyc.write_bytes(b"\x00\x01" + b"PK\x05\x06" + b"\x00" * 20)
    assert run(bundle, export_script).returncode == 0


def _put(bundle: Path, rel: str) -> Path:
    path = bundle / "Contents" / "Resources" / "lib" / "python3.12" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00")
    return path


COMPILED_LAYOUTS = [
    "sales_copilot/auth/_dev_keys.pyc",
    "sales_copilot/auth/__pycache__/_dev_keys.cpython-312.pyc",
    "sales_copilot/auth/__pycache__/_dev_keys.cpython-312.opt-1.pyc",
    "sales_copilot/auth/__pycache__/_dev_keys.cpython-312.opt-2.pyc",
    "sales_copilot/auth/__pycache__/_dev_keys.pyc",
    "sales_copilot/auth/_dev_keys.py",
]


@pytest.mark.parametrize("layout", COMPILED_LAYOUTS)
def test_forbidden_module_fails_in_every_compiled_layout(bundle: Path, export_script: Path, layout: str) -> None:
    _put(bundle, layout)
    result = run(bundle, export_script)
    assert result.returncode == 1, result.stdout
    assert "_dev_keys" in result.stderr


@pytest.mark.parametrize("layout", COMPILED_LAYOUTS)
def test_allowed_module_passes_in_every_compiled_layout(bundle: Path, export_script: Path, layout: str) -> None:
    _put(bundle, layout.replace("_dev_keys", "tokens"))
    result = run(bundle, export_script)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("layout", COMPILED_LAYOUTS)
def test_forbidden_module_inside_zip_fails(bundle: Path, export_script: Path, layout: str) -> None:
    archive = bundle / "Contents" / "Resources" / "lib" / "python312.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(layout, b"\x00")
    result = run(bundle, export_script)
    assert result.returncode == 1, result.stdout
    assert "_dev_keys" in result.stderr


@pytest.mark.parametrize("layout", COMPILED_LAYOUTS)
def test_allowed_module_inside_zip_passes(bundle: Path, export_script: Path, layout: str) -> None:
    archive = bundle / "Contents" / "Resources" / "lib" / "python312.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(layout.replace("_dev_keys", "tokens"), b"\x00")
    result = run(bundle, export_script)
    assert result.returncode == 0, result.stderr


def test_deep_lane_pycache_fails(bundle: Path, export_script: Path) -> None:
    _put(bundle, "sales_copilot/mcp_bridge/__pycache__/server.cpython-312.pyc")
    assert run(bundle, export_script).returncode == 1


def test_deep_lane_only_flag_checks_deep_lane_without_export_script(bundle: Path, tmp_path: Path) -> None:
    missing = tmp_path / "absent.sh"
    cmd = [sys.executable, str(GATE), str(bundle), "--export-script", str(missing)]
    assert subprocess.run(cmd, capture_output=True, text=True, check=False).returncode == 2
    cmd = [sys.executable, str(GATE), str(bundle), "--deep-lane-only"]
    assert subprocess.run(cmd, capture_output=True, text=True, check=False).returncode == 0
    _put(bundle, "sales_copilot/modules/insight/__pycache__/x.cpython-312.pyc")
    assert subprocess.run(cmd, capture_output=True, text=True, check=False).returncode == 1


def test_build_dmg_in_public_tree_builds_from_current_tree(tmp_path: Path) -> None:
    """No export_public.sh next to the script: it must not call the export and must not refuse the tree."""
    import platform

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        pytest.skip("build_dmg.sh needs an Apple Silicon Mac")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("build_dmg.sh", "safe_tmp_rm.sh"):
        (scripts / name).write_text((GATE.parent / name).read_text(encoding="utf-8"))
    result = subprocess.run(
        ["bash", str(scripts / "build_dmg.sh"), "--out", str(tmp_path / "out")],
        capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "Public tree: building from" in result.stdout
    assert "is not an export tree" in result.stderr
    assert "refusing to build from the private checkout" not in result.stderr


def test_build_dmg_in_private_checkout_still_refuses_itself(tmp_path: Path) -> None:
    import platform

    if platform.system() != "Darwin" or platform.machine() != "arm64":
        pytest.skip("build_dmg.sh needs an Apple Silicon Mac")
    if not (GATE.parent / "export_public.sh").is_file():
        pytest.skip("public tree: there is no private checkout to refuse")
    root = GATE.parent.parent
    result = subprocess.run(
        ["bash", str(GATE.parent / "build_dmg.sh"), "--source", str(root), "--out", str(tmp_path)],
        capture_output=True, text=True, check=False, env={"PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "refusing to build from the private checkout" in result.stderr


def test_forbidden_file_checks_forbidden_paths_not_only_deep_lane(bundle: Path, tmp_path: Path) -> None:
    """Public tree: the list file replaces the export script and a FORBIDDEN_PATHS entry hits."""
    listing = tmp_path / "list.txt"
    listing.write_text("# comment\nconfig/objection_responses.yaml\n", encoding="utf-8")
    cmd = [sys.executable, str(GATE), str(bundle), "--forbidden-file", str(listing)]
    assert subprocess.run(cmd, capture_output=True, text=True, check=False).returncode == 0
    (bundle / "Contents" / "Resources" / "config" / "objection_responses.yaml").write_text("x: 1\n")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "objection_responses.yaml" in result.stderr


def test_forbidden_file_missing_or_empty_is_a_gate_error(bundle: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("# only a comment\n", encoding="utf-8")
    for target in (empty, tmp_path / "absent.txt"):
        cmd = [sys.executable, str(GATE), str(bundle), "--forbidden-file", str(target)]
        assert subprocess.run(cmd, capture_output=True, text=True, check=False).returncode == 2


def test_shipped_forbidden_file_matches_export_script() -> None:
    root = Path(__file__).resolve().parents[1]
    export = root / "scripts" / "export_public.sh"
    if not export.exists():
        pytest.skip("public tree: no export script to compare against")
    sys.path.insert(0, str(root / "scripts"))
    try:
        import check_bundle_gate as gate
    finally:
        sys.path.pop(0)
    assert gate.parse_forbidden_file(root / "scripts" / "bundle_forbidden_paths.txt") == gate.parse_forbidden(export)


def test_build_dmg_public_tree_passes_the_forbidden_list() -> None:
    text = (Path(__file__).resolve().parents[1] / "scripts" / "build_dmg.sh").read_text(encoding="utf-8")
    assert "--deep-lane-only" not in text
    assert "--forbidden-file" in text
