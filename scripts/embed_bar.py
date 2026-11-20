#!/usr/bin/env python3
"""Embed "OnCue Bar.app" in OnCue.app (ADR-006, point 11).

Usage: embed_bar.py <OnCue.app> <"OnCue Bar.app">
The bar lands in Contents/Helpers/, next to Contents/Resources, which is where Apple
puts helper programs; py2app only fills Contents/Resources and Contents/Frameworks.
The copy keeps symlinks. An existing copy is replaced. Exit code 0 on success, 1 on an
unusable argument or a bar bundle without its executable.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

BAR_NAME = "OnCue Bar.app"
BAR_EXECUTABLE = Path("Contents/MacOS/OnCueBar")


def bar_destination(app: Path) -> Path:
    """Where the bar goes inside ``app``."""
    return app / "Contents" / "Helpers" / BAR_NAME


def embed(app: Path, bar: Path) -> Path:
    """Copy ``bar`` into ``app``; returns the new location. Raises ValueError on a bad bundle."""
    if not (app / "Contents" / "MacOS").is_dir():
        raise ValueError(f"{app} is not an app bundle")
    if not (bar / BAR_EXECUTABLE).is_file():
        raise ValueError(f"{bar} has no {BAR_EXECUTABLE}")
    dest = bar_destination(app)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bar, dest, symlinks=True)
    return dest


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 1
    try:
        dest = embed(Path(argv[1]), Path(argv[2]))
    except (ValueError, OSError) as exc:
        print(f"embed_bar: {exc}", file=sys.stderr)
        return 1
    print(f"embedded {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
