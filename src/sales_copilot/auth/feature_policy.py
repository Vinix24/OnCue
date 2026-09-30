"""Central feature entitlement checks.

Legal notice: this module enforces the OnCue commercial license. Removing or
circumventing it to obtain Pro features without a valid, paid license
violates the OnCue commercial license and applicable copyright law. See
LICENSE-COMMERCIAL.md.

Resolves the active tier from the configured license via the Ed25519 verifier
(with legacy HMAC fallback until the migration deadline), applies an offline-first
revocation check, and maps the tier to an explicit per-tier feature-set.
``is_pro()`` remains as a compatibility wrapper for existing call sites.

Dev-build admin override: on the owner's own dev checkout (``_dev_keys``
present), ``FeaturePolicy.current_tier()`` defaults to unconditional
``"enterprise"`` regardless of any configured license, so the owner always has
full access on their own build. ``SALES_COPILOT_DEV_TIER`` lets a dev checkout
force a specific tier (or fall back to the real license flow) for local
testing; see ``FeaturePolicy.current_tier`` for the exact contract. Production
builds are entirely unaffected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from sales_copilot.auth.audit import key_fingerprint, log_verify_decision
from sales_copilot.auth.build_pubkey import dev_pubkey_fingerprint
from sales_copilot.auth.license_format import (
    FEATURE_AUTOSTART,
    FEATURE_CALLTAP,
    FEATURE_CENTRAL_AUDIT,
    FEATURE_DEEP_INSIGHTS,
    FEATURE_DYNAMIC_SLIDES,
    FEATURE_LIVE_COACHING,
    FEATURE_MCP_BRIDGE,
    FEATURE_REPORT_DELIVERY_ENDPOINT,
    FEATURE_RESPONSE_PLAYBOOK,
    FEATURE_SCRIPT_TRACKING,
    FEATURE_SCRIPT_TRACKING_COMPUTE,
    FEATURE_SCRIPT_TRACKING_LIVE,
    FREE_FEATURES,
    LEGACY_FEATURE_EXPANSIONS,
    TIER_FEATURES,
)
from sales_copilot.auth.license_verifier import decode_and_verify
from sales_copilot.auth.revocation_cache import (
    RevocationStatus,
    check_revoked,
    is_degraded,
    mark_degraded,
)

__all__ = [
    "FEATURE_AUTOSTART",
    "FEATURE_CALLTAP",
    "FEATURE_CENTRAL_AUDIT",
    "FEATURE_DEEP_INSIGHTS",
    "FEATURE_DYNAMIC_SLIDES",
    "FEATURE_LIVE_COACHING",
    "FEATURE_MCP_BRIDGE",
    "FEATURE_REPORT_DELIVERY_ENDPOINT",
    "FEATURE_RESPONSE_PLAYBOOK",
    "FEATURE_SCRIPT_TRACKING",
    "FEATURE_SCRIPT_TRACKING_COMPUTE",
    "FEATURE_SCRIPT_TRACKING_LIVE",
    "RESPONSE_LOCKED_TEASER",
    "FeaturePolicy",
    "get_feature_policy",
    "resolve_response_suggestion",
]

# Free/Pro seam for the response layer ("detectie free, kaartenbak Pro"): the
# card itself always fires on Free, only the curated response text is gated.
RESPONSE_LOCKED_TEASER = "\U0001f512 Pro: bekijk de beste tegenreactie"

_LICENSE_ENV = "SALES_COPILOT_LICENSE"
_PRO_TIERS = frozenset({"pro", "enterprise"})

# Dev-build admin override: on the owner's own dev checkout (``_dev_keys``
# present, see ``build_pubkey.dev_pubkey_fingerprint``), the default tier is
# unconditional "enterprise" -- full access, no license required. This is
# scoped to dev builds only; a packaged release build never has ``_dev_keys``
# and always uses the real license-verification flow below.
_DEV_TIER_ENV = "SALES_COPILOT_DEV_TIER"
_FORCEABLE_DEV_TIERS = frozenset({"free", "pro", "enterprise"})


def _is_entitled(
    tier: str, license_id: str, pubkey_fingerprint: str | None
) -> tuple[bool, RevocationStatus]:
    """Return (entitled, revocation_status) for the key.

    Pro/Enterprise tiers are fail-closed: any revocation service outage that
    exceeds the grace window degrades the key to free.

    Dev-build exception: when the verified license is signed with this
    build's dev/test pubkey (``_dev_keys`` present -- see
    ``build_pubkey.dev_pubkey_fingerprint``), an ``UNAVAILABLE`` revocation
    result fails OPEN instead, so the owner's own dev checkout keeps its Pro
    features when the revocation service can't be reached. An explicit
    ``REVOKED`` result is always denied, dev builds included. Production
    builds (no ``_dev_keys`` module, so ``dev_pubkey_fingerprint()`` returns
    ``None``) are unaffected and keep the original fail-closed behaviour
    exactly.
    """
    status = check_revoked(license_id, tier=tier)
    if status is RevocationStatus.ENTITLED:
        return True, status
    if tier in _PRO_TIERS:
        dev_fp = dev_pubkey_fingerprint()
        if (
            status is RevocationStatus.UNAVAILABLE
            and dev_fp is not None
            and pubkey_fingerprint == dev_fp
        ):
            return True, status
        # UNAVAILABLE inside the grace window still blocks Pro features; the
        # user sees a transient degradation rather than a silent bypass.
        return False, status
    # Free tier has nothing to revoke; REVOKED/UNAVAILABLE are treated as entitled.
    return True, status


@dataclass(frozen=True)
class FeaturePolicy:
    """Resolve feature access from the currently configured license."""

    def current_tier(self) -> str:
        """Resolve the active tier.

        On a dev build (``_dev_keys`` present) this defaults to unconditional
        ``"enterprise"`` -- the owner's own checkout, full access, no license
        key required. ``SALES_COPILOT_DEV_TIER`` is a dev-only test escape:

        * unset / any value other than below -> ``"enterprise"`` (admin
          full access, the default).
        * ``"free"`` / ``"pro"`` / ``"enterprise"`` -> force that tier, for
          exercising tier-specific behaviour locally.
        * ``"license"`` -> skip the override and fall back to the real
          license-verification flow below, so the license chain itself
          (including the #53 revocation fail-open) can be tested from a dev
          checkout.

        Production builds (no ``_dev_keys``) never read
        ``SALES_COPILOT_DEV_TIER`` and keep the license-verification flow
        below exactly as before.
        """
        if dev_pubkey_fingerprint() is not None:
            override = os.environ.get(_DEV_TIER_ENV, "").strip().lower()
            if override in _FORCEABLE_DEV_TIERS:
                log_verify_decision(
                    "",
                    decision="dev_build_forced_tier",
                    tier=override,
                    extra={"source": "feature_policy", "dev_tier_env": override},
                )
                return override
            if override != "license":
                log_verify_decision(
                    "",
                    decision="dev_build_full_access",
                    tier="enterprise",
                    extra={"source": "feature_policy"},
                )
                return "enterprise"

        key = os.environ.get(_LICENSE_ENV, "").strip()
        if not key:
            log_verify_decision(
                "",
                decision="no_license",
                tier="free",
                extra={"source": "feature_policy"},
            )
            return "free"
        license_key = decode_and_verify(key)
        kf = key_fingerprint(key)
        if license_key is None:
            log_verify_decision(
                kf,
                decision="invalid_or_untrusted",
                tier="free",
                extra={"source": "feature_policy"},
            )
            return "free"
        if is_degraded(license_key.license_id):
            log_verify_decision(
                kf,
                decision="degraded_to_free",
                tier="free",
                pubkey_fingerprint=license_key.pubkey_fingerprint,
                extra={"source": "feature_policy", "declared_tier": license_key.tier},
            )
            return "free"
        entitled, status = _is_entitled(
            license_key.tier, license_key.license_id, license_key.pubkey_fingerprint
        )
        if not entitled:
            mark_degraded(license_key.license_id)
            log_verify_decision(
                kf,
                decision="not_entitled",
                tier="free",
                pubkey_fingerprint=license_key.pubkey_fingerprint,
                revocation_status=status.value,
                extra={"source": "feature_policy", "declared_tier": license_key.tier},
            )
            return "free"
        log_verify_decision(
            kf,
            decision="entitled",
            tier=license_key.tier,
            pubkey_fingerprint=license_key.pubkey_fingerprint,
            revocation_status=status.value,
            extra={"source": "feature_policy"},
        )
        return license_key.tier

    def allows(self, feature_id: str) -> bool:
        # Enforcement point for the OnCue commercial license: this gate is
        # what separates Free from the paid Pro/Enterprise features.
        # Circumventing it to obtain Pro features without a valid, paid
        # license violates the OnCue commercial license and applicable
        # copyright law. See LICENSE-COMMERCIAL.md.
        granted = TIER_FEATURES.get(self.current_tier(), FREE_FEATURES)
        if feature_id in granted:
            return True
        # Legacy alias expansion (Phase 1, finding #1): a grant set that only
        # carries a pre-split legacy capability id still counts as granting
        # every capability that id was split into. See
        # ``license_format.LEGACY_FEATURE_EXPANSIONS``.
        return any(
            feature_id in expanded and legacy_id in granted
            for legacy_id, expanded in LEGACY_FEATURE_EXPANSIONS.items()
        )

    def is_pro(self) -> bool:
        return self.current_tier() in _PRO_TIERS


_FEATURE_POLICY = FeaturePolicy()


def get_feature_policy() -> FeaturePolicy:
    return _FEATURE_POLICY


def resolve_response_suggestion(
    curated: str | None,
    *,
    feature_policy: FeaturePolicy | None = None,
) -> str:
    """Gate a curated response behind FEATURE_RESPONSE_PLAYBOOK.

    Returns an empty string when there is nothing curated to show (e.g. no
    matching case, no configured YAML response), the curated text when the
    session is Pro-entitled, or the shared teaser constant otherwise.
    """
    if not curated:
        return ""
    policy = feature_policy or get_feature_policy()
    if policy.allows(FEATURE_RESPONSE_PLAYBOOK):
        return curated
    return RESPONSE_LOCKED_TEASER
