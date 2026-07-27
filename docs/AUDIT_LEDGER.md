# Audit Ledger — AI Act Annex III compliance

## Purpose

The audit ledger records every processing of candidate text in the recruitment profile. This meets the logging requirement of **AI Act Annex III** (high-risk AI systems for recruitment and selection): every decision the AI system makes, including the input that led to that decision, must be traceable.

The ledger is **append-only**: records are never overwritten or deleted without an explicit legal basis (see Retention policy).

---

## Activation

The audit ledger is enabled automatically when the `recruitment` preset is active (`PRESET=recruitment` in `.env`). For the `sales` and `coach` profiles, audit records are **never** written.

No additional configuration is required for basic functionality. The NDJSON backend starts automatically.

---

## Backends

### NDJSON (default)

Local append-only logging to `.vnx-data/audit/recruitment.ndjson`.

Rotation: when the file reaches 10 MB, it is renamed to `recruitment.YYYYMMDDTHHMMSS.ndjson` and a new file begins.

Configuration: no env vars required.

### Supabase (optional)

Central audit log for multi-user deployments. The Supabase backend itself is selectable on all tiers via `AUDIT_BACKEND=supabase` — `get_audit_writer()` (`core/audit_ledger.py:165-193`) has no entitlement gate. Only the central, tamper-evident hash chain (hashes-only ingest to the license endpoint) is a Pro feature. Requires:

```env
AUDIT_BACKEND=supabase
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_KEY=<service_role_key>
```

Table: `recruitment_audit` (create via the Supabase dashboard or a migration).

If env vars are missing, the system falls back to NDJSON — no data is lost.

---

## NDJSON format

Each line is a JSON object. Sample record:

```json
{
  "ts": "2026-05-16T13:16:01.234567+00:00",
  "session_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "event_type": "detection",
  "pii_hits": 1,
  "redacted_len": 9,
  "detection_count": 1,
  "model_id": "gemini-2.5-flash",
  "latency_ms": 287
}
```

Field explanation:

| Field | Type | Description |
|---|---|---|
| `ts` | ISO 8601 UTC | Time of processing |
| `session_id` | UUID | Unique per pipeline instance (= per conversation) |
| `event_type` | string | `detection` (match found) or `text_processed` (no match) |
| `pii_hits` | int | Number of PII patterns found and redacted |
| `redacted_len` | int | Number of redacted characters (original length minus clean length) |
| `detection_count` | int | 1 on match, 0 on no match |
| `model_id` | string | LLM model used for confirmation |
| `latency_ms` | int | Processing time of the pipeline call |

---

## Retention policy

**Statutory retention period:** 6 months minimum for consent-based recruitment processes (GDPR Art. 30 + AI Act processing log).

Practical management:

- NDJSON files rotate automatically at 10 MB
- Rotated files follow the naming convention `recruitment.YYYYMMDDTHHMMSS.ndjson`
- Delete files older than 6 months via a periodic cleanup (cron or manual)

Example cleanup command (files older than 180 days):

```bash
find .vnx-data/audit/ -name "recruitment.*.ndjson" -mtime +180 -delete
```

---

## HMAC signing

### Purpose

Every NDJSON line is given an HMAC-SHA256 signature in the `_hmac` field. This enables tamper detection: if a line is altered after the fact, the signature no longer matches.

### Activation

Set the `AUDIT_HMAC_SECRET` env var to a random secret string (minimum 32 characters recommended):

```env
AUDIT_HMAC_SECRET=<random-secret-string>
```

For the recruitment preset, `AUDIT_HMAC_SECRET` is mandatory: startup fails
closed when the secret is missing. Non-recruitment presets may write records
without a signature; in that case one warning appears in the logs
per writer instance:

```
WARNING AUDIT_HMAC_SECRET not set — audit records are unsigned
```

### Format of signed records

```json
{
  "ts": "2026-05-16T13:16:01.234567+00:00",
  "session_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "event_type": "detection",
  "pii_hits": 1,
  "redacted_len": 9,
  "detection_count": 1,
  "model_id": "gemini-2.5-flash",
  "latency_ms": 287,
  "_hmac": "a3f9d2c1b4e7...64-char-hex..."
}
```

The `_hmac` field is a SHA-256 hex digest over the canonical JSON of all other fields (`sort_keys=True, default=str`).

### Verification

Use `verify_audit_ledger()` from Python:

```python
from pathlib import Path
from sales_copilot.core.audit_ledger import verify_audit_ledger

secret = b"your-secret-string"
verified, tampered, tampered_lines = verify_audit_ledger(
    Path(".vnx-data/audit/recruitment.ndjson"), secret
)
print(f"Verified: {verified}, Tampered: {tampered}, Lines: {tampered_lines}")
```

### Migration

Existing NDJSON files without `_hmac` remain fully readable for
migration. Lines without `_hmac` are skipped during verification (not considered
tampered). Recruitment mode accepts no new unsigned records. After activating
`AUDIT_HMAC_SECRET`, new records are signed immediately.

---

## Right to be forgotten

When a candidate requests deletion of their data (GDPR Art. 17), the associated audit records must be deleted or anonymized.

Identify records via `session_id` (which must be linked to the candidate identity in a separate register outside the ledger).

**Step by step:**

1. Look up `session_id` in the candidate register
2. Grep all NDJSON files for that session_id:
   ```bash
   grep -l '"session_id": "<uuid>"' .vnx-data/audit/*.ndjson
   ```
3. Delete the matching lines (not the whole file) or replace `session_id` with a hash
4. Document the deletion in a separate deletion log

With Supabase:
```sql
DELETE FROM recruitment_audit WHERE session_id = '<uuid>';
```

---

## Supabase table schema

```sql
CREATE TABLE recruitment_audit (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    session_id  UUID NOT NULL,
    event_type  TEXT NOT NULL,
    pii_hits    INTEGER NOT NULL DEFAULT 0,
    redacted_len INTEGER NOT NULL DEFAULT 0,
    detection_count INTEGER NOT NULL DEFAULT 0,
    model_id    TEXT NOT NULL,
    latency_ms  INTEGER NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_recruitment_audit_session ON recruitment_audit (session_id);
CREATE INDEX idx_recruitment_audit_ts ON recruitment_audit (ts);
```
