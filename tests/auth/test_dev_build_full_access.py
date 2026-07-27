"""Dev-build admin full-access override (Dispatch A).

Covers ``FeaturePolicy.current_tier()`` defaulting to unconditional
``"enterprise"`` on the owner's own dev checkout, the ``SALES_COPILOT_DEV_TIER``
test escape hatch, and the consistency of ``TelephonyTapGuard`` with the same
tier resolution -- all scoped to dev builds only, with production builds
(no ``_dev_keys``) entirely unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

from _dev_anchor import requires_dev_anchor

from sales_copilot.audio.telephony_guard import get_telephony_tap_guard
from sales_copilot.auth import build_pubkey as build_pubkey_module
from sales_copilot.auth import revocation_cache
from sales_copilot.auth.build_pubkey import dev_pubkey_fingerprint
from sales_copilot.auth.feature_policy import FEATURE_CALLTAP, FeaturePolicy

_VECTOR = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "license_pro_test_vector.json").read_text(
        encoding="utf-8"
    )
)


def _isolate(monkeypatch, tmp_path, *, revoked: bool = False) -> None:
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(
        revocation_cache,
        "_fetch_revocation",
        lambda license_id, base_url: {"revoked": revoked, "expires_at": 0},
    )


# ---------------------------------------------------------------------------
# FeaturePolicy.current_tier() dev-build admin override
# ---------------------------------------------------------------------------


@requires_dev_anchor
def test_dev_build_defaults_to_enterprise_full_access(monkeypatch, tmp_path) -> None:
    """Dev build, no license, no override -> unconditional enterprise (admin full access)."""
    assert dev_pubkey_fingerprint() is not None  # sanity: this checkout is a dev build
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("SALES_COPILOT_DEV_TIER", raising=False)
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    policy = FeaturePolicy()

    assert policy.current_tier() == "enterprise"
    assert policy.is_pro() is True
    assert policy.allows(FEATURE_CALLTAP) is True


@requires_dev_anchor
def test_dev_build_defaults_to_enterprise_even_with_license_set(monkeypatch, tmp_path) -> None:
    """The enterprise default ignores any configured license entirely."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("SALES_COPILOT_DEV_TIER", raising=False)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "not-a-real-license-key")

    policy = FeaturePolicy()

    assert policy.current_tier() == "enterprise"


def test_dev_build_dev_tier_env_forces_free(monkeypatch, tmp_path) -> None:
    """SALES_COPILOT_DEV_TIER=free forces the free tier for local testing."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "free")
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    policy = FeaturePolicy()

    assert policy.current_tier() == "free"
    assert policy.is_pro() is False
    assert policy.allows(FEATURE_CALLTAP) is False


def test_dev_build_dev_tier_env_license_falls_back_to_free_without_key(
    monkeypatch, tmp_path
) -> None:
    """SALES_COPILOT_DEV_TIER=license + no/invalid key -> real flow -> free."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "license")
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    policy = FeaturePolicy()

    assert policy.current_tier() == "free"


def test_dev_build_dev_tier_env_license_falls_back_to_free_with_invalid_key(
    monkeypatch, tmp_path
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "license")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "not-a-real-license-key")

    policy = FeaturePolicy()

    assert policy.current_tier() == "free"


@requires_dev_anchor
def test_dev_build_dev_tier_env_license_confirms_53_fail_open_intact(
    monkeypatch, tmp_path
) -> None:
    """SALES_COPILOT_DEV_TIER=license + valid dev Pro key + revocation UNAVAILABLE
    -> still entitled Pro. Confirms the #53 fail-open path is intact under the
    override, so the real license chain can be tested from a dev checkout."""
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda license_id, base_url: None)
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "license")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    policy = FeaturePolicy()

    assert policy.current_tier() == "pro"
    assert policy.is_pro() is True
    assert policy.allows(FEATURE_CALLTAP) is True


def test_prod_build_dev_tier_env_ignored(monkeypatch, tmp_path) -> None:
    """Prod build (no _dev_keys) -> SALES_COPILOT_DEV_TIER is ignored entirely."""
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "enterprise")
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    assert dev_pubkey_fingerprint() is None  # sanity: simulated prod build

    policy = FeaturePolicy()

    assert policy.current_tier() == "free"
    assert policy.is_pro() is False


# ---------------------------------------------------------------------------
# TelephonyTapGuard.excluded_telephony_processes() consistency with tier
# ---------------------------------------------------------------------------


@requires_dev_anchor
def test_telephony_dev_build_default_yields_empty_exclusion_set(monkeypatch, tmp_path) -> None:
    """Dev build, default (unset) tier -> no telephony process is excluded."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("SALES_COPILOT_DEV_TIER", raising=False)
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    guard = get_telephony_tap_guard()

    assert guard.excluded_telephony_processes() == ()


def test_telephony_dev_build_forced_free_still_excludes(monkeypatch, tmp_path) -> None:
    """Forcing SALES_COPILOT_DEV_TIER=free for local testing still gates telephony,
    the same as a real free-tier user -- no inconsistent/dual gating."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "free")
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    guard = get_telephony_tap_guard()

    assert guard.excluded_telephony_processes() == ("avconferenced",)


def test_telephony_prod_build_excludes_avconferenced(monkeypatch, tmp_path) -> None:
    """Prod build (no _dev_keys) -> avconferenced stays excluded (Pro-gate), unchanged."""
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "enterprise")
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    guard = get_telephony_tap_guard()

    assert guard.excluded_telephony_processes() == ("avconferenced",)
