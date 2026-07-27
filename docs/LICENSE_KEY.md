# License Key System

## Purpose

The license key gate serves two goals: collecting leads (email capture) and gating Pro-tier features. The free tier runs fully without a key for 14 days; after that a key is required. Pro-tier features unlock when a `pro` or `enterprise` key is present.

## Key Formats

Two formats coexist until the migration deadline **2026-09-01**.

### SCP- keys (primary, Ed25519 asymmetric)

```
SCP-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX
```

RFC 4648 base32, uppercase, no padding, grouped every 4 characters with `-`, prefixed `SCP-`. The binary payload is 91 bytes with the following layout:

| Offset | Length | Field | Notes |
|--------|--------|-------|-------|
| 0 | 1 byte | `version` | `0x01` |
| 1 | 1 byte | `tier` | `0`=free, `1`=pro, `2`=enterprise |
| 2 | 1 byte | `rotation_epoch` | Selects which embedded public key verifies the key |
| 3–6 | 4 bytes | `issued_at` | Big-endian uint32 Unix seconds |
| 7–10 | 4 bytes | `expires_at` | Big-endian uint32 Unix seconds |
| 11–26 | 16 bytes | `license_id` | Random 128-bit pseudonymous identifier |
| 27–90 | 64 bytes | `signature` | Ed25519 signature over bytes 0–26 |

The signed payload is bytes `[0:27]`; the signature covers exactly that slice. Verification uses an Ed25519 public key embedded in client source (`PUBKEY_CURRENT` in `auth/license_verifier.py`). The private key exists only on the license server — there is no way to mint a valid `SCP-` key locally.

Production keys are issued by the license server after a verified purchase. The license server (Cloudflare Worker) is live for pilot key issuance with the full double-opt-in flow (request → confirm → key → seat). The paid checkout flow has not yet been production-tested; during the pilot, all keys are free.

### SC- keys (legacy, HMAC-SHA256 — deprecated, accepted until 2026-09-01)

```
SC-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX
```

Seven groups of four base32 characters. 17-byte binary payload: `tier_byte (1) + issued_at (4) + expires_at (4) + HMAC-SHA256[:8] (8)`. The HMAC is keyed with `SALES_COPILOT_LICENSE_SECRET`.

This format is deprecated because anyone who holds the secret can mint Pro-tier keys. After `2026-09-01` the verifier rejects all `SC-` keys unconditionally. If you hold an `SC-` key, request a replacement `SCP-` key before that date.

## How Verification Works

`decode_and_verify(key)` in `auth/license_verifier.py` handles both formats:

- **`SCP-` path:** Strips prefix, base32-decodes, checks version byte, looks up the public key for the key's `rotation_epoch`, verifies the Ed25519 signature, checks expiry. Fully offline — no network call at verification time.
- **`SC-` path:** Delegates to the legacy HMAC validator. Requires `SALES_COPILOT_LICENSE_SECRET` in the environment. Rejected outright after `2026-09-01`.

Both paths return a `LicenseKey` object with `.tier`, `.issued_at`, `.expires_at`, and — for `SCP-` keys — a hex `.license_id`. Legacy `SC-` keys leave `.license_id` empty.

## Tiers and Feature Access

Feature access is resolved by `FeaturePolicy` in `auth/feature_policy.py`. Call `get_feature_policy().allows(feature_id)` to check access.

The full Free/Pro/Enterprise feature table is canonical in `docs/contracts/LICENSE_PRO_CONTRACT.md` (source of truth: `license_format.py`).

The legacy `coaching.script_tracking` flag is still granted to Pro/Enterprise and expands to both `coaching.script_tracking.compute` and `coaching.script_tracking.live` at resolution time (`LEGACY_FEATURE_EXPANSIONS` in `auth/license_format.py`).

`FeaturePolicy.current_tier()` reads `SALES_COPILOT_LICENSE` from the environment, calls `decode_and_verify`, then checks revocation, and returns the tier string (`"free"`, `"pro"`, `"enterprise"`). On missing key, invalid key, or revoked key it returns `"free"`.

`is_pro()` is a compatibility wrapper for existing call sites and returns `True` for both `pro` and `enterprise` tiers.

## Revocation

For `SCP-` keys, the client maintains an offline-first revocation cache at `~/.sales_copilot/revocation_cache.json` with a 7-day TTL.

When the cache entry is missing or stale, the client phones home:

```
POST {SALES_COPILOT_LICENSE_CHECK_URL}/license/check
Content-Type: application/json

{"license_id": "<32 hex chars>"}
```

The default endpoint is `https://license.salescopilot.app`. Override with the `SALES_COPILOT_LICENSE_CHECK_URL` environment variable.

Response:

```json
{"revoked": false, "expires_at": 1798761600}
```

On a network failure the client uses any prior cached value; if no prior value exists it fails open — the key is treated as not revoked. A revoked key can therefore lag at most ~7 days before it is blocked locally. This is by design: UX continuity over hard lock. The payload carries no PII beyond the random `license_id`.

Legacy `SC-` keys have no `license_id` and skip the revocation check entirely.

## Installing a License Key

Add the key to your `.env` file:

```bash
SALES_COPILOT_LICENSE=SCP-XXXX-XXXX-...
```

Restart the server. The startup log will confirm:

```
License: License valid — tier=pro, expires=2027-06-14.
```

`SALES_COPILOT_LICENSE_SECRET` is only needed to validate legacy `SC-` keys. It is no longer generated by `scripts/generate_secrets.py`. If you still hold `SC-` keys and need to validate them, set the secret manually in `.env`. Once all keys are migrated to `SCP-` format, this variable can be removed from your environment.

## 14-Day Grace Flow

On first startup without a license key:

1. A grace marker file is created at `.vnx-data/license_grace_marker`.
2. The server starts normally and broadcasts a `license_grace` WebSocket event to connected dashboards.
3. The dashboard shows a non-blocking banner with remaining days.
4. After 14 days the server broadcasts `license_expired` and the banner upgrades to a hard prompt.

The server never kills itself over license status — only the UI escalates the warning level.

## Email Capture Endpoint

Request a free key via POST:

```bash
curl -X POST http://localhost:8760/api/v1/license/request \
  -H "Content-Type: application/json" \
  -d '{"email": "you@company.com"}'
```

Response:

```json
{
  "status": "ok",
  "message": "License key issued for you@company.com. Set SALES_COPILOT_LICENSE in your .env to activate.",
  "license_key": "SC-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX"
}
```

The endpoint currently issues legacy `SC-` format keys (30-day free tier). The license server is live and issues `SCP-` keys to pilot users through the double-opt-in signup flow; the local `/api/v1/license/request` endpoint will be upgraded to issue `SCP-` keys directly in a future release. Calling the endpoint twice with the same email returns `status: "exists"` — no duplicate leads are created.

## Status Endpoint

```bash
curl http://localhost:8760/api/v1/license/status
```

Returns current license state:

```json
{
  "status": "grace",
  "message": "No license key found. Running in grace period: 11 day(s) remaining."
}
```

Possible `status` values: `valid`, `grace`, `expired`, `missing`.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SALES_COPILOT_LICENSE` | No | License key (`SCP-` or legacy `SC-`). Omit for 14-day grace period. |
| `SALES_COPILOT_LICENSE_SECRET` | Only for legacy `SC-` keys | HMAC secret for validating legacy keys. Not generated by `generate_secrets.py`. Remove after migration deadline 2026-09-01. |
| `SALES_COPILOT_LICENSE_CHECK_URL` | No | Override the revocation check endpoint. Defaults to `https://license.salescopilot.app`. |

## Security Notes

- `SCP-` keys use Ed25519 asymmetric signing. The private key exists only on the license server; the embedded public key can only verify, never mint. A tampered `SCP-` key is rejected at verification.
- Legacy `SC-` keys used a shared HMAC secret — anyone with the secret could generate Pro keys. This is why the format is being sunset.
- Keys do not contain the user's email in cleartext. `SCP-` keys carry only a random 128-bit `license_id`.
- The revocation check sends only the `license_id` in the request body — no email, no key, no call data. The endpoint host (Cloudflare) does technically process the client IP at the transport layer, so the check is pseudonymous, not anonymous. See `docs/PRIVACY.md`.
- The grace marker is stored locally at `.vnx-data/license_grace_marker`. Deleting it resets the 14-day clock.
