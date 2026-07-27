"""Tests for the client-side Ed25519 license verifier."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from _dev_anchor import requires_dev_anchor
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sales_copilot.auth import _embedded_keys, license_verifier
from sales_copilot.auth import build_pubkey as build_pubkey_module
from sales_copilot.auth.license_format import (
    LEGACY_HMAC_DEADLINE,
    SCP_PREFIX,
    SCP_SIGNATURE_OFFSET,
)
from sales_copilot.auth.license_key import LICENSE_SECRET_ENV, generate_key
from sales_copilot.auth.license_verifier import decode_and_verify

_VECTOR_PATH = Path(__file__).parents[1] / "fixtures" / "license_pro_test_vector.json"
_GROUP = 4


def _load_vector() -> dict:
    return json.loads(_VECTOR_PATH.read_text(encoding="utf-8"))


def _decode_scp(key: str) -> bytes:
    body = key.removeprefix(f"{SCP_PREFIX}-").replace("-", "")
    padding = "=" * ((8 - len(body) % 8) % 8)
    return base64.b32decode(body + padding)


def _encode_scp(raw: bytes) -> str:
    b32 = base64.b32encode(raw).decode().rstrip("=")
    groups = [b32[i : i + _GROUP] for i in range(0, len(b32), _GROUP)]
    return f"{SCP_PREFIX}-" + "-".join(groups)


def _build_scp(priv: Ed25519PrivateKey, *, tier_code: int, issued_at: int, expires_at: int) -> str:
    payload = (
        bytes([1, tier_code, 0])
        + issued_at.to_bytes(4, "big")
        + expires_at.to_bytes(4, "big")
        + bytes(range(16))
    )
    return _encode_scp(payload + priv.sign(payload))


@requires_dev_anchor
def test_valid_scp_vector_decodes_to_pro() -> None:
    vector = _load_vector()
    decoded = vector["decoded"]

    lk = decode_and_verify(vector["key"])

    assert lk is not None
    assert lk.tier == "pro"
    assert lk.issued_at == datetime.fromtimestamp(decoded["issued_at"], tz=UTC)
    assert lk.expires_at == datetime.fromtimestamp(decoded["expires_at"], tz=UTC)
    # SCP keys surface the pseudonymous license_id on its dedicated field.
    assert lk.license_id == decoded["license_id_hex"]


def test_flipped_signature_byte_fails() -> None:
    vector = _load_vector()
    raw = bytearray(_decode_scp(vector["key"]))
    raw[SCP_SIGNATURE_OFFSET] ^= 0xFF

    assert decode_and_verify(_encode_scp(bytes(raw))) is None


def test_expired_scp_fails(monkeypatch) -> None:
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    now = datetime.now(UTC)
    key = _build_scp(
        priv,
        tier_code=1,
        issued_at=int((now - timedelta(days=400)).timestamp()),
        expires_at=int((now - timedelta(days=35)).timestamp()),
    )

    # Signature verifies against the patched epoch-0 key, so failure is expiry.
    monkeypatch.setattr(license_verifier, "trusted_pubkeys", lambda: {0: pub})
    assert decode_and_verify(key) is None


def test_legacy_sc_after_deadline_fails(monkeypatch) -> None:
    secret = "test-secret-123"
    monkeypatch.setenv(LICENSE_SECRET_ENV, secret)
    sc_key = generate_key("user@example.com", "pro", secret)

    with mock.patch.object(license_verifier, "date") as mdate:
        mdate.today.return_value = LEGACY_HMAC_DEADLINE + timedelta(days=1)
        assert decode_and_verify(sc_key) is None


def test_legacy_sc_before_deadline_succeeds(monkeypatch) -> None:
    secret = "test-secret-123"
    monkeypatch.setenv(LICENSE_SECRET_ENV, secret)
    sc_key = generate_key("user@example.com", "pro", secret)

    with mock.patch.object(license_verifier, "date") as mdate:
        mdate.today.return_value = LEGACY_HMAC_DEADLINE - timedelta(days=1)
        lk = decode_and_verify(sc_key)

    assert lk is not None
    assert lk.tier == "pro"


def test_embedded_production_pubkey_accepts_signed_key(monkeypatch) -> None:
    """A key signed with the embedded production test-key must verify.

    This is the positive test required by the pre-launch auth hardening spec:
    the verifier must not silently reject every SCP key.
    """
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    # Disable the dev key so epoch 0 is occupied by the production key.
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", pub.hex())

    now = datetime.now(UTC)
    key = _build_scp(
        priv,
        tier_code=1,
        issued_at=int(now.timestamp()),
        expires_at=int((now + timedelta(days=30)).timestamp()),
    )

    lk = decode_and_verify(key)
    assert lk is not None
    assert lk.tier == "pro"
    assert lk.pubkey_fingerprint is not None


def test_no_embedded_keys_rejects_scp_and_falls_to_free(monkeypatch) -> None:
    """Without any embedded trusted key the verifier rejects SCP keys -> free tier."""
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", None)

    vector = _load_vector()
    assert decode_and_verify(vector["key"]) is None
