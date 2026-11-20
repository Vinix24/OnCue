#!/usr/bin/env python3
"""Validate and assemble CHANGELOG fragments from ``changelog.d/``.

A PR adds ``changelog.d/<slug>.<section>.md`` instead of editing CHANGELOG.md.
At release time ``--version X --date YYYY-MM-DD`` moves the fragments into
CHANGELOG.md and deletes them. ``--check`` validates every fragment.

Fragment format: Markdown list items only. Each item starts with "- " at
column 0; continuation lines are indented by two spaces. No headings.
"""

from __future__ import annotations

import argparse
import difflib
import os
import re
import sys
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path

SECTIONS = ("added", "changed", "fixed", "security", "removed", "documentation")
# Canonical heading order inside a version section.
CANONICAL = ("Highlights", "Added", "Changed", "Fixed", "Security", "Removed", "Documentation")
NAME_RE = re.compile(r"^(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.(?P<section>[a-z]+)\.md$")
VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
README = "README.md"


class FragmentError(Exception):
    """One or more fragments are invalid, or the changelog cannot take them."""


@dataclass(frozen=True)
class Fragment:
    path: Path
    section: str
    items: tuple[str, ...]  # each item is the full text, lines joined by "\n"


def _validate_name(name: str) -> tuple[str, str]:
    m = NAME_RE.match(name)
    if not m:
        raise FragmentError(f"{name}: name must be <slug>.<section>.md (slug: lowercase letters, digits, hyphens)")
    if m.group("section") not in SECTIONS:
        raise FragmentError(f"{name}: section '{m.group('section')}' is not one of {', '.join(SECTIONS)}")
    return m.group("slug"), m.group("section")


def parse_fragment(path: Path) -> Fragment:
    _, section = _validate_name(path.name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FragmentError(f"{path.name}: cannot read file: {exc}") from exc
    items: list[list[str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith("- "):
            if not line[2:].strip():
                raise FragmentError(f"{path.name}:{lineno}: empty list item")
            items.append([line])
        elif line.startswith("  ") and items:
            items[-1].append(line)
        elif line.startswith("#"):
            raise FragmentError(f"{path.name}:{lineno}: headings are not allowed in a fragment")
        else:
            raise FragmentError(
                f"{path.name}:{lineno}: expected '- ' at column 0 (new item) or a two-space indented continuation line"
            )
    if not items:
        raise FragmentError(f"{path.name}: no list item (a fragment must not be empty)")
    return Fragment(path, section, tuple("\n".join(i) for i in items))


def load_fragments(frag_dir: Path) -> list[Fragment]:
    """Parse every fragment, ordered by filename. Raises FragmentError listing all bad files."""
    if not frag_dir.is_dir():
        return []
    errors: list[str] = []
    found: list[Fragment] = []
    for path in sorted(frag_dir.iterdir(), key=lambda p: p.name):
        if path.name == README or path.name.startswith("."):
            continue
        if not path.is_file():
            errors.append(f"{path.name}: not a regular file")
            continue
        try:
            found.append(parse_fragment(path))
        except FragmentError as exc:
            errors.append(str(exc))
    if errors:
        raise FragmentError("\n".join(errors))
    return found


def _section_bounds(lines: list[str], start: int) -> int:
    """Index of the next '## ' heading after ``start``, or len(lines)."""
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            return i
    return len(lines)


def _find_version(lines: list[str], version: str) -> int | None:
    prefix = f"## [{version}]"
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    return None


def _append_items(lines: list[str], head: int, end: int, heading: str, items: list[str]) -> int:
    """Append items to ``### heading`` inside lines[head:end]. Returns the new ``end``."""
    sub = None
    for i in range(head + 1, end):
        if lines[i].strip() == f"### {heading}":
            sub = i
            break
    new_lines = [ln for item in items for ln in item.split("\n")]
    if sub is not None:
        sub_end = end
        for i in range(sub + 1, end):
            if lines[i].startswith("### "):
                sub_end = i
                break
        ins = sub_end
        while ins > sub + 1 and not lines[ins - 1].strip():
            ins -= 1
        lines[ins:ins] = new_lines
        return end + len(new_lines)
    # Missing heading: place it before the first existing canonical heading that ranks later.
    rank = CANONICAL.index(heading)
    ins = None
    for i in range(head + 1, end):
        m = re.match(r"^### (.+?)\s*$", lines[i])
        if m and m.group(1) in CANONICAL and CANONICAL.index(m.group(1)) > rank:
            ins = i
            break
    block = [f"### {heading}", "", *new_lines, ""]
    if ins is None:
        ins = end
        while ins > head + 1 and not lines[ins - 1].strip():
            ins -= 1
        block = ["", *block[:-1]]
    lines[ins:ins] = block
    return end + len(block)


def assemble_text(text: str, fragments: list[Fragment], version: str, date: str) -> str:
    lines = text.split("\n")
    unreleased = _find_version(lines, "Unreleased")
    if unreleased is None:
        raise FragmentError("CHANGELOG.md has no '## [Unreleased]' section")
    by_section: dict[str, list[str]] = {}
    for frag in fragments:  # already ordered by filename
        by_section.setdefault(frag.section.capitalize(), []).extend(frag.items)

    head = _find_version(lines, version)
    if head is None:
        end = _section_bounds(lines, unreleased)
        ins = end
        while ins > unreleased + 1 and not lines[ins - 1].strip():
            ins -= 1
        lines[ins:ins] = ["", f"## [{version}] — {date}"]
        head = ins + 1
        end = _section_bounds(lines, head)
    else:
        lines[head] = re.sub(r"^(## \[[^\]]+\]).*$", rf"\1 — {date}", lines[head])
        end = _section_bounds(lines, head)

    for heading in CANONICAL:
        if heading in by_section:
            end = _append_items(lines, head, end, heading, by_section[heading])
    out = "\n".join(lines)
    return re.sub(r"\n{3,}(## \[)", r"\n\n\1", out)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def run_check(root: Path) -> int:
    try:
        frags = load_fragments(root / "changelog.d")
    except FragmentError as exc:
        print(f"changelog fragments invalid:\n{exc}", file=sys.stderr)
        return 1
    print(f"{len(frags)} fragment(s) valid")
    return 0


def run_assemble(root: Path, version: str, date: str, dry_run: bool) -> int:
    if not VERSION_RE.match(version):
        print(f"invalid --version: {version!r}", file=sys.stderr)
        return 1
    try:
        _date.fromisoformat(date)
    except ValueError:
        print(f"invalid --date (want YYYY-MM-DD): {date!r}", file=sys.stderr)
        return 1
    changelog = root / "CHANGELOG.md"
    try:
        frags = load_fragments(root / "changelog.d")
        old = changelog.read_text(encoding="utf-8")
        new = assemble_text(old, frags, version, date)
    except (FragmentError, OSError) as exc:
        print(f"cannot assemble:\n{exc}", file=sys.stderr)
        return 1
    if not frags:
        print("no fragments to assemble", file=sys.stderr)
        return 1
    if dry_run:
        sys.stdout.writelines(
            difflib.unified_diff(old.splitlines(True), new.splitlines(True), "CHANGELOG.md", "CHANGELOG.md (assembled)")
        )
        return 0
    tmp = changelog.with_name(changelog.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    os.replace(tmp, changelog)
    for frag in frags:
        os.remove(frag.path)
        print(f"{frag.path.name} -> [{version}] / {frag.section.capitalize()} ({len(frag.items)} item(s))")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="validate every fragment")
    ap.add_argument("--version", help="version to assemble into, e.g. 1.0.0")
    ap.add_argument("--date", help="release date, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="print the diff, touch nothing")
    ap.add_argument("--root", type=Path, default=_repo_root(), help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.check:
        return run_check(args.root)
    if not (args.version and args.date):
        ap.error("--version and --date are required unless --check is given")
    return run_assemble(args.root, args.version, args.date, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
