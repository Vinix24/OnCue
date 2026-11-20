"""normalize_bundle_macho trims bytes behind the last Mach-O segment and nothing else."""
from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "normalize_bundle_macho", Path(__file__).resolve().parents[1] / "scripts" / "normalize_bundle_macho.py"
)
nb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(nb)


def _macho(fileoff: int = 16, filesize: int = 96, extra: bytes = b"") -> bytes:
    seg = struct.pack("<II16sQQQQ", nb.LC_SEGMENT_64, 72, b"__LINKEDIT", 0, 0x1000, fileoff, filesize)
    seg += b"\0" * (72 - len(seg))
    header = struct.pack("<IIIIIIII", nb.MH_MAGIC_64, 0x0100000C, 0, 6, 1, len(seg), 0, 0)
    body = header + seg
    body += b"\0" * (fileoff + filesize - len(body))
    return body + extra


def test_extent_is_end_of_last_segment(tmp_path):
    p = tmp_path / "a.dylib"
    p.write_bytes(_macho(extra=b"\x01" * 10))
    assert nb.macho_extent(p) == 112


def test_trim_cuts_stray_data_and_reports_change(tmp_path):
    p = tmp_path / "a.dylib"
    p.write_bytes(_macho(extra=b"\x01" * 10 + b"\0" * 5))
    assert nb.trim(p) is True
    assert p.stat().st_size == 112


def test_trim_leaves_exact_file_alone(tmp_path):
    p = tmp_path / "a.dylib"
    p.write_bytes(_macho())
    assert nb.trim(p) is False
    assert p.stat().st_size == 112


def test_not_macho_and_truncated_are_untouched(tmp_path):
    text = tmp_path / "b.so"
    text.write_bytes(b"not a mach-o file at all, but long enough to read a header from")
    short = tmp_path / "c.so"
    short.write_bytes(b"\xcf\xfa\xed")
    assert nb.macho_extent(text) is None and nb.trim(text) is False
    assert nb.macho_extent(short) is None and nb.trim(short) is False


def test_corrupt_load_command_size_is_not_trusted(tmp_path):
    data = bytearray(_macho(extra=b"\x01" * 4))
    data[36:40] = struct.pack("<I", 0)  # cmdsize of the first load command
    p = tmp_path / "d.dylib"
    p.write_bytes(bytes(data))
    assert nb.macho_extent(p) is None
    assert nb.trim(p) is False


def test_main_requires_an_app_directory(tmp_path):
    assert nb.main(["x", str(tmp_path / "missing.app")]) == 1
    assert nb.main(["x"]) == 1


def _app_with_stray_dylib(tmp_path):
    app = tmp_path / "X.app"
    app.mkdir()
    (app / "a.dylib").write_bytes(_macho(extra=b"\x01" * 10))
    return app


def test_main_fails_with_stderr_when_codesign_fails(tmp_path, monkeypatch, capsys):
    app = _app_with_stray_dylib(tmp_path)

    def fake_run(cmd, **kwargs):
        return nb.subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom: not signed")

    monkeypatch.setattr(nb.subprocess, "run", fake_run)
    assert nb.main(["normalize_bundle_macho", str(app)]) == 1
    err = capsys.readouterr().err
    assert "boom: not signed" in err and "a.dylib" in err


def test_main_succeeds_when_codesign_succeeds(tmp_path, monkeypatch, capsys):
    app = _app_with_stray_dylib(tmp_path)
    monkeypatch.setattr(
        nb.subprocess, "run",
        lambda cmd, **kw: nb.subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    assert nb.main(["normalize_bundle_macho", str(app)]) == 0
    assert "trimmed 1 file" in capsys.readouterr().out
