"""Lightweight, privacy-safe measurement signals for the launch funnel.

Three opt-in/config-driven signals:

1. Detection-correction ping — aggregated counts per category, never transcript.
2. Pro-license heartbeat — license fingerprint + timestamp, no PII.
3. Install-code attribution — captured at install/consult/audit-intake.

All signals are best-effort and non-blocking: a downstream outage must never
break the live copilot experience. Hardcoded endpoints are forbidden; every
destination is configurable via environment variables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

from sales_copilot.core.audit_ledger import AuditWriter, get_audit_writer
from sales_copilot.core.config import MeasurementConfig
from sales_copilot.core.pii_filter import redact_pii

logger = logging.getLogger(__name__)

_LICENSE_ENV = "SALES_COPILOT_LICENSE"
_POST_TIMEOUT_S = 5.0


def _license_fingerprint(key: str) -> str:
    """Stable, non-reversible SHA-256 fingerprint of a license key."""
    normalized = (key or "").strip().upper().replace(" ", "")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _post_json(url: str, payload: dict[str, Any]) -> bool:
    """POST JSON to ``url``. Returns True on 2xx, False otherwise.

    Network failures are swallowed so measurement never blocks the caller.
    """
    data = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_POST_TIMEOUT_S) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.debug("measurement POST to %s failed (non-blocking): %s", url, exc)
        return False


class CorrectionAccumulator:
    """In-memory aggregation bucket for detection corrections.

    Per category counts are flushed as a single payload when the caller flushes
    (e.g. end of call) or when a size threshold is reached. The payload never
    contains transcript text or trigger phrases.
    """

    def __init__(self) -> None:
        self._predicted_counts: dict[str, int] = {}
        self._corrected_counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(
        self,
        detection_type: str,
        predicted_category: str,
        corrected_category: str | None = None,
    ) -> None:
        """Record one user correction."""
        with self._lock:
            pred_key = f"{detection_type}:{predicted_category}"
            self._predicted_counts[pred_key] = self._predicted_counts.get(pred_key, 0) + 1
            if corrected_category:
                corr_key = f"{detection_type}:{corrected_category}"
                self._corrected_counts[corr_key] = self._corrected_counts.get(corr_key, 0) + 1

    def flush_payload(
        self,
        *,
        install_code: str = "",
        license_fingerprint: str = "",
    ) -> dict[str, Any]:
        """Build and reset the aggregated payload."""
        with self._lock:
            payload = {
                "event_type": "detection_correction",
                "ts": datetime.now(UTC).isoformat(),
                "predicted_counts": dict(self._predicted_counts),
                "corrected_counts": dict(self._corrected_counts),
                "install_code": install_code,
                "license_fingerprint": license_fingerprint,
            }
            self._predicted_counts.clear()
            self._corrected_counts.clear()
            return payload

    def is_empty(self) -> bool:
        with self._lock:
            return not self._predicted_counts and not self._corrected_counts

    def peek_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        with self._lock:
            return dict(self._predicted_counts), dict(self._corrected_counts)


# Module-level accumulator reused across the process lifetime.
_correction_accumulator = CorrectionAccumulator()


def _strip_transcript_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Defence-in-depth: remove any field that could carry transcript text.

    The caller is already forbidden from passing trigger_phrase/transcript; this
    function makes the invariant verifiable in tests.
    """
    forbidden = {"trigger_phrase", "transcript", "text", "quote", "response_suggestion"}
    return {key: value for key, value in payload.items() if key not in forbidden}


def record_detection_correction(
    detection_type: str,
    predicted_category: str,
    *,
    corrected_category: str | None = None,
    config: MeasurementConfig | None = None,
) -> dict[str, Any] | None:
    """Record a user correction and, when opt-in, enqueue an aggregated signal.

    Returns the aggregated payload when opt-in is enabled and a flush is
    triggered, otherwise ``None``. No transcript or trigger phrase is accepted
    or transmitted.
    """
    cfg = config or MeasurementConfig.from_env()
    if not cfg.correction_opt_in:
        return None

    # Sanitize category names defensively (categories come from config, but PII
    # redaction makes the output provably safe).
    clean_predicted, _ = redact_pii(predicted_category)
    clean_corrected = None
    if corrected_category:
        clean_corrected, _ = redact_pii(corrected_category)

    _correction_accumulator.record(
        detection_type=detection_type,
        predicted_category=clean_predicted,
        corrected_category=clean_corrected,
    )

    license_key = os.environ.get(_LICENSE_ENV, "").strip()
    fingerprint = _license_fingerprint(license_key)

    payload = _correction_accumulator.flush_payload(
        install_code=cfg.install_code,
        license_fingerprint=fingerprint,
    )
    payload = _strip_transcript_fields(payload)

    endpoint = cfg.correction_endpoint
    if endpoint:
        _post_json(endpoint, payload)
    return payload


def build_correction_payload(
    *,
    install_code: str = "",
    license_key: str = "",
) -> dict[str, Any]:
    """Build the current aggregated correction payload without flushing.

    Useful for tests and for callers that want to control flush timing.
    """
    predicted_counts, corrected_counts = _correction_accumulator.peek_counts()
    return _strip_transcript_fields(
        {
            "event_type": "detection_correction",
            "ts": datetime.now(UTC).isoformat(),
            "predicted_counts": predicted_counts,
            "corrected_counts": corrected_counts,
            "install_code": install_code,
            "license_fingerprint": _license_fingerprint(license_key),
        }
    )


class ProLicenseHeartbeat:
    """Periodic heartbeat sent only while a valid Pro license is active.

    The heartbeat carries a license fingerprint and a timestamp — no PII. It is
    delivered on a background thread so the main loop is never blocked. If the
    endpoint is down the failure is logged and suppressed; there is no retry
    storm because the next scheduled beat simply tries again.
    """

    def __init__(
        self,
        config: MeasurementConfig | None = None,
        *,
        feature_policy: Any | None = None,
    ) -> None:
        self.cfg = config or MeasurementConfig.from_env()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        if feature_policy is not None:
            self._policy = feature_policy
        else:
            from sales_copilot.auth.feature_policy import get_feature_policy

            self._policy = get_feature_policy()

    def _is_pro(self) -> bool:
        try:
            return self._policy.is_pro()
        except Exception as exc:
            logger.debug("heartbeat tier check failed (non-blocking): %s", exc)
            return False

    def _send_beat(self) -> None:
        endpoint = self.cfg.heartbeat_endpoint
        if not endpoint:
            return
        if not self._is_pro():
            return

        license_key = os.environ.get(_LICENSE_ENV, "").strip()
        payload = {
            "event_type": "pro_license_heartbeat",
            "ts": datetime.now(UTC).isoformat(),
            "license_fingerprint": _license_fingerprint(license_key),
            "install_code": self.cfg.install_code,
        }
        ok = _post_json(endpoint, payload)
        if not ok:
            logger.debug("pro heartbeat POST failed (non-blocking); next beat in %ss", self.cfg.heartbeat_interval_s)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._send_beat()
            self._stop_event.wait(self.cfg.heartbeat_interval_s)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if not self.cfg.heartbeat_enabled or not self.cfg.heartbeat_endpoint:
            logger.debug("pro heartbeat disabled or endpoint not configured")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="pro-license-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)


def get_install_code(config: MeasurementConfig | None = None) -> str:
    """Return the configured install/attribution code."""
    return (config or MeasurementConfig.from_env()).install_code


def record_install_attribution(
    *,
    source: str = "intake",
    writer: AuditWriter | None = None,
    config: MeasurementConfig | None = None,
) -> dict[str, Any] | None:
    """Record an install/attribution audit event when a code is configured.

    Returns the written record, or ``None`` when no code is configured or the
    write fails. The code itself is written to the local audit trail only.
    """
    cfg = config or MeasurementConfig.from_env()
    install_code = cfg.install_code.strip()
    if not install_code:
        return None

    record = {
        "ts": datetime.now(UTC).isoformat(),
        "event_type": "install_attribution",
        "source": source,
        "install_code": install_code,
    }
    try:
        (writer or get_audit_writer()).write(record)
    except Exception as exc:
        logger.error("install attribution audit write failed (non-blocking): %s", exc)
        return None
    return record


def start_pro_license_heartbeat(
    config: MeasurementConfig | None = None,
    *,
    feature_policy: Any | None = None,
) -> ProLicenseHeartbeat:
    """Start the global Pro-license heartbeat and return the controller."""
    heartbeat = ProLicenseHeartbeat(config, feature_policy=feature_policy)
    heartbeat.start()
    return heartbeat
