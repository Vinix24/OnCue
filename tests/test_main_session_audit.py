"""Session lifecycle audit events flow through the tiered writer."""

from __future__ import annotations

import json

import pytest

import sales_copilot.core.audit_ledger as ledger_module
from sales_copilot import __main__ as main_mod
from sales_copilot.core.audit_ledger import NDJSONAuditWriter


class _RedirectedNDJSON(NDJSONAuditWriter):
    def __init__(self, path=None) -> None:  # noqa: ARG002
        # Real instances write into a temp path set per test via monkeypatch.
        super().__init__(path=_RedirectedNDJSON._test_path / "audit.ndjson")


@pytest.fixture
def _redirected_writer(monkeypatch, tmp_path):
    _RedirectedNDJSON._test_path = tmp_path
    monkeypatch.setattr(ledger_module, "NDJSONAuditWriter", _RedirectedNDJSON)
    yield tmp_path


def test_write_audit_event_appends_to_local_ledger(monkeypatch, _redirected_writer, tmp_path):
    monkeypatch.setenv("AUDIT_BACKEND", "ndjson")
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    main_mod._write_audit_event(
        "session_start",
        {"session_id": "sess-123", "preset": "recruitment"},
    )

    lines = (tmp_path / "audit.ndjson").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event_type"] == "session_start"
    assert record["session_id"] == "sess-123"
    assert record["preset"] == "recruitment"
    assert "ts" in record


def test_write_audit_event_failure_is_non_blocking(monkeypatch, _redirected_writer, caplog):
    monkeypatch.setenv("AUDIT_BACKEND", "ndjson")
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    class _Boom:
        def write(self, record: dict) -> None:
            raise RuntimeError("disk full")

    monkeypatch.setattr(ledger_module, "NDJSONAuditWriter", _Boom)

    # Must not raise.
    main_mod._write_audit_event("session_end", {"session_id": "sess-456"})

    assert "audit write failed (non-blocking)" in caplog.text
