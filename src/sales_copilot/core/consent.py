"""Per-session consent recording and configurable consent-strength tiers.

The deploying organisation (the AVG controller) chooses how strict the consent
gate is via ``CONSENT_TIER``. The ladder runs from ``off`` (no gate, not
recommended) through ``audit`` (record only, default) and ``soft`` (non-blocking
nudge) to ``strict`` (blocks call start until explicit consent is recorded).

Recording is non-blocking by default (``CONSENT_TIER=audit``). Compliance-strict
deployments can opt in to ``strict``. Tracking can be disabled independently
with ``CONSENT_TRACKING_ENABLED=false``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from sales_copilot.core.audit_ledger import AuditWriter, get_audit_writer
from sales_copilot.core.paths import resolve_app_path

logger = logging.getLogger(__name__)

_ENABLED_ENV = "CONSENT_TRACKING_ENABLED"
_TIER_ENV = "CONSENT_TIER"
_LEGACY_GATE_MODE_ENV = "CONSENT_GATE_MODE"
_FALSEY = {"0", "false", "no", "off", ""}
_VALID_TIERS = frozenset({"off", "audit", "soft", "strict"})
_LEGACY_MODE_MAP = {"audit": "audit", "blocking": "strict"}


def consent_tracking_enabled() -> bool:
    return os.environ.get(_ENABLED_ENV, "true").strip().lower() not in _FALSEY


def _raw_env_tier() -> str | None:
    """Return the explicit ``CONSENT_TIER`` env value if set and valid."""
    value = os.environ.get(_TIER_ENV)
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized if normalized in _VALID_TIERS else None


def _legacy_env_tier() -> str | None:
    """Map the legacy ``CONSENT_GATE_MODE`` env var to the new tier ladder.

    ``audit`` maps to ``audit``; ``blocking`` maps to ``strict``. Any other
    legacy value is ignored (fail-open: never silently block on a typo).
    """
    value = os.environ.get(_LEGACY_GATE_MODE_ENV)
    if value is None:
        return None
    normalized = value.strip().lower()
    return _LEGACY_MODE_MAP.get(normalized)


def _preset_default_tier(preset_name: str | None) -> str:
    """Return the default consent tier for a preset, falling back to ``audit``."""
    if not preset_name:
        return "audit"
    preset_path = resolve_app_path(f"config/presets/{preset_name.strip().lower()}.yaml")
    if not preset_path.exists():
        return "audit"
    try:
        import yaml

        with preset_path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception:
        return "audit"
    tier = str(data.get("default_consent_tier", "audit")).strip().lower()
    return tier if tier in _VALID_TIERS else "audit"


def consent_tier(preset_name: str | None = None) -> str:
    """Resolve the active consent tier.

    Resolution order (first wins):
    1. ``CONSENT_TIER`` environment variable.
    2. Legacy ``CONSENT_GATE_MODE`` environment variable (mapped with a
       deprecation warning).
    3. Preset default (``default_consent_tier`` in the YAML).
    4. Global default ``audit``.

    The ``off`` tier is available but not recommended; callers should log a
    startup warning when it is selected.
    """
    explicit = _raw_env_tier()
    if explicit is not None:
        return explicit
    legacy = _legacy_env_tier()
    if legacy is not None:
        logger.warning(
            "%s is deprecated; map it to %s=%s in your .env instead.",
            _LEGACY_GATE_MODE_ENV,
            _TIER_ENV,
            legacy,
        )
        return legacy
    preset_tier = _preset_default_tier(preset_name)
    if preset_tier in _VALID_TIERS:
        return preset_tier
    return "audit"


def must_block_start(consent_given: bool, tier: str | None = None) -> bool:
    """Return ``True`` when a call must be REFUSED for lack of consent.

    Only the ``strict`` tier without recorded consent blocks. All other tiers
    return ``False``. If ``tier`` is omitted, the active tier is resolved from
    the environment/preset.
    """
    active_tier = (tier or consent_tier()).strip().lower()
    return active_tier == "strict" and not consent_given


def should_soft_nudge(consent_given: bool, tier: str | None = None) -> bool:
    """Return ``True`` when a non-blocking consent nudge should be shown.

    The ``soft`` tier shows a clear warning when consent has not been recorded,
    but the call still proceeds.
    """
    active_tier = (tier or consent_tier()).strip().lower()
    return active_tier == "soft" and not consent_given


def record_consent(
    session_id: str,
    *,
    asked: bool,
    given: bool,
    writer: AuditWriter | None = None,
    ts: datetime | None = None,
) -> dict | None:
    """Write a non-blocking consent audit event.

    Returns the written record, or ``None`` when tracking is disabled or the
    write fails — this never raises, so it cannot block a call from starting.
    """
    if not consent_tracking_enabled():
        return None
    record = {
        "ts": (ts or datetime.now(UTC)).isoformat(),
        "session_id": session_id,
        "event_type": "consent",
        "consent_asked": bool(asked),
        "consent_given": bool(given),
    }
    try:
        (writer or get_audit_writer()).write(record)
    except Exception as exc:
        logger.error("consent record write failed (non-blocking): %s", exc)
        return None
    return record
