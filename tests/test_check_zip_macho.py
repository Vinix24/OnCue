"""scripts/check_zip_macho.py, its place in build_dmg.sh and the unzipped packages in setup_app.py."""

from __future__ import annotations

import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check_zip_macho.py"
THIN64 = b"\xcf\xfa\xed\xfe" + b"\x07\x00\x00\x01" + b"\x00" * 24
THIN32_BE = b"\xfe\xed\xfa\xce" + b"\x00" * 24
FAT = b"\xca\xfe\xba\xbe" + struct.pack(">I", 2) + b"\x00" * 24
JAVA_CLASS = b"\xca\xfe\xba\xbe" + struct.pack(">I", 65) + b"\x00" * 24


def _bundle(tmp_path: Path, members: dict[str, bytes], *, compression: int = zipfile.ZIP_STORED) -> Path:
    app = tmp_path / "OnCue.app"
    lib = app / "Contents" / "Resources" / "lib"
    lib.mkdir(parents=True)
    with zipfile.ZipFile(lib / "python312.zip", "w", compression=compression) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return app


def _run(app: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CHECK), str(app)], capture_output=True, text=True, check=False)


@pytest.mark.parametrize(
    "data", [THIN64, THIN32_BE, FAT, b"\xfe\xed\xfa\xcf" + b"\x00" * 8, b"\xca\xfe\xba\xbf" + struct.pack(">I", 1)]
)
def test_macho_header_in_a_zip_fails_and_is_listed_whatever_the_extension(tmp_path: Path, data: bytes) -> None:
    app = _bundle(tmp_path, {"pkg/mod.py": b"x = 1\n", "PIL/.dylibs/libz.dylib": data, "pkg/blob.data": data})
    result = _run(app)
    assert result.returncode == 1
    assert "python312.zip/PIL/.dylibs/libz.dylib" in result.stderr
    assert "python312.zip/pkg/blob.data" in result.stderr
    assert "mod.py" not in result.stderr


def test_zip_with_only_python_and_data_passes(tmp_path: Path) -> None:
    app = _bundle(
        tmp_path, {"pkg/mod.py": b"x = 1\n", "pkg/mod.pyc": b"\xa7\x0d\x0d\x0a" + b"\x00" * 12, "d.json": b"{}"}
    )
    result = _run(app)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_java_class_header_is_not_a_fat_macho(tmp_path: Path) -> None:
    assert _run(_bundle(tmp_path, {"x/Y.class": JAVA_CLASS, "short": b"\xca\xfe"})).returncode == 0


def test_real_file_on_disk_is_not_the_checks_business(tmp_path: Path) -> None:
    app = _bundle(tmp_path, {"a.py": b"1\n"})
    (app / "Contents" / "Resources" / "lib" / "libx.dylib").write_bytes(THIN64)
    assert _run(app).returncode == 0


def test_missing_bundle_and_corrupt_zip_fail_closed(tmp_path: Path) -> None:
    assert _run(tmp_path / "absent.app").returncode == 2
    app = _bundle(tmp_path, {"a.py": b"1\n"})
    zip_path = app / "Contents" / "Resources" / "lib" / "python312.zip"
    zip_path.write_bytes(zip_path.read_bytes()[:-30])
    result = _run(app)
    assert result.returncode == 2 and "cannot read zip" in result.stderr


def test_corrupt_deflated_member_fails_closed_without_a_traceback(tmp_path: Path) -> None:
    # An intact central directory with garbage in a deflated member: opening the archive succeeds
    # and member.read(8) raises zlib.error. That must be exit 2 with the documented message,
    # not an uncaught traceback (exit 1).
    app = _bundle(tmp_path, {"pkg/mod.py": b"x = 1\n" * 100}, compression=zipfile.ZIP_DEFLATED)
    zip_path = app / "Contents" / "Resources" / "lib" / "python312.zip"
    raw = bytearray(zip_path.read_bytes())
    assert raw[:4] == b"PK\x03\x04"
    name_len = struct.unpack("<H", raw[26:28])[0]
    extra_len = struct.unpack("<H", raw[28:30])[0]
    comp_size = struct.unpack("<I", raw[18:22])[0]
    start = 30 + name_len + extra_len
    assert comp_size > 0
    raw[start : start + comp_size] = b"\xff" * comp_size
    zip_path.write_bytes(bytes(raw))
    result = _run(app)
    assert result.returncode == 2
    assert "cannot read zip" in result.stderr
    assert "Traceback" not in result.stderr


def test_build_dmg_runs_the_check_after_py2app_and_before_signing() -> None:
    text = (ROOT / "scripts" / "build_dmg.sh").read_text(encoding="utf-8")
    py2app = text.index("setup_app.py py2app")
    check = text.index('check_zip_macho.py" "$APP"')
    gate = text.index('scripts/check_bundle_gate.py" "$APP"')
    sign = text.index('scripts/normalize_bundle_macho.py" "$APP"')
    assert py2app < gate < check < sign < text.index('scripts/sign_app.py" app')


def test_setup_app_unzips_the_packages_that_carry_binaries() -> None:
    import setup_app

    assert set(setup_app.UNZIPPED_BINARY_PACKAGES) >= {"PIL", "mlx", "google", "_sounddevice_data", "llvmlite"}
    source = (ROOT / "setup_app.py").read_text(encoding="utf-8")
    assert "*UNZIPPED_BINARY_PACKAGES" in source.split('"packages": [')[1].split('"includes"')[0]
    assert 'cmdclass={"py2app": _Py2appUnzipped}' in source


def test_package_dir_resolves_a_namespace_package_and_refuses_a_missing_one(tmp_path: Path, monkeypatch) -> None:
    import setup_app

    (tmp_path / "nspkg_demo" / "lib").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(tmp_path))
    assert setup_app._package_dir("nspkg_demo") == str((tmp_path / "nspkg_demo").resolve())
    with pytest.raises(RuntimeError, match="cannot unzip"):
        setup_app._package_dir("no_such_pkg_xyz")
