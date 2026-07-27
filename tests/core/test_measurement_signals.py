"""Tests for privacy-safe measurement signals.

Coverage:
- Detection-correction ping is opt-in (default off) and never carries transcript.
- Pro-license heartbeat is graceful when the endpoint is down.
- Install/attribution code is recorded in the audit trail at intake.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from sales_copilot.core import measurement_signals as ms
from sales_copilot.core.config import MeasurementConfig
from sales_copilot.core.measurement_signals import (
    CorrectionAccumulator,
    ProLicenseHeartbeat,
    _license_fingerprint,
    _post_json,
    build_correction_payload,
    record_detection_correction,
    record_install_attribution,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_accumulator(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test a fresh in-memory accumulator."""
    fresh = CorrectionAccumulator()
    monkeypatch.setattr(ms, "_correction_accumulator", fresh)


class _FakePolicy:
    def __init__(self, pro: bool = False):
        self._pro = pro

    def is_pro(self) -> bool:
        return self._pro


# ---------------------------------------------------------------------------
# Detection-correction ping
# ---------------------------------------------------------------------------


def test_correction_default_opt_in_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without explicit opt-in the correction is recorded locally but not forwarded."""
    monkeypatch.delenv("MEASUREMENT_CORRECTION_OPT_IN", raising=False)
    cfg = MeasurementConfig.from_env()

    payload = record_detection_correction(
        "pain_point",
        "prijs",
        corrected_category="timing",
        config=cfg,
    )

    assert payload is None


def test_correction_opt_in_payload_has_no_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in correction payload is aggregated and PII-stripped; no transcript fields."""
    monkeypatch.setenv("MEASUREMENT_CORRECTION_OPT_IN", "true")
    monkeypatch.setenv("MEASUREMENT_CORRECTION_ENDPOINT", "http://localhost:9999/correction")
    monkeypatch.setenv("INSTALL_CODE", "landing-42")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SCP-test-key")
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(ms, "_post_json", lambda url, payload: posted.append(payload) or True)

    cfg = MeasurementConfig.from_env()
    payload = record_detection_correction(
        "objection",
        "prijs",
        corrected_category="Jan de Vries",  # would be redacted if it leaked
        config=cfg,
    )

    assert payload is not None
    assert payload["event_type"] == "detection_correction"
    assert payload["install_code"] == "landing-42"
    assert payload["license_fingerprint"] == _license_fingerprint("SCP-test-key")
    assert payload["predicted_counts"] == {"objection:prijs": 1}
    assert payload["corrected_counts"] == {"objection:[NAAM]": 1}
    # Transcript-like fields must never be present.
    for forbidden in ("trigger_phrase", "transcript", "text", "quote", "response_suggestion"):
        assert forbidden not in payload
    assert len(posted) == 1


def test_correction_payload_redacts_pii_in_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    """Category names are redacted before they enter the payload."""
    monkeypatch.setenv("MEASUREMENT_CORRECTION_OPT_IN", "true")

    payload = record_detection_correction(
        "pain_point",
        "email jan@example.com",
        config=MeasurementConfig.from_env(),
    )

    assert payload is not None
    assert "jan@example.com" not in str(payload)
    assert "[EMAIL]" in str(payload)


def test_build_correction_payload_without_flushing() -> None:
    """Build payload reads current counts without resetting the accumulator."""
    acc = CorrectionAccumulator()
    acc.record("objection", "prijs")
    acc.record("objection", "prijs")
    acc.record("objection", "timing", corrected_category="scope")
    monkeypatch_local = pytest.MonkeyPatch()
    monkeypatch_local.setattr(ms, "_correction_accumulator", acc)

    payload = build_correction_payload(install_code="demo", license_key="SCP-key")

    assert payload["predicted_counts"] == {"objection:prijs": 2, "objection:timing": 1}
    assert payload["corrected_counts"] == {"objection:scope": 1}
    # Accumulator must still hold the counts after build.
    assert not acc.is_empty()
    monkeypatch_local.undo()


# ---------------------------------------------------------------------------
# Pro-license heartbeat
# ---------------------------------------------------------------------------


def test_heartbeat_skips_when_not_pro(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heartbeat sends nothing when the active tier is not Pro."""
    monkeypatch.setenv("MEASUREMENT_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("MEASUREMENT_HEARTBEAT_ENDPOINT", "http://localhost:9999/beat")
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(ms, "_post_json", lambda url, payload: posted.append(payload) or True)

    heartbeat = ProLicenseHeartbeat(feature_policy=_FakePolicy(pro=False))
    heartbeat._send_beat()

    assert posted == []


def test_heartbeat_sends_when_pro(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heartbeat sends license fingerprint + timestamp when Pro is active."""
    monkeypatch.setenv("MEASUREMENT_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("MEASUREMENT_HEARTBEAT_ENDPOINT", "http://localhost:9999/beat")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SCP-pro-key")
    monkeypatch.setenv("INSTALL_CODE", "partner-x")
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(ms, "_post_json", lambda url, payload: posted.append(payload) or True)

    heartbeat = ProLicenseHeartbeat(feature_policy=_FakePolicy(pro=True))
    heartbeat._send_beat()

    assert len(posted) == 1
    payload = posted[0]
    assert payload["event_type"] == "pro_license_heartbeat"
    assert payload["license_fingerprint"] == _license_fingerprint("SCP-pro-key")
    assert payload["install_code"] == "partner-x"
    assert "ts" in payload
    # No PII may leave.
    assert "SCP-pro-key" not in str(payload)
    assert "pro_license_heartbeat" in str(payload)


def test_heartbeat_graceful_when_endpoint_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Endpoint failure is non-fatal and does not raise."""
    monkeypatch.setenv("MEASUREMENT_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("MEASUREMENT_HEARTBEAT_ENDPOINT", "http://localhost:1/beat")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SCP-pro-key")

    heartbeat = ProLicenseHeartbeat(feature_policy=_FakePolicy(pro=True))

    # Must not raise despite the refused connection.
    heartbeat._send_beat()


def test_heartbeat_background_loop_respects_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Background thread sends beats at the configured interval and stops cleanly."""
    monkeypatch.setenv("MEASUREMENT_HEARTBEAT_ENABLED", "true")
    monkeypatch.setenv("MEASUREMENT_HEARTBEAT_ENDPOINT", "http://localhost:9999/beat")
    monkeypatch.setenv("MEASUREMENT_HEARTBEAT_INTERVAL_S", "0")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(ms, "_post_json", lambda url, payload: calls.append(payload) or True)

    cfg = MeasurementConfig.from_env()
    heartbeat = ProLicenseHeartbeat(cfg, feature_policy=_FakePolicy(pro=True))
    heartbeat.start()
    # Give the daemon thread a chance to fire at least once.
    time.sleep(0.05)
    heartbeat.stop()

    assert len(calls) >= 1


# ---------------------------------------------------------------------------
# Install-code attribution
# ---------------------------------------------------------------------------


def test_install_code_recorded_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """An install code is written to the local audit trail."""
    monkeypatch.setenv("INSTALL_CODE", "consult-2026-07")
    written: list[dict[str, Any]] = []
    writer = MagicMock()
    writer.write = written.append

    record = record_install_attribution(source="intake", writer=writer)

    assert record is not None
    assert record["event_type"] == "install_attribution"
    assert record["install_code"] == "consult-2026-07"
    assert record["source"] == "intake"


def test_install_code_skipped_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No audit event is written when no install code is configured."""
    monkeypatch.delenv("INSTALL_CODE", raising=False)
    writer = MagicMock()

    record = record_install_attribution(source="intake", writer=writer)

    assert record is None
    writer.write.assert_not_called()


# ---------------------------------------------------------------------------
# _post_json helper
# ---------------------------------------------------------------------------


def test_post_json_returns_false_on_bad_url() -> None:
    """_post_json swallows transport errors and returns False."""
    assert _post_json("http://localhost:1/nope", {"x": 1}) is False
