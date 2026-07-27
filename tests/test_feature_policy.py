from datetime import UTC, datetime, timedelta

from sales_copilot.auth.feature_policy import FEATURE_CALLTAP, FeaturePolicy
from sales_copilot.auth.license_key import generate_key

_SECRET = "feature-policy-secret-32-chars-ok"


def test_feature_policy_defaults_calltap_to_denied(monkeypatch) -> None:
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)
    monkeypatch.delenv("SALES_COPILOT_LICENSE_SECRET", raising=False)

    assert FeaturePolicy().allows(FEATURE_CALLTAP) is False


def test_feature_policy_allows_calltap_for_valid_pro(monkeypatch) -> None:
    now = datetime.now(UTC)
    key = generate_key(
        "pro@example.com",
        "pro",
        _SECRET,
        issued_at=now,
        expires_at=now + timedelta(days=30),
    )
    monkeypatch.setenv("SALES_COPILOT_LICENSE", key)
    monkeypatch.setenv("SALES_COPILOT_LICENSE_SECRET", _SECRET)

    assert FeaturePolicy().allows(FEATURE_CALLTAP) is True


def test_feature_policy_rejects_tampered_license(monkeypatch) -> None:
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SC-INVALID")
    monkeypatch.setenv("SALES_COPILOT_LICENSE_SECRET", _SECRET)

    assert FeaturePolicy().allows(FEATURE_CALLTAP) is False
