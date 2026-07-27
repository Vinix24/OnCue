"""Startup license authority — startup_check shares one verifier with feature_policy.

Pins the fix for the license split-brain (#3): ``check_license_at_startup()``
(behind ``/api/v1/license/status``) must resolve a key with the same Ed25519
authority as ``FeaturePolicy.current_tier()`` (behind ``/api/v1/license/features``),
and must distinguish EXPIRED from MISSING per its own docstring.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _dev_anchor import requires_dev_anchor
from _scp_signing import make_scp_key
from fastapi.testclient import TestClient

from sales_copilot.auth import _embedded_keys, revocation_cache
from sales_copilot.auth import build_pubkey as build_pubkey_module
from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.auth.startup_check import LicenseStatus, check_license_at_startup
from sales_copilot.websocket import hub

_VECTOR = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "license_pro_test_vector.json").read_text(
        encoding="utf-8"
    )
)
_PAID_TIERS = frozenset({"pro", "enterprise"})


def _isolate_revocation(monkeypatch, tmp_path, *, revoked: bool = False) -> None:
    """Pin the revocation cache to tmp + a deterministic fetch (no network)."""
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(
        revocation_cache,
        "_fetch_revocation",
        lambda license_id, base_url: {"revoked": revoked, "expires_at": 0},
    )


def _trust_only(monkeypatch, pubkey_hex: str) -> None:
    """Make ``pubkey_hex`` the sole trusted epoch-0 key (simulated prod build).

    Drops the dev key module and embeds ``pubkey_hex`` as the production pubkey,
    so an ephemeral, in-memory-signed key verifies without any committed seed.
    """
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", pubkey_hex)


def _expired_pro_key(monkeypatch) -> str:
    """A signature-valid SCP- Pro key whose ``expires_at`` is in the past.

    Signs with an ephemeral keypair and trusts its pubkey, so the key verifies
    but resolves to EXPIRED rather than a bad-signature MISSING.
    """
    issued = datetime.now(UTC) - timedelta(days=400)
    key, pubkey_hex = make_scp_key("pro", 30, now=issued)  # expires ~370 days ago
    _trust_only(monkeypatch, pubkey_hex)
    return key


@requires_dev_anchor
def test_valid_scp_pro_reports_valid(monkeypatch, tmp_path) -> None:
    _isolate_revocation(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    status, message = check_license_at_startup()

    assert status == LicenseStatus.VALID
    assert "pro" in message.lower()


def test_expired_scp_reports_expired_not_missing(monkeypatch, tmp_path) -> None:
    _isolate_revocation(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _expired_pro_key(monkeypatch))

    status, message = check_license_at_startup()

    assert status == LicenseStatus.EXPIRED
    assert "expired" in message.lower()


def test_tampered_scp_reports_missing(monkeypatch, tmp_path) -> None:
    _isolate_revocation(monkeypatch, tmp_path)
    # Flip the final base32 char (signature region) so the key length is
    # unchanged but the Ed25519 signature no longer verifies.
    original = _VECTOR["key"]
    tampered = original[:-1] + ("A" if original[-1] != "A" else "B")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", tampered)

    status, message = check_license_at_startup()

    assert status == LicenseStatus.MISSING
    assert "invalid" in message.lower()


def test_garbage_key_reports_missing(monkeypatch, tmp_path) -> None:
    _isolate_revocation(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "not-a-real-license-key")

    status, _ = check_license_at_startup()

    assert status == LicenseStatus.MISSING


@requires_dev_anchor
def test_revoked_scp_reports_expired(monkeypatch, tmp_path) -> None:
    _isolate_revocation(monkeypatch, tmp_path, revoked=True)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    status, message = check_license_at_startup()

    assert status == LicenseStatus.EXPIRED
    assert "revoked" in message.lower()


@pytest.mark.parametrize(
    ("make_key", "revoked", "expect_valid"),
    [
        pytest.param(
            lambda mp: _VECTOR["key"], False, True, marks=requires_dev_anchor
        ),  # valid pro (dev-signed vector; unverifiable without the dev anchor)
        (lambda mp: _VECTOR["key"], True, False),  # revoked
        (_expired_pro_key, False, False),  # expired
        (lambda mp: "not-a-real-license-key", False, False),  # garbage
    ],
)
def test_status_agrees_with_feature_policy(
    monkeypatch, tmp_path, make_key, revoked, expect_valid
) -> None:
    """One authority: ``check_license_at_startup`` reports VALID for exactly the
    keys ``FeaturePolicy`` resolves to a paid tier — never one without the other."""
    _isolate_revocation(monkeypatch, tmp_path, revoked=revoked)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", make_key(monkeypatch))

    status, _ = check_license_at_startup()
    tier = FeaturePolicy().current_tier()

    is_valid = status == LicenseStatus.VALID
    assert is_valid == (tier in _PAID_TIERS)
    assert is_valid is expect_valid


@requires_dev_anchor
def test_dev_build_unavailable_reports_valid_agreeing_with_feature_policy(
    monkeypatch, tmp_path
) -> None:
    """Dev build + revocation UNAVAILABLE must agree with FeaturePolicy: both VALID/pro.

    Regression guard for the split-brain this dispatch fixes: FeaturePolicy now
    fails open for a dev-signed Pro key under UNAVAILABLE, so startup_check must
    report VALID too, not a stale EXPIRED banner over unlocked Pro features.
    """
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda license_id, base_url: None)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    status, message = check_license_at_startup()
    tier = FeaturePolicy().current_tier()

    assert status == LicenseStatus.VALID
    assert "pro" in message.lower()
    assert tier == "pro"


def test_prod_build_unavailable_reports_expired_agreeing_with_feature_policy(
    monkeypatch, tmp_path
) -> None:
    """Prod build + revocation UNAVAILABLE stays fail-closed on both authorities.

    Signs a Pro key with a throwaway, in-memory Ed25519 keypair (via
    :func:`make_scp_key`) to stand in for a real prod key, since the production
    signing seed never lives in this repo — the client holds no signer. The dev
    key module is then patched away so only this fake "prod" pubkey is trusted,
    faithfully simulating a packaged release build reaching the
    revocation-UNAVAILABLE branch.
    """
    prod_signed_pro_key, prod_pubkey_hex = make_scp_key("pro", 365)

    _trust_only(monkeypatch, prod_pubkey_hex)
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda license_id, base_url: None)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", prod_signed_pro_key)

    status, message = check_license_at_startup()
    tier = FeaturePolicy().current_tier()

    assert status == LicenseStatus.EXPIRED
    assert "cannot be verified" in message.lower()
    assert tier == "free"


@requires_dev_anchor
def test_status_route_agrees_with_features_route(monkeypatch, tmp_path) -> None:
    """The live ``/api/v1/license/status`` and ``/api/v1/license/features`` routes
    resolve the same key consistently (no expired banner over unlocked Pro)."""
    _isolate_revocation(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    client = TestClient(hub.app)
    status_body = client.get("/api/v1/license/status").json()
    features_body = client.get("/api/v1/license/features").json()

    assert status_body["status"] == "valid"
    assert features_body["tier"] == "pro"
    is_valid = status_body["status"] == LicenseStatus.VALID.value
    assert is_valid == (features_body["tier"] in _PAID_TIERS)
