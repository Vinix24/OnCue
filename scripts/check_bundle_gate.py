#!/usr/bin/env python3
"""Bundle gate: fail when a built OnCue.app holds anything the public export forbids.

Usage: check_bundle_gate.py <OnCue.app>
           [--export-script scripts/export_public.sh | --forbidden-file scripts/bundle_forbidden_paths.txt]

Scans every file name in the bundle, including the members of zip archives (py2app's
zipped site-packages) and .pyc files, against each FORBIDDEN_PATHS entry of the export
script and against the private deep-lane modules. An entry matches when its path
components appear in order as consecutive components of a bundle path; a ``.py`` entry
also matches its ``.pyc``. Exit 0: clean. Exit 1: hits (printed). Exit 2: the gate could
not run (missing bundle, unreadable or empty forbidden list); it never passes on a failure.
Called by scripts/build_dmg.sh; signing and notarization steps call it again before signing.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

# Private deep-lane: always checked, also if the export script's list ever loses them.
DEEP_LANE = ("src/sales_copilot/modules/insight", "src/sales_copilot/mcp_bridge")
# "bin" in FORBIDDEN_PATHS means the export's top-level bin/ directory. A directory of that
# name is normal elsewhere in a bundle (torch/bin, the whisper.cpp build/bin), so it only
# counts at the place where the export root lands in the bundle.
ANCHORED = {"bin": ("Contents", "Resources", "bin")}
# The one legitimate file under that directory: the prebuilt AudioTee program the bundle
# ships (src/sales_copilot/audio/capture.py resolves it).
ALLOWED = frozenset({"Contents/Resources/bin", "Contents/Resources/bin/audiotee"})
DEFAULT_EXPORT_SCRIPT = Path(__file__).resolve().parent / "export_public.sh"


class GateError(Exception):
    """The gate could not run to a verdict."""


def parse_forbidden(export_script: Path) -> list[str]:
    try:
        text = export_script.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(f"cannot read {export_script}: {exc}") from exc
    block = re.search(r"^FORBIDDEN_PATHS=\(\n(.*?)^\)", text, re.S | re.M)
    if block is None:
        raise GateError(f"no FORBIDDEN_PATHS array in {export_script}")
    entries = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            entries.append(line.strip("\"'"))
    if not entries:
        raise GateError(f"FORBIDDEN_PATHS in {export_script} is empty")
    return entries


def parse_forbidden_file(path: Path) -> list[str]:
    """Read a plain list (one path per line, ``#`` comments) such as bundle_forbidden_paths.txt."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateError(f"cannot read {path}: {exc}") from exc
    entries = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not entries:
        raise GateError(f"forbidden list {path} is empty")
    return entries


def _matches(parts: tuple[str, ...], entry: str) -> bool:
    # In the bundle the packages sit directly under site-packages, without the export's src/.
    if entry in ANCHORED:
        return parts[: len(ANCHORED[entry])] == ANCHORED[entry]
    wants = {tuple(p for p in entry.split("/") if p)}
    if entry.startswith("src/"):
        wants.add(tuple(p for p in entry[4:].split("/") if p))
    if entry.endswith(".py"):
        # Module/package form of the same source path (``_dev_keys`` as a directory).
        wants |= {w[:-1] + (w[-1][:-3],) for w in wants}
    return any(
        variant[i : i + len(w)] == w
        for variant in _source_variants(parts)
        for w in wants
        for i in range(len(variant) - len(w) + 1)
    )


def _source_variants(parts: tuple[str, ...]) -> list[tuple[str, ...]]:
    """The path itself plus, for compiled files, the source path they were built from.

    Covers ``pkg/mod.pyc`` and the ``pkg/__pycache__/mod.cpython-312[.opt-N].pyc`` layout;
    the same applies to ``.pyo``. Members of a zip archive arrive as ordinary path parts.
    """
    variants = [parts]
    match = _COMPILED.match(parts[-1])
    if match:
        directory = parts[:-1]
        if directory and directory[-1] == "__pycache__":
            directory = directory[:-1]
        variants.append((*directory, f"{match.group(1)}.py"))
    return variants


_COMPILED = re.compile(r"^([^.]+)(?:\.[^.]+(?:\.opt-\d+)?)?\.py[co]$")


def _starts_like_zip(path: Path) -> bool:
    """True for a real zip. is_zipfile alone also accepts any file that merely contains the
    end-of-archive signature somewhere, such as a .pyc holding the literal bytes."""
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"PK\x03\x04"
    except OSError as exc:
        raise GateError(f"cannot read {path}: {exc}") from exc


def _names(bundle: Path):
    """Yield every relative path in the bundle, with zip members as ``<zip>/<member>``."""
    for path in sorted(bundle.rglob("*")):
        rel = path.relative_to(bundle).as_posix()
        yield rel
        if path.is_file() and not path.is_symlink() and _starts_like_zip(path):
            try:
                with zipfile.ZipFile(path) as archive:
                    for member in archive.namelist():
                        yield f"{rel}/{member}"
            except (zipfile.BadZipFile, OSError) as exc:
                raise GateError(f"cannot read zip {path}: {exc}") from exc


def scan(bundle: Path, forbidden: list[str]) -> list[str]:
    if not bundle.is_dir():
        raise GateError(f"{bundle} is not a directory")
    entries = [*forbidden, *DEEP_LANE]
    hits = []
    for rel in _names(bundle):
        if rel in ALLOWED:
            continue
        parts = tuple(rel.split("/"))
        for entry in entries:
            if _matches(parts, entry):
                hits.append(f"{rel}  (matches {entry})")
                break
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--export-script", type=Path, default=DEFAULT_EXPORT_SCRIPT)
    parser.add_argument(
        "--forbidden-file",
        type=Path,
        help="plain list of forbidden paths (public tree, no export_public.sh); replaces --export-script",
    )
    parser.add_argument(
        "--deep-lane-only",
        action="store_true",
        help="public tree without export_public.sh: check only the private deep-lane modules",
    )
    args = parser.parse_args(argv)
    try:
        if args.deep_lane_only:
            forbidden = []
        elif args.forbidden_file is not None:
            forbidden = parse_forbidden_file(args.forbidden_file)
        else:
            forbidden = parse_forbidden(args.export_script)
        hits = scan(args.bundle, forbidden)
    except GateError as exc:
        print(f"BUNDLE GATE ERROR: {exc}", file=sys.stderr)
        return 2
    if hits:
        print(f"BUNDLE GATE FAILED: {len(hits)} forbidden path(s) in {args.bundle}:", file=sys.stderr)
        for hit in hits:
            print(f"  {hit}", file=sys.stderr)
        return 1
    print(f"BUNDLE GATE OK: {args.bundle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
