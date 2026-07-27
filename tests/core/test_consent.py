"""Non-blocking per-session consent recording."""

from __future__ import annotations

from datetime import UTC, datetime

from sales_copilot.core import consent


class _CaptureWriter:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


def test_records_consent_when_enabled(monkeypatch) -> None:
    monkeypatch.delenv("CONSENT_TRACKING_ENABLED", raising=False)
    writer = _CaptureWriter()

    record = consent.record_consent(
        "sess-1",
        asked=True,
        given=True,
        writer=writer,
        ts=datetime(2026, 6, 13, tzinfo=UTC),
    )

    assert record is not None
    assert writer.records == [record]
    assert record["event_type"] == "consent"
    assert record["session_id"] == "sess-1"
    assert record["consent_asked"] is True
    assert record["consent_given"] is True


def test_disabled_records_nothing(monkeypatch) -> None:
    monkeypatch.setenv("CONSENT_TRACKING_ENABLED", "false")
    writer = _CaptureWriter()

    assert consent.record_consent("s", asked=True, given=False, writer=writer) is None
    assert writer.records == []


def test_write_failure_is_non_blocking(monkeypatch) -> None:
    monkeypatch.delenv("CONSENT_TRACKING_ENABLED", raising=False)

    class _Boom:
        def write(self, record: dict) -> None:
            raise RuntimeError("disk full")

    # Must not raise — consent recording can never block a call.
    assert consent.record_consent("s", asked=True, given=True, writer=_Boom()) is None
