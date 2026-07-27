"""Audit-ledger voor AI Act Annex III compliance (recruitment profile).

Append-only audit trail per detection-batch. Default NDJSON lokaal,
optioneel Supabase voor multi-user / centralized audit.

Geactiveerd via env AUDIT_BACKEND=ndjson|supabase (default ndjson).
HMAC-signing via AUDIT_HMAC_SECRET env-var (SHA-256, per record).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sales_copilot.core.paths import resolve_app_path

logger = logging.getLogger(__name__)

_MAX_NDJSON_SIZE = 10 * 1024 * 1024  # 10 MB rotation threshold


def _sign_record(record: dict, secret: bytes) -> str:
    """HMAC-SHA256 over canonicalized JSON (sort_keys, default=str)."""
    payload = json.dumps(record, sort_keys=True, default=str).encode()
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _sign_or_warn(record: dict, warned: bool) -> tuple[dict, bool]:
    """Sign a record with AUDIT_HMAC_SECRET, or warn once when the secret is absent.

    Shared by every audit-writer backend so the tamper-evidence behaviour cannot
    drift per backend. When the secret is set, returns a shallow copy carrying
    ``_hmac`` (signed without the ``_hmac`` key, matching ``verify_audit_ledger``).
    When absent, returns the record unchanged and warns once per writer instance.

    Returns ``(record, warned)`` — callers persist the new ``warned`` flag.
    """
    secret_str = os.environ.get("AUDIT_HMAC_SECRET")
    if secret_str:
        signed = dict(record)  # shallow copy — don't mutate caller's dict
        signed["_hmac"] = _sign_record(signed, secret_str.encode())
        return signed, warned
    if not warned:
        logger.warning("AUDIT_HMAC_SECRET not set — audit records are unsigned")
        warned = True
    return record, warned


def verify_audit_ledger(
    path: Path, secret: bytes
) -> tuple[int, int, list[int]]:
    """Verify HMAC signatures in an NDJSON audit file.

    Returns (verified_count, tampered_count, tampered_line_numbers).
    Lines without _hmac (legacy) are skipped — not counted as tampered.
    """
    verified = 0
    tampered = 0
    tampered_lines: list[int] = []

    with path.open(encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                tampered += 1
                tampered_lines.append(lineno)
                continue

            stored_hmac = record.get("_hmac")
            if stored_hmac is None:
                continue  # legacy record — no signature to verify

            check_record = {k: v for k, v in record.items() if k != "_hmac"}
            expected = _sign_record(check_record, secret)

            if hmac.compare_digest(stored_hmac, expected):
                verified += 1
            else:
                tampered += 1
                tampered_lines.append(lineno)

    return (verified, tampered, tampered_lines)


class AuditWriter(Protocol):
    """Audit-writer protocol. Implementations: NDJSON, Supabase."""

    def write(self, record: dict) -> None: ...


class NDJSONAuditWriter:
    """Append-only NDJSON writer. Rotates at 10 MB. Signs with HMAC when AUDIT_HMAC_SECRET is set."""

    def __init__(self, path: Path | str = "") -> None:
        self.path = Path(path) if path else resolve_app_path(".vnx-data/audit/recruitment.ndjson")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(stat.S_IRWXU)
        except OSError:
            pass
        self._lock = threading.Lock()
        self._hmac_warned = False

    def write(self, record: dict) -> None:
        try:
            with self._lock:
                self._rotate_if_needed()
                record, self._hmac_warned = _sign_or_warn(record, self._hmac_warned)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, default=str) + "\n")
                self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except Exception as exc:
            logger.error("AuditLedger NDJSON write failed: %s", exc)
            raise

    def _rotate_if_needed(self) -> None:
        if self.path.exists() and self.path.stat().st_size >= _MAX_NDJSON_SIZE:
            rotated = self.path.with_suffix(
                f".{datetime.now(UTC):%Y%m%dT%H%M%S}.ndjson"
            )
            self.path.rename(rotated)


class SupabaseAuditWriter:
    """Optional Supabase writer. Fail-fast naar NDJSON als config ontbreekt."""

    def __init__(self) -> None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if not (url and key):
            raise RuntimeError(
                "SupabaseAuditWriter requires SUPABASE_URL and SUPABASE_KEY env. "
                "Falling back to NDJSONAuditWriter."
            )
        try:
            from supabase import create_client
        except ImportError as exc:
            raise RuntimeError(
                "supabase-py not installed. Run: pip install supabase"
            ) from exc
        self._client = create_client(url, key)
        self._table = "recruitment_audit"
        self._hmac_warned = False

    def write(self, record: dict) -> None:
        try:
            record, self._hmac_warned = _sign_or_warn(record, self._hmac_warned)
            self._client.table(self._table).insert(record).execute()
        except Exception as exc:
            logger.error("AuditLedger Supabase write failed: %s", exc)
            raise


def get_audit_writer(*, require_hmac: bool = False) -> AuditWriter:
    """Factory. Returns a TieredAuditWriter wrapping NDJSON by default,
    Supabase if configured.

    The wrapped writer always persists locally. When the active license is
    entitled to ``compliance.central_audit`` (Pro/Enterprise), the
    TieredAuditWriter additionally ships a client-side SHA-256 hash of each
    record to the central ``/audit/ingest`` endpoint — never the raw record or
    any PII.
    """
    backend = os.environ.get("AUDIT_BACKEND", "ndjson").lower()
    local: AuditWriter
    if backend == "supabase":
        try:
            local = SupabaseAuditWriter()
        except RuntimeError as exc:
            logger.warning("Supabase audit unavailable, fallback to NDJSON: %s", exc)
            local = NDJSONAuditWriter()
    else:
        local = NDJSONAuditWriter()

    if require_hmac and not os.environ.get("AUDIT_HMAC_SECRET", "").strip():
        raise RuntimeError("AUDIT_HMAC_SECRET is required for recruitment audit")

    # Lazy import avoids a circular dependency: compliance_audit imports
    # AuditWriter from this module.
    from sales_copilot.core.compliance_audit import TieredAuditWriter

    return TieredAuditWriter(local)


def build_audit_record(
    *,
    session_id: str,
    event_type: str,
    pii_hits: int,
    redacted_len: int,
    detection_count: int,
    model_id: str,
    latency_ms: int,
) -> dict:
    """Build single audit-record. Format: AI Act Annex III compatible."""
    return {
        "ts": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "event_type": event_type,
        "pii_hits": pii_hits,
        "redacted_len": redacted_len,
        "detection_count": detection_count,
        "model_id": model_id,
        "latency_ms": latency_ms,
    }
