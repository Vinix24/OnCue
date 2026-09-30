"""Static wiring for the dashboard's detector-status strip.

Behaviour lives in two places: tests/js/detector_status.test.mjs drives the
module itself, and tests/test_detector_status_publisher.py covers the backend
that feeds it. These tests guard the seams in between -- the markup exists in
both HTML shells, the module is actually loaded, the hub channel is registered,
the i18n keys resolve in every catalog including the demo-shim's inlined
copies, and the strip stays a read-only status surface.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD = _ROOT / "dashboard"
_DEMO = _ROOT / "demo"
_JS_ROOT = _DASHBOARD / "js"

_DETECTOR_STATUS_JS = (_JS_ROOT / "detector-status.js").read_text(encoding="utf-8")
_HTML_SHELLS = (_DASHBOARD / "index.html", _DEMO / "index.html")

# Keys detector-status.js resolves at render time.
_DETECTOR_KEYS = (
    "status_waiting",
    "status_listening",
    "status_active",
    "status_no_input",
    "status_stale",
    "status_stopped",
    "toggle_title",
    "reason_waiting",
    "reason_stale",
    "reason_no_input",
    "reason_nothing_received",
    "reason_healthy",
    "reason_not_prospect",
    "reason_below_min",
    "reason_debounced",
    "reason_low_confidence",
    "reason_dropped_none",
    "reason_nothing_matched",
    "count_received",
    "count_skipped_not_prospect",
    "count_buffered_below_min",
    "count_debounced",
    "count_classified",
    "count_dropped_none",
    "count_dropped_low_confidence",
    "count_dispatched",
    "confidence_high",
    "confidence_uncertain",
)


def _flatten(catalog: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in catalog.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= _flatten(value, f"{dotted}.")
        else:
            keys.add(dotted)
    return keys


# --- markup + module wiring ---------------------------------------------------


def test_both_shells_host_the_detector_status_strip() -> None:
    for html_path in _HTML_SHELLS:
        html = html_path.read_text(encoding="utf-8")
        for needle in (
            'id="detector-status"',
            'id="detector-status-toggle"',
            'id="detector-status-label"',
            'id="detector-status-detail"',
        ):
            assert needle in html, f"{needle} missing from {html_path.name}"


def test_both_shells_load_the_detector_status_module() -> None:
    for html_path in _HTML_SHELLS:
        html = html_path.read_text(encoding="utf-8")
        assert "js/detector-status.js" in html, f"detector-status.js not loaded in {html_path.name}"


def test_the_strip_sits_with_the_detection_panel_not_in_a_scrolling_list() -> None:
    """It has to stay visible mid-call, so it lives in the pain-points card
    itself rather than inside the scrollable panel (#206's lesson about the
    detection-off indicator, applied to the same surface)."""
    for html_path in _HTML_SHELLS:
        html = html_path.read_text(encoding="utf-8")
        panel_index = html.index('id="pain-points-panel"')
        status_index = html.index('id="detector-status"')
        assert status_index > panel_index, f"strip must follow the panel in {html_path.name}"
        assert status_index - panel_index < 1200, (
            f"strip drifted away from the pain-points card in {html_path.name}"
        )


def test_every_dashboard_script_tag_is_cache_busted() -> None:
    """A stale cached module hides a fix. Every local script carries a ?v=."""
    for html_path in _HTML_SHELLS:
        html = html_path.read_text(encoding="utf-8")
        for src in re.findall(r'<script src="((?:\.\./)?(?:dashboard/)?js/[^"]+)"', html):
            assert "?v=" in src, f"{src} in {html_path.name} has no cache-busting query"


def test_app_resets_the_strip_between_calls() -> None:
    app_js = (_JS_ROOT / "app.js").read_text(encoding="utf-8")
    assert "resetDetectorStatus" in app_js, "resetCallPanels must clear last call's counters"


def test_detection_cards_tier_their_confidence() -> None:
    for module in ("pain-points.js", "objections.js", "opportunities.js"):
        text = (_JS_ROOT / module).read_text(encoding="utf-8")
        assert "applyConfidenceTier" in text, f"{module} does not tier its confidence"
        assert "detectorConfidenceTier" in text, f"{module} does not use the shared tier rule"


# --- hub channel --------------------------------------------------------------


def test_hub_allows_the_detector_status_channel() -> None:
    from sales_copilot.websocket import hub_core

    assert "detector-status" in hub_core._ALLOWED_CHANNELS


def test_the_strip_subscribes_to_that_channel() -> None:
    assert "/ws/detector-status" in _DETECTOR_STATUS_JS


def test_the_strip_is_read_only() -> None:
    """A status surface listens; it never pushes anything back onto the hub."""
    assert ".send(" not in _DETECTOR_STATUS_JS
    assert "fetch(" not in _DETECTOR_STATUS_JS


def test_the_strip_renders_no_transcript_text() -> None:
    """The channel carries counters only, and the renderer must never reach for
    a text field even if one ever appeared in the payload."""
    for field in ("trigger_phrase", "evidence_quote", ".text", "transcript"):
        assert f"payload.{field}" not in _DETECTOR_STATUS_JS
        assert f"status.{field}" not in _DETECTOR_STATUS_JS


# --- i18n coverage ------------------------------------------------------------


def test_detector_keys_in_dashboard_catalogs() -> None:
    for lang in ("en", "nl"):
        catalog = json.loads((_DASHBOARD / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        flat = _flatten(catalog)
        for key in _DETECTOR_KEYS:
            assert f"detector.{key}" in flat, f"detector.{key} missing from dashboard/i18n/{lang}.json"


def test_detector_keys_in_demo_shim_catalogs() -> None:
    """demo-shim.js inlines both catalogs; every key must appear in each so the
    demo page never renders a raw dotted key."""
    shim = (_DEMO / "demo-shim.js").read_text(encoding="utf-8")
    for key in _DETECTOR_KEYS:
        occurrences = len(re.findall(rf"\b{key}:", shim))
        assert occurrences >= 2, f"detector.{key} missing from a demo-shim catalog (found {occurrences}x)"


def test_every_key_the_module_resolves_exists_in_the_catalog() -> None:
    """Catch a key added to the JS but forgotten in the catalog, including the
    two built by interpolation (detector.status_<state>, detector.count_<name>)."""
    catalog = json.loads((_DASHBOARD / "i18n" / "nl.json").read_text(encoding="utf-8"))
    flat = _flatten(catalog)
    literal_keys = set(re.findall(r'window\.t\("(detector\.[a-z_]+)"', _DETECTOR_STATUS_JS))
    assert literal_keys, "no literal detector.* keys found — did the module change shape?"
    for key in literal_keys:
        assert key in flat, f"{key} used by detector-status.js is missing from nl.json"


# --- demo parity --------------------------------------------------------------


def test_demo_shim_publishes_detector_status() -> None:
    """The demo has no detector, so the shim derives the same counters from what
    the timeline actually emits — a truthful strip rather than a scripted one."""
    shim = (_DEMO / "demo-shim.js").read_text(encoding="utf-8")
    assert '"detector-status"' in shim
    assert '"detector_status"' in shim
    assert "detectorCounts" in shim


# --- docs stay in step with the hub -------------------------------------------


def test_docs_channel_lists_match_the_hub_allow_list() -> None:
    """The channel lists in TTD and ARCHITECTURE are derived facts, and they had
    already drifted (they omitted /ws/insights, /ws/script-tracking, /ws/phase,
    /ws/system and /ws/wizard before this change). Pin them to the code so the
    next channel cannot be added without the docs following."""
    from sales_copilot.websocket import hub_core

    expected = {f"/ws/{channel}" for channel in hub_core._ALLOWED_CHANNELS}

    ttd_path = _ROOT / "docs" / "TTD.md"
    if not ttd_path.is_file():
        pytest.skip("docs/TTD.md is excluded from the public export")
    ttd = ttd_path.read_text(encoding="utf-8")
    bullet = re.search(
        r"^- Channels \(the authoritative set.*?(?=\n\n)", ttd, re.DOTALL | re.MULTILINE
    )
    assert bullet is not None, "the TTD channel bullet was renamed or removed"
    assert set(re.findall(r"/ws/[a-z-]+", bullet.group(0))) == expected

    architecture = (_ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
    node = re.search(r'CHANNELS\["channels:[^"]+"\]', architecture)
    assert node is not None, "the ARCHITECTURE channels node was renamed or removed"
    assert set(re.findall(r"/ws/[a-z-]+", node.group(0))) == expected


def test_docs_document_the_detector_status_payload() -> None:
    """CLAUDE.md points implementers at TTD section 7 for every WS message."""
    ttd_path = _ROOT / "docs" / "TTD.md"
    if not ttd_path.is_file():
        pytest.skip("docs/TTD.md is excluded from the public export")
    ttd = ttd_path.read_text(encoding="utf-8")
    assert "**Channel: `/ws/detector-status`**" in ttd
    assert '"type": "detector_status"' in ttd
    for counter in (
        "received",
        "skipped_not_prospect",
        "buffered_below_min",
        "debounced",
        "classified",
        "dropped_none",
        "dropped_low_confidence",
        "dispatched",
    ):
        assert f'"{counter}"' in ttd, f"counter {counter} missing from the TTD payload schema"
