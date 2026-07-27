"""Email capture endpoint — write leads to NDJSON, issue temp license keys."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, field_validator

from sales_copilot.auth.license_key import LICENSE_SECRET_ENV, generate_key
from sales_copilot.core.paths import resolve_app_path

LEADS_FILE = resolve_app_path(".vnx-data/leads.ndjson")

# Max length of an email address per RFC 5321 (forward-path limit). Reject longer
# input before it ever reaches storage or key issuance.
_MAX_EMAIL_LEN = 254

# Per-IP token-bucket rate limit for the unauthenticated lead/license endpoint.
# Localhost-only today, but the endpoint is public-facing by design, so it is
# rate-limited at the process level. Override the window with LEAD_RATE_LIMIT_PER_MIN.
_LEAD_RATE_WINDOW_S = 60.0

_PLACEHOLDER_SECRETS = frozenset({
    "change-me-32-chars-min-placeholder",
    "<GENERATE_ON_FIRST_RUN>",
})


def _secret() -> str:
    # LEGACY: the HMAC secret is only required to mint legacy ``SC-`` free keys in
    # capture_lead(). Ed25519 ``SCP-`` validation needs no client secret. This
    # issuance path sunsets 2026-09-01 — do not add new callers.
    secret = os.environ.get(LICENSE_SECRET_ENV, "").strip()
    if not secret or secret in _PLACEHOLDER_SECRETS:
        raise RuntimeError(
            f"{LICENSE_SECRET_ENV} is not set or uses the default placeholder. "
            "Generate a secure secret with: python scripts/generate_secrets.py"
        )
    return secret


class _TokenBucket:
    """In-process per-key token bucket. Best-effort, thread-safe.

    Refills ``max_tokens`` over ``window_s``; ``allow`` consumes one token and
    returns ``False`` when the bucket is empty (rate exceeded).
    """

    def __init__(self, max_tokens: int, window_s: float) -> None:
        self._max = float(max(1, max_tokens))
        self._refill_per_s = self._max / window_s
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, *, now: float | None = None) -> bool:
        ts = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._buckets.get(key, (self._max, ts))
            tokens = min(self._max, tokens + max(0.0, ts - last) * self._refill_per_s)
            if tokens < 1.0:
                self._buckets[key] = (tokens, ts)
                return False
            self._buckets[key] = (tokens - 1.0, ts)
            return True


def _lead_rate_limit() -> int:
    raw = os.environ.get("LEAD_RATE_LIMIT_PER_MIN", "10").strip()
    try:
        value = int(raw)
    except ValueError:
        return 10
    return value if value > 0 else 10


_lead_rate_limiter = _TokenBucket(_lead_rate_limit(), _LEAD_RATE_WINDOW_S)


def check_lead_rate_limit(ip: str) -> bool:
    """Return ``True`` when a lead/license request from ``ip`` is allowed."""
    return _lead_rate_limiter.allow(ip or "unknown")


class LeadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    source: str = "dashboard"
    utm_source: str | None = None

    @field_validator("email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if len(v) > _MAX_EMAIL_LEN:
            raise ValueError(f"email too long (max {_MAX_EMAIL_LEN} chars)")
        if not v or "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError(f"invalid email: {v!r}")
        return v


class LeadResponse(BaseModel):
    status: str
    message: str
    license_key: str | None = None


def _read_existing_emails() -> set[str]:
    if not LEADS_FILE.exists():
        return set()
    emails: set[str] = set()
    try:
        for line in LEADS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if isinstance(record.get("email"), str):
                    emails.add(record["email"].strip().lower())
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return emails


def _append_lead(record: dict) -> None:
    LEADS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        LEADS_FILE.parent.chmod(stat.S_IRWXU)
    except OSError:
        pass
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    # Atomic append: a single os.write under O_APPEND is atomic on POSIX, so
    # concurrent writers cannot interleave or tear a record. 0o600 is applied on
    # creation and re-asserted so the leads file stays owner-only.
    fd = os.open(LEADS_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)
    try:
        LEADS_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


async def capture_lead(request: LeadRequest) -> LeadResponse:
    """Write lead to NDJSON. Idempotent — same email returns existing key."""
    email = request.email  # already validated+normalized by pydantic
    existing = _read_existing_emails()

    if email in existing:
        return LeadResponse(
            status="exists",
            message="Email already registered. Check your inbox for your license key.",
            license_key=None,
        )

    now = datetime.now(UTC)
    tier = "free"
    expires_at = now + timedelta(days=30)

    secret = _secret()
    key = generate_key(email, tier, secret, expires_at=expires_at, issued_at=now)

    record = {
        "email": email,
        "tier": tier,
        "source": request.source,
        "utm_source": request.utm_source,
        "license_key": key,
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "created_at": now.isoformat(),
    }
    _append_lead(record)

    return LeadResponse(
        status="ok",
        message=(
            f"License key issued for {email}. "
            "Set SALES_COPILOT_LICENSE in your .env to activate."
        ),
        license_key=key,
    )
