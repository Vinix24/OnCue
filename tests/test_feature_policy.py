from datetime import timedelta
from unittest import mock

from sales_copilot.auth import license_verifier
from sales_copilot.auth.feature_policy import FEATURE_CALLTAP, FeaturePolicy
from sales_copilot.auth.license_format import LEGACY_HMAC_DEADLINE
from sales_copilot.auth.license_key import generate_key

_SECRET = "feature-policy-secret-32-chars-ok"


def test_feature_policy_defaults_calltap_to_denied(monkeypatch) -> None:
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)
    monkeypatch.delenv("SALES_COPILOT_LICENSE_SECRET", raising=False)

    assert FeaturePolicy().allows(FEATURE_CALLTAP) is False


def test_feature_policy_allows_calltap_for_valid_pro(monkeypatch) -> None:
    # Exercises the real FeaturePolicy().allows() -- the enforcement point
    # that separates Free from Pro -- through a real SC- legacy key, not the
    # conftest _TierFeaturePolicy stub. The legacy HMAC path only verifies
    # before LEGACY_HMAC_DEADLINE, so the calendar date is frozen just ahead
    # of it (same pattern as
    # test_license_verifier.py::test_legacy_sc_before_deadline_succeeds).
    monkeypatch.setenv("SALES_COPILOT_LICENSE_SECRET", _SECRET)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", generate_key("user@example.com", "pro", _SECRET))

    with mock.patch.object(license_verifier, "date") as mdate:
        mdate.today.return_value = LEGACY_HMAC_DEADLINE - timedelta(days=1)
        assert FeaturePolicy().allows(FEATURE_CALLTAP) is True


def test_feature_policy_rejects_tampered_license(monkeypatch) -> None:
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SC-INVALID")
    monkeypatch.setenv("SALES_COPILOT_LICENSE_SECRET", _SECRET)

    assert FeaturePolicy().allows(FEATURE_CALLTAP) is False
