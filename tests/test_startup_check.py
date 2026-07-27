"""Tests for F03: license secret enforcement in startup_check and email_capture."""

from __future__ import annotations

import pytest

from sales_copilot.auth.startup_check import (
    LicenseStatus,
    check_license_at_startup,
)

_PLACEHOLDER = "change-me-32-chars-min-placeholder"


# ---------------------------------------------------------------------------
# check_license_at_startup — unverifiable keys are MISSING (Ed25519 authority)
# ---------------------------------------------------------------------------
#
# The validation path now verifies the Ed25519 signature via decode_and_verify
# and needs no SALES_COPILOT_LICENSE_SECRET, so the client secret no longer
# gates the verdict. An unverifiable key reports MISSING (bad signature/format).
# The richer status matrix (valid / expired / revoked / parity with
# feature_policy) lives in tests/auth/test_startup_check_authority.py.


def test_license_status_missing_with_placeholder_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A placeholder secret no longer changes the verdict for an invalid key."""
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SC-FAKE-FAKE-FAKE-FAKE-FAKE-FAKE-FAKE")
    monkeypatch.setenv("SALES_COPILOT_LICENSE_SECRET", _PLACEHOLDER)

    status, message = check_license_at_startup()

    assert status == LicenseStatus.MISSING
    assert "invalid" in message.lower()


def test_license_status_missing_when_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unverifiable key is MISSING even with no secret configured."""
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SC-FAKE-FAKE-FAKE-FAKE-FAKE-FAKE-FAKE")
    monkeypatch.delenv("SALES_COPILOT_LICENSE_SECRET", raising=False)

    status, message = check_license_at_startup()

    assert status == LicenseStatus.MISSING


# ---------------------------------------------------------------------------
# email_capture._secret — no string default
# ---------------------------------------------------------------------------


def test_email_capture_no_fallback_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """email_capture._secret() must raise RuntimeError when env var is missing."""
    monkeypatch.delenv("SALES_COPILOT_LICENSE_SECRET", raising=False)

    from sales_copilot.auth import email_capture

    with pytest.raises(RuntimeError, match="SALES_COPILOT_LICENSE_SECRET"):
        email_capture._secret()


def test_email_capture_placeholder_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """email_capture._secret() must raise RuntimeError for placeholder value."""
    monkeypatch.setenv("SALES_COPILOT_LICENSE_SECRET", _PLACEHOLDER)

    from sales_copilot.auth import email_capture

    with pytest.raises(RuntimeError, match="placeholder"):
        email_capture._secret()


def test_email_capture_no_fallback_variable() -> None:
    """Verify _FALLBACK_SECRET variable no longer exists in email_capture."""
    from sales_copilot.auth import email_capture

    assert not hasattr(email_capture, "_FALLBACK_SECRET"), (
        "_FALLBACK_SECRET must be removed from email_capture (was the exploitable default)"
    )
