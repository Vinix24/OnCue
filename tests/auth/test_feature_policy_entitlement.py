"""Feature-based entitlement + revocation behaviour for FeaturePolicy."""

from __future__ import annotations

import json
from pathlib import Path

from _dev_anchor import requires_dev_anchor

from sales_copilot.auth import build_pubkey as build_pubkey_module
from sales_copilot.auth import feature_policy as feature_policy_module
from sales_copilot.auth import revocation_cache
from sales_copilot.auth.build_pubkey import dev_pubkey_fingerprint
from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.auth.license_format import (
    FEATURE_CALLTAP,
    FEATURE_CENTRAL_AUDIT,
    FEATURE_LIVE_COACHING,
)
from sales_copilot.auth.revocation_cache import RevocationStatus

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


def test_free_denies_all_pro_features(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    policy = FeaturePolicy()
    assert policy.current_tier() == "free"
    assert policy.allows(FEATURE_CALLTAP) is False
    assert policy.allows(FEATURE_CENTRAL_AUDIT) is False
    assert policy.allows(FEATURE_LIVE_COACHING) is False
    assert policy.is_pro() is False


@requires_dev_anchor
def test_pro_allows_calltap_and_central_audit(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    policy = FeaturePolicy()
    assert policy.current_tier() == "pro"
    assert policy.allows(FEATURE_CALLTAP) is True
    assert policy.allows(FEATURE_CENTRAL_AUDIT) is True
    assert policy.allows(FEATURE_LIVE_COACHING) is True
    assert policy.is_pro() is True


def test_revoked_pro_degrades_to_free(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path, revoked=True)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    policy = FeaturePolicy()
    assert policy.current_tier() == "free"
    assert policy.allows(FEATURE_CALLTAP) is False
    assert policy.is_pro() is False


def test_degraded_pro_stays_free_after_outage_blip(monkeypatch, tmp_path) -> None:
    """Once marked degraded, a Pro key remains free even if revocation later says OK."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    # First call: revocation says revoked → degrades and persists.
    monkeypatch.setattr(
        revocation_cache,
        "_fetch_revocation",
        lambda license_id, base_url: {"revoked": True, "expires_at": 0},
    )
    policy = FeaturePolicy()
    assert policy.current_tier() == "free"

    # Second call: revocation service is reachable again and says entitled.
    # Without the persistent degraded marker this would flap back to Pro.
    monkeypatch.setattr(
        revocation_cache,
        "_fetch_revocation",
        lambda license_id, base_url: {"revoked": False, "expires_at": 0},
    )
    policy = FeaturePolicy()
    assert policy.current_tier() == "free"
    assert policy.is_pro() is False
    assert policy.allows(FEATURE_CALLTAP) is False


def test_unavailable_beyond_grace_degrades_to_free(monkeypatch, tmp_path) -> None:
    """Pro with revocation UNAVAILABLE beyond the grace window degrades permanently."""
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setenv("SCP_REVOCATION_GRACE_S", "60")
    monkeypatch.setattr(revocation_cache, "_now", lambda: 10_000_000.0)
    cache = tmp_path / "rc.json"
    cache.write_text(
        json.dumps(
            {
                "abc": {"revoked": False, "expires_at": 0, "checked_at": 0.0},
                "_meta": {"last_success_at": 9_999_000.0},
            }
        )
    )
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda lid, url: None)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    policy = FeaturePolicy()
    assert policy.current_tier() == "free"
    assert policy.is_pro() is False


# ---------------------------------------------------------------------------
# Dev-build fail-open exception: UNAVAILABLE tolerates the owner's own dev
# checkout, REVOKED never does, and production builds are unaffected.
# ---------------------------------------------------------------------------


@requires_dev_anchor
def test_dev_build_pro_entitled_when_revocation_unavailable(monkeypatch, tmp_path) -> None:
    """Dev build + valid Pro dev-license + revocation UNAVAILABLE -> entitled Pro.

    No prior cache and a failing network fetch put the first ``check_revoked``
    call inside the grace window with no cached value, which is exactly
    ``RevocationStatus.UNAVAILABLE`` (see revocation_cache.check_revoked).
    """
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda license_id, base_url: None)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])

    policy = FeaturePolicy()
    assert policy.current_tier() == "pro"
    assert policy.is_pro() is True
    assert policy.allows(FEATURE_CALLTAP) is True


def test_prod_build_pro_not_entitled_when_revocation_unavailable(monkeypatch, tmp_path) -> None:
    """Prod build (no ``_dev_keys``) + revocation UNAVAILABLE -> not entitled.

    Exercises ``_is_entitled`` directly: a real prod-signed Pro license can't be
    constructed in this repo (the prod signing seed is never checked in), so the
    fail-closed contract is verified at the unit boundary where the dev/prod
    distinction actually lives, with ``_dev_keys`` patched away to simulate a
    packaged release build.

    Isolates the revocation cache: this checkout's real, on-disk
    ``~/.sales_copilot/revocation_cache.json`` carries a stale ``_meta`` from
    actual local Pro-license usage. Left unpatched, the fabricated license_id
    below falls through to that real ``_meta.last_success_at``, and once it is
    more than the grace window in the past the "no cache, no network" branch
    resolves to REVOKED instead of the UNAVAILABLE this test exercises. A fresh
    tmp_path cache has no ``_meta``, so the first check deterministically lands
    inside the grace window.
    """
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda license_id, base_url: None)

    entitled, status = feature_policy_module._is_entitled(
        "pro", "unit-test-license-id-unavailable", "some-pubkey-fingerprint"
    )

    assert status is RevocationStatus.UNAVAILABLE
    assert entitled is False


@requires_dev_anchor
def test_revoked_denied_on_dev_build(monkeypatch, tmp_path) -> None:
    """REVOKED stays denied on a dev build, even when the key is dev-signed."""
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(
        revocation_cache,
        "_fetch_revocation",
        lambda license_id, base_url: {"revoked": True, "expires_at": 0},
    )
    dev_fp = dev_pubkey_fingerprint()
    assert dev_fp is not None  # sanity: this test runs from a dev checkout

    entitled, status = feature_policy_module._is_entitled(
        "pro", "unit-test-license-id-revoked-dev", dev_fp
    )

    assert status is RevocationStatus.REVOKED
    assert entitled is False


def test_revoked_denied_on_prod_build(monkeypatch, tmp_path) -> None:
    """REVOKED stays denied on a prod build, unaffected by the dev fail-open exception."""
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(
        revocation_cache,
        "_fetch_revocation",
        lambda license_id, base_url: {"revoked": True, "expires_at": 0},
    )

    entitled, status = feature_policy_module._is_entitled(
        "pro", "unit-test-license-id-revoked-prod", "irrelevant-fingerprint"
    )

    assert status is RevocationStatus.REVOKED
    assert entitled is False
