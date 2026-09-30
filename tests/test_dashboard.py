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


# ---------------------------------------------------------------------------
# Autostart consent prompt structure
# ---------------------------------------------------------------------------


def test_autostart_consent_prompt_present() -> None:
    """The autostart consent prompt container and its controls exist in the dashboard HTML."""
    parser = _parse_dashboard()

    assert "autostart-consent-prompt" in parser.elements, (
        "autostart-consent-prompt missing from dashboard HTML"
    )
    prompt = parser.elements["autostart-consent-prompt"]
    prompt_classes = prompt.get("class", "") or ""
    assert "hidden" in prompt_classes, (
        f"autostart-consent-prompt must be hidden by default; got: {prompt_classes!r}"
    )
    assert prompt.get("role") == "dialog", (
        "autostart-consent-prompt must have role=dialog for accessibility"
    )

    assert "autostart-process-name" in parser.elements, (
        "autostart-process-name missing — JS cannot render the detected process name"
    )

    assert "autostart-confirm-btn" in parser.elements, (
        "autostart-confirm-btn missing — cannot bind confirm click handler"
    )
    assert parser.elements["autostart-confirm-btn"].get("tag") == "button"

    assert "autostart-decline-btn" in parser.elements, (
        "autostart-decline-btn missing — cannot bind decline click handler"
    )
    assert parser.elements["autostart-decline-btn"].get("tag") == "button"

    assert "autostart-outcome" in parser.elements, (
        "autostart-outcome missing — cannot show confirmation/decline result"
    )
    outcome = parser.elements["autostart-outcome"]
    outcome_classes = outcome.get("class", "") or ""
    assert "hidden" in outcome_classes, (
        f"autostart-outcome must be hidden by default; got: {outcome_classes!r}"
    )

    html_text = _DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "autostart-consent.js" in html_text, (
        "autostart-consent.js script tag missing from dashboard HTML"
    )


def test_autostart_consent_i18n_keys_exist() -> None:
    """Every i18n key referenced by the autostart consent prompt exists in nl.json."""
    import json

    nl_path = _DASHBOARD_HTML.parent / "i18n" / "nl.json"
    catalog = json.loads(nl_path.read_text(encoding="utf-8"))
    autostart = catalog.get("autostart", {})

    required_keys = [
        "prompt_prefix",
        "confirm_button",
        "decline_button",
        "confirmed",
        "declined",
        "process_phone_call",
    ]
    for key in required_keys:
        assert key in autostart, f"autostart.{key} missing from nl.json i18n catalog"


# ---------------------------------------------------------------------------
# Stop UX: daily "Stop & rapport" vs rare "OnCue afsluiten"
# ---------------------------------------------------------------------------
#
# Two actions both labelled "stop" did very different things: #end-call ends
# the conversation (server stays up), the round ⏹ shuts the whole server down.
# These tests pin the resolution: both actions remain present and reachable,
# the server-stop is no longer the most prominent header button, no blocking
# browser confirm() is used, and the shutdown screen + confirm modal exist as
# real DOM elements instead of document.body innerHTML surgery.

_STOP_CONTROL_JS = Path(__file__).parent.parent / "dashboard" / "js" / "stop-control.js"


def test_both_stop_actions_present() -> None:
    """Both the call-stop and the server-stop buttons exist in the dashboard."""
    parser = _parse_dashboard()
    assert "end-call" in parser.elements, (
        "end-call (Stop & rapport) button missing — daily call-stop action gone"
    )
    assert parser.elements["end-call"].get("tag") == "button"
    assert "stop-server-btn" in parser.elements, (
        "stop-server-btn missing — server-stop action unreachable"
    )
    assert parser.elements["stop-server-btn"].get("tag") == "button"


def test_server_stop_is_not_prominent() -> None:
    """The server-stop button is a subtle secondary action, not the standout red .stop-btn.

    It must reuse the generic icon-btn pattern (like theme-toggle and swap-speakers)
    so the daily Stop & rapport action is never overshadowed by the rare, destructive
    server-shutdown. The old prominent .stop-btn class must be gone.
    """
    parser = _parse_dashboard()
    stop_btn = parser.elements["stop-server-btn"]
    classes = (stop_btn.get("class") or "").split()
    assert "icon-btn" in classes, (
        f"stop-server-btn must use the generic icon-btn base so it is not the "
        f"most prominent header button; got classes: {classes!r}"
    )
    assert "stop-btn" not in classes, (
        f"stop-server-btn must not carry the prominent standalone .stop-btn class "
        f"(it overshadowed the daily Stop & rapport action); got classes: {classes!r}"
    )
    # A subtle modifier keeps it recognisable as a stop without screaming.
    assert "stop-btn--subtle" in classes, (
        f"stop-server-btn should keep a subtle stop modifier; got classes: {classes!r}"
    )


def test_stop_control_has_no_blocking_confirm() -> None:
    """stop-control.js must not use the blocking browser confirm()/alert()/prompt().

    The dashboard uses in-page banners/modals instead (see autostart-consent,
    license and pro-upgrade modals). A blocking dialog freezes the page and
    does not match the rest of the UI.
    """
    src = _STOP_CONTROL_JS.read_text(encoding="utf-8")
    # Strip comments before scanning so words like "confirm" appearing in a
    # doc comment do not trip the check — only an actual call counts.
    code_only = re.sub(r"//[^\n]*", "", src)
    code_only = re.sub(r"/\*.*?\*/", "", code_only, flags=re.DOTALL)
    forbidden = re.findall(r"(?<![A-Za-z0-9_])(confirm|alert|prompt)\s*\(", code_only)
    assert not forbidden, (
        f"stop-control.js uses a blocking browser dialog ({', '.join(forbidden)}): "
        "use the in-dashboard confirm modal instead"
    )


def test_stop_control_does_not_surgery_body_innerhtml() -> None:
    """stop-control.js must not overwrite document.body.innerHTML.

    The shutdown state is a real DOM element (#stop-server-screen) styled like
    the rest of the dashboard and including restart instructions. Overwriting
    document.body inline lost all styling and told the user nothing about how
    to come back.
    """
    src = _STOP_CONTROL_JS.read_text(encoding="utf-8")
    assert "document.body.innerHTML" not in src, (
        "stop-control.js overwrites document.body.innerHTML — use the #stop-server-screen "
        "DOM element instead"
    )


def test_stop_server_confirm_modal_present() -> None:
    """The server-stop confirm modal exists and is hidden by default, with its controls."""
    parser = _parse_dashboard()

    assert "stop-server-confirm-modal" in parser.elements, (
        "stop-server-confirm-modal missing — stop-control.js cannot gate the shutdown"
    )
    modal = parser.elements["stop-server-confirm-modal"]
    modal_classes = modal.get("class", "") or ""
    assert "hidden" in modal_classes, (
        f"stop-server-confirm-modal must be hidden by default; got: {modal_classes!r}"
    )
    assert modal.get("role") == "dialog", "confirm modal must have role=dialog"
    assert modal.get("aria-modal") == "true", "confirm modal must be aria-modal=true"

    assert "stop-server-confirm-btn" in parser.elements, (
        "stop-server-confirm-btn missing — cannot bind the confirm handler"
    )
    assert parser.elements["stop-server-confirm-btn"].get("tag") == "button"
    assert "stop-server-cancel-btn" in parser.elements, (
        "stop-server-cancel-btn missing — cannot bind the cancel handler"
    )
    assert parser.elements["stop-server-cancel-btn"].get("tag") == "button"


def test_stop_server_shutdown_screen_present() -> None:
    """The shutdown screen exists, is hidden by default, and carries restart instructions."""
    parser = _parse_dashboard()

    assert "stop-server-screen" in parser.elements, (
        "stop-server-screen missing — stop-control.js cannot show the shutdown state"
    )
    screen = parser.elements["stop-server-screen"]
    screen_classes = screen.get("class", "") or ""
    assert "hidden" in screen_classes, (
        f"stop-server-screen must be hidden by default; got: {screen_classes!r}"
    )
    # Restart steps are i18n-bound list items inside the screen.
    html_text = _DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "stop_server_done_restart_step1" in html_text, (
        "shutdown screen must tell the user how to restart (step 1 missing)"
    )
    assert "stop_server_done_restart_step2" in html_text, (
        "shutdown screen must tell the user to refresh the tab (step 2 missing)"
    )


def test_stop_server_i18n_keys_exist() -> None:
    """Every i18n key referenced by the stop UX exists in both nl.json and en.json."""
    import json

    required_keys = [
        "stop_server_title",
        "stop_server_confirm_title",
        "stop_server_confirm_message",
        "stop_server_confirm_button",
        "stop_server_cancel_button",
        "stop_server_done_title",
        "stop_server_done_message",
        "stop_server_done_restart_heading",
        "stop_server_done_restart_step1",
        "stop_server_done_restart_step2",
    ]
    for lang in ("nl", "en"):
        catalog_path = _DASHBOARD_HTML.parent / "i18n" / f"{lang}.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        header = catalog.get("header", {})
        for key in required_keys:
            assert key in header, f"header.{key} missing from {lang}.json i18n catalog"
        # The old single-message key was split into a richer set; it must not
        # linger as a duplicate that could be wired by accident.
        assert "stop_server_confirm" not in header, (
            f"stale header.stop_server_confirm key still in {lang}.json — was replaced "
            "by stop_server_confirm_title/_message/_button"
        )


def test_end_call_tooltip_distinguishes_from_server_stop() -> None:
    """The end-call button carries a tooltip that says it stops the call but keeps the server."""
    parser = _parse_dashboard()
    end_call = parser.elements["end-call"]
    attr = end_call.get("data-i18n-attr") or ""
    assert "setup.end_call_title" in attr, (
        "end-call button must bind the setup.end_call_title tooltip so the label "
        "self-explains the call-vs-server distinction"
    )
    # And the key must exist.
    import json

    nl_path = _DASHBOARD_HTML.parent / "i18n" / "nl.json"
    catalog = json.loads(nl_path.read_text(encoding="utf-8"))
    assert "end_call_title" in catalog.get("setup", {}), (
        "setup.end_call_title missing from nl.json i18n catalog"
    )
