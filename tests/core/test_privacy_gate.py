"""Full privacy x provider-tier matrix for the klantmap-als-eenheid D2 privacy-poort.

``tier_of()`` itself (local/tenant/public classification) is tested exhaustively in
``tests/core/test_outbound_policy.py``; this file only tests the privacy-ceiling ranking
on top of it -- every combination of a client's ``klant.yaml`` ``privacy`` field against
every provider tier, plus the ``enforce_privacy`` error path.
"""

from __future__ import annotations

import pytest

from sales_copilot.core.privacy_gate import (
    PrivacyGateError,
    enforce_privacy,
    privacy_allows,
)

# provider -> tier, for readable parametrize ids. Providers are picked to hit each tier
# with TRUST_OWN_TENANT set, matching how tier_of() itself classifies them.
_LOCAL_PROVIDER = "ollama"
_TENANT_PROVIDER = "azure"
_PUBLIC_PROVIDER = "gemini"


@pytest.fixture(autouse=True)
def _tier_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix the provider->tier mapping used by every test in this file.

    ollama on loopback -> local; azure with TRUST_OWN_TENANT -> tenant; gemini -> always
    public (a public multi-tenant API is never trusted, flag or not).
    """
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("TRUST_OWN_TENANT", "true")


class TestPrivacyAllowsMatrix:
    """3 (privacy) x 3 (tier) = 9 combinations, plus privacy=None."""

    def test_no_privacy_ceiling_allows_any_provider(self) -> None:
        assert privacy_allows(None, _LOCAL_PROVIDER) is True
        assert privacy_allows(None, _TENANT_PROVIDER) is True
        assert privacy_allows(None, _PUBLIC_PROVIDER) is True

    def test_local_privacy_allows_only_local_provider(self) -> None:
        assert privacy_allows("local", _LOCAL_PROVIDER) is True
        assert privacy_allows("local", _TENANT_PROVIDER) is False
        assert privacy_allows("local", _PUBLIC_PROVIDER) is False

    def test_tenant_privacy_allows_local_and_tenant_not_public(self) -> None:
        assert privacy_allows("tenant", _LOCAL_PROVIDER) is True
        assert privacy_allows("tenant", _TENANT_PROVIDER) is True
        assert privacy_allows("tenant", _PUBLIC_PROVIDER) is False

    def test_public_privacy_allows_any_provider(self) -> None:
        assert privacy_allows("public", _LOCAL_PROVIDER) is True
        assert privacy_allows("public", _TENANT_PROVIDER) is True
        assert privacy_allows("public", _PUBLIC_PROVIDER) is True


class TestEnforcePrivacy:
    def test_allowed_combination_does_not_raise(self) -> None:
        enforce_privacy("tenant", _TENANT_PROVIDER, client_slug="acme-corp")

    def test_local_privacy_with_public_provider_raises(self) -> None:
        with pytest.raises(PrivacyGateError) as exc_info:
            enforce_privacy("local", _PUBLIC_PROVIDER, client_slug="acme-corp")
        message = str(exc_info.value)
        assert "acme-corp" in message
        assert "local" in message
        assert _PUBLIC_PROVIDER in message

    def test_tenant_privacy_with_public_provider_raises(self) -> None:
        with pytest.raises(PrivacyGateError):
            enforce_privacy("tenant", _PUBLIC_PROVIDER, client_slug="acme-corp")

    def test_local_privacy_with_tenant_provider_raises(self) -> None:
        with pytest.raises(PrivacyGateError):
            enforce_privacy("local", _TENANT_PROVIDER, client_slug="acme-corp")

    def test_privacy_gate_error_is_a_value_error(self) -> None:
        """hub_api.start_call_api maps any bare ValueError to HTTP 400 -- this must
        stay a ValueError subclass or the gate stops surfacing as a 400."""
        assert issubclass(PrivacyGateError, ValueError)

    def test_no_client_yaml_privacy_never_raises(self) -> None:
        enforce_privacy(None, _PUBLIC_PROVIDER, client_slug="acme-corp")

    def test_lane_name_is_included_in_the_error_when_given(self) -> None:
        """hub_core._apply_klant_config checks the detector AND insight lane's provider
        against the same ceiling -- the message must say which lane crossed it."""
        with pytest.raises(PrivacyGateError) as exc_info:
            enforce_privacy("local", _PUBLIC_PROVIDER, client_slug="acme-corp", lane="insight")
        assert "insight" in str(exc_info.value)

    def test_omitted_lane_leaves_the_message_unchanged(self) -> None:
        """Backward compatible: a caller that doesn't pass ``lane`` gets the exact same
        message as before multi-lane checking existed."""
        with pytest.raises(PrivacyGateError) as exc_info:
            enforce_privacy("local", _PUBLIC_PROVIDER, client_slug="acme-corp")
        assert "-lane)" not in str(exc_info.value)
