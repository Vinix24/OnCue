"""Dashboard UI structure + license endpoint integration tests."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# HTML structure helpers
# ---------------------------------------------------------------------------


class _ElementFinder(HTMLParser):
    """Collect id → attributes mapping from an HTML document."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        el_id = attrs_dict.get("id")
        if el_id:
            self.elements[el_id] = {"tag": tag, **attrs_dict}


_DASHBOARD_HTML = Path(__file__).parent.parent / "dashboard" / "index.html"


def _parse_dashboard() -> _ElementFinder:
    parser = _ElementFinder()
    parser.feed(_DASHBOARD_HTML.read_text(encoding="utf-8"))
    return parser


# ---------------------------------------------------------------------------
# Test 1: banner exists and hidden by default (verifies license-grace-event wiring)
# ---------------------------------------------------------------------------


def test_banner_visible_after_grace_event() -> None:
    """
    license-banner exists in the HTML, is hidden by default, and has the
    open-license-modal button required by the license-grace-event handler.
    """
    parser = _parse_dashboard()

    assert "license-banner" in parser.elements, "license-banner element missing from dashboard HTML"
    banner = parser.elements["license-banner"]
    classes = banner.get("class", "") or ""
    assert "hidden" in classes, (
        f"license-banner must have 'hidden' class by default; got: {classes!r}"
    )

    assert "open-license-modal" in parser.elements, (
        "open-license-modal button missing — license.js cannot bind click handler"
    )

    assert "license-modal" in parser.elements, "license-modal element missing from dashboard HTML"
    modal = parser.elements["license-modal"]
    modal_classes = modal.get("class", "") or ""
    assert "hidden" in modal_classes, (
        f"license-modal must be hidden by default; got: {modal_classes!r}"
    )

    assert "license-form" in parser.elements, "license-form missing — modal has no submit form"


def test_script_tracking_panel_exists() -> None:
    parser = _parse_dashboard()
    assert "script-tracking-section" in parser.elements, (
        "script-tracking-section card missing from dashboard HTML"
    )
    assert "script-tracking-panel" in parser.elements, (
        "script-tracking-panel missing — script-tracking.js cannot render checklist"
    )
    assert "script-tracking-empty" not in parser.elements, (
        "script-tracking-empty must be a child div, not a top-level id"
    )


# ---------------------------------------------------------------------------
# Test 2: form submit body matches what the API endpoint expects
# ---------------------------------------------------------------------------


_TEST_LICENSE_SECRET = "secret-32-chars-min-for-testing!"


@pytest.fixture()
def _tmp_leads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    leads = tmp_path / ".vnx-data" / "leads.ndjson"
    import sales_copilot.auth.email_capture as ec_module

    monkeypatch.setattr(ec_module, "LEADS_FILE", leads)
    monkeypatch.setenv("SALES_COPILOT_LICENSE_SECRET", _TEST_LICENSE_SECRET)
    return leads


def test_audio_warning_elements_present() -> None:
    """
    audio-warning-self and audio-warning-prospect exist in the HTML,
    are hidden by default, and live inside audio-warnings-container.
    The container must exist so JS can manage them on the coaching channel.
    """
    parser = _parse_dashboard()

    assert "audio-warnings-container" in parser.elements, (
        "audio-warnings-container missing — audio warning JS cannot find its mount point"
    )

    assert "audio-warning-self" in parser.elements, (
        "audio-warning-self element missing from dashboard HTML"
    )
    self_el = parser.elements["audio-warning-self"]
    self_classes = self_el.get("class", "") or ""
    assert "hidden" in self_classes, (
        f"audio-warning-self must be hidden by default; got: {self_classes!r}"
    )
    assert self_el.get("role") == "alert", (
        "audio-warning-self must have role=alert for accessibility"
    )

    assert "audio-warning-prospect" in parser.elements, (
        "audio-warning-prospect element missing from dashboard HTML"
    )
    prospect_el = parser.elements["audio-warning-prospect"]
    prospect_classes = prospect_el.get("class", "") or ""
    assert "hidden" in prospect_classes, (
        f"audio-warning-prospect must be hidden by default; got: {prospect_classes!r}"
    )


def test_start_call_button_present() -> None:
    """start-call button exists so the debounce handler can bind to it."""
    parser = _parse_dashboard()
    assert "start-call" in parser.elements, "start-call button missing from dashboard HTML"
    assert parser.elements["start-call"].get("tag") == "button"


def test_domain_preset_selector_present() -> None:
    parser = _parse_dashboard()

    assert "domain-preset" in parser.elements
    assert parser.elements["domain-preset"].get("tag") == "select"


def test_end_call_button_present() -> None:
    """end-call button exists so the debounce handler can bind to it."""
    parser = _parse_dashboard()
    assert "end-call" in parser.elements, "end-call button missing from dashboard HTML"
    assert parser.elements["end-call"].get("tag") == "button"


def test_consent_indicator_elements_present() -> None:
    """Consent card, checkbox, badge and status exist for soft/strict tiers."""
    parser = _parse_dashboard()
    assert "consent-card" in parser.elements, "consent-card missing from dashboard HTML"
    card = parser.elements["consent-card"]
    classes = card.get("class", "") or ""
    assert "hidden" in classes, "consent-card must be hidden by default (shown by JS for soft/strict)"
    assert "consent-given" in parser.elements, "consent-given checkbox missing"
    assert parser.elements["consent-given"].get("type") == "checkbox"
    assert "consent-tier-badge" in parser.elements, "consent-tier-badge missing"
    assert "consent-status" in parser.elements, "consent-status missing"
    assert "consent.js" in _DASHBOARD_HTML.read_text(encoding="utf-8")


def test_modal_submit_hits_endpoint(_tmp_leads: Path) -> None:
    """
    POST /api/v1/license/request with the same body that license.js sends
    returns 200 with status=ok and a SC- license_key.
    """
    from sales_copilot.websocket import hub

    client = TestClient(hub.app)

    # Mirrors the fetch body in license.js
    payload = {"email": "dashboard@test.com", "source": "dashboard"}
    response = client.post("/api/v1/license/request", json=payload)

    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert data.get("status") == "ok", f"Expected status=ok, got: {data}"
    assert "license_key" in data, f"Response missing license_key: {data}"
    assert data["license_key"].startswith("SC-"), f"license_key format wrong: {data['license_key']!r}"

    # Lead was written to the NDJSON file
    assert _tmp_leads.exists(), "leads.ndjson was not created"
    record = json.loads(_tmp_leads.read_text(encoding="utf-8").strip())
    assert record["email"] == "dashboard@test.com"


# ---------------------------------------------------------------------------
# Objection → hero spotlight routing
# ---------------------------------------------------------------------------

_OBJECTIONS_JS = Path(__file__).parent.parent / "dashboard" / "js" / "objections.js"
_DEMO_DIR = Path(__file__).parent.parent / "demo"

# demo/ is deliberately not part of the OSS export (not on the
# export_public.sh allowlist); demo-asset tests only run where it exists.
_requires_demo_assets = pytest.mark.skipif(
    not _DEMO_DIR.is_dir(),
    reason="demo/ assets absent (not part of the OSS export)",
)


def test_hero_spotlight_element_present() -> None:
    """The hero card and its headline (#monologue-warning) exist for objections.js to drive."""
    parser = _parse_dashboard()
    assert "hero" in parser.elements, "hero card missing from dashboard HTML"
    assert "monologue-warning" in parser.elements, (
        "monologue-warning headline missing — objections.js cannot route objections to the hero"
    )
    assert "objections.js" in _DASHBOARD_HTML.read_text(encoding="utf-8"), (
        "objections.js script tag missing from dashboard HTML"
    )


def test_objection_also_routes_to_hero_spotlight() -> None:
    """objections.js drives the hero headline in addition to the right-rail panel."""
    src = _OBJECTIONS_JS.read_text(encoding="utf-8")
    assert "objections-panel" in src, "right-rail objections panel wiring lost"
    assert 'getElementById("monologue-warning")' in src, (
        "objections.js does not reference the hero headline (#monologue-warning)"
    )
    hero_fn = re.search(r"const showObjectionInHero = \(payload\) => \{(.*?)\n\};", src, re.DOTALL)
    assert hero_fn, "showObjectionInHero routing function missing from objections.js"
    body = hero_fn.group(1)
    assert "payload.response_suggestion" in body, (
        "hero spotlight must be built from the payload's own response_suggestion"
    )
    # The guard is on heroHeadline, not response_suggestion: the hero now
    # spotlights even when curation is empty (shows category + trigger_phrase).
    assert "!heroHeadline" in body, (
        "hero routing must be gated on the hero headline element being present"
    )


def test_hero_spotlight_honors_pro_lock_teaser() -> None:
    """The locked teaser is shown verbatim in the hero — never a fabricated answer."""
    src = _OBJECTIONS_JS.read_text(encoding="utf-8")
    assert "RESPONSE_LOCKED_TEASER" in src, "Pro-lock teaser constant missing from objections.js"
    hero_fn = re.search(r"const showObjectionInHero = \(payload\) => \{(.*?)\n\};", src, re.DOTALL)
    assert hero_fn, "showObjectionInHero routing function missing from objections.js"
    body = hero_fn.group(1)
    # The hero builds a spotlight string via ternary: when response_suggestion is
    # truthy it is included verbatim (teaser included); when empty, fallback text
    # from category/trigger_phrase is used. The spotlight variable is then
    # assigned to heroHeadline.textContent.
    text_assignments = re.findall(r"textContent\s*=\s*([^;]+);", body)
    assert text_assignments, f"expected at least one hero text assignment, got {text_assignments}"
    assert "payload.response_suggestion" in body, (
        "hero spotlight must render the payload response_suggestion verbatim (via ternary)"
    )


@_requires_demo_assets
def test_demo_timelines_objection_feeds_hero() -> None:
    """Both demo timelines emit the objection with a response_suggestion, so the demo lights the hero."""
    for name in ("demo-timeline.js", "demo-timeline-nl.js"):
        src = (_DEMO_DIR / name).read_text(encoding="utf-8")
        assert 'emit("objections"' in src, f"{name} does not emit an objection event"
        block = src.split('emit("objections"', 1)[1]
        match = re.search(r'response_suggestion:\s*"([^"]+)"', block)
        assert match, f"{name} objection carries no response_suggestion — hero stays dark in the demo"


# ---------------------------------------------------------------------------
# Opportunity (buying-signal) panel structure
# ---------------------------------------------------------------------------


def test_opportunities_panel_present() -> None:
    """The green opportunity panel exists and is wired like the objections panel."""
    parser = _parse_dashboard()

    assert "opportunities-panel" in parser.elements, (
        "opportunities-panel missing — buying-signals cannot render in the dashboard"
    )
    panel = parser.elements["opportunities-panel"]
    assert panel.get("tag") == "div"
    assert "pain-points" in (panel.get("class", "") or "")

    assert "opportunities.js" in _DASHBOARD_HTML.read_text(encoding="utf-8"), (
        "opportunities.js script tag missing from dashboard HTML"
    )
