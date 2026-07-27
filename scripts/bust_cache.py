#!/usr/bin/env python3
"""Rewrite local JS/CSS asset URLs in HTML files with ?v=<project-version>."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
HTML_GLOBS = ["dashboard/*.html", "presentation/*.html"]

ASSET_RE = re.compile(r'(?P<attr>src|href)="(?P<url>[^"]+)"')


def is_local_asset(url: str) -> bool:
    if url.startswith(("http://", "https://", "//", "data:", "#")):
        return False
    base = url.split("?", 1)[0].split("#", 1)[0].lower()
    return base.endswith((".js", ".css"))


def with_version(url: str, version: str) -> str:
    anchor = ""
    if "#" in url:
        url, anchor = url.split("#", 1)
        anchor = f"#{anchor}"

    base, _, query = url.partition("?")
    query_parts = [p for p in query.split("&") if p and not p.startswith("v=")]
    query_parts.append(f"v={version}")
    new_query = "&".join(query_parts)
    return f"{base}?{new_query}{anchor}"


def rewrite_file(path: Path, version: str) -> bool:
    original = path.read_text(encoding="utf-8")

    def replacer(match: re.Match[str]) -> str:
        attr = match.group("attr")
        url = match.group("url")
        if not is_local_asset(url):
            return match.group(0)
        return f'{attr}="{with_version(url, version)}"'

    updated = ASSET_RE.sub(replacer, original)
    if updated == original:
        return False

    path.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    with PYPROJECT.open("rb") as fh:
        version = tomllib.load(fh)["project"]["version"]

    changed = []
    for pattern in HTML_GLOBS:
        for html in ROOT.glob(pattern):
            if rewrite_file(html, version):
                changed.append(html)

    if changed:
        for item in changed:
            print(f"updated {item.relative_to(ROOT)}")
    else:
        print("no local JS/CSS links needed updates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
