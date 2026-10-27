"""The presentation and dashboard must load every asset from the local hub.

OnCue's default is that nothing leaves the machine. A CDN or Google Fonts
reference makes the browser contact a third party, so no served HTML or CSS may
reference an http(s) asset URL, and the vendored replacements must be served.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sales_copilot.websocket import hub

ROOT = Path(__file__).resolve().parents[1]
SERVED_DIRS = [ROOT / "presentation", ROOT / "dashboard"]

# <link href>, <script src>, <img src>, <source src>, <iframe src> load assets.
# <a href> is navigation and is allowed to point at an external page.
_HTML_ASSET = re.compile(
    r"<(?:link|script|img|source|iframe|video|audio)\b[^>]*?\b(?:href|src)\s*=\s*[\"'](https?:)?//",
    re.IGNORECASE,
)
_CSS_URL = re.compile(r"url\(\s*[\"']?(?:https?:)?//", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import\s+(?:url\(\s*)?[\"']?(?:https?:)?//", re.IGNORECASE)


def _served_files(suffixes: tuple[str, ...]) -> list[Path]:
    return [
        path
        for base in SERVED_DIRS
        for path in base.rglob("*")
        if path.is_file() and path.suffix in suffixes
    ]


def test_served_html_has_no_external_asset_urls() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in _served_files((".html",))
        if _HTML_ASSET.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_served_css_has_no_external_asset_urls() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in _served_files((".css",))
        if _CSS_URL.search(text := path.read_text(encoding="utf-8")) or _CSS_IMPORT.search(text)
    ]
    assert offenders == []


def test_csp_allows_no_third_party_script_or_style_origin() -> None:
    client = TestClient(hub.app)
    csp = client.get("/presentation/").headers["content-security-policy"]
    directives = {d.split()[0]: d.split()[1:] for d in csp.split(";") if d.strip()}

    for name in ("script-src", "style-src", "font-src"):
        assert not any(src.startswith(("http:", "https:")) for src in directives[name]), name
    assert "jsdelivr" not in csp


def test_presentation_local_references_are_served_by_the_hub() -> None:
    """Every local path index.html and the theme CSS point at returns HTTP 200."""
    index = (ROOT / "presentation" / "index.html").read_text(encoding="utf-8")
    theme = (ROOT / "presentation" / "theme" / "sales-copilot.css").read_text(encoding="utf-8")

    paths = [
        "/presentation/" + ref.split("?")[0]
        for ref in re.findall(r"(?:href|src)=\"([^\"]+)\"", index)
        if not ref.startswith(("http", "//", "#"))
    ]
    paths += [
        "/presentation/vendor/inter/" + Path(ref).name
        for ref in re.findall(r"url\(\"\.\./vendor/inter/([^\"]+)\"\)", theme)
    ]
    paths += [
        "/presentation/vendor/reveal.js/dist/theme/fonts/source-sans-pro/source-sans-pro.css",
        "/presentation/vendor/reveal.js/dist/theme/fonts/source-sans-pro/source-sans-pro-regular.woff",
    ]
    assert any("vendor/reveal.js/dist/reveal.js" in p for p in paths)
    assert sum("inter-latin-" in p for p in paths) == 4

    client = TestClient(hub.app)
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert len(response.content) > 0, path


@pytest.mark.parametrize("weight", [400, 600, 700, 900])
def test_inter_weight_is_declared_with_swap(weight: int) -> None:
    theme = (ROOT / "presentation" / "theme" / "sales-copilot.css").read_text(encoding="utf-8")
    pattern = (
        r"@font-face\s*\{[^}]*font-family:\s*\"Inter\";[^}]*font-weight:\s*" + str(weight) + ";"
        r"[^}]*font-display:\s*swap;[^}]*inter-latin-" + str(weight) + r"-normal\.woff2"
    )
    assert re.search(pattern, theme)
