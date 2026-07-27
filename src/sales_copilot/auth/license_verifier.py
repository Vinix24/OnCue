"""Client-side Ed25519 verification for SCP license keys.

Legal notice: this module enforces the OnCue commercial license. Removing or
circumventing it to obtain Pro features without a valid, paid license
violates the OnCue commercial license and applicable copyright law. See
LICENSE-COMMERCIAL.md.

Verifies v1 ``SCP-`` keys fully offline against Ed25519 public keys that were
embedded at build time (see :mod:`sales_copilot.auth.build_pubkey`). Legacy
``SC-`` HMAC keys are delegated to :mod:`sales_copilot.auth.license_key`, but
only accepted while ``date.today() <= LEGACY_HMAC_DEADLINE``.

The signed payload is ``bytes[0:27]`` and the signature is ``bytes[27:91]`` per
``docs/contracts/LICENSE_PRO_CONTRACT.md``.

Return shape: :func:`decode_and_verify` returns the existing
:class:`~sales_copilot.auth.license_key.LicenseKey` so both branches share one
type. For ``SCP-`` keys the pseudonymous ``license_id`` (hex) is populated on
the dedicated ``LicenseKey.license_id`` field — the identifier the contract
sends to ``/license/check``. Legacy ``SC-`` keys leave it empty.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, date, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from sales_copilot.auth.audit import (
    key_fingerprint as _key_fingerprint,
)
from sales_copilot.auth.audit import (
    log_verify_decision,
)
from sales_copilot.auth.audit import (
    pubkey_fingerprint as _pubkey_fingerprint,
)
from sales_copilot.auth.build_pubkey import trusted_pubkeys
from sales_copilot.auth.license_format import (
    LEGACY_HMAC_DEADLINE,
    SC_PREFIX,
    SCP_EXPIRES_AT_END,
    SCP_EXPIRES_AT_OFFSET,
    SCP_ISSUED_AT_END,
    SCP_ISSUED_AT_OFFSET,
    SCP_LICENSE_ID_END,
    SCP_LICENSE_ID_OFFSET,
    SCP_PAYLOAD_SLICE,
    SCP_PREFIX,
    SCP_ROTATION_EPOCH_OFFSET,
    SCP_SIGNATURE_SLICE,
    SCP_TIER_OFFSET,
    SCP_TOTAL_BYTES,
    SCP_VERSION,
    SCP_VERSION_OFFSET,
    TIER_CODES,
)
from sales_copilot.auth.license_key import (
    LICENSE_SECRET_ENV,
    LicenseKey,
    validate_key,
)

_TIER_NAMES: dict[int, str] = {code: name for name, code in TIER_CODES.items()}


def _decode_scp_bytes(key: str) -> bytes | None:
    """Decode an ``SCP-`` body as unpadded RFC 4648 base32; require 91 bytes."""
    body = key.removeprefix(f"{SCP_PREFIX}-").replace("-", "")
    if not body:
        return None
    padding = "=" * ((8 - len(body) % 8) % 8)
    try:
        raw = base64.b32decode(body + padding)
    except (ValueError, TypeError):
        return None
    if len(raw) != SCP_TOTAL_BYTES:
        return None
    return raw


def _verify_scp(key: str, check_expiry: bool = True) -> LicenseKey | None:
    kf = _key_fingerprint(key)
    raw = _decode_scp_bytes(key)
    if raw is None:
        log_verify_decision(
            kf, decision="invalid_format", extra={"reason": "base32_decode_failed"}
        )
        return None
    if raw[SCP_VERSION_OFFSET] != SCP_VERSION:
        log_verify_decision(
            kf,
            decision="invalid_format",
            extra={
                "reason": "version_mismatch",
                "version": raw[SCP_VERSION_OFFSET],
                "expected": SCP_VERSION,
            },
        )
        return None

    tier = _TIER_NAMES.get(raw[SCP_TIER_OFFSET])
    if tier is None:
        log_verify_decision(
            kf,
            decision="invalid_format",
            extra={"reason": "unknown_tier_code", "tier_code": raw[SCP_TIER_OFFSET]},
        )
        return None

    pubkeys = trusted_pubkeys()
    pubkey_bytes = pubkeys.get(raw[SCP_ROTATION_EPOCH_OFFSET])
    pf = _pubkey_fingerprint(pubkey_bytes)
    if pubkey_bytes is None:
        log_verify_decision(
            kf,
            decision="no_trusted_pubkey",
            tier=tier,
            pubkey_fingerprint=pf,
            extra={"rotation_epoch": raw[SCP_ROTATION_EPOCH_OFFSET]},
        )
        return None

    try:
        Ed25519PublicKey.from_public_bytes(pubkey_bytes).verify(
            raw[SCP_SIGNATURE_SLICE], raw[SCP_PAYLOAD_SLICE]
        )
    except InvalidSignature:
        log_verify_decision(
            kf,
            decision="bad_signature",
            tier=tier,
            pubkey_fingerprint=pf,
        )
        return None

    issued_at = int.from_bytes(raw[SCP_ISSUED_AT_OFFSET:SCP_ISSUED_AT_END], "big")
    expires_at = int.from_bytes(raw[SCP_EXPIRES_AT_OFFSET:SCP_EXPIRES_AT_END], "big")
    # When check_expiry is False the signature is still required, but an expired
    # key is returned (with its real expires_at) so the caller can distinguish
    # "expired" from "bad signature/format". The default True preserves the
    # feature_policy contract: an expired key resolves to None -> free tier.
    if check_expiry and expires_at <= int(datetime.now(UTC).timestamp()):
        log_verify_decision(
            kf,
            decision="expired",
            tier=tier,
            pubkey_fingerprint=pf,
            expires_at=datetime.fromtimestamp(expires_at, tz=UTC).isoformat(),
        )
        return None

    license_id_hex = raw[SCP_LICENSE_ID_OFFSET:SCP_LICENSE_ID_END].hex()
    log_verify_decision(
        kf,
        decision="valid",
        tier=tier,
        pubkey_fingerprint=pf,
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC).isoformat(),
    )
    return LicenseKey(
        email="",
        tier=tier,
        issued_at=datetime.fromtimestamp(issued_at, tz=UTC),
        expires_at=datetime.fromtimestamp(expires_at, tz=UTC),
        signature="",
        license_id=license_id_hex,
        pubkey_fingerprint=pf,
    )


def _verify_legacy_hmac(key: str) -> LicenseKey | None:
    kf = _key_fingerprint(key)
    if date.today() > LEGACY_HMAC_DEADLINE:
        log_verify_decision(
            kf,
            decision="legacy_deadline_expired",
            extra={"deadline": LEGACY_HMAC_DEADLINE.isoformat()},
        )
        return None
    secret = os.environ.get(LICENSE_SECRET_ENV, "").strip()
    if not secret:
        log_verify_decision(
            kf,
            decision="legacy_missing_secret",
        )
        return None
    result = validate_key(key, secret)
    if result is None:
        log_verify_decision(
            kf,
            decision="legacy_invalid",
        )
    else:
        log_verify_decision(
            kf,
            decision="legacy_valid",
            tier=result.tier,
            expires_at=result.expires_at.isoformat(),
        )
    return result


def decode_and_verify(key: str, check_expiry: bool = True) -> LicenseKey | None:
    """Verify a license key offline and return its decoded :class:`LicenseKey`.

    ``SCP-`` keys are checked against the embedded Ed25519 public key selected by
    their ``rotation_epoch``. ``SC-`` keys are delegated to the legacy HMAC
    validator, but only until ``LEGACY_HMAC_DEADLINE``. Any malformed input,
    failed signature, or unknown prefix returns ``None``.

    ``check_expiry`` defaults to True, so an expired key also returns ``None`` —
    the contract the ``feature_policy`` caller relies on (expired -> free tier).
    Pass ``check_expiry=False`` to verify the signature but still return an
    expired ``SCP-`` key, letting the caller report EXPIRED distinctly from a
    bad-signature MISSING (used by ``startup_check``).
    """
    text = (key or "").strip()
    if not text:
        return None
    upper = text.upper()
    if upper.startswith(f"{SCP_PREFIX}-"):
        return _verify_scp(upper, check_expiry=check_expiry)
    if upper.startswith(f"{SC_PREFIX}-"):
        # Legacy SC- HMAC keys always reject when expired, regardless of
        # check_expiry. This branch is sunsetting on LEGACY_HMAC_DEADLINE
        # (2026-09-01), so the expired-vs-invalid distinction is not worth
        # threading through it — an expired SC- key surfaces as MISSING.
        return _verify_legacy_hmac(text)
    log_verify_decision(
        _key_fingerprint(text),
        decision="invalid_format",
        extra={"reason": "unknown_prefix"},
    )
    return None
