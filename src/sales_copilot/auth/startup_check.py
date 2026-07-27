"""License validation at startup — grace period, env-var check, status reporting.

Legal notice: this module is part of the code that enforces the OnCue
commercial license. Removing or circumventing it to obtain Pro features
without a valid, paid license violates the OnCue commercial license and
applicable copyright law. See LICENSE-COMMERCIAL.md.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from enum import Enum

from sales_copilot.auth.audit import key_fingerprint, log_verify_decision
from sales_copilot.auth.build_pubkey import dev_pubkey_fingerprint
from sales_copilot.auth.license_key import GRACE_DAYS
from sales_copilot.auth.license_verifier import decode_and_verify
from sales_copilot.auth.revocation_cache import RevocationStatus, check_revoked
from sales_copilot.core.paths import resolve_app_path

_GRACE_MARKER = resolve_app_path(".vnx-data/license_grace_marker")
_LICENSE_ENV = "SALES_COPILOT_LICENSE"


class LicenseStatus(Enum):
    VALID = "valid"
    EXPIRED = "expired"
    MISSING = "missing"
    GRACE = "grace"


def _touch_grace_marker() -> None:
    """Create the grace marker on first startup without a license."""
    _GRACE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    if not _GRACE_MARKER.exists():
        _GRACE_MARKER.touch()


def get_grace_marker_age_days() -> int | None:
    """Return age of grace marker in days, or None if marker doesn't exist."""
    if not _GRACE_MARKER.exists():
        return None
    mtime = _GRACE_MARKER.stat().st_mtime
    age_seconds = time.time() - mtime
    return int(age_seconds / 86400)


def reset_grace_marker() -> None:
    """Remove the grace marker. Used in tests and for clean reinstalls."""
    if _GRACE_MARKER.exists():
        _GRACE_MARKER.unlink()


def check_license_at_startup() -> tuple[LicenseStatus, str]:
    """Check license status at application startup.

    Returns (status, message) tuple. Never raises — always returns a result.

    This shares one authority with :class:`feature_policy.FeaturePolicy`: both
    resolve the configured key via the Ed25519 verifier (``decode_and_verify``)
    plus the offline revocation check, so ``/api/v1/license/status`` and
    ``/api/v1/license/features`` can never disagree about the same key.

    Flow:
      1. SALES_COPILOT_LICENSE not set -> GRACE period (14 days from first run),
         then EXPIRED once the grace window closes.
      2. SALES_COPILOT_LICENSE set -> verify the Ed25519 signature (no client
         secret is required for SCP- keys):
         - Bad signature / malformed / unknown prefix -> MISSING (treated absent)
         - Valid signature, ``expires_at`` in the past -> EXPIRED
         - Valid signature, revoked                    -> EXPIRED (revoked)
         - Valid signature, revocation UNAVAILABLE, key signed with this
           build's dev pubkey (``_dev_keys`` present) -> VALID (fail-open,
           mirrors ``FeaturePolicy._is_entitled`` so the two authorities never
           disagree on the owner's own dev checkout)
         - Valid signature, revocation UNAVAILABLE, otherwise -> EXPIRED
         - Valid signature, live, not revoked          -> VALID
    """
    license_value = (os.environ.get(_LICENSE_ENV, "") or "").strip()

    if not license_value:
        _touch_grace_marker()
        age = get_grace_marker_age_days()
        if age is None:
            age = 0
        days_remaining = max(0, GRACE_DAYS - age)
        if days_remaining == 0:
            log_verify_decision(
                "",
                decision="grace_expired",
                tier="free",
                extra={"source": "startup_check"},
            )
            return (
                LicenseStatus.EXPIRED,
                (
                    f"Grace period expired ({GRACE_DAYS}-day trial ended). "
                    "Request a free license key at the dashboard or set "
                    f"SALES_COPILOT_LICENSE in your .env."
                ),
            )
        log_verify_decision(
            "",
            decision="grace_active",
            tier="free",
            extra={"source": "startup_check", "days_remaining": days_remaining},
        )
        return (
            LicenseStatus.GRACE,
            (
                f"No license key found. Running in grace period: "
                f"{days_remaining} day(s) remaining. "
                "Request a free key via the dashboard."
            ),
        )

    # Verify the signature but keep an expired key so we can report EXPIRED
    # distinctly from a MISSING (bad signature/format).
    lk = decode_and_verify(license_value, check_expiry=False)
    kf = key_fingerprint(license_value)
    if lk is None:
        log_verify_decision(
            kf,
            decision="invalid_or_untrusted",
            tier="free",
            extra={"source": "startup_check", "status": "missing"},
        )
        return (
            LicenseStatus.MISSING,
            "License key is invalid (bad signature or format). "
            "Request a new key via the dashboard.",
        )

    if lk.expires_at <= datetime.now(UTC):
        log_verify_decision(
            kf,
            decision="expired",
            tier=lk.tier,
            pubkey_fingerprint=lk.pubkey_fingerprint,
            expires_at=lk.expires_at.isoformat(),
            extra={"source": "startup_check", "status": "expired"},
        )
        return (
            LicenseStatus.EXPIRED,
            (
                f"License key expired on {lk.expires_at.date().isoformat()}. "
                "Request a new key via the dashboard."
            ),
        )

    status = check_revoked(lk.license_id, tier=lk.tier)
    if status is RevocationStatus.REVOKED:
        log_verify_decision(
            kf,
            decision="revoked",
            tier=lk.tier,
            pubkey_fingerprint=lk.pubkey_fingerprint,
            revocation_status=status.value,
            extra={"source": "startup_check", "status": "expired"},
        )
        return (
            LicenseStatus.EXPIRED,
            "License key has been revoked. Request a new key via the dashboard.",
        )

    dev_fp = dev_pubkey_fingerprint()
    is_dev_signed = dev_fp is not None and lk.pubkey_fingerprint == dev_fp

    if status is RevocationStatus.UNAVAILABLE and not is_dev_signed:
        log_verify_decision(
            kf,
            decision="revocation_unavailable",
            tier=lk.tier,
            pubkey_fingerprint=lk.pubkey_fingerprint,
            revocation_status=status.value,
            extra={"source": "startup_check", "status": "expired"},
        )
        # Valid signature, live key, but revocation service is unreachable and
        # we have no cached value. Degrade gracefully without claiming VALID.
        return (
            LicenseStatus.EXPIRED,
            (
                "License status cannot be verified (revocation service unavailable). "
                "Please check your connection."
            ),
        )

    # Either genuinely ENTITLED, or UNAVAILABLE on a dev-signed key -- the same
    # fail-open exception FeaturePolicy._is_entitled applies, so this reports
    # VALID instead of a mismatched EXPIRED banner over unlocked Pro features.
    log_verify_decision(
        kf,
        decision="valid_dev_fail_open" if status is RevocationStatus.UNAVAILABLE else "valid",
        tier=lk.tier,
        pubkey_fingerprint=lk.pubkey_fingerprint,
        revocation_status=status.value,
        expires_at=lk.expires_at.isoformat(),
        extra={"source": "startup_check", "status": "valid"},
    )
    return (
        LicenseStatus.VALID,
        f"License valid — tier={lk.tier}, expires={lk.expires_at.date().isoformat()}.",
    )
