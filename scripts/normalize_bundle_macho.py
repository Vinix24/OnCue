#!/usr/bin/env python3
"""Trim the bytes behind the last segment of the Mach-O files in a built .app.

py2app rewrites the load commands of the libraries it copies and leaves the file
longer than its last segment (padding plus a stray signature) and a signature that no longer matches. codesign
then refuses the bundle ("main executable failed strict validation"). This drops
the trailing bytes and the stale signature so the bundle can be signed again.

Usage: normalize_bundle_macho.py <App.app>
Exit code 0 when done, 1 on an unreadable bundle path.
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

MH_MAGIC_64 = 0xFEEDFACF
LC_SEGMENT_64 = 0x19
_SUFFIXES = {".dylib", ".so"}


def macho_extent(path: Path) -> int | None:
    """End offset of the last segment of a thin 64-bit Mach-O file, else None."""
    with path.open("rb") as fh:
        head = fh.read(32)
        if len(head) < 32 or struct.unpack("<I", head[:4])[0] != MH_MAGIC_64:
            return None
        ncmds, sizeofcmds = struct.unpack("<II", head[16:24])
        cmds = fh.read(sizeofcmds)
    end = 0
    off = 0
    for _ in range(ncmds):
        if off + 8 > len(cmds):
            return None
        cmd, size = struct.unpack("<II", cmds[off : off + 8])
        if size < 8:
            return None
        if cmd == LC_SEGMENT_64:
            fileoff, filesize = struct.unpack("<QQ", cmds[off + 40 : off + 56])
            end = max(end, fileoff + filesize)
        off += size
    return end or None


def trim(path: Path) -> bool:
    """Cut everything behind the last segment (padding and a stray signature). True when changed.

    Nothing behind the last segment is ever loaded, so dropping it cannot change behaviour.
    """
    extent = macho_extent(path)
    if extent is None:
        return False
    size = path.stat().st_size
    if size <= extent:
        return False
    with path.open("r+b") as fh:
        fh.truncate(extent)
    return True


def candidates(app: Path):
    for p in sorted(app.rglob("*")):
        if p.is_symlink() or not p.is_file():
            continue
        if p.suffix in _SUFFIXES or p.parent.name in {"MacOS", "bin"}:
            yield p


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not Path(argv[1]).is_dir():
        print(__doc__, file=sys.stderr)
        return 1
    changed = 0
    for p in candidates(Path(argv[1])):
        if trim(p):
            # The old signature no longer matches the header; codesign rebuilds it.
            result = subprocess.run(["codesign", "--remove-signature", str(p)],
                                    check=False, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"normalize_bundle_macho: codesign --remove-signature failed for "
                      f"{p} (exit {result.returncode}): {result.stderr.strip()}",
                      file=sys.stderr)
                return 1
            changed += 1
    print(f"normalize_bundle_macho: trimmed {changed} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
