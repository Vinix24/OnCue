"""Tests for the license key gate — generate, validate, email capture, startup check."""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from sales_copilot.auth.email_capture import (
    LeadRequest,
    capture_lead,
)
from sales_copilot.auth.license_key import (
    LICENSE_PREFIX,
    LICENSE_SECRET_ENV,
    LicenseKey,
    _decode_key_bytes,
    generate_key,
    validate_key,
    validate_key_with_email,
)
from sales_copilot.auth.startup_check import (
    _LICENSE_ENV,
    LicenseStatus,
    check_license_at_startup,
    reset_grace_marker,
)

_SECRET = "secret-32-chars-min-for-testing!"
_EMAIL = "test@example.com"
_TIER = "free"

# ---------------------------------------------------------------------------
# generate_key — format
# ---------------------------------------------------------------------------


def test_generate_key_format() -> None:
    key = generate_key(_EMAIL, _TIER, _SECRET)
    assert key.startswith(f"{LICENSE_PREFIX}-"), f"Key must start with {LICENSE_PREFIX}-"
    # Format: SC-XXXX-XXXX-... (at least one group)
    assert re.match(r"^SC(-[A-Z2-7]{4})+$", key), f"Unexpected format: {key}"


def test_generate_key_has_multiple_groups() -> None:
    key = generate_key(_EMAIL, _TIER, _SECRET)
    # 17 bytes → 28 base32 chars → 7 groups of 4 chars
    parts = key.split("-")
    assert parts[0] == LICENSE_PREFIX
    groups = parts[1:]
    assert len(groups) == 7, f"Expected 7 groups, got {len(groups)}: {key}"
    for g in groups:
        assert len(g) == 4, f"Each group must be 4 chars: {g!r}"


def test_generate_key_consistent() -> None:
    """Same inputs → same key (deterministic)."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    exp = now + timedelta(days=365)
    k1 = generate_key(_EMAIL, _TIER, _SECRET, expires_at=exp, issued_at=now)
    k2 = generate_key(_EMAIL, _TIER, _SECRET, expires_at=exp, issued_at=now)
    assert k1 == k2


# ---------------------------------------------------------------------------
# validate_key — happy path
# ---------------------------------------------------------------------------


def test_validate_key_returns_license_key() -> None:
    key = generate_key(_EMAIL, _TIER, _SECRET)
    lk = validate_key(key, _SECRET)
    assert lk is not None
    assert isinstance(lk, LicenseKey)
    assert lk.tier == _TIER
    assert lk.expires_at > datetime.now(UTC)
    assert isinstance(lk.issued_at, datetime)
    assert isinstance(lk.signature, str)


def test_validate_key_with_email_returns_email() -> None:
    key = generate_key(_EMAIL, "pro", _SECRET)
    lk = validate_key_with_email(key, _EMAIL, _SECRET)
    assert lk is not None
    assert lk.email == _EMAIL
    assert lk.tier == "pro"


# ---------------------------------------------------------------------------
# validate_key — failure cases
# ---------------------------------------------------------------------------


def test_validate_key_expired_returns_none() -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    issued = past - timedelta(days=2)
    key = generate_key(_EMAIL, _TIER, _SECRET, expires_at=past, issued_at=issued)
    assert validate_key(key, _SECRET) is None


def test_validate_key_hmac_tamper_returns_none() -> None:
    """Flip a signature byte — HMAC mismatch must return None."""
    key = generate_key(_EMAIL, _TIER, _SECRET)
    raw = _decode_key_bytes(key)
    assert raw is not None
    # Flip the last byte of the signature (bytes 9-16 → index 16)
    tampered = bytearray(raw)
    tampered[16] ^= 0xFF
    # Re-encode as key
    b32 = base64.b32encode(bytes(tampered)).decode().rstrip("=")
    groups = [b32[i : i + 4] for i in range(0, len(b32), 4)]
    tampered_key = f"{LICENSE_PREFIX}-" + "-".join(groups)
    assert validate_key(tampered_key, _SECRET) is None


def test_validate_key_wrong_secret_returns_none() -> None:
    key = generate_key(_EMAIL, _TIER, _SECRET)
    assert validate_key(key, "wrong-secret") is None


def test_validate_key_missing_dashes_returns_none() -> None:
    # No dashes at all → no SC- prefix → None
    key = generate_key(_EMAIL, _TIER, _SECRET)
    no_dashes = key.replace("-", "")
    assert validate_key(no_dashes, _SECRET) is None


def test_validate_key_wrong_prefix_returns_none() -> None:
    key = generate_key(_EMAIL, _TIER, _SECRET)
    bad_prefix = "AB" + key[2:]
    assert validate_key(bad_prefix, _SECRET) is None


def test_validate_key_lowercase_input_normalised() -> None:
    """Lowercase key must be accepted (normalised to uppercase internally)."""
    key = generate_key(_EMAIL, _TIER, _SECRET)
    lk = validate_key(key.lower(), _SECRET)
    assert lk is not None


def test_validate_key_empty_returns_none() -> None:
    assert validate_key("", _SECRET) is None
    assert validate_key("   ", _SECRET) is None


# ---------------------------------------------------------------------------
# LicenseKey serialization / deserialization
# ---------------------------------------------------------------------------


def test_licenskey_dataclass_fields() -> None:
    now = datetime.now(UTC)
    lk = LicenseKey(
        email="a@b.com",
        tier="pro",
        issued_at=now,
        expires_at=now + timedelta(days=365),
        signature="ABCDEFGH",
    )
    assert lk.email == "a@b.com"
    assert lk.tier == "pro"
    assert lk.signature == "ABCDEFGH"


def test_licenskey_round_trip_via_dict() -> None:
    key = generate_key(_EMAIL, _TIER, _SECRET)
    lk = validate_key(key, _SECRET)
    assert lk is not None
    d = {
        "email": lk.email,
        "tier": lk.tier,
        "issued_at": lk.issued_at.isoformat(),
        "expires_at": lk.expires_at.isoformat(),
        "signature": lk.signature,
    }
    restored = LicenseKey(
        email=d["email"],
        tier=d["tier"],
        issued_at=datetime.fromisoformat(d["issued_at"]),
        expires_at=datetime.fromisoformat(d["expires_at"]),
        signature=d["signature"],
    )
    assert restored.tier == lk.tier
    assert restored.issued_at == lk.issued_at


# ---------------------------------------------------------------------------
# generate_key — edge cases
# ---------------------------------------------------------------------------


def test_generate_key_empty_email_does_not_crash() -> None:
    key = generate_key("", _TIER, _SECRET)
    assert key.startswith(f"{LICENSE_PREFIX}-")


def test_generate_key_whitespace_email_stripped() -> None:
    k1 = generate_key("  user@test.com  ", _TIER, _SECRET)
    k2 = generate_key("user@test.com", _TIER, _SECRET)
    # Whitespace stripped → same HMAC payload
    assert k1 == k2


def test_generate_key_unicode_email_does_not_crash() -> None:
    key = generate_key("tëst@example.com", _TIER, _SECRET)
    assert key.startswith(f"{LICENSE_PREFIX}-")


# ---------------------------------------------------------------------------
# email_capture — capture_lead
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_leads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    leads = tmp_path / ".vnx-data" / "leads.ndjson"
    import sales_copilot.auth.email_capture as ec_module
    monkeypatch.setattr(ec_module, "LEADS_FILE", leads)
    monkeypatch.setenv("SALES_COPILOT_LICENSE_SECRET", _SECRET)
    return leads


@pytest.mark.asyncio
async def test_capture_lead_writes_to_ndjson(tmp_leads: Path) -> None:
    req = LeadRequest(email="lead@test.com", source="test")
    resp = await capture_lead(req)
    assert resp.status == "ok"
    assert tmp_leads.exists()
    record = json.loads(tmp_leads.read_text(encoding="utf-8").strip())
    assert record["email"] == "lead@test.com"
    assert "license_key" in record
    assert record["license_key"].startswith("SC-")


@pytest.mark.asyncio
async def test_capture_lead_idempotent(tmp_leads: Path) -> None:
    req = LeadRequest(email="dupe@test.com", source="test")
    r1 = await capture_lead(req)
    r2 = await capture_lead(req)
    assert r1.status == "ok"
    assert r2.status == "exists"
    # Only one record in file
    lines = [ln for ln in tmp_leads.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1


def test_capture_lead_malformed_email_raises() -> None:
    with pytest.raises(ValidationError):
        LeadRequest(email="not-an-email")


def test_capture_lead_malformed_email_no_at_raises() -> None:
    with pytest.raises(ValidationError):
        LeadRequest(email="missingatsign.com")


def test_capture_lead_rejects_client_selected_tier() -> None:
    with pytest.raises(ValidationError):
        LeadRequest(email="lead@test.com", tier="pro")


# ---------------------------------------------------------------------------
# startup_check
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_grace_marker() -> None:
    reset_grace_marker()
    yield
    reset_grace_marker()


def test_startup_check_missing_env_returns_grace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sales_copilot.auth.startup_check as sc_module
    monkeypatch.setenv(_LICENSE_ENV, "")
    monkeypatch.setattr(sc_module, "_GRACE_MARKER", tmp_path / "grace_marker")
    status, msg = check_license_at_startup()
    assert status == LicenseStatus.GRACE
    assert "grace" in msg.lower() or "day" in msg.lower()


def test_startup_check_valid_env_returns_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    key = generate_key(_EMAIL, _TIER, _SECRET)
    monkeypatch.setenv(_LICENSE_ENV, key)
    monkeypatch.setenv(LICENSE_SECRET_ENV, _SECRET)
    status, msg = check_license_at_startup()
    assert status == LicenseStatus.VALID
    assert "valid" in msg.lower()


def test_startup_check_expired_grace_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sales_copilot.auth.startup_check as sc_module

    marker = tmp_path / "grace_marker"
    marker.touch()
    # Fake the mtime to 15 days ago
    import time
    old_mtime = time.time() - (15 * 86400)
    os.utime(marker, (old_mtime, old_mtime))

    monkeypatch.setenv(_LICENSE_ENV, "")
    monkeypatch.setattr(sc_module, "_GRACE_MARKER", marker)

    status, msg = check_license_at_startup()
    assert status == LicenseStatus.EXPIRED
    assert "expired" in msg.lower() or "grace" in msg.lower()
