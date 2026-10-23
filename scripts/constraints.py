#!/usr/bin/env python3
"""Keep constraints.txt in step with uv.lock.

constraints.txt pins every package in uv.lock (all extras, platform markers
kept) so that `python -m pip install ... -c constraints.txt` resolves to the
versions CI tests. End users need pip only; uv is needed to regenerate the file.

The installer tooling (pip, setuptools, wheel) is left out on purpose. Those
packages install everything else and the installers and CI upgrade them first.
Pinning them would make pip try to replace itself (refused on Windows when run
through the pip launcher) and would freeze setuptools at a version that
pip-audit can flag, for users who never asked for that version.

    python scripts/constraints.py --write   regenerate constraints.txt (needs uv)
    python scripts/constraints.py --check   fail when constraints.txt and uv.lock disagree

--check reads both files directly and does not need uv, so it runs in CI and
in the public export.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "uv.lock"
CONSTRAINTS_PATH = REPO_ROOT / "constraints.txt"

HEADER = (
    "# Generated from uv.lock by scripts/constraints.py. Do not edit by hand.\n"
    "# Use: python -m pip install -e \".[full,dev]\" -c constraints.txt\n"
    "# Regenerate after a lock change: python scripts/constraints.py --write\n"
)

EXPORT_COMMAND = [
    "uv",
    "export",
    "--format",
    "requirements-txt",
    "--all-extras",
    "--no-emit-project",
    "--no-hashes",
    "--no-header",
    "--locked",
]

INSTALLER_TOOLING = frozenset({"pip", "setuptools", "wheel"})

_PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)")


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_pins(lock_path: Path) -> set[tuple[str, str]]:
    """(name, version) of every locked package except the project itself and the installer tooling."""
    data = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    pins: set[tuple[str, str]] = set()
    for package in data.get("package", []):
        source = package.get("source") or {}
        if source.get("editable") == "." or source.get("virtual") == ".":
            continue
        if normalize_name(package["name"]) in INSTALLER_TOOLING:
            continue
        version = package.get("version")
        if not version:
            raise ValueError(f"uv.lock package {package.get('name')!r} has no version")
        pins.add((normalize_name(package["name"]), version))
    return pins


def constraint_pins(constraints_path: Path) -> set[tuple[str, str]]:
    """(name, version) of every pin in constraints.txt; any other non-comment line is an error."""
    pins: set[tuple[str, str]] = set()
    for number, raw in enumerate(constraints_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN.match(line)
        if match is None:
            raise ValueError(f"{constraints_path.name}:{number}: not a pinned requirement: {raw!r}")
        pins.add((normalize_name(match["name"]), match["version"]))
    return pins


def diff(lock_path: Path, constraints_path: Path) -> list[str]:
    """Human-readable differences between uv.lock and constraints.txt; empty when in sync."""
    locked = lock_pins(lock_path)
    constrained = constraint_pins(constraints_path)
    problems = [f"in uv.lock but not in constraints.txt: {n}=={v}" for n, v in sorted(locked - constrained)]
    problems += [f"in constraints.txt but not in uv.lock: {n}=={v}" for n, v in sorted(constrained - locked)]
    return problems


def _is_installer_tooling(requirement_line: str) -> bool:
    match = _PIN.match(requirement_line.strip())
    return match is not None and normalize_name(match["name"]) in INSTALLER_TOOLING


def export_constraints() -> str:
    result = subprocess.run(EXPORT_COMMAND, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"uv export failed (exit {result.returncode}):\n{result.stderr.strip()}")
    lines = [line for line in result.stdout.splitlines(keepends=True) if not _is_installer_tooling(line)]
    return HEADER + "".join(lines)


def write(constraints_path: Path) -> None:
    content = export_constraints()
    tmp = constraints_path.with_suffix(constraints_path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(constraints_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate constraints.txt from uv.lock (needs uv)")
    mode.add_argument("--check", action="store_true", help="fail when constraints.txt does not match uv.lock")
    args = parser.parse_args(argv)

    if args.write:
        write(CONSTRAINTS_PATH)
        print(f"wrote {CONSTRAINTS_PATH.relative_to(REPO_ROOT)}")
        return 0

    if not CONSTRAINTS_PATH.is_file():
        print("constraints.txt is missing. Run: python scripts/constraints.py --write", file=sys.stderr)
        return 1
    problems = diff(LOCK_PATH, CONSTRAINTS_PATH)
    if problems:
        print("constraints.txt is out of sync with uv.lock:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("Run: python scripts/constraints.py --write", file=sys.stderr)
        return 1
    print("constraints.txt is consistent with uv.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
