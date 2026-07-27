"""Offline-first revocation cache: TTL, phone-home, offline grace, enum status."""

from __future__ import annotations

import json

from sales_copilot.auth import revocation_cache
from sales_copilot.auth.revocation_cache import RevocationStatus


def test_empty_license_id_is_never_revoked() -> None:
    assert revocation_cache.check_revoked("") is RevocationStatus.ENTITLED


def test_fresh_cache_used_without_phoning_home(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "rc.json"
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", cache)
    monkeypatch.setattr(revocation_cache, "_now", lambda: 1000.0)
    cache.write_text(
        json.dumps({"abc": {"revoked": True, "expires_at": 0, "checked_at": 1000.0}})
    )

    def _boom(license_id: str, base_url: str) -> dict | None:
        raise AssertionError("must not phone home while cache is fresh")

    monkeypatch.setattr(revocation_cache, "_fetch_revocation", _boom)
    assert revocation_cache.check_revoked("abc") is RevocationStatus.REVOKED


def test_stale_cache_triggers_phone_home(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "rc.json"
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", cache)
    monkeypatch.setattr(revocation_cache, "_now", lambda: 10_000_000.0)
    cache.write_text(
        json.dumps({"abc": {"revoked": False, "expires_at": 0, "checked_at": 0.0}})
    )
    monkeypatch.setattr(
        revocation_cache, "_fetch_revocation", lambda lid, url: {"revoked": True}
    )
    assert revocation_cache.check_revoked("abc") is RevocationStatus.REVOKED


def test_offline_falls_back_to_prior_cache(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "rc.json"
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", cache)
    monkeypatch.setattr(revocation_cache, "_now", lambda: 10_000_000.0)
    cache.write_text(
        json.dumps({"abc": {"revoked": True, "expires_at": 0, "checked_at": 0.0}})
    )
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda lid, url: None)
    assert revocation_cache.check_revoked("abc") is RevocationStatus.REVOKED


def test_offline_without_cache_fails_open_for_free(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda lid, url: None)
    assert revocation_cache.check_revoked("unknown-id", tier="free") is RevocationStatus.ENTITLED


def test_offline_without_cache_fails_closed_for_pro_inside_grace(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda lid, url: None)
    monkeypatch.setattr(revocation_cache, "_now", lambda: 1000.0)
    monkeypatch.setenv("SCP_REVOCATION_GRACE_S", "3600")
    assert revocation_cache.check_revoked("unknown-id", tier="pro") is RevocationStatus.UNAVAILABLE


def test_offline_without_cache_fails_closed_for_pro_after_grace(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda lid, url: None)
    monkeypatch.setattr(revocation_cache, "_now", lambda: 100_000.0)
    monkeypatch.setenv("SCP_REVOCATION_GRACE_S", "60")
    # Seed a last_success_at far enough in the past to exceed the grace window.
    cache = tmp_path / "rc.json"
    cache.write_text(json.dumps({"_meta": {"last_success_at": 99_000.0}}))
    assert revocation_cache.check_revoked("unknown-id", tier="pro") is RevocationStatus.REVOKED


def test_successful_fetch_resets_last_success_at(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "rc.json"
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", cache)
    monkeypatch.setattr(revocation_cache, "_now", lambda: 1000.0)
    monkeypatch.setattr(
        revocation_cache,
        "_fetch_revocation",
        lambda lid, url: {"revoked": False},
    )
    revocation_cache.check_revoked("abc", tier="pro")
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["_meta"]["last_success_at"] == 1000.0


def test_stale_revoked_false_cache_exceeding_grace_is_unavailable_for_pro(
    monkeypatch, tmp_path
) -> None:
    """A stale 'revoked=false' must not entitle Pro once the grace window closes."""
    cache = tmp_path / "rc.json"
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", cache)
    monkeypatch.setattr(revocation_cache, "_now", lambda: 10_000_000.0)
    monkeypatch.setenv("SCP_REVOCATION_GRACE_S", "60")
    cache.write_text(
        json.dumps(
            {
                "abc": {"revoked": False, "expires_at": 0, "checked_at": 0.0},
                "_meta": {"last_success_at": 9_999_000.0},
            }
        )
    )
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", lambda lid, url: None)
    assert revocation_cache.check_revoked("abc", tier="pro") is RevocationStatus.UNAVAILABLE


def test_authority_url_override_only_in_dev(monkeypatch, tmp_path) -> None:
    """SALES_COPILOT_LICENSE_CHECK_URL is honored only when _dev_keys is present."""
    captured: list[str] = []

    def _capture(license_id: str, base_url: str) -> dict | None:
        captured.append(base_url)
        return {"revoked": False}

    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(revocation_cache, "_fetch_revocation", _capture)
    monkeypatch.setenv("SALES_COPILOT_LICENSE_CHECK_URL", "https://evil.local")

    # Release build: override ignored.
    monkeypatch.setattr(revocation_cache, "_is_dev_build", lambda: False)
    revocation_cache.check_revoked("release-id", tier="pro")
    assert captured[-1] == revocation_cache._DEFAULT_URL

    # Dev build: override honored.
    monkeypatch.setattr(revocation_cache, "_is_dev_build", lambda: True)
    revocation_cache.check_revoked("dev-id", tier="pro")
    assert captured[-1] == "https://evil.local"
