from __future__ import annotations

import json
import logging
import threading
from unittest.mock import patch

import pytest

import sales_copilot.core.audit_ledger as ledger_module
from sales_copilot.core.audit_ledger import (
    NDJSONAuditWriter,
    SupabaseAuditWriter,
    build_audit_record,
    get_audit_writer,
    verify_audit_ledger,
)
from sales_copilot.core.compliance_audit import TieredAuditWriter


def _sample_record(**overrides) -> dict:
    base = build_audit_record(
        session_id="test-session-001",
        event_type="text_processed",
        pii_hits=0,
        redacted_len=0,
        detection_count=0,
        model_id="gemini-2.5-flash",
        latency_ms=42,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# NDJSON: create and append
# ---------------------------------------------------------------------------


def test_ndjson_create_and_append_three_records(tmp_path):
    path = tmp_path / "audit.ndjson"
    writer = NDJSONAuditWriter(path)

    for i in range(3):
        writer.write(_sample_record(session_id=f"session-{i}"))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert "ts" in parsed
        assert "session_id" in parsed


def test_ndjson_records_are_valid_json_and_chronological(tmp_path):
    path = tmp_path / "audit.ndjson"
    writer = NDJSONAuditWriter(path)

    writer.write(_sample_record())
    writer.write(_sample_record())
    writer.write(_sample_record())

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    timestamps = [json.loads(line)["ts"] for line in lines]
    assert timestamps == sorted(timestamps), "Records must be in chronological order"


# ---------------------------------------------------------------------------
# NDJSON: rotation
# ---------------------------------------------------------------------------


def test_ndjson_rotate_at_threshold(tmp_path):
    path = tmp_path / "audit.ndjson"
    writer = NDJSONAuditWriter(path)

    with patch.object(ledger_module, "_MAX_NDJSON_SIZE", 1):
        writer.write(_sample_record())  # creates file, size > 1
        writer.write(_sample_record())  # triggers rotation before write

    rotated = [f for f in tmp_path.iterdir() if f.name != "audit.ndjson"]
    assert len(rotated) == 1, "Exactly one rotated file expected"
    assert path.exists(), "Fresh file must exist after rotation"

    # rotated file has the first record, current file has the second
    rotated_lines = rotated[0].read_text().strip().splitlines()
    current_lines = path.read_text().strip().splitlines()
    assert len(rotated_lines) == 1
    assert len(current_lines) == 1


# ---------------------------------------------------------------------------
# NDJSON: error handling
# ---------------------------------------------------------------------------


def test_ndjson_malformed_circular_ref_fails_closed(tmp_path, caplog):
    path = tmp_path / "audit.ndjson"
    writer = NDJSONAuditWriter(path)
    record: dict = {"key": "value"}
    record["circular"] = record  # json.dumps raises ValueError

    with caplog.at_level(logging.ERROR, logger="sales_copilot.core.audit_ledger"):
        with pytest.raises(ValueError):
            writer.write(record)

    assert "AuditLedger NDJSON write failed" in caplog.text


# ---------------------------------------------------------------------------
# NDJSON: concurrent writes
# ---------------------------------------------------------------------------


def test_ndjson_concurrent_writes(tmp_path):
    path = tmp_path / "audit.ndjson"
    writer = NDJSONAuditWriter(path)
    total_records = 200
    threads_count = 2
    per_thread = total_records // threads_count
    errors: list[Exception] = []

    def write_batch() -> None:
        try:
            for _ in range(per_thread):
                writer.write(_sample_record())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write_batch) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Thread errors: {errors}"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == total_records
    for line in lines:
        json.loads(line)  # every line must be valid JSON


# ---------------------------------------------------------------------------
# Supabase: fail-fast
# ---------------------------------------------------------------------------


def test_supabase_fails_without_env_vars(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        SupabaseAuditWriter()


def test_supabase_fails_with_url_but_no_key(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SUPABASE_URL"):
        SupabaseAuditWriter()


# ---------------------------------------------------------------------------
# Supabase: HMAC signing parity with the NDJSON writer
# ---------------------------------------------------------------------------


class _FakeInsert:
    def __init__(self, capture: list[dict], record: dict) -> None:
        self._capture = capture
        self._record = record

    def execute(self) -> dict:
        self._capture.append(self._record)
        return {"data": [self._record]}


class _FakeTable:
    def __init__(self, capture: list[dict]) -> None:
        self._capture = capture

    def insert(self, record: dict) -> _FakeInsert:
        return _FakeInsert(self._capture, record)


class _FakeSupabaseClient:
    def __init__(self, capture: list[dict]) -> None:
        self._capture = capture

    def table(self, _name: str) -> _FakeTable:
        return _FakeTable(self._capture)


def _make_supabase_writer(monkeypatch, capture: list[dict]) -> SupabaseAuditWriter:
    """Build a SupabaseAuditWriter backed by a fake client (no network, no supabase-py)."""
    import sys
    import types

    fake_module = types.ModuleType("supabase")
    fake_module.create_client = lambda _url, _key: _FakeSupabaseClient(capture)
    monkeypatch.setitem(sys.modules, "supabase", fake_module)
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    return SupabaseAuditWriter()


def test_supabase_writer_hmac_signs_when_secret_set(monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "test-secret-key")
    capture: list[dict] = []
    writer = _make_supabase_writer(monkeypatch, capture)

    writer.write(_sample_record())

    assert len(capture) == 1
    inserted = capture[0]
    assert "_hmac" in inserted
    assert len(inserted["_hmac"]) == 64  # SHA-256 hex digest


def test_supabase_writer_no_hmac_when_secret_missing(monkeypatch, caplog):
    monkeypatch.delenv("AUDIT_HMAC_SECRET", raising=False)
    capture: list[dict] = []
    writer = _make_supabase_writer(monkeypatch, capture)

    with caplog.at_level(logging.WARNING, logger="sales_copilot.core.audit_ledger"):
        writer.write(_sample_record())
        writer.write(_sample_record())  # second write must not warn again

    assert len(capture) == 2
    assert all("_hmac" not in row for row in capture)
    assert caplog.text.count("AUDIT_HMAC_SECRET not set") == 1  # warn-once guard


def test_supabase_writer_signature_verifies_against_inserted_record(monkeypatch):
    import hmac as _hmac

    from sales_copilot.core.audit_ledger import _sign_record

    monkeypatch.setenv("AUDIT_HMAC_SECRET", "verify-secret")
    capture: list[dict] = []
    writer = _make_supabase_writer(monkeypatch, capture)

    writer.write(_sample_record())

    inserted = capture[0]
    stored = inserted["_hmac"]
    check_record = {k: v for k, v in inserted.items() if k != "_hmac"}
    expected = _sign_record(check_record, b"verify-secret")
    assert _hmac.compare_digest(stored, expected)


# ---------------------------------------------------------------------------
# Factory: fallback behaviour
# ---------------------------------------------------------------------------


def test_factory_falls_back_to_ndjson_when_supabase_fails(monkeypatch):
    monkeypatch.setenv("AUDIT_BACKEND", "supabase")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    writer = get_audit_writer()
    assert isinstance(writer, TieredAuditWriter)
    assert isinstance(writer._local, NDJSONAuditWriter)


def test_get_audit_writer_ndjson_default(monkeypatch):
    monkeypatch.delenv("AUDIT_BACKEND", raising=False)
    writer = get_audit_writer()
    assert isinstance(writer, TieredAuditWriter)
    assert isinstance(writer._local, NDJSONAuditWriter)


def test_get_audit_writer_ndjson_explicit(monkeypatch):
    monkeypatch.setenv("AUDIT_BACKEND", "ndjson")
    writer = get_audit_writer()
    assert isinstance(writer, TieredAuditWriter)
    assert isinstance(writer._local, NDJSONAuditWriter)


def test_get_audit_writer_invalid_backend_falls_back_to_ndjson(monkeypatch):
    monkeypatch.setenv("AUDIT_BACKEND", "kafka")  # unknown backend
    writer = get_audit_writer()
    assert isinstance(writer, TieredAuditWriter)
    assert isinstance(writer._local, NDJSONAuditWriter)


def test_required_recruitment_audit_rejects_missing_hmac(monkeypatch):
    monkeypatch.setenv("AUDIT_BACKEND", "ndjson")
    monkeypatch.delenv("AUDIT_HMAC_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="AUDIT_HMAC_SECRET"):
        get_audit_writer(require_hmac=True)


# ---------------------------------------------------------------------------
# build_audit_record
# ---------------------------------------------------------------------------


def test_build_audit_record_all_fields_present():
    record = build_audit_record(
        session_id="abc-123",
        event_type="detection",
        pii_hits=2,
        redacted_len=15,
        detection_count=1,
        model_id="gemini-2.5-flash",
        latency_ms=350,
    )
    expected_keys = {
        "ts",
        "session_id",
        "event_type",
        "pii_hits",
        "redacted_len",
        "detection_count",
        "model_id",
        "latency_ms",
    }
    assert expected_keys == record.keys()
    assert record["session_id"] == "abc-123"
    assert record["event_type"] == "detection"
    assert record["pii_hits"] == 2
    assert record["detection_count"] == 1


# ---------------------------------------------------------------------------
# HMAC signing
# ---------------------------------------------------------------------------


def test_ndjson_writer_hmac_signs_when_secret_set(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "test-secret-key")
    path = tmp_path / "audit.ndjson"
    writer = NDJSONAuditWriter(path)
    writer.write(_sample_record())

    line = json.loads(path.read_text().strip())
    assert "_hmac" in line
    assert len(line["_hmac"]) == 64  # SHA-256 hex digest = 64 chars


def test_ndjson_writer_no_hmac_when_secret_missing(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("AUDIT_HMAC_SECRET", raising=False)
    path = tmp_path / "audit.ndjson"
    writer = NDJSONAuditWriter(path)

    with caplog.at_level(logging.WARNING, logger="sales_copilot.core.audit_ledger"):
        writer.write(_sample_record())

    line = json.loads(path.read_text().strip())
    assert "_hmac" not in line
    assert "AUDIT_HMAC_SECRET" in caplog.text


def test_verify_audit_ledger_detects_tamper(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_HMAC_SECRET", "test-secret-key")
    path = tmp_path / "audit.ndjson"
    writer = NDJSONAuditWriter(path)
    writer.write(_sample_record())
    writer.write(_sample_record())

    lines = path.read_text().splitlines()
    record = json.loads(lines[1])
    record["detection_count"] = 999  # tamper with second record
    lines[1] = json.dumps(record)
    path.write_text("\n".join(lines) + "\n")

    verified, tampered, tampered_lines = verify_audit_ledger(path, b"test-secret-key")
    assert verified == 1
    assert tampered == 1
    assert tampered_lines == [2]


def test_verify_audit_ledger_handles_mixed_legacy_lines(tmp_path, monkeypatch):
    path = tmp_path / "audit.ndjson"
    legacy = _sample_record()
    path.write_text(json.dumps(legacy) + "\n")  # unsigned legacy record

    monkeypatch.setenv("AUDIT_HMAC_SECRET", "test-secret")
    writer = NDJSONAuditWriter(path)
    writer.write(_sample_record())  # signed record appended

    verified, tampered, tampered_lines = verify_audit_ledger(path, b"test-secret")
    assert verified == 1  # only the signed line counts
    assert tampered == 0
    assert tampered_lines == []


def test_verify_audit_ledger_constant_time_compare():
    import inspect

    source = inspect.getsource(verify_audit_ledger)
    assert "compare_digest" in source, "Must use hmac.compare_digest for constant-time comparison"


# ---------------------------------------------------------------------------
# build_audit_record
# ---------------------------------------------------------------------------


def test_build_audit_record_timestamp_is_utc():
    from datetime import datetime

    record = build_audit_record(
        session_id="s",
        event_type="text_processed",
        pii_hits=0,
        redacted_len=0,
        detection_count=0,
        model_id="m",
        latency_ms=0,
    )
    ts = datetime.fromisoformat(record["ts"])
    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 0, "Timestamp must be UTC"
