"""Static wiring tests for the dashboard "card used" hint-feedback control.

The endpoint + store are covered by tests/test_hub_endpoints.py and
tests/test_hint_feedback_store.py. These tests guard the new UI half of the
learning loop: the feedback module posts the exact HintFeedbackRequest
payload shape to /api/hint-feedback with the shared auth-token pattern, the
control is wired into every card that carries a response_suggestion plus the
hero coaching cue, and the i18n keys exist in every catalog (dashboard
en/nl + the demo-shim copies) so nothing renders as a raw key.
"""

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD = _ROOT / "dashboard"
_JS_ROOT = _DASHBOARD / "js"
_DEMO = _ROOT / "demo"

_HINT_FEEDBACK_JS = (_JS_ROOT / "hint-feedback.js").read_text(encoding="utf-8")

_FEEDBACK_KEYS = ("useful_label", "not_useful_label", "useful_aria", "not_useful_aria")


def _flatten(catalog: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in catalog.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= _flatten(value, f"{dotted}.")
        else:
            keys.add(dotted)
    return keys


# --- endpoint contract -------------------------------------------------------


def test_feedback_module_posts_to_hint_feedback_endpoint() -> None:
    assert '"/api/hint-feedback"' in _HINT_FEEDBACK_JS


def test_feedback_module_sends_exact_request_fields() -> None:
    """Payload must match HintFeedbackRequest (hub_api.py): hint + feedback
    ('up'|'down') required, context_utterance(s) + timestamp_ms optional."""
    for field in ("hint", "feedback", "context_utterance", "context_utterances", "timestamp_ms"):
        assert field in _HINT_FEEDBACK_JS, f"payload field {field!r} missing from hint-feedback.js"


def test_feedback_module_uses_shared_auth_headers() -> None:
    assert "copilotAuthHeaders" in _HINT_FEEDBACK_JS
    assert "copilotAuthReady" in _HINT_FEEDBACK_JS


def test_feedback_module_fails_quietly() -> None:
    """No alerts/popups on a failed POST — a console.warn at most."""
    assert "alert(" not in _HINT_FEEDBACK_JS
    assert ".catch(" in _HINT_FEEDBACK_JS


# --- dashboard wiring --------------------------------------------------------


def test_card_modules_attach_control() -> None:
    for module in ("objections.js", "pain-points.js", "opportunities.js"):
        text = (_JS_ROOT / module).read_text(encoding="utf-8")
        assert "createHintFeedbackControl" in text, f"{module} does not attach the feedback control"


def test_hero_cue_wires_feedback_host() -> None:
    app_js = (_JS_ROOT / "app.js").read_text(encoding="utf-8")
    assert "hintFeedback?.showHero" in app_js
    assert "hintFeedback?.hideHero" in app_js


def test_index_html_loads_module_and_hosts_hero_control() -> None:
    for html_path in (_DASHBOARD / "index.html", _DEMO / "index.html"):
        html = html_path.read_text(encoding="utf-8")
        assert "js/hint-feedback.js" in html, f"hint-feedback.js script tag missing from {html_path.name}"
        assert 'id="hero-feedback"' in html, f"#hero-feedback host missing from {html_path.name}"


# --- i18n coverage ------------------------------------------------------------


def test_feedback_keys_in_dashboard_catalogs() -> None:
    for lang in ("en", "nl"):
        catalog = json.loads((_DASHBOARD / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        flat = _flatten(catalog)
        for key in _FEEDBACK_KEYS:
            assert f"feedback.{key}" in flat, f"feedback.{key} missing from dashboard/i18n/{lang}.json"


def test_feedback_keys_in_demo_shim_catalogs() -> None:
    """demo-shim.js inlines both catalogs (EN + NL); every feedback key must
    appear at least twice so neither catalog renders a raw key."""
    shim = (_DEMO / "demo-shim.js").read_text(encoding="utf-8")
    for key in _FEEDBACK_KEYS:
        occurrences = len(re.findall(rf"\b{key}\b", shim))
        assert occurrences >= 2, f"feedback.{key} missing from a demo-shim catalog (found {occurrences}x)"


def test_feedback_keys_resolved_in_demo_languages() -> None:
    """The demo-shim feedback sections must carry real copy, not raw keys."""
    shim = (_DEMO / "demo-shim.js").read_text(encoding="utf-8")
    assert '"Gebruikt / nuttig"' in shim  # NL catalog
    assert '"Used / useful"' in shim  # EN catalog
