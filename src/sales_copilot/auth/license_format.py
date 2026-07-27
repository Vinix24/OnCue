"""Canonical constants for the OnCue license-key contract.

Legal notice: this module is part of the code that enforces the OnCue
commercial license. Removing or circumventing it to obtain Pro features
without a valid, paid license violates the OnCue commercial license and
applicable copyright law. See LICENSE-COMMERCIAL.md.
"""

from datetime import date

SCP_PREFIX = "SCP"
SC_PREFIX = "SC"
SCP_VERSION = 0x01

SCP_VERSION_OFFSET = 0
SCP_TIER_OFFSET = 1
SCP_ROTATION_EPOCH_OFFSET = 2
SCP_ISSUED_AT_OFFSET = 3
SCP_ISSUED_AT_END = 7
SCP_EXPIRES_AT_OFFSET = 7
SCP_EXPIRES_AT_END = 11
SCP_LICENSE_ID_OFFSET = 11
SCP_LICENSE_ID_END = 27
SCP_SIGNATURE_OFFSET = 27
SCP_SIGNATURE_END = 91
SCP_TOTAL_BYTES = 91

SCP_VERSION_BYTES = 1
SCP_TIER_BYTES = 1
SCP_ROTATION_EPOCH_BYTES = 1
SCP_ISSUED_AT_BYTES = 4
SCP_EXPIRES_AT_BYTES = 4
SCP_LICENSE_ID_BYTES = 16
SCP_SIGNATURE_BYTES = 64
SCP_FIELD_SIZES = (
    SCP_VERSION_BYTES,
    SCP_TIER_BYTES,
    SCP_ROTATION_EPOCH_BYTES,
    SCP_ISSUED_AT_BYTES,
    SCP_EXPIRES_AT_BYTES,
    SCP_LICENSE_ID_BYTES,
    SCP_SIGNATURE_BYTES,
)

SCP_PAYLOAD_SLICE = slice(SCP_VERSION_OFFSET, SCP_SIGNATURE_OFFSET)
SCP_SIGNATURE_SLICE = slice(SCP_SIGNATURE_OFFSET, SCP_SIGNATURE_END)

BASE32_SPEC = "RFC 4648, uppercase, no padding, grouped every 4 characters"
BASE32_GROUP_SIZE = 4

TIER_CODES = {"free": 0, "pro": 1, "enterprise": 2}

LEGACY_HMAC_DEADLINE = date(2026, 9, 1)

FEATURE_CALLTAP = "audio.calltap"
FEATURE_CENTRAL_AUDIT = "compliance.central_audit"
FEATURE_DYNAMIC_SLIDES = "presentation.dynamic_slides"
FEATURE_AUTOSTART = "system.autostart"
FEATURE_LIVE_COACHING = "coaching.live"
# Legacy, pre-split, all-or-nothing script-tracking flag. Superseded by the
# two named capabilities below (finding #1, salesprep-pro design doc section
# 3) but kept as a real capability id -- still granted to Pro/Enterprise so
# any call site that has not migrated keeps working, and used as the source
# id in LEGACY_FEATURE_EXPANSIONS below so a grant of this flag always implies
# a grant of both split capabilities too.
FEATURE_SCRIPT_TRACKING = "coaching.script_tracking"
FEATURE_SCRIPT_TRACKING_COMPUTE = "coaching.script_tracking.compute"
FEATURE_SCRIPT_TRACKING_LIVE = "coaching.script_tracking.live"
FEATURE_RESPONSE_PLAYBOOK = "coaching.response_playbook"
FEATURE_IDS = frozenset(
    {
        FEATURE_CALLTAP,
        FEATURE_CENTRAL_AUDIT,
        FEATURE_DYNAMIC_SLIDES,
        FEATURE_AUTOSTART,
        FEATURE_LIVE_COACHING,
        FEATURE_SCRIPT_TRACKING,
        FEATURE_SCRIPT_TRACKING_COMPUTE,
        FEATURE_SCRIPT_TRACKING_LIVE,
        FEATURE_RESPONSE_PLAYBOOK,
    }
)

# Free computes script coverage (deterministic, local, no LLM required) and
# writes the post-call scorecard -- it just never gets the live push or the
# curated remedy. See salesprep-pro design doc section 3.
FREE_FEATURES = frozenset({FEATURE_SCRIPT_TRACKING_COMPUTE})
PRO_FEATURES = frozenset(
    {
        FEATURE_CALLTAP,
        FEATURE_CENTRAL_AUDIT,
        FEATURE_DYNAMIC_SLIDES,
        FEATURE_AUTOSTART,
        FEATURE_LIVE_COACHING,
        FEATURE_SCRIPT_TRACKING,
        FEATURE_SCRIPT_TRACKING_COMPUTE,
        FEATURE_SCRIPT_TRACKING_LIVE,
        FEATURE_RESPONSE_PLAYBOOK,
    }
)
ENTERPRISE_FEATURES = frozenset(
    {
        FEATURE_CALLTAP,
        FEATURE_CENTRAL_AUDIT,
        FEATURE_DYNAMIC_SLIDES,
        FEATURE_AUTOSTART,
        FEATURE_LIVE_COACHING,
        FEATURE_SCRIPT_TRACKING,
        FEATURE_SCRIPT_TRACKING_COMPUTE,
        FEATURE_SCRIPT_TRACKING_LIVE,
        FEATURE_RESPONSE_PLAYBOOK,
    }
)

# Keep every tier explicit: Enterprise currently has the same features as Pro,
# but it is not derived from Pro and can be extended independently.
TIER_FEATURES = {
    "free": FREE_FEATURES,
    "pro": PRO_FEATURES,
    "enterprise": ENTERPRISE_FEATURES,
}

# Back-compat alias/expansion table (Phase 1, finding #1): a tier (or any
# other capability source) that only carries a legacy, pre-split capability
# id must still be treated as granting every capability it was split into.
# ``FeaturePolicy.allows()`` (auth/feature_policy.py) consults this so a
# license carrying just ``coaching.script_tracking`` resolves both
# ``coaching.script_tracking.compute`` and ``coaching.script_tracking.live``
# as granted, without retrofitting a check at every call site.
#
# Rollback: deleting this table, removing FEATURE_SCRIPT_TRACKING_COMPUTE /
# FEATURE_SCRIPT_TRACKING_LIVE from FREE_FEATURES / PRO_FEATURES /
# ENTERPRISE_FEATURES, and reverting FREE_FEATURES to frozenset() restores
# today's exact all-or-nothing behavior on the single FEATURE_SCRIPT_TRACKING
# flag.
LEGACY_FEATURE_EXPANSIONS: dict[str, frozenset[str]] = {
    FEATURE_SCRIPT_TRACKING: frozenset(
        {FEATURE_SCRIPT_TRACKING_COMPUTE, FEATURE_SCRIPT_TRACKING_LIVE}
    ),
}
