#!/usr/bin/env python3
"""Zip Mach-O check: fail when a zip inside a built OnCue.app holds a Mach-O file.

Usage: check_zip_macho.py <OnCue.app>

codesign cannot reach a file inside a zip (py2app's python312.zip), and notarization
rejects the bundle for every unsigned or unsealed Mach-O in there. Members are detected by
their magic bytes, not by file extension: thin 32 and 64 bit (either byte order) and fat
archives. Exit 0: no Mach-O inside any zip. Exit 1: hits (each member printed). Exit 2: the
check could not run (missing bundle, unreadable zip); it never passes on a failure.
Called by scripts/build_dmg.sh after py2app and before signing, next to check_bundle_gate.py.
"""

from __future__ import annotations

import argparse
import lzma
import struct
import sys
import zipfile
import zlib
from pathlib import Path

THIN = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"}
FAT = {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
# 0xcafebabe is also the magic of a Java class file; a fat header counts few architectures,
# a class file holds a version number there.
MAX_FAT_ARCHS = 30


class CheckError(Exception):
    """The check could not run to a verdict."""


def is_macho(header: bytes) -> bool:
    """True when the first bytes of a file are a Mach-O (thin or fat) header."""
    if header[:4] in THIN:
        return True
    return header[:4] in FAT and len(header) >= 8 and struct.unpack(">I", header[4:8])[0] < MAX_FAT_ARCHS


def _is_zip(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"PK\x03\x04"
    except OSError as exc:
        raise CheckError(f"cannot read {path}: {exc}") from exc


def scan(bundle: Path) -> list[str]:
    """Every ``<zip>/<member>`` in the bundle whose content starts with a Mach-O header."""
    if not bundle.is_dir():
        raise CheckError(f"{bundle} is not a directory")
    hits = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.is_symlink() or not _is_zip(path):
            continue
        rel = path.relative_to(bundle).as_posix()
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    with archive.open(info) as member:
                        if is_macho(member.read(8)):
                            hits.append(f"{rel}/{info.filename}")
        except (
            zipfile.BadZipFile,
            OSError,
            NotImplementedError,
            RuntimeError,
            zlib.error,
            lzma.LZMAError,
            EOFError,
        ) as exc:
            # zlib/lzma errors and EOFError: an intact central directory with corrupt or truncated
            # member data fails on member.read(8), not on opening the archive.
            raise CheckError(f"cannot read zip {path}: {exc}") from exc
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        hits = scan(args.bundle)
    except CheckError as exc:
        print(f"ZIP MACH-O CHECK ERROR: {exc}", file=sys.stderr)
        return 2
    if hits:
        print(f"ZIP MACH-O CHECK FAILED: {len(hits)} Mach-O member(s) inside zips of {args.bundle}:", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        return 1
    print(f"ZIP MACH-O CHECK OK: {args.bundle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
