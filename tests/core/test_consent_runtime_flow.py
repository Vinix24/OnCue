"""Consent enforcement on the real session-start path.

These tests exercise ``__main__._resolve_start_call_consent`` — the same helper
used by the orchestrator's start-call loop — to prove that a strict preset
(recruitment) blocks without consent and proceeds with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot import __main__ as main_mod
from sales_copilot.core import consent

# The recruitment preset is Pro content and is absent from the OSS export. The
# strict-tier enforcement below is compliance logic and must stay fully tested
# everywhere: resolve preset YAMLs against the test-only fixtures
# (tests/fixtures/presets/recruitment.yaml -> default_consent_tier: strict).
_FIXTURE_PRESETS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "presets"


@pytest.fixture(autouse=True)
def _fixture_preset_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        consent,
        "resolve_app_path",
        lambda rel: _FIXTURE_PRESETS_DIR / Path(rel).name,
    )


@pytest.mark.parametrize("tier", ["off", "audit", "soft"])
def test_non_strict_tiers_never_block_without_consent(monkeypatch, tier) -> None:
    monkeypatch.setenv("CONSENT_TIER", tier)
    payload = {"preset_name": "sales"}
    call_tier, signal, given, blocked = main_mod._resolve_start_call_consent(
        payload, "sales", "sess-1"
    )
    assert call_tier == tier
    assert signal is None
    assert given is False
    assert blocked is False


def test_recruitment_strict_blocks_without_consent(monkeypatch) -> None:
    monkeypatch.delenv("CONSENT_TIER", raising=False)
    monkeypatch.delenv("CONSENT_GATE_MODE", raising=False)
    payload = {"preset_name": "recruitment"}
    call_tier, signal, given, blocked = main_mod._resolve_start_call_consent(
        payload, "recruitment", "sess-2"
    )
    assert call_tier == "strict"
    assert signal is None
    assert given is False
    assert blocked is True


def test_recruitment_strict_allows_with_explicit_consent(monkeypatch) -> None:
    monkeypatch.delenv("CONSENT_TIER", raising=False)
    monkeypatch.delenv("CONSENT_GATE_MODE", raising=False)
    payload = {
        "preset_name": "recruitment",
        "consent": {"asked": True, "given": True},
    }
    call_tier, signal, given, blocked = main_mod._resolve_start_call_consent(
        payload, "recruitment", "sess-3"
    )
    assert call_tier == "strict"
    assert signal == (True, True)
    assert given is True
    assert blocked is False


def test_env_tier_overrides_recruitment_preset_default(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "audit")
    payload = {"preset_name": "recruitment"}
    call_tier, _signal, _given, blocked = main_mod._resolve_start_call_consent(
        payload, "recruitment", "sess-4"
    )
    assert call_tier == "audit"
    assert blocked is False


def test_strict_env_blocks_sales_preset(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "strict")
    payload = {"preset_name": "sales"}
    call_tier, _signal, _given, blocked = main_mod._resolve_start_call_consent(
        payload, "sales", "sess-5"
    )
    assert call_tier == "strict"
    assert blocked is True


def test_soft_tier_shows_nudge_but_does_not_block(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "soft")
    payload = {"preset_name": "sales"}
    call_tier, _signal, given, blocked = main_mod._resolve_start_call_consent(
        payload, "sales", "sess-6"
    )
    assert call_tier == "soft"
    assert blocked is False
    assert consent.should_soft_nudge(given, tier=call_tier) is True
