"""Central compliance-audit: tamper-evident hash chain to the license server.

Pro entitlement only. Each audit record is hashed CLIENT-SIDE and only the hash
is sent to ``/audit/ingest`` — the raw record stays local, so no PII reaches the
server. Best-effort and non-blocking: the local audit write is always
authoritative and a central failure never breaks it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request

from sales_copilot.auth.feature_policy import FeaturePolicy, get_feature_policy
from sales_copilot.auth.license_format import FEATURE_CENTRAL_AUDIT
from sales_copilot.core.audit_ledger import AuditWriter

logger = logging.getLogger(__name__)

_URL_ENV = "SALES_COPILOT_LICENSE_CHECK_URL"
_DEFAULT_URL = "https://license.salescopilot.app"
_LICENSE_ENV = "SALES_COPILOT_LICENSE"
_TIMEOUT = 5.0


def payload_hash(record: dict) -> str:
    """Stable SHA-256 of a record's canonical JSON."""
    canonical = json.dumps(record, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_body(hash_hex: str) -> bytes:
    """JSON body for the central audit POST — payload_hash only, never PII."""
    return json.dumps({"payload_hash": hash_hex}).encode("utf-8")


def _post_hash(hash_hex: str, key: str, base_url: str) -> bool:
    url = base_url.rstrip("/") + "/audit/ingest"
    data = _request_body(hash_hex)
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return False


class TieredAuditWriter:
    """Always writes locally; additionally ships a tamper-evident hash to the
    central audit chain when the ``central_audit`` feature is entitled (Pro)."""

    def __init__(
        self, local: AuditWriter, *, feature_policy: FeaturePolicy | None = None
    ) -> None:
        self._local = local
        self._policy = feature_policy or get_feature_policy()

    def write(self, record: dict) -> None:
        self._local.write(record)  # local is authoritative; PII stays here
        if not self._policy.allows(FEATURE_CENTRAL_AUDIT):
            return
        key = os.environ.get(_LICENSE_ENV, "").strip()
        if not key:
            return
        base_url = os.environ.get(_URL_ENV, "").strip() or _DEFAULT_URL
        try:
            if not _post_hash(payload_hash(record), key, base_url):
                logger.warning("central audit hash post failed (non-blocking)")
        except Exception as exc:
            logger.error("central audit post error (non-blocking): %s", exc)
