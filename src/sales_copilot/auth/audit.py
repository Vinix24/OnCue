"""Structured audit logging for license verification decisions.

Every verification decision is logged with a SHA-256 fingerprint of the license
key and the public key used for signature verification. The raw key, private
seed, or any secret material is never written to the audit log.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

_logger = logging.getLogger("sales_copilot.auth.audit")


def key_fingerprint(key: str) -> str:
    """Return a stable, non-reversible SHA-256 fingerprint of ``key``.

    The full 64-character hex is intentionally used so operators can correlate
    entries across logs without the key itself ever being stored.
    """
    normalized = (key or "").strip().upper().replace(" ", "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def pubkey_fingerprint(pubkey_bytes: bytes | None) -> str | None:
    """Return a SHA-256 fingerprint of the embedded public key bytes, if any."""
    if pubkey_bytes is None:
        return None
    return hashlib.sha256(pubkey_bytes).hexdigest()


def log_verify_decision(
    key_fingerprint: str,
    *,
    decision: str,
    tier: str | None = None,
    pubkey_fingerprint: str | None = None,
    revocation_status: str | None = None,
    expires_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Log a license verification decision in a structured, auditable form.

    Parameters match the canonical fields required by the pre-launch auth
    hardening spec. Never pass the raw key or private seed here.
    """
    payload: dict[str, Any] = {
        "event": "license_verify_decision",
        "key_fingerprint": key_fingerprint,
        "decision": decision,
    }
    if tier is not None:
        payload["tier"] = tier
    if pubkey_fingerprint is not None:
        payload["pubkey_fingerprint"] = pubkey_fingerprint
    if revocation_status is not None:
        payload["revocation_status"] = revocation_status
    if expires_at is not None:
        payload["expires_at"] = expires_at
    if extra:
        payload.update(extra)

    _logger.info(
        "%(event)s: %(payload)s",
        {
            "event": "license_verify_decision",
            "payload": json.dumps(payload, sort_keys=True),
        },
    )
