"""Consent tier ladder — behavior on the session-start path.

Tests the configurable CONSENT_TIER ladder (off/audit/soft/strict), backward
compatibility with the legacy CONSENT_GATE_MODE env var, preset defaults, and
env-override precedence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot.__main__ import _extract_consent
from sales_copilot.core import consent

# The vertical Pro preset packs (coach/recruitment/acquisitie) are Pro content
# and are absent from the OSS export. The consent-tier ladder is compliance
# logic, so it must stay fully tested everywhere: resolve preset YAMLs against
# the test-only fixtures (tests/fixtures/presets/) instead of the real packs.
# Fixture tree: sales=audit, recruitment=strict; coach/acquisitie intentionally
# absent -> the loader's fail-soft default ("audit") is what those tests assert.
_FIXTURE_PRESETS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "presets"


@pytest.fixture(autouse=True)
def _fixture_preset_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        consent,
        "resolve_app_path",
        lambda rel: _FIXTURE_PRESETS_DIR / Path(rel).name,
    )

# --- consent_tier resolution -------------------------------------------------


def test_tier_defaults_to_audit(monkeypatch) -> None:
    monkeypatch.delenv("CONSENT_TIER", raising=False)
    monkeypatch.delenv("CONSENT_GATE_MODE", raising=False)
    assert consent.consent_tier() == "audit"


def test_tier_env_overrides_default(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "strict")
    assert consent.consent_tier() == "strict"


def test_tier_invalid_env_falls_back_to_preset_default(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "banana")
    monkeypatch.delenv("CONSENT_GATE_MODE", raising=False)
    assert consent.consent_tier("recruitment") == "strict"


def test_tier_env_wins_over_preset_default(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "audit")
    assert consent.consent_tier("recruitment") == "audit"


def test_legacy_audit_maps_to_audit(monkeypatch) -> None:
    monkeypatch.delenv("CONSENT_TIER", raising=False)
    monkeypatch.setenv("CONSENT_GATE_MODE", "audit")
    assert consent.consent_tier() == "audit"


def test_legacy_blocking_maps_to_strict(monkeypatch) -> None:
    monkeypatch.delenv("CONSENT_TIER", raising=False)
    monkeypatch.setenv("CONSENT_GATE_MODE", "blocking")
    assert consent.consent_tier() == "strict"


def test_env_tier_wins_over_legacy_gate_mode(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "soft")
    monkeypatch.setenv("CONSENT_GATE_MODE", "blocking")
    assert consent.consent_tier() == "soft"


# --- preset defaults ---------------------------------------------------------


def test_recruitment_preset_defaults_to_strict() -> None:
    assert consent.consent_tier("recruitment") == "strict"


def test_sales_preset_defaults_to_audit() -> None:
    assert consent.consent_tier("sales") == "audit"


def test_coach_preset_defaults_to_audit() -> None:
    assert consent.consent_tier("coach") == "audit"


def test_acquisitie_preset_defaults_to_audit() -> None:
    assert consent.consent_tier("acquisitie") == "audit"


# --- must_block_start --------------------------------------------------------


def test_off_never_blocks(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "off")
    assert consent.must_block_start(False) is False
    assert consent.must_block_start(True) is False


def test_audit_never_blocks(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "audit")
    assert consent.must_block_start(False) is False
    assert consent.must_block_start(True) is False


def test_soft_never_blocks(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "soft")
    assert consent.must_block_start(False) is False
    assert consent.must_block_start(True) is False


def test_strict_blocks_without_consent(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "strict")
    assert consent.must_block_start(False) is True


def test_strict_allows_with_consent(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "strict")
    assert consent.must_block_start(True) is False


def test_explicit_tier_argument_wins(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "audit")
    assert consent.must_block_start(False, tier="strict") is True


# --- should_soft_nudge -------------------------------------------------------


def test_soft_nudges_without_consent(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "soft")
    assert consent.should_soft_nudge(False) is True


def test_soft_does_not_nudge_with_consent(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "soft")
    assert consent.should_soft_nudge(True) is False


def test_audit_does_not_nudge(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "audit")
    assert consent.should_soft_nudge(False) is False


def test_strict_does_not_nudge_it_blocks(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "strict")
    assert consent.should_soft_nudge(False) is False


# --- _extract_consent --------------------------------------------------------


def test_extract_consent_nested() -> None:
    assert _extract_consent({"consent": {"asked": True, "given": True}}) == (True, True)
    assert _extract_consent({"consent": {"asked": True, "given": False}}) == (True, False)


def test_extract_consent_nested_given_implies_asked() -> None:
    assert _extract_consent({"consent": {"given": True}}) == (True, True)


def test_extract_consent_flat() -> None:
    assert _extract_consent({"consent_given": True}) == (True, True)
    assert _extract_consent({"consent_given": False}) == (False, False)


def test_extract_consent_absent_returns_none() -> None:
    assert _extract_consent({"prospect": {"name": "Acme"}}) is None
    assert _extract_consent({}) is None


# --- end-to-end gate decision (mirrors the __main__ start-call logic) -------


def test_strict_blocks_when_payload_carries_no_consent(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "strict")
    signal = _extract_consent({"prospect": {"name": "Acme"}})
    given = bool(signal and signal[1])
    assert consent.must_block_start(consent_given=given) is True


def test_strict_allows_when_payload_consent_given(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "strict")
    signal = _extract_consent({"consent": {"asked": True, "given": True}})
    given = bool(signal and signal[1])
    assert consent.must_block_start(consent_given=given) is False


def test_audit_allows_even_without_consent(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "audit")
    signal = _extract_consent({})
    given = bool(signal and signal[1])
    assert consent.must_block_start(consent_given=given) is False


def test_soft_does_not_block_without_consent(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TIER", "soft")
    signal = _extract_consent({})
    given = bool(signal and signal[1])
    assert consent.must_block_start(consent_given=given) is False
    assert consent.should_soft_nudge(consent_given=given) is True
