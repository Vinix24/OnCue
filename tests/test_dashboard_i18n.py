"""Tests for the dashboard i18n mechanism and static string coverage (PR1 +
PR2 + PR3 of 4 -- see claudedocs/2026-07-24-dashboard-i18n-design.md).

PR1 verified the mechanism: every data-i18n / data-i18n-attr key referenced
in dashboard/index.html must resolve in the nl catalog (the default +
fallback language), and the en catalog must not introduce keys nl does not
have. PR2 extracted the remaining static, hardcoded, user-facing strings in
dashboard/index.html. PR3 routes the runtime-rendered strings dashboard/js/*.js
builds through the same t() mechanism (the EN-default flip is PR4 scope) --
the tests below extend the same mechanism checks and add targeted coverage
assertions for that extraction.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_DASHBOARD_ROOT = Path(__file__).parent.parent / "dashboard"
_INDEX_HTML = _DASHBOARD_ROOT / "index.html"
_JS_ROOT = _DASHBOARD_ROOT / "js"
_NL_CATALOG = _DASHBOARD_ROOT / "i18n" / "nl.json"
_EN_CATALOG = _DASHBOARD_ROOT / "i18n" / "en.json"

_DATA_I18N_RE = re.compile(r'data-i18n="([^"]+)"')
_DATA_I18N_ATTR_RE = re.compile(r'data-i18n-attr="([^"]+)"')
_JS_T_CALL_RE = re.compile(
    r'(?:window\.SalesCopilotI18n\.t|window\.t|(?<![\w.])t)\(\s*[\"\'](?P<key>[a-zA-Z0-9_.]+)[\"\']'
)


def _flatten(catalog: dict, prefix: str = "") -> set[str]:
    """Flatten a nested dotted-key catalog into the set of leaf key paths."""
    keys: set[str] = set()
    for name, value in catalog.items():
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, dict):
            keys |= _flatten(value, path)
        else:
            keys.add(path)
    return keys


def _html_i18n_key_counts() -> Counter[str]:
    """Count how many elements reference each dotted key, across both
    data-i18n (textContent) and data-i18n-attr (attribute) usages."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    counts: Counter[str] = Counter(_DATA_I18N_RE.findall(html))
    for spec in _DATA_I18N_ATTR_RE.findall(html):
        for pair in spec.split(","):
            if ":" not in pair:
                continue
            _, key = pair.split(":", 1)
            counts[key.strip()] += 1
    return counts


def _html_i18n_keys() -> set[str]:
    return set(_html_i18n_key_counts())


def _js_i18n_keys() -> set[str]:
    """Collect every dotted key referenced via a t()-style call literal
    across dashboard/js/*.js (t("key"), window.t("key"),
    window.SalesCopilotI18n.t("key")). Keys resolved indirectly through a
    lookup object (e.g. script-tracking.js's STATUS_LABEL_KEYS) are not
    literal call sites and are intentionally out of scope for this scan."""
    keys: set[str] = set()
    for js_file in _JS_ROOT.glob("*.js"):
        text = js_file.read_text(encoding="utf-8")
        keys |= {match.group("key") for match in _JS_T_CALL_RE.finditer(text)}
    return keys


@pytest.fixture(scope="module")
def nl_catalog() -> dict:
    return json.loads(_NL_CATALOG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def en_catalog() -> dict:
    return json.loads(_EN_CATALOG.read_text(encoding="utf-8"))


# --- catalog + HTML wiring ---------------------------------------------------


def test_catalog_files_exist() -> None:
    assert _NL_CATALOG.is_file(), "dashboard/i18n/nl.json missing"
    assert _EN_CATALOG.is_file(), "dashboard/i18n/en.json missing"


def test_html_data_i18n_keys_resolve_in_nl_catalog(nl_catalog: dict) -> None:
    html_keys = _html_i18n_keys()
    assert html_keys, "no data-i18n / data-i18n-attr keys found in dashboard/index.html"
    nl_keys = _flatten(nl_catalog)
    missing = html_keys - nl_keys
    assert not missing, f"data-i18n keys missing from nl.json (default catalog): {sorted(missing)}"


def test_en_catalog_keys_are_subset_of_nl(nl_catalog: dict, en_catalog: dict) -> None:
    nl_keys = _flatten(nl_catalog)
    en_keys = _flatten(en_catalog)
    extra = en_keys - nl_keys
    assert not extra, f"en.json has keys not present in nl.json (default): {sorted(extra)}"


def test_nl_catalog_keys_are_subset_of_en(nl_catalog: dict, en_catalog: dict) -> None:
    """Full nl/en key parity (PR3 scope): guard the reverse direction too --
    a key added to nl.json without its en.json counterpart would otherwise
    only be caught by test_en_catalog_keys_are_subset_of_nl's complement."""
    nl_keys = _flatten(nl_catalog)
    en_keys = _flatten(en_catalog)
    missing = nl_keys - en_keys
    assert not missing, f"nl.json has keys not present in en.json: {sorted(missing)}"


def test_js_t_call_keys_resolve_in_nl_catalog(nl_catalog: dict) -> None:
    """PR3: every i18n key referenced via a t()-style call literal in
    dashboard/js/*.js must resolve in the nl catalog (the default +
    fallback language), mirroring the HTML data-i18n coverage check."""
    js_keys = _js_i18n_keys()
    assert js_keys, "no t(...) / window.t(...) call-site keys found in dashboard/js/*.js"
    nl_keys = _flatten(nl_catalog)
    missing = js_keys - nl_keys
    assert not missing, f"JS t() keys missing from nl.json (default catalog): {sorted(missing)}"


def test_html_wires_i18n_script() -> None:
    html = _INDEX_HTML.read_text(encoding="utf-8")
    assert "js/i18n.js" in html, "i18n.js script tag missing from dashboard/index.html"


def test_shared_close_dialog_key_used_on_both_dismiss_buttons() -> None:
    """common.close_dialog is intentionally reused across two elements
    (see design doc "Reusable keys") -- guard against a future edit
    accidentally forking it into per-element keys."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    assert html.count('data-i18n-attr="aria-label:common.close_dialog"') == 2


def test_reused_i18n_keys_appear_expected_number_of_times() -> None:
    """PR2 intentionally reuses several keys across multiple elements that
    share the same source string (modal close buttons, "live" badges, the
    shared empty-summary message, talk-time labels repeated in the side
    panel and the cumulative-goal line, the neutral sentiment default, and
    the call-medium label/aria-label pair) -- guard against a future edit
    accidentally forking one of these into per-element duplicates."""
    counts = _html_i18n_key_counts()
    expected_counts = {
        "common.close_dialog": 2,
        "common.close_window": 2,
        "common.live_badge": 2,
        "sentiment.neutral": 2,
        "setup.call_medium_label": 2,
        "summary.empty_state": 2,
        "talktime.prospect_label": 2,
        "talktime.you_label": 2,
    }
    for key, expected in expected_counts.items():
        assert counts[key] == expected, (
            f"expected {key!r} to be referenced {expected} times, "
            f"found {counts[key]}"
        )


def test_index_html_no_longer_has_bare_untranslated_static_strings() -> None:
    """Targeted regression check for the PR2 bulk extraction: a sample of
    strings that were previously bare text/attribute values in
    dashboard/index.html must now only appear wrapped in a data-i18n /
    data-i18n-attr carrying element, not as loose untranslated text. This
    is not a brittle full-file scan -- it spot-checks one representative
    string per converted region."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    spot_checks = [
        ("license.get_free_key", 'data-i18n="license.get_free_key">Krijg gratis key<'),
        ("license.modal_title", 'data-i18n="license.modal_title">Gratis bonus + license-key<'),
        ("degrade.upgrade_cta", 'data-i18n="degrade.upgrade_cta"'),
        ("pro_upgrade.modal_title", 'data-i18n="pro_upgrade.modal_title">Sales Pro feature<'),
        ("setup.screen_mode_title", 'data-i18n="setup.screen_mode_title">Screen mode<'),
        ("setup.module_talk_time", 'data-i18n="setup.module_talk_time">Talk Time<'),
        ("setup.start_call_button", 'data-i18n="setup.start_call_button">Start Call<'),
        ("consent.checkbox_label", 'data-i18n="consent.checkbox_label">'),
        ("header.no_prospect_set", 'data-i18n="header.no_prospect_set">Nog geen prospect ingesteld<'),
        ("objections.title", 'data-i18n="objections.title">Bezwaren<'),
        ("painpoints.empty_state", 'data-i18n="painpoints.empty_state">Nog geen detections binnen.<'),
        ("opportunities.title", 'data-i18n="opportunities.title">Kansen<'),
        ("talktime.title", 'data-i18n="talktime.title">Talking time<'),
        ("sentiment.title", 'data-i18n="sentiment.title">Klantstemming<'),
        ("scripttracking.title", 'data-i18n="scripttracking.title">Gespreksplan<'),
        ("summary.title", 'data-i18n="summary.title">Samenvatting<'),
        ("report.pain_points_title", 'data-i18n="report.pain_points_title">Pain points detected<'),
        ("report.new_call_button", 'data-i18n="report.new_call_button">New Call<'),
    ]
    for key, needle in spot_checks:
        assert needle in html, f"expected {key!r} conversion pattern not found: {needle!r}"


# --- backend language source --------------------------------------------------


def test_backend_api_config_exposes_default_language(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dashboard reads its active language from /api/config -- the single
    source of truth shared with the backend LANGUAGE env var. Unset LANGUAGE
    must resolve to the Dutch-native default (nl is default for the pilot)."""
    monkeypatch.delenv("LANGUAGE", raising=False)

    from sales_copilot.websocket import hub

    client = TestClient(hub.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json().get("language") == "nl"


def test_backend_api_config_language_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGUAGE", "en")

    from sales_copilot.websocket import hub

    client = TestClient(hub.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json().get("language") == "en"


def test_backend_api_config_language_follows_env_nl(monkeypatch: pytest.MonkeyPatch) -> None:
    """nl stays a first-class toggle end-to-end through /api/config."""
    monkeypatch.setenv("LANGUAGE", "nl")

    from sales_copilot.websocket import hub

    client = TestClient(hub.app)
    response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json().get("language") == "nl"


def test_js_i18n_default_language_is_nl() -> None:
    """dashboard/js/i18n.js's fallback constant must match the backend's
    Dutch-native default (nl is default for the pilot) -- a missing key or catalog
    must never fall back to English on the dashboard."""
    js = (_JS_ROOT / "i18n.js").read_text(encoding="utf-8")
    assert 'const DEFAULT_LANGUAGE = "nl";' in js
