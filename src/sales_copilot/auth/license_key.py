"""HMAC-SHA256 signed license keys for OnCue.

Key format: SC-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX (7 groups of 4 base32 chars)
Binary layout (17 bytes → 28 base32 chars, no padding):
  byte 0:     tier_code  (0=free, 1=pro, 2=enterprise)
  bytes 1-4:  issued_at  (big-endian uint32 unix timestamp)
  bytes 5-8:  expires_at (big-endian uint32 unix timestamp)
  bytes 9-16: HMAC-SHA256(f"{tier_code}|{iat}|{exp}", secret)[:8]

Email is NOT stored in or bound to the key.
generate_key() accepts email so callers can log/store it; it does not affect the key.
validate_key() recovers tier and timestamps; LicenseKey.email is always "".
For email-bound verification use validate_key_with_email().
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

LICENSE_PREFIX = "SC"
LICENSE_SECRET_ENV = "SALES_COPILOT_LICENSE_SECRET"
GRACE_DAYS = 14

_TIER_CODES: dict[str, int] = {"free": 0, "pro": 1, "enterprise": 2}
_TIER_NAMES: dict[int, str] = {v: k for k, v in _TIER_CODES.items()}

_STRUCT_FMT = ">BII"  # 1 + 4 + 4 = 9 bytes
_SIG_BYTES = 8
_TOTAL_BYTES = struct.calcsize(_STRUCT_FMT) + _SIG_BYTES  # 17 bytes
_GROUP_SIZE = 4


@dataclass
class LicenseKey:
    email: str
    tier: str
    issued_at: datetime
    expires_at: datetime
    signature: str
    license_id: str = ""
    pubkey_fingerprint: str | None = None


def _compute_signature(payload: str, secret: str) -> bytes:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()


def _encode_key(raw: bytes) -> str:
    b32 = base64.b32encode(raw).decode().rstrip("=")
    groups = [b32[i : i + _GROUP_SIZE] for i in range(0, len(b32), _GROUP_SIZE)]
    return f"{LICENSE_PREFIX}-" + "-".join(groups)


def _decode_key_bytes(key: str) -> bytes | None:
    key = (key or "").strip().upper().replace(" ", "")
    prefix = f"{LICENSE_PREFIX}-"
    if not key.startswith(prefix):
        return None
    data_str = key[len(prefix) :].replace("-", "")
    if not data_str:
        return None
    pad = (8 - len(data_str) % 8) % 8
    try:
        return base64.b32decode(data_str + "=" * pad)
    except Exception:
        return None


def _hmac_payload(tier_code: int, iat: int, exp: int) -> str:
    return f"{tier_code}|{iat}|{exp}"


def generate_key(
    email: str,
    tier: str,
    secret: str,
    expires_at: datetime | None = None,
    issued_at: datetime | None = None,
) -> str:
    """Generate a signed license key.

    email is accepted for logging/storage in leads.ndjson but does not affect
    the key bytes. The key encodes tier + timestamps + HMAC.
    """
    tier = (tier or "free").strip().lower()
    now = (issued_at or datetime.now(UTC)).replace(microsecond=0)
    if expires_at is None:
        expires_at = now + timedelta(days=365)
    expires_at = expires_at.replace(microsecond=0)

    tier_code = _TIER_CODES.get(tier, 0)
    iat = int(now.timestamp())
    exp = int(expires_at.timestamp())

    header = struct.pack(_STRUCT_FMT, tier_code, iat, exp)
    sig = _compute_signature(_hmac_payload(tier_code, iat, exp), secret)[:_SIG_BYTES]
    return _encode_key(header + sig)


def validate_key(key: str, secret: str) -> LicenseKey | None:
    """Validate a license key. Returns LicenseKey on success, None on any failure.

    Detects: malformed format, wrong prefix, HMAC tampering, expiry.
    LicenseKey.email is always "" — use validate_key_with_email() if you need
    email-bound verification.
    """
    raw = _decode_key_bytes(key)
    if raw is None or len(raw) < _TOTAL_BYTES:
        return None

    try:
        tier_code, iat, exp = struct.unpack(_STRUCT_FMT, raw[:9])
    except struct.error:
        return None

    if tier_code not in _TIER_NAMES:
        return None

    sig_stored = raw[9 : 9 + _SIG_BYTES]
    if len(sig_stored) != _SIG_BYTES:
        return None

    sig_expected = _compute_signature(_hmac_payload(tier_code, iat, exp), secret)[:_SIG_BYTES]
    if not hmac.compare_digest(sig_stored, sig_expected):
        return None

    now_ts = int(datetime.now(UTC).timestamp())
    if exp < now_ts:
        return None

    return LicenseKey(
        email="",
        tier=_TIER_NAMES[tier_code],
        issued_at=datetime.fromtimestamp(iat, tz=UTC),
        expires_at=datetime.fromtimestamp(exp, tz=UTC),
        signature=base64.b32encode(sig_stored).decode().rstrip("="),
    )


def validate_key_with_email(key: str, email: str, secret: str) -> LicenseKey | None:
    """Same as validate_key but populates LicenseKey.email from the argument."""
    lk = validate_key(key, secret)
    if lk is None:
        return None
    return LicenseKey(
        email=(email or "").strip().lower(),
        tier=lk.tier,
        issued_at=lk.issued_at,
        expires_at=lk.expires_at,
        signature=lk.signature,
    )
