"""Static wiring tests for the track 3 (deep insight lane) dashboard panel (PR-D2).

Mirrors tests/test_dashboard_hint_feedback.py's approach: these are structural/
string-presence checks on the vanilla dashboard files, not runtime tests --
the engine round-trip is covered by tests/test_insight_engine.py and the HTTP
endpoint by tests/test_hub_endpoints.py.
"""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DASHBOARD = _ROOT / "dashboard"
_JS_ROOT = _DASHBOARD / "js"

_INSIGHTS_JS = (_JS_ROOT / "insights.js").read_text(encoding="utf-8")
_INDEX_HTML = (_DASHBOARD / "index.html").read_text(encoding="utf-8")
_REPORT_JS = (_JS_ROOT / "report.js").read_text(encoding="utf-8")
_PRO_UPGRADE_JS = (_JS_ROOT / "pro-upgrade.js").read_text(encoding="utf-8")

_INSIGHT_TYPES = ("doorvraag", "inzicht", "risico", "feitencheck", "antwoord")

_I18N_KEYS = (
    "title",
    "badge_label",
    "empty_state",
    "type_doorvraag",
    "type_inzicht",
    "type_risico",
    "type_feitencheck",
    "type_antwoord",
    "speculation_laag",
    "speculation_hoog",
    "source_engine",
    "source_mcp",
    "question_label",
    "dismiss_label",
    "ask_placeholder",
    "ask_submit",
    "budget_exhausted",
    "pro_gate_message",
    "upgrade_button",
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


# --- markup wiring ------------------------------------------------------------


def test_index_html_hosts_insights_panel_and_ask_box() -> None:
    for element_id in (
        "insights-section",
        "insights-panel",
        "insights-ask-form",
        "insights-ask-input",
        "insights-ask-submit",
    ):
        assert f'id="{element_id}"' in _INDEX_HTML, f"#{element_id} missing from dashboard/index.html"


def test_index_html_loads_insights_module() -> None:
    assert "js/insights.js" in _INDEX_HTML


def test_index_html_hosts_report_insights_section() -> None:
    assert 'id="report-insights"' in _INDEX_HTML


# --- channel + endpoint wiring -------------------------------------------------


def test_insights_module_subscribes_to_insights_channel() -> None:
    assert "/ws/insights" in _INSIGHTS_JS


def test_insights_module_posts_ask_to_insights_ask_endpoint() -> None:
    assert '"/api/insights/ask"' in _INSIGHTS_JS


def test_insights_module_wegklik_reuses_hint_feedback_endpoint() -> None:
    """Dismiss must go through the existing feedback_store flow, not a new store."""
    assert '"/api/hint-feedback"' in _INSIGHTS_JS
    assert '"down"' in _INSIGHTS_JS


def test_insights_module_dismiss_includes_session_id() -> None:
    """PR-D3b: the wegklik POST must carry the real session_id so
    _recent_rejected_insights() can filter per-session."""
    assert "session_id" in _INSIGHTS_JS
    assert "tester-session-id" in _INSIGHTS_JS


def test_insights_module_uses_shared_auth_headers() -> None:
    assert "copilotAuthHeaders" in _INSIGHTS_JS
    assert "copilotAuthReady" in _INSIGHTS_JS


def test_insights_module_fails_quietly() -> None:
    """No alerts/popups -- breathing-bar philosophy, non-disruptive feed."""
    assert "alert(" not in _INSIGHTS_JS
    assert ".catch(" in _INSIGHTS_JS


def test_insights_module_renders_every_insight_type_as_a_distinct_badge() -> None:
    """Badge labels are built via `insights.type_${key}` template lookups (see
    badgeLabel()); every type must be listed in INSIGHT_TYPES so it resolves to
    a real i18n key rather than falling back to "inzicht"."""
    assert "insights.type_${key}" in _INSIGHTS_JS
    for insight_type in _INSIGHT_TYPES:
        assert f'"{insight_type}"' in _INSIGHTS_JS


def test_insights_module_handles_budget_exhausted_notice() -> None:
    assert "insight_budget_exhausted" in _INSIGHTS_JS


def test_insights_module_renders_source_badge_for_mcp_and_engine() -> None:
    """PR-M2: pushed (MCP) insights must be visually distinguishable from
    engine-generated ones -- see `insights.source_${key}` template lookups."""
    assert "insights.source_${key}" in _INSIGHTS_JS
    assert '"mcp"' in _INSIGHTS_JS
    assert '"engine"' in _INSIGHTS_JS
    assert "insight-source" in _INSIGHTS_JS


def test_insights_module_registers_reset_hook() -> None:
    assert "window.resetInsightsPanel" in _INSIGHTS_JS
    assert "window.resetInsightsPanel" in (_JS_ROOT / "app.js").read_text(encoding="utf-8")


# --- report-view wiring --------------------------------------------------------


def test_report_module_renders_insights_section() -> None:
    assert "report-insights" in _REPORT_JS
    assert "insightsFromReport" in _REPORT_JS
    assert "report.insights_title" in _REPORT_JS


# --- Pro gating ----------------------------------------------------------------


def test_pro_upgrade_module_gates_deep_insights_feature() -> None:
    assert '"coaching.deep_insights"' in _PRO_UPGRADE_JS
    assert "applyInsightsGating" in _PRO_UPGRADE_JS
    assert "insights-section" in _PRO_UPGRADE_JS


# --- i18n coverage ---------------------------------------------------------------


def test_insights_keys_in_dashboard_catalogs() -> None:
    for lang in ("en", "nl"):
        catalog = json.loads((_DASHBOARD / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        flat = _flatten(catalog)
        for key in _I18N_KEYS:
            assert f"insights.{key}" in flat, f"insights.{key} missing from dashboard/i18n/{lang}.json"
        assert "report.insights_title" in flat, f"report.insights_title missing from dashboard/i18n/{lang}.json"
        assert "report.no_insights" in flat, f"report.no_insights missing from dashboard/i18n/{lang}.json"
