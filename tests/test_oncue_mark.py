"""Concept C ("O with baton"): the app mark, favicons and the loading animation.

The generated SVG is the contract shared by the .icns pipeline, the dashboard and
the presentation. These tests pin the geometry and the loading-state wiring so a
later regeneration cannot silently fall back to the old waveform.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARK_SVG_PATHS = (
    ROOT / "dashboard" / "assets" / "oncue-mark.svg",
    ROOT / "presentation" / "assets" / "oncue-mark.svg",
)
PAGES = (
    ROOT / "dashboard" / "index.html",
    ROOT / "dashboard" / "recorder.html",
    ROOT / "dashboard" / "library.html",
    ROOT / "dashboard" / "wizard" / "index.html",
)
LOADER_CSS = ROOT / "dashboard" / "css" / "oncue-loader.css"
_ICON_LINK = re.compile(r"<link\b[^>]*\brel=\"icon\"[^>]*>", re.IGNORECASE)


def _generator():
    spec = importlib.util.spec_from_file_location(
        "generate_icon", ROOT / "scripts" / "launcher" / "icon" / "generate_icon.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_emits_the_committed_svg_verbatim() -> None:
    generator = _generator()
    svg = generator.build_svg()
    for path in MARK_SVG_PATHS:
        assert path.read_text(encoding="utf-8") == svg, path


def test_mark_geometry_and_colours_match_concept_c() -> None:
    svg = MARK_SVG_PATHS[0].read_text(encoding="utf-8")
    assert 'viewBox="0 0 64 64"' in svg
    assert '<rect x="2" y="2" width="60" height="60" rx="14" fill="#14171f" />' in svg
    assert '<circle cx="29" cy="36" r="14" fill="none" stroke="#f5f5f4" stroke-width="5" />' in svg
    assert '<line x1="24" y1="41" x2="46" y2="19" stroke="#f5f5f4" stroke-width="3" stroke-linecap="round" />' in svg
    assert '<circle cx="48" cy="17" r="4.6" fill="#ef4444" />' in svg


def test_small_raster_sizes_thicken_only_the_baton() -> None:
    generator = _generator()
    assert generator.baton_width_for(16) > generator.baton_width_for(32) > generator.BATON_WIDTH
    assert generator.baton_width_for(1024) == generator.BATON_WIDTH


def test_each_page_has_exactly_one_svg_favicon() -> None:
    for page in PAGES:
        html = page.read_text(encoding="utf-8")
        links = _ICON_LINK.findall(html)
        assert len(links) == 1, f"{page}: expected one favicon, found {links}"
        assert 'type="image/svg+xml"' in links[0], page
        assert "oncue-mark.svg" in links[0], page


def test_loading_pages_share_one_loader_stylesheet_and_mark() -> None:
    for page in PAGES:
        html = page.read_text(encoding="utf-8")
        assert "oncue-loader.css" in html, f"{page}: loader stylesheet not linked"
        assert "oncue-loader" in html, f"{page}: loading state does not use the mark"


def test_loader_beats_a_4_4_bar_and_respects_reduced_motion() -> None:
    css = LOADER_CSS.read_text(encoding="utf-8")
    assert "transform-origin: 24px 41px;" in css
    assert "animation: oncue-baton-beat 2.2s ease-in-out infinite;" in css
    assert "animation: oncue-dot-pulse 2.2s ease-in-out infinite;" in css
    for percentage, angle in (("18%", "13deg"), ("40%", "-8deg"), ("62%", "9deg"), ("82%", "-11deg")):
        assert re.search(rf"{percentage}\s*\{{\s*transform: rotate\({angle}\);", css), (percentage, angle)
    assert re.search(r"18%\s*\{\s*transform: scale\(1\.45\);", css)
    assert "@media (prefers-reduced-motion: reduce)" in css
    reduced = css.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".oncue-loader-baton" in reduced and ".oncue-loader-dot" in reduced
    assert "animation: none;" in reduced
