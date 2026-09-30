"""Tests for the customer-configured trigger delivery (modules/reports/delivery.py).

Coverage:
- Directory sink writes atomically and lands the exact payload under the
  documented filename.
- Endpoint sink posts exactly once on success.
- A failing endpoint retries within its bound, then surfaces the failure
  loudly without touching the local copy.
- Both sinks default off.
- Startup validation fails fast, with an actionable message, on an unusable
  directory or a malformed endpoint URL.

No real network calls are made: the HTTP side is mocked via
``urllib.request.urlopen``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from pathlib import Path

import pytest

from sales_copilot.auth.license_format import TIER_FEATURES
from sales_copilot.core.config import ReportDeliveryConfig
from sales_copilot.modules.reports import delivery

PAYLOAD = json.dumps({"session_id": "session-1", "full_transcript": []}).encode("utf-8")
FILENAME = "2026-09-06T12-00-00_session-1_report.json"


class _StubFeaturePolicy:
    """Test-only FeaturePolicy stub that resolves allows() from TIER_FEATURES directly.

    Mirrors ``tests/conftest.py``'s ``_TierFeaturePolicy`` but adds an
    "enterprise" case, which the shared conftest fixtures don't cover.
    """

    def __init__(self, tier: str) -> None:
        self._tier = tier

    def current_tier(self) -> str:
        return self._tier

    def allows(self, feature_id: str) -> bool:
        return feature_id in TIER_FEATURES.get(self._tier, frozenset())


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def getcode(self) -> int:
        return self.status


# ---------------------------------------------------------------------------
# Directory sink
# ---------------------------------------------------------------------------


def test_deliver_to_directory_writes_exact_payload(tmp_path: Path) -> None:
    delivery.deliver_to_directory(
        PAYLOAD, FILENAME, str(tmp_path), session_id="session-1", local_report_path=None
    )

    destination = tmp_path / FILENAME
    assert destination.read_bytes() == PAYLOAD


def test_deliver_to_directory_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    delivery.deliver_to_directory(
        PAYLOAD, FILENAME, str(tmp_path), session_id="session-1", local_report_path=None
    )

    leftovers = [p for p in tmp_path.iterdir() if p.name != FILENAME]
    assert leftovers == []


def test_deliver_to_directory_atomic_write_never_leaves_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-write must never leave a half-written file at the final name."""
    destination = tmp_path / FILENAME

    def _boom_before_replace(_src: str, _dst: object) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(delivery.os, "replace", _boom_before_replace)

    delivery.deliver_to_directory(
        PAYLOAD, FILENAME, str(tmp_path), session_id="session-1", local_report_path=None
    )

    # The failure is logged (below) and swallowed; critically, no partial file
    # exists at the final destination and no stray temp file survives either.
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_deliver_to_directory_failure_is_logged_loudly(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing_parent = tmp_path / "does-not-exist"
    local_copy = tmp_path / "local" / FILENAME

    with caplog.at_level(logging.ERROR):
        delivery.deliver_to_directory(
            PAYLOAD,
            FILENAME,
            str(missing_parent),
            session_id="session-1",
            local_report_path=local_copy,
        )

    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert any(str(local_copy) in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# Endpoint sink
# ---------------------------------------------------------------------------


def test_deliver_to_endpoint_posts_once_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        return _FakeResponse(200)

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)

    delivery.deliver_to_endpoint(
        PAYLOAD,
        "http://localhost:5555/webhook",
        session_id="session-1",
        local_report_path=None,
        timeout_s=5.0,
        max_attempts=3,
        retry_backoff_s=0.0,
        sleep=lambda _seconds: None,
    )

    assert calls == ["http://localhost:5555/webhook"]


def test_deliver_to_endpoint_retries_within_bound_then_surfaces_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def _always_fails(request: object, timeout: float) -> None:
        attempts.append(1)
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _always_fails)

    local_copy = Path("/tmp/does-not-matter/report.json")

    with caplog.at_level(logging.WARNING):
        delivery.deliver_to_endpoint(
            PAYLOAD,
            "http://localhost:5555/webhook",
            session_id="session-1",
            local_report_path=local_copy,
            timeout_s=1.0,
            max_attempts=3,
            retry_backoff_s=1.0,
            sleep=sleeps.append,
        )

    # Bounded: exactly max_attempts network calls, never an unbounded retry loop.
    assert len(attempts) == 3
    # Backoff between attempts only (2 waits for 3 attempts), linear in attempt number.
    assert sleeps == [1.0, 2.0]

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert "EXHAUSTED" in error_records[0].message
    assert str(local_copy) in error_records[0].message


def test_deliver_to_endpoint_non_2xx_status_counts_as_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-2xx response is a failure, not a silently accepted delivery."""

    def _fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        return _FakeResponse(500)

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)

    with caplog.at_level(logging.WARNING):
        delivery.deliver_to_endpoint(
            PAYLOAD,
            "http://localhost:5555/webhook",
            session_id="session-1",
            local_report_path=None,
            timeout_s=1.0,
            max_attempts=1,
            retry_backoff_s=0.0,
            sleep=lambda _seconds: None,
        )

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(error_records) == 1
    assert "EXHAUSTED" in error_records[0].message


# ---------------------------------------------------------------------------
# Pro gate on the endpoint sink (operator decision 2026-09-28)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_endpoint_gate_warning() -> None:
    """The gate warning is logged once per process; reset it so tests are order-independent."""
    delivery._endpoint_gate_warned = False


def test_deliver_report_free_tier_skips_endpoint_but_directory_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[str] = []

    def _fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        return _FakeResponse(200)

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)

    config = ReportDeliveryConfig(directory=str(tmp_path), endpoint="http://localhost:5555/webhook")

    with caplog.at_level(logging.WARNING):
        delivery.deliver_report(
            PAYLOAD,
            FILENAME,
            config,
            session_id="session-1",
            local_report_path=None,
            feature_policy=_StubFeaturePolicy("free"),
        )

    assert calls == []
    assert (tmp_path / FILENAME).read_bytes() == PAYLOAD
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Pro" in warnings[0].message


def test_deliver_report_free_tier_endpoint_warning_logged_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        return _FakeResponse(200)

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)

    config = ReportDeliveryConfig(directory=str(tmp_path), endpoint="http://localhost:5555/webhook")

    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            delivery.deliver_report(
                PAYLOAD,
                FILENAME,
                config,
                session_id="session-1",
                local_report_path=None,
                feature_policy=_StubFeaturePolicy("free"),
            )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_deliver_report_pro_tier_delivers_to_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        return _FakeResponse(200)

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)

    config = ReportDeliveryConfig(directory=None, endpoint="http://localhost:5555/webhook")

    delivery.deliver_report(
        PAYLOAD,
        FILENAME,
        config,
        session_id="session-1",
        local_report_path=None,
        feature_policy=_StubFeaturePolicy("pro"),
    )

    assert calls == ["http://localhost:5555/webhook"]


def test_deliver_report_enterprise_tier_delivers_to_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _fake_urlopen(request: object, timeout: float) -> _FakeResponse:
        calls.append(request.full_url)  # type: ignore[attr-defined]
        return _FakeResponse(200)

    monkeypatch.setattr(delivery.urllib.request, "urlopen", _fake_urlopen)

    config = ReportDeliveryConfig(directory=None, endpoint="http://localhost:5555/webhook")

    delivery.deliver_report(
        PAYLOAD,
        FILENAME,
        config,
        session_id="session-1",
        local_report_path=None,
        feature_policy=_StubFeaturePolicy("enterprise"),
    )

    assert calls == ["http://localhost:5555/webhook"]


# ---------------------------------------------------------------------------
# Both sinks default off / orchestration
# ---------------------------------------------------------------------------


def test_deliver_report_in_background_is_noop_when_unconfigured() -> None:
    config = ReportDeliveryConfig(directory=None, endpoint=None)

    thread = delivery.deliver_report_in_background(
        PAYLOAD, FILENAME, config, session_id="session-1", local_report_path=None
    )

    assert thread is None


def test_deliver_report_in_background_writes_to_directory_when_configured(
    tmp_path: Path,
) -> None:
    config = ReportDeliveryConfig(directory=str(tmp_path), endpoint=None)

    thread = delivery.deliver_report_in_background(
        PAYLOAD, FILENAME, config, session_id="session-1", local_report_path=None
    )

    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert (tmp_path / FILENAME).read_bytes() == PAYLOAD


def test_report_delivery_config_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPORT_DELIVERY_DIR", raising=False)
    monkeypatch.delenv("REPORT_DELIVERY_ENDPOINT", raising=False)

    config = ReportDeliveryConfig.from_env()

    assert config.directory is None
    assert config.endpoint is None


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def test_validate_report_delivery_config_passes_when_unconfigured() -> None:
    delivery.validate_report_delivery_config(ReportDeliveryConfig(directory=None, endpoint=None))


def test_validate_report_delivery_config_rejects_missing_directory(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    config = ReportDeliveryConfig(directory=str(missing), endpoint=None)

    with pytest.raises(delivery.ReportDeliveryConfigError, match=str(missing)):
        delivery.validate_report_delivery_config(config)


def test_validate_report_delivery_config_rejects_file_as_directory(tmp_path: Path) -> None:
    a_file = tmp_path / "not-a-dir"
    a_file.write_text("x", encoding="utf-8")
    config = ReportDeliveryConfig(directory=str(a_file), endpoint=None)

    with pytest.raises(delivery.ReportDeliveryConfigError, match="not a directory"):
        delivery.validate_report_delivery_config(config)


def test_validate_report_delivery_config_accepts_writable_directory(tmp_path: Path) -> None:
    config = ReportDeliveryConfig(directory=str(tmp_path), endpoint=None)

    delivery.validate_report_delivery_config(config)  # must not raise


def test_validate_report_delivery_config_rejects_malformed_endpoint() -> None:
    config = ReportDeliveryConfig(directory=None, endpoint="not-a-url")

    with pytest.raises(delivery.ReportDeliveryConfigError, match="not-a-url"):
        delivery.validate_report_delivery_config(config)


def test_validate_report_delivery_config_accepts_http_endpoint() -> None:
    config = ReportDeliveryConfig(directory=None, endpoint="http://localhost:5678/webhook")

    delivery.validate_report_delivery_config(config)  # must not raise
