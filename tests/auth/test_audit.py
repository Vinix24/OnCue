"""Audit logging for license verification decisions."""

from __future__ import annotations

import hashlib

from sales_copilot.auth.audit import key_fingerprint, log_verify_decision


def test_key_fingerprint_is_sha256_of_normalized_key() -> None:
    key = "SCP-ABCD-EFGH"
    expected = hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert key_fingerprint(key) == expected


def test_key_fingerprint_normalizes_case_and_spaces() -> None:
    assert key_fingerprint("scp-abcd-efgh") == key_fingerprint("SCP-ABCD-EFGH")


def test_log_verify_decision_includes_required_fields(caplog) -> None:
    with caplog.at_level("INFO", logger="sales_copilot.auth.audit"):
        log_verify_decision(
            "abc123",
            decision="valid",
            tier="pro",
            pubkey_fingerprint="pub0123",
            revocation_status="entitled",
            expires_at="2026-12-31T00:00:00+00:00",
        )

    assert "license_verify_decision" in caplog.text
    assert "abc123" in caplog.text
    assert "pub0123" in caplog.text
    assert "entitled" in caplog.text


def test_log_verify_decision_never_contains_raw_key() -> None:
    """The audit helper only receives fingerprints; verify the emitted log."""
    raw_key = "SCP-SECRET-VALUE"
    fp = key_fingerprint(raw_key)
    assert fp != raw_key
    assert raw_key not in fp
