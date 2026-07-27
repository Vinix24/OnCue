"""Tiered audit writer: local always, central tamper-evident hash only when Pro."""

from __future__ import annotations

import json

import sales_copilot.core.audit_ledger as ledger_module
import sales_copilot.core.compliance_audit as compliance_audit
from sales_copilot.core.audit_ledger import NDJSONAuditWriter, get_audit_writer
from sales_copilot.core.compliance_audit import TieredAuditWriter, payload_hash


class _Local:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


class _Policy:
    def __init__(self, allowed: bool) -> None:
        self._allowed = allowed

    def allows(self, feature_id: str) -> bool:
        return self._allowed


def test_free_writes_local_only(monkeypatch) -> None:
    posted: list[str] = []
    monkeypatch.setattr(
        compliance_audit, "_post_hash", lambda h, k, u: posted.append(h) or True
    )
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SCP-anything")
    local = _Local()

    TieredAuditWriter(local, feature_policy=_Policy(False)).write({"event_type": "x"})

    assert len(local.records) == 1
    assert posted == []  # free tier never phones the central audit chain


def test_pro_ships_only_the_hash(monkeypatch) -> None:
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        compliance_audit, "_post_hash", lambda h, k, u: posted.append((h, k)) or True
    )
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SCP-key")
    local = _Local()
    record = {"event_type": "pain_point", "name": "Jan de Vries"}

    TieredAuditWriter(local, feature_policy=_Policy(True)).write(record)

    assert len(local.records) == 1  # full record (with PII) stays local
    assert len(posted) == 1
    assert posted[0][0] == payload_hash(record)  # only the hash leaves
    assert posted[0][1] == "SCP-key"


def test_pro_central_failure_does_not_break_local(monkeypatch) -> None:
    def _boom(hash_hex: str, key: str, base_url: str) -> bool:
        raise RuntimeError("network down")

    monkeypatch.setattr(compliance_audit, "_post_hash", _boom)
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SCP-key")
    local = _Local()

    # Must not raise — local audit is authoritative.
    TieredAuditWriter(local, feature_policy=_Policy(True)).write({"event_type": "x"})
    assert len(local.records) == 1


# ---------------------------------------------------------------------------
# Wired flow: get_audit_writer() returns a TieredAuditWriter
# ---------------------------------------------------------------------------


def _redirect_ndjson(tmp_path):
    """Return an NDJSONAuditWriter subclass that writes into tmp_path."""

    class _Redirected(NDJSONAuditWriter):
        def __init__(self, path=None) -> None:  # noqa: ARG002
            super().__init__(path=tmp_path / "audit.ndjson")

    return _Redirected


def test_wired_free_writes_local_only_no_post(monkeypatch, tmp_path) -> None:
    """Free tier: local NDJSON is written, no central POST is attempted."""
    monkeypatch.setattr(
        ledger_module, "NDJSONAuditWriter", _redirect_ndjson(tmp_path)
    )
    monkeypatch.setattr(
        compliance_audit, "get_feature_policy", lambda: _Policy(False)
    )
    posted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        compliance_audit, "_post_hash", lambda h, k, u: posted.append((h, k, u)) or True
    )
    monkeypatch.setenv("AUDIT_BACKEND", "ndjson")
    monkeypatch.delenv("SALES_COPILOT_LICENSE", raising=False)

    writer = get_audit_writer()
    assert isinstance(writer, TieredAuditWriter)
    writer.write({"event_type": "consent", "session_id": "s1"})

    assert len(posted) == 0
    lines = (tmp_path / "audit.ndjson").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "consent"


def test_wired_pro_posts_hash_only(monkeypatch, tmp_path) -> None:
    """Pro with central_audit: local record is kept, only a SHA-256 hash is posted."""
    monkeypatch.setattr(
        ledger_module, "NDJSONAuditWriter", _redirect_ndjson(tmp_path)
    )
    monkeypatch.setattr(
        compliance_audit, "get_feature_policy", lambda: _Policy(True)
    )
    posted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        compliance_audit, "_post_hash", lambda h, k, u: posted.append((h, k, u)) or True
    )
    monkeypatch.setenv("AUDIT_BACKEND", "ndjson")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SCP-pro-key")

    writer = get_audit_writer()
    assert isinstance(writer, TieredAuditWriter)
    record = {"event_type": "session_start", "session_id": "s2", "name": "Jan de Vries"}
    writer.write(record)

    # Local record is authoritative and still contains PII.
    lines = (tmp_path / "audit.ndjson").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    local = json.loads(lines[0])
    assert local["event_type"] == "session_start"
    assert local["name"] == "Jan de Vries"

    # Central path only saw a hash + auth metadata.
    assert len(posted) == 1
    hash_hex, key, base_url = posted[0]
    assert hash_hex == payload_hash(record)
    assert len(hash_hex) == 64  # SHA-256 hex digest
    assert key == "SCP-pro-key"
    assert base_url == "https://license.salescopilot.app"


def test_wired_pro_central_down_keeps_local_and_logs(monkeypatch, tmp_path, caplog) -> None:
    """A failed central POST must not break the local audit write or raise."""
    monkeypatch.setattr(
        ledger_module, "NDJSONAuditWriter", _redirect_ndjson(tmp_path)
    )
    monkeypatch.setattr(
        compliance_audit, "get_feature_policy", lambda: _Policy(True)
    )

    def _failing_post(hash_hex: str, key: str, base_url: str) -> bool:
        return False

    monkeypatch.setattr(compliance_audit, "_post_hash", _failing_post)
    monkeypatch.setenv("AUDIT_BACKEND", "ndjson")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SCP-pro-key")

    writer = get_audit_writer()
    record = {"event_type": "session_end", "session_id": "s3"}

    with caplog.at_level("WARNING", logger="sales_copilot.core.compliance_audit"):
        writer.write(record)

    lines = (tmp_path / "audit.ndjson").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_type"] == "session_end"
    assert "central audit hash post failed (non-blocking)" in caplog.text


def test_wired_post_payload_never_contains_pii(monkeypatch, tmp_path) -> None:
    """The actual JSON body sent to /audit/ingest contains no raw record fields."""
    monkeypatch.setattr(
        ledger_module, "NDJSONAuditWriter", _redirect_ndjson(tmp_path)
    )
    monkeypatch.setattr(
        compliance_audit, "get_feature_policy", lambda: _Policy(True)
    )
    bodies: list[bytes] = []

    def _capture_body(hash_hex: str, key: str, base_url: str) -> bool:
        bodies.append(compliance_audit._request_body(hash_hex))
        return True

    monkeypatch.setattr(compliance_audit, "_post_hash", _capture_body)
    monkeypatch.setenv("AUDIT_BACKEND", "ndjson")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "SCP-pro-key")

    writer = get_audit_writer()
    record = {
        "event_type": "consent",
        "session_id": "s4",
        "transcript": "Bel me niet meer, mijn nummer is 0612345678",
    }
    writer.write(record)

    assert len(bodies) == 1
    body = json.loads(bodies[0])
    assert set(body.keys()) == {"payload_hash"}
    assert len(body["payload_hash"]) == 64
    assert "transcript" not in body
    assert "0612345678" not in bodies[0].decode("utf-8")
