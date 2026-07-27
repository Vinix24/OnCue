"""Ephemeral SCP license signer for tests only.

The client holds no signing seed: production licenses are issued server-side by
the Cloudflare license-worker, and the client only verifies. Tests that need a
signature-valid ``SCP-`` key therefore mint one in memory here and point the
verifier's trusted pubkey at the matching public key.

:func:`make_scp_key` generates a fresh :class:`Ed25519PrivateKey` on every call,
signs a v1 ``SCP-`` payload with it, and returns ``(scp_key, pubkey_hex)``. The
private key is discarded on return -- no seed is stored or committed anywhere.

The byte layout and base32 grouping mirror the client verifier in
:mod:`sales_copilot.auth.license_verifier` and the canonical offsets in
:mod:`sales_copilot.auth.license_format`.
"""

from __future__ import annotations

import base64
import secrets
import struct
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sales_copilot.auth.license_format import (
    BASE32_GROUP_SIZE,
    SCP_PREFIX,
    SCP_VERSION,
    TIER_CODES,
)

_SECONDS_PER_DAY = 86_400


def make_scp_key(
    tier: str = "pro",
    exp_days: int = 365,
    *,
    now: datetime | None = None,
    license_id: bytes | None = None,
) -> tuple[str, str]:
    """Sign a fresh ``SCP-`` key with a throwaway keypair; return ``(scp_key, pubkey_hex)``.

    A new :class:`Ed25519PrivateKey` is generated in memory on every call and
    discarded on return, so no signing seed is ever stored. Patch the verifier's
    trusted pubkey (``_embedded_keys._PROD_PUBKEY_HEX`` or
    ``build_pubkey.trusted_pubkeys``) to the returned ``pubkey_hex`` so the
    signed key verifies.

    ``now`` sets the issued-at instant (default: current time), letting callers
    mint an already-expired key by passing a past timestamp.
    """
    tier_code = TIER_CODES.get(tier)
    if tier_code is None:
        raise ValueError("tier must be free, pro, or enterprise")
    if exp_days <= 0:
        raise ValueError("exp_days must be positive")

    lid = license_id if license_id is not None else secrets.token_bytes(16)
    if len(lid) != 16:
        raise ValueError("license_id must be exactly 16 bytes")

    issued = int((now or datetime.now(UTC)).timestamp())
    expires = issued + exp_days * _SECONDS_PER_DAY

    private_key = Ed25519PrivateKey.generate()
    payload = bytes([SCP_VERSION, tier_code, 0]) + struct.pack(">II", issued, expires) + lid
    signature = private_key.sign(payload)
    body = base64.b32encode(payload + signature).decode().rstrip("=")
    groups = [body[i : i + BASE32_GROUP_SIZE] for i in range(0, len(body), BASE32_GROUP_SIZE)]
    scp_key = f"{SCP_PREFIX}-" + "-".join(groups)
    pubkey_hex = private_key.public_key().public_bytes_raw().hex()
    return scp_key, pubkey_hex
