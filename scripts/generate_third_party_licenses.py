#!/usr/bin/env python3
"""Regenerate ``THIRD_PARTY_LICENSES.md`` from the installed environment.

Live Sales Copilot is distributed under AGPL-3.0 (see ``LICENSE`` and
``docs/LICENSING.md``). MIT, BSD and Apache-licensed dependencies all require
their copyright and permission notices to travel with the distribution, so
this file exists to carry that attribution. This script produces it from the
live dependency metadata instead of by hand, using the same discovery
mechanism as ``scripts/check_dependency_licenses.py`` (``importlib.metadata``)
so the two stay consistent.

Only the auto-generated "Full Python dependency inventory" section is
produced here. Every hand-authored section (the vendored `whisper.cpp` /
`AudioTee` attributions, the curated "Key runtime dependencies" list, and the
JavaScript dependencies section) lives verbatim in
``scripts/third_party_licenses_template.md`` and is carried through unchanged.

The output is deterministic: dependencies are sorted by name, license file
content is deduplicated, and table cells never carry embedded license text
(that was the source of the historical 23 MB bloat: pip-licenses markdown
tables pad every cell in a row out to the width of its widest cell, and a
full license body in one cell inflated every other cell on that row along
with it). License bodies live in their own per-dependency ```text``` blocks
below a compact summary table instead.

Run it with the project virtualenv so it sees the real dependency set::

    .venv/bin/python scripts/generate_third_party_licenses.py

Pass ``--check`` to verify the committed file is up to date without writing
(exit 1 if it would change). This is not wired into CI as a blocking gate:
the exact dependency set (and therefore the file) is expected to differ
across machines with different installed extras, so a mismatch here is a
prompt for a human to look, not a build failure.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from check_dependency_licenses import _collect_signals, _display_label  # noqa: E402

ROOT_DIR = SCRIPT_DIR.parent
TEMPLATE_PATH = SCRIPT_DIR / "third_party_licenses_template.md"
DEFAULT_OUTPUT = ROOT_DIR / "THIRD_PARTY_LICENSES.md"
MARKER = "<!-- GENERATED:PYTHON_DEPENDENCY_INVENTORY -->"

# Priority order for picking a single "the" URL out of a package's Project-URL
# entries when there is no Home-page. Matched case-insensitively against the
# label before the comma in each entry (PEP 621 `label, url` form).
_URL_LABEL_PRIORITY = (
    "homepage",
    "home",
    "home-page",
    "repository",
    "source code",
    "source",
    "github",
    "code",
)

# Filenames under a distribution's own dist-info/egg-info directory that carry
# attribution text we must preserve.
_LICENSE_FILENAME_MARKERS = ("license", "licence", "copying", "notice", "copyright")


def _normalize(name: str) -> str:
    """Match the PEP 503 normalization importlib.metadata uses for dist names."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _self_package_name() -> str:
    pyproject_path = ROOT_DIR / "pyproject.toml"
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    name = data.get("project", {}).get("name")
    if not name:
        raise RuntimeError(f"could not find [project].name in {pyproject_path}")
    return name


@dataclass(frozen=True)
class DependencyRecord:
    name: str
    version: str
    license_label: str
    url: str
    license_text: str | None


def _pick_url(meta: metadata.PackageMetadata) -> str:
    home_page = meta.get("Home-page")
    if home_page and home_page.strip():
        return home_page.strip()

    parsed: list[tuple[str, str]] = []
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        label, url = label.strip(), url.strip()
        if url:
            parsed.append((label, url))

    for wanted in _URL_LABEL_PRIORITY:
        for label, url in parsed:
            if label.lower() == wanted:
                return url
    for wanted in _URL_LABEL_PRIORITY:
        for label, url in parsed:
            if wanted in label.lower():
                return url
    if parsed:
        return parsed[0][1]
    return ""


def _own_dist_info_license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """Return (filename, text) for license-ish files in the distribution's own metadata dir.

    Scoped to the top-level `<pkg>.dist-info` / `.egg-info` directory only, so a package that
    vendors other distributions internally (e.g. setuptools bundling its `_vendor/` tree, each
    with its own nested dist-info) does not pull those vendored sub-licenses into this
    distribution's own attribution entry.
    """
    candidates: list[tuple[str, str]] = []
    for f in dist.files or []:
        parts = f.parts
        if not parts:
            continue
        top = parts[0]
        if not (top.endswith(".dist-info") or top.endswith(".egg-info")):
            continue
        filename = parts[-1]
        if not any(marker in filename.lower() for marker in _LICENSE_FILENAME_MARKERS):
            continue
        try:
            text = dist.locate_file(f).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            candidates.append((filename, text))

    candidates.sort(key=lambda pair: pair[0].lower())
    seen_hashes: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for filename, text in candidates:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        deduped.append((filename, text))
    return deduped


def _license_text_for(dist: metadata.Distribution, long_signals: list[str]) -> str | None:
    file_texts = _own_dist_info_license_texts(dist)
    if file_texts:
        if len(file_texts) == 1:
            return file_texts[0][1]
        return "\n\n".join(f"--- {filename} ---\n\n{text}" for filename, text in file_texts)
    if long_signals:
        return long_signals[0].strip()
    return None


def collect_dependencies(skip_normalized_names: set[str]) -> list[DependencyRecord]:
    seen: dict[str, DependencyRecord] = {}
    for dist in metadata.distributions():
        meta = dist.metadata
        name = meta.get("Name")
        if not name:
            continue
        key = _normalize(name)
        if key in skip_normalized_names or key in seen:
            continue
        version = meta.get("Version") or "?"
        short, long, spdx = _collect_signals(meta)
        label = _display_label(short, spdx, bool(long))
        record = DependencyRecord(
            name=name,
            version=version,
            license_label=label,
            url=_pick_url(meta),
            license_text=_license_text_for(dist, long),
        )
        seen[key] = record
    return sorted(seen.values(), key=lambda r: r.name.lower())


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def render_summary_table(records: list[DependencyRecord]) -> str:
    lines = ["| Name | Version | License | URL |", "|---|---|---|---|"]
    for r in records:
        lines.append(
            f"| {_escape_cell(r.name)} | {_escape_cell(r.version)} | "
            f"{_escape_cell(r.license_label)} | {_escape_cell(r.url)} |"
        )
    return "\n".join(lines)


def render_license_sections(records: list[DependencyRecord]) -> str:
    blocks: list[str] = []
    for r in records:
        lines = [f"#### {r.name} {r.version} \u2014 {r.license_label}"]
        if r.url:
            lines.append(r.url)
        lines.append("")
        if r.license_text:
            lines.append("```text")
            lines.append(r.license_text)
            lines.append("```")
        else:
            lines.append(
                "_No license file found in package metadata; verify the upstream "
                "license before distributing._"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_inventory(records: list[DependencyRecord]) -> str:
    return (
        "#### Summary\n\n"
        + render_summary_table(records)
        + "\n\n#### License texts\n\n"
        + render_license_sections(records)
    )


def build_document(records: list[DependencyRecord]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if MARKER not in template:
        raise RuntimeError(f"{TEMPLATE_PATH} is missing the {MARKER} marker")
    return template.replace(MARKER, render_inventory(records))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_third_party_licenses.py",
        description=(
            "Regenerate THIRD_PARTY_LICENSES.md from the installed environment. "
            "Run with the project virtualenv (e.g. "
            ".venv/bin/python scripts/generate_third_party_licenses.py)."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if the output file would change.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output path (default: {DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args(argv)

    skip = {_normalize(_self_package_name())}
    records = collect_dependencies(skip)
    document = build_document(records)

    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else None
        if current != document:
            print(
                f"{args.output} is stale; run "
                "'.venv/bin/python scripts/generate_third_party_licenses.py'",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} is up to date ({len(records)} dependencies).")
        return 0

    args.output.write_text(document, encoding="utf-8")
    print(f"Wrote {args.output} ({len(records)} dependencies).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
