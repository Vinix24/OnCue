"""The Free-tier response teaser comes from the i18n catalog (OI-045)."""

from __future__ import annotations

import pytest

from sales_copilot.auth import feature_policy


def test_english_teaser() -> None:
    assert feature_policy.response_locked_teaser("en") == "\U0001f512 Pro: see the best counter-response"
    assert feature_policy.RESPONSE_LOCKED_TEASER == feature_policy.response_locked_teaser("en")


def test_dutch_teaser() -> None:
    assert feature_policy.response_locked_teaser("nl") == "\U0001f512 Pro: bekijk de beste tegenreactie"


def test_teaser_follows_the_configured_language(monkeypatch: pytest.MonkeyPatch) -> None:
    assert feature_policy.response_locked_teaser() == feature_policy.response_locked_teaser("en")
    monkeypatch.setenv("LANGUAGE", "nl")
    assert feature_policy.response_locked_teaser() == feature_policy.response_locked_teaser("nl")


def test_unknown_language_falls_back_to_english_not_the_key() -> None:
    assert feature_policy.response_locked_teaser("fr") == feature_policy.response_locked_teaser("en")


def test_the_gate_returns_the_teaser_in_the_configured_language(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Free:
        def allows(self, _feature_id: str) -> bool:
            return False

    monkeypatch.setenv("LANGUAGE", "nl")
    assert feature_policy.resolve_response_suggestion("curated", feature_policy=_Free()) == (
        "\U0001f512 Pro: bekijk de beste tegenreactie"
    )
    monkeypatch.setenv("LANGUAGE", "en")
    assert feature_policy.resolve_response_suggestion("curated", feature_policy=_Free()) == (
        "\U0001f512 Pro: see the best counter-response"
    )
