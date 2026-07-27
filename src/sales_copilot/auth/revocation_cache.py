"""Offline-first license revocation check with a TTL cache and Pro fail-closed grace.

The client phones home to ``/license/check`` with the pseudonymous ``license_id``
only when the local cache is missing or stale. On a network failure it falls back
to any prior cached value while inside ``SCP_REVOCATION_GRACE_S``; once the grace
window closes a stale cached ``revoked=false`` is no longer trusted and Pro tiers
are treated as ``UNAVAILABLE`` (fail-closed). For Pro/Enterprise keys with no
prior cache and no network, the result is ``UNAVAILABLE`` while inside the grace
window, then ``REVOKED`` once it closes.

The revocation authority URL is only overrideable in dev builds (where
``sales_copilot.auth._dev_keys`` is present). Release builds always use the
embedded default.

The request payload carries no PII beyond the random ``license_id``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path

_CACHE_PATH = Path.home() / ".sales_copilot" / "revocation_cache.json"
_METRICS_PATH = Path.home() / ".sales_copilot" / "metrics" / "revocation_denied_outage.ndjson"
_DEGRADED_META_KEY = "_degraded_to_free"
_TTL_SECONDS = 7 * 24 * 3600
_URL_ENV = "SALES_COPILOT_LICENSE_CHECK_URL"
_DEFAULT_URL = "https://license.salescopilot.app"
_TIMEOUT = 5.0

# Grace window during which a Pro key may keep working while revocation is
# unavailable. After this window closes, an unavailable revocation service is
# treated as REVOKED (fail-closed).
_REVOCATION_GRACE_ENV = "SCP_REVOCATION_GRACE_S"
_DEFAULT_GRACE_SECONDS = 24 * 3600

_PRO_TIERS = frozenset({"pro", "enterprise"})


class RevocationStatus(Enum):
    ENTITLED = "entitled"
    REVOKED = "revoked"
    UNAVAILABLE = "unavailable"


def _now() -> float:
    return time.time()


def _grace_seconds() -> int:
    try:
        return max(0, int(os.environ.get(_REVOCATION_GRACE_ENV, _DEFAULT_GRACE_SECONDS)))
    except (ValueError, TypeError):
        return _DEFAULT_GRACE_SECONDS


def _read_cache() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_cache(cache: dict) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass  # cache is an optimisation; never fail the caller


def _fetch_revocation(license_id: str, base_url: str) -> dict | None:
    """POST /license/check. Returns the parsed body, or None on any failure."""
    url = base_url.rstrip("/") + "/license/check"
    data = json.dumps({"license_id": license_id}).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _status_from_bool(revoked: bool) -> RevocationStatus:
    return RevocationStatus.REVOKED if revoked else RevocationStatus.ENTITLED


def _record_success(cache: dict, now: float) -> None:
    cache["_meta"] = {"last_success_at": now}


def _last_success_at(cache: dict) -> float | None:
    meta = cache.get("_meta")
    if isinstance(meta, dict):
        return meta.get("last_success_at")
    return None


def _read_degraded_set(cache: dict) -> set[str]:
    meta = cache.get("_meta")
    if not isinstance(meta, dict):
        return set()
    degraded = meta.get(_DEGRADED_META_KEY)
    if isinstance(degraded, list):
        return {str(item) for item in degraded if isinstance(item, str)}
    return set()


def _write_degraded_set(cache: dict, degraded: set[str]) -> None:
    meta = cache.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        cache["_meta"] = meta
    meta[_DEGRADED_META_KEY] = sorted(degraded)


def _is_dev_build() -> bool:
    """Return True only when the dev-only key module is present."""
    try:
        from sales_copilot.auth import _dev_keys  # noqa: F401
        return True
    except ImportError:
        return False


def _emit_revocation_denied_outage(license_id: str) -> None:
    """Record a denial caused by an outage that exceeded the grace window.

    Best-effort local counter; never raises. The license_id is hashed before
    storage so the metrics file contains no raw identifiers.
    """
    try:
        _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "ts": _now(),
                "license_id_sha256": hashlib.sha256(license_id.encode("utf-8")).hexdigest(),
            },
            sort_keys=True,
        )
        with _METRICS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # pragma: no cover - metric emission must never break auth
        pass


def is_degraded(license_id: str) -> bool:
    """Return True if this license_id has been permanently degraded to free.

    The degradation bit is stored in the shared revocation cache so the app
    remembers the one-way transition across restarts and does not flap back to
    Pro when the revocation service briefly becomes reachable again.
    """
    if not license_id:
        return False
    cache = _read_cache()
    return license_id in _read_degraded_set(cache)


def mark_degraded(license_id: str) -> None:
    """Persistently mark ``license_id`` as degraded to free.

    Idempotent and best-effort: never raises. Once set, ``FeaturePolicy``
    short-circuits to the free tier for this license_id.
    """
    if not license_id:
        return
    cache = _read_cache()
    degraded = _read_degraded_set(cache)
    if license_id in degraded:
        return
    degraded.add(license_id)
    _write_degraded_set(cache, degraded)
    _write_cache(cache)


def check_revoked(license_id: str, tier: str = "free") -> RevocationStatus:
    """Return the revocation status for ``license_id``.

    * Empty ``license_id`` (free / legacy HMAC) -> ``ENTITLED``.
    * Fresh cache -> cached value.
    * Stale or missing -> phone home; cache and return the result.
    * Network failure with prior cache -> fall back to the cached value only
      while inside ``SCP_REVOCATION_GRACE_S``. Beyond the grace window the stale
      value is not trusted; Pro/Enterprise tiers become ``UNAVAILABLE``.
    * Network failure without cache and Pro/Enterprise tier -> ``UNAVAILABLE``
      while inside ``SCP_REVOCATION_GRACE_S``, then ``REVOKED`` (fail-closed).
    * Network failure without cache and non-Pro tier -> ``ENTITLED``.
    * The authority URL is only overrideable via
      ``SALES_COPILOT_LICENSE_CHECK_URL`` in dev builds.
    """
    if not license_id:
        return RevocationStatus.ENTITLED

    cache = _read_cache()
    entry = cache.get(license_id)
    now = _now()
    if entry is not None and (now - entry.get("checked_at", 0)) < _TTL_SECONDS:
        return _status_from_bool(entry.get("revoked", False))

    # Release builds always phone the embedded authority; dev builds may override.
    if _is_dev_build():
        base_url = os.environ.get(_URL_ENV, "").strip() or _DEFAULT_URL
    else:
        base_url = _DEFAULT_URL

    result = _fetch_revocation(license_id, base_url)
    if result is not None:
        revoked = bool(result.get("revoked", False))
        cache[license_id] = {
            "revoked": revoked,
            "expires_at": result.get("expires_at", 0),
            "checked_at": now,
        }
        _record_success(cache, now)
        _write_cache(cache)
        return _status_from_bool(revoked)

    if entry is not None:
        last_success = _last_success_at(cache) or entry.get("checked_at")
        if last_success is None:
            _record_success(cache, now)
            _write_cache(cache)
            last_success = now
        if (now - last_success) <= _grace_seconds():
            return _status_from_bool(entry.get("revoked", False))
        if tier in _PRO_TIERS:
            _emit_revocation_denied_outage(license_id)
            return RevocationStatus.UNAVAILABLE
        return _status_from_bool(entry.get("revoked", False))

    if tier in _PRO_TIERS:
        last_success = _last_success_at(cache)
        if last_success is None:
            _record_success(cache, now)
            _write_cache(cache)
            last_success = now
        if (now - last_success) < _grace_seconds():
            return RevocationStatus.UNAVAILABLE
        return RevocationStatus.REVOKED

    return RevocationStatus.ENTITLED
