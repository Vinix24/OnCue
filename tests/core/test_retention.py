"""Data-retention policy resolution + cutoff."""

from __future__ import annotations

from datetime import UTC, datetime

from sales_copilot.core import retention


def test_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("DATA_RETENTION_DAYS", raising=False)
    assert retention.resolve_retention_days() == 0
    assert retention.retention_cutoff() is None


def test_invalid_value_disables(monkeypatch) -> None:
    monkeypatch.setenv("DATA_RETENTION_DAYS", "not-a-number")
    assert retention.resolve_retention_days() == 0
    assert retention.retention_cutoff() is None


def test_zero_or_negative_disables(monkeypatch) -> None:
    monkeypatch.setenv("DATA_RETENTION_DAYS", "-5")
    assert retention.resolve_retention_days() == 0


def test_cutoff_computed(monkeypatch) -> None:
    monkeypatch.setenv("DATA_RETENTION_DAYS", "30")
    now = datetime(2026, 6, 13, tzinfo=UTC)
    assert retention.retention_cutoff(now) == datetime(2026, 5, 14, tzinfo=UTC)
