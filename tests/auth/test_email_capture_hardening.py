"""Lead/license endpoint hardening (#22, #29).

#22 — email length cap (RFC 5321 max 254) and per-IP token-bucket rate limit.
#29 — atomic O_APPEND lead writes produce well-formed NDJSON under concurrency.
"""

from __future__ import annotations

import json
import stat
import threading

import pytest
from pydantic import ValidationError

from sales_copilot.auth import email_capture
from sales_copilot.auth.email_capture import LeadRequest, _TokenBucket

# --- #22 email length cap --------------------------------------------------


def test_overlong_email_rejected() -> None:
    long_email = "a" * 250 + "@example.com"  # 262 chars > 254
    assert len(long_email) > email_capture._MAX_EMAIL_LEN
    with pytest.raises(ValidationError):
        LeadRequest(email=long_email)


def test_email_at_limit_accepted() -> None:
    local = "a" * (email_capture._MAX_EMAIL_LEN - len("@example.com"))
    email = f"{local}@example.com"
    assert len(email) == email_capture._MAX_EMAIL_LEN
    assert LeadRequest(email=email).email == email


# --- #22 token-bucket rate limit -------------------------------------------


def test_rate_limit_trips_after_n() -> None:
    bucket = _TokenBucket(3, 60.0)
    assert bucket.allow("1.2.3.4", now=0.0)
    assert bucket.allow("1.2.3.4", now=0.0)
    assert bucket.allow("1.2.3.4", now=0.0)
    assert bucket.allow("1.2.3.4", now=0.0) is False  # 4th in-window denied


def test_rate_limit_is_per_key() -> None:
    bucket = _TokenBucket(1, 60.0)
    assert bucket.allow("a", now=0.0)
    assert bucket.allow("a", now=0.0) is False
    assert bucket.allow("b", now=0.0)  # different IP unaffected


def test_rate_limit_refills_over_time() -> None:
    bucket = _TokenBucket(2, 60.0)
    assert bucket.allow("x", now=0.0)
    assert bucket.allow("x", now=0.0)
    assert bucket.allow("x", now=0.0) is False
    # refill rate is 2/60 tokens/s → ~1 token back after 30s.
    assert bucket.allow("x", now=30.0)


def test_public_rate_limit_helper_returns_bool() -> None:
    assert isinstance(email_capture.check_lead_rate_limit("9.9.9.9"), bool)


# --- #29 atomic append -----------------------------------------------------


def test_atomic_append_no_torn_lines_under_concurrency(tmp_path, monkeypatch) -> None:
    leads = tmp_path / "leads.ndjson"
    monkeypatch.setattr(email_capture, "LEADS_FILE", leads)

    n_threads = 8
    per_thread = 25

    def worker(tid: int) -> None:
        for i in range(per_thread):
            email_capture._append_lead(
                {"email": f"user{tid}-{i}@example.com", "tier": "free"}
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = leads.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * per_thread
    for line in lines:
        record = json.loads(line)  # raises if a line was torn/interleaved
        assert record["email"].endswith("@example.com")


def test_atomic_append_keeps_file_owner_only(tmp_path, monkeypatch) -> None:
    leads = tmp_path / "leads.ndjson"
    monkeypatch.setattr(email_capture, "LEADS_FILE", leads)

    email_capture._append_lead({"email": "owner@example.com", "tier": "free"})

    assert stat.S_IMODE(leads.stat().st_mode) == 0o600
