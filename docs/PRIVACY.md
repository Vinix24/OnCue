# Privacy, GDPR, and the AI Act

This document describes how OnCue handles personal data, which GDPR obligations
may apply, and how the architecture is designed to limit the privacy impact.

It is intended for users, procurement staff, lawyers, and data protection
officers (DPOs) who want to assess whether OnCue can be deployed in their
organization.

This document is not legal advice. The organization deploying OnCue remains
responsible for GDPR compliance itself. Engage a lawyer or DPO for formal DPIAs and
data processing agreements.

---

## Architecture: local versus cloud

The core of OnCue's privacy position is an explicit separation between what
stays local and what optionally goes to an external service.

```
LOCAL (your Mac)
  Audio capture (mic + system)
  Silero VAD (pause detection)
  Whisper transcription (whisper.cpp or mlx-whisper)
  Session recordings (data/sessions/<id>/)
  Post-call report
  Case database (SQLite)

         |
         | only if configured
         | only short fragments (1-3 sentences)
         v

CLOUD (optional, your choice)
  LLM provider (Gemini, OpenRouter, Groq, OpenAI, or fully local via Ollama)
  Supabase (case synchronization, optional)
```

### Configurable privacy tiers

Raw audio is always transcribed locally with whisper.cpp and never leaves the
device, unlike cloud Whisper services. Only short, PII-redacted transcript
fragments can be sent to an LLM destination. That destination is explicitly
configurable:

1. **Fully local with Ollama.** Nothing leaves the device.
2. **Your own cloud tenant.** Use Azure OpenAI within your own
   Azure subscription, Vertex AI within your own GCP project, or AWS Bedrock.
   This keeps the redacted fragments within your existing
   cloud contract/DPA and chosen EU region, without adding a new processor.
3. **A configured third-party API.** The redacted fragments are
   processed under that provider's terms, region settings, and DPA.

The own-tenant tier enables compliant processing within your existing tenant;
the configuration does not automatically make an organization compliant. The
organization deploying OnCue remains the data controller and remains
responsible for the legal basis, configuration, region, contracts, and use.

**What never leaves the machine:**

- Raw audio (mic stream and system stream)
- Full transcripts
- Session recordings (WAV files)

**What optionally goes to an LLM provider:**

- Short transcript fragments of 1-3 sentences for pain point and objection detection
- No full recording, no name or contact details unless they appear in the
  transcription

**Fully local processing (zero external API calls):**

Set `LLM_PROVIDER=ollama` in `.env` combined with a local Ollama model
(for example `qwen2.5:7b`). In that case nothing leaves the machine.

---

## Which data is processed?

| Data type | Where processed | Retained until |
|---|---|---|
| Audio (mic + system) | Locally on your Mac | Session duration (buffer), WAV in `data/sessions/` if `RECORD_AUDIO=true` |
| Transcript fragments (prospect speech) | Local (Whisper), optionally to LLM provider for detection | Session duration for buffers; post-call report in `data/sessions/` |
| Full transcript | Local in post-call report | Until you delete it (in `data/sessions/`) |
| Talk-time statistics | Local in post-call report | Until you delete it |
| API keys | Local in `.env` (gitignored) | Not logged, not sent |
| Case database | Local SQLite or optionally Supabase | Until you delete it or delete the Supabase project |

**Disabling session recordings:**

```bash
# in .env
RECORD_AUDIO=false
```

If `RECORD_AUDIO=false` is set, no WAV files are stored.
Transcription and detection keep working unchanged via in-memory audio buffers.

---

## LLM provider and data sharing

When using a cloud LLM (Gemini, OpenRouter, Groq, OpenAI), the system by default
sends PII-redacted transcript fragments and context-document fragments to that
provider's API. `ALLOW_RAW_LLM_PII=true` explicitly disables this protection.

What exactly is sent:

- A sliding window of the prospect's last 3-5 utterances (typically 50-150 words)
- A system prompt with pain point categories from `config/pain_points.yaml`
- No full call recording, no name or contact details of the user

The provider receives no information about who the user is, unless the prospect's
name happens to appear in the transcription.

**Data minimization:** the system only classifies prospect speech
(`ONLY_CLASSIFY_PROSPECT=true` is the default). The user's own speech
is not sent to the LLM for detection.

**GDPR legal basis when using a cloud LLM:**

If the transcription contains personal data of the prospect (name, company), then
personal data is being processed through an external processor.
Required step: conclude a data processing agreement (DPA) with the LLM provider.

All major providers (Anthropic, Google, OpenAI, Groq) offer a DPA for
business use. Consult the documentation of the specific provider.

---

## Consent for recording

OnCue only records conversation audio if you set `RECORD_AUDIO=true`.
By default recording is off (`RECORD_AUDIO=false`); you turn it on explicitly.

### Configurable consent tier

The deploying organization decides how strict the consent gate is via
`CONSENT_TIER` in `.env`:

| Tier | Behavior | When to use |
|---|---|---|
| `off` | No consent gate. | Not recommended; logged at startup. |
| `audit` | Record asked/given if the start-call message includes it; never blocks. | Responsible DEFAULT for sales/coaching. |
| `soft` | Non-blocking warning/nudge on the dashboard if consent is missing. | B2B scenarios with internal policy. |
| `strict` | Only starts after explicit consent (given=true). | Recruitment and other high-compliance scenarios. |

**Default per preset** (env always wins):

- `sales`, `coach`, `acquisitie` → `audit`
- `recruitment` → `strict`

The strictest option (`strict`) exists and is available, but the choice and the
responsibility lie with the deploying organization (the data controller).
OnCue does not enforce any specific legal basis; the tool provides a
configurable gate that fits different deployment scenarios.

**Legacy backward compat:** `CONSENT_GATE_MODE=audit` maps to
`CONSENT_TIER=audit`; `CONSENT_GATE_MODE=blocking` maps to
`CONSENT_TIER=strict`. Set `CONSENT_TIER` explicitly to override the legacy
variable.

**Duty to inform:** conversation partners have the right to information about the
recording. Depending on the legal context (employer/employee, client/service
provider) and the applicable law, different rules apply.

In most Dutch B2B contexts it is common and sufficient to mention at the
start of the conversation: "This conversation is being recorded for internal
quality purposes." Recruitment conversations are subject to stricter standards
(see [USE_CASES.md](USE_CASES.md) section 3).

**Consult a lawyer** if you are unsure whether and how to ask for consent
in your specific context.

---

## Retention periods

OnCue imposes no automatic retention periods. Responsibility for
retention periods lies with the organization deploying the system.

Guidelines for common scenarios:

| Data layer | Common period | Basis |
|---|---|---|
| Raw audio (`mic.wav`, `system.wav`) | 30-90 days | Audit and disputes; no purpose beyond that |
| Full transcript + report | 12-24 months | Sales administration or coaching analysis |
| Distilled key moments / summary | 24-36 months | Knowledge management and coaching |

Implementation: delete files in `data/sessions/` according to your policy.
Automated purge is available via `DATA_RETENTION_DAYS` in `.env` combined
with `scripts/retention_purge.py` (to be scheduled via cron). By default
auto-purge is off (`DATA_RETENTION_DAYS=0`).

---

## AI Act positioning

The EU AI Act entered into force on 1 August 2024. High-risk systems (Annex III)
must meet additional transparency, documentation, and human-oversight requirements.
Sales coaching falls outside that category (low risk); recruitment mode falls under
Annex III and has additional obligations (DPIA, conformity assessment, human
oversight, transparency). Canonical classification and rationale:
[SCOPE_STATEMENT.md §4](SCOPE_STATEMENT.md#4-ai-act-positie), operational
obligations: [USE_CASES.md](USE_CASES.md) section 3, full risk analysis:
[DPIA_RECRUITMENT.md](DPIA_RECRUITMENT.md).

---

## Local-cloud split as a GDPR strategy

OnCue's hybrid architecture is deliberately designed to minimize the GDPR
footprint. The reasoning:

1. **Raw audio never leaves the machine.** The risks of a data breach at an
   external provider are thereby eliminated for the most sensitive data type.

2. **Transcription is local.** Whisper runs on the Mac. The textual form of the
   conversation is not sent to an external party unless the LLM provider receives
   fragments for classification.

3. **The cloud receives anonymized context.** The LLM provider receives short
   fragments that need to be assessed semantically. Whether those fragments
   contain personal data depends on whether people's names appear in the
   transcription.

4. **Fully local operation is possible.** With `LLM_PROVIDER=ollama`, everything runs
   on your own machine, including the LLM classification. There is then no external
   processing of any kind.

**Comparison with cloud-only call-intelligence tools:**

| Aspect | OnCue (local) | Typical cloud suite |
|---|---|---|
| Audio stored at provider | No | Yes (typically 12-36 months) |
| Transcript stored at provider | No | Yes |
| DPA with provider required | Only for LLM (optional) | Mandatory for all data |
| Risk of provider data breach | Low (no audio, no full transcript) | High (full recordings) |
| Fully offline use possible | Yes (Ollama) | No |

---

## Audit trail and receipt ledger

OnCue has two audit layers:

**Local audit ledger (all tiers):** `AuditWriter` writes an append-only
NDJSON log of every AI decision that touches personal data. This includes
consent events, detection outcomes in recruitment mode, and other
AI Act Annex III-relevant decisions. The log lives in `data/sessions/` and
does not leave the machine.

**Central audit chaining (Pro):** `TieredAuditWriter` additionally sends a
SHA-256 hash of each record to the central endpoint. See the "Central
compliance audit" section above.

**LLM provider receipt:** there is no automatic log of which transcript fragment
was sent to which external LLM provider. If your DPIA requires such a log,
there are two options:

1. Enable `LLM_PROVIDER=ollama` so there are no external API calls.
2. Implement a logging wrapper around the LLM client that logs every API call
   to a local NDJSON file.

---

## License revocation phone-home

OnCue makes one targeted network call to the license server at startup.
This is deliberately limited to the minimum needed to signal a revoked or expired
license.

**What is sent:**

Only the `license_id` field — a randomly generated 128-bit number (32 hex
characters) assigned when the license was created. It contains no email address,
name, key text, audio, transcript, or other conversation data.

**Endpoint and frequency:**

`POST https://license.salescopilot.app/license/check` (configurable via
`SALES_COPILOT_LICENSE_CHECK_URL`). The call happens at startup when the
local cache is missing or older than 7 days. In between, the cached
outcome from `~/.sales_copilot/revocation_cache.json` is used.

**Response:**

`{"revoked": bool, "expires_at": int}` — no additional data.

**Behavior on network failure:**

The client falls back to the cached value. If there is no cache, the
system continues (fail open within the grace window). A revoked license can
therefore be signaled with up to ~7 days of delay — this is a
deliberate trade-off in favor of usability.

**IP address:**

Cloudflare technically processes the request's IP address as infrastructure.
The phone-home is therefore pseudonymous (via `license_id`), not fully anonymous.
The IP address is not stored by the license service as user data.

**Legal basis:**

Legitimate interest of the rights holder to detect revoked licenses. The
processing is proportionate: only a pseudonymous identifier, no
substantive conversation data, at most once every 7 days.

**Free of phone-home:**

Free-tier users (no license set) make no revocation call.
`SC-` keys (deprecated HMAC format) have no `license_id` and also make no
revocation call.

---

## Central compliance audit (Pro)

When a Pro license is active and consent tracking is on
(`CONSENT_TRACKING_ENABLED=true`), `TieredAuditWriter` writes every audit record
to two destinations: local SQLite (always, regardless of tier) and a central
endpoint on the license server.

**What goes to the server:**

Only a SHA-256 hash of the record — computed client-side before
sending. The raw record (including any personal data) does not leave the
machine. The server receives the hash plus the license key for authorization;
with that it builds a chained, tamper-resistant audit chain.

**What stays local:**

The full audit record including all fields. The hash is not reversible to
the original content.

**Free tier:**

Local SQLite only. No central call. See also the section on privacy tiers
below.

**Consent tracking:**

`consent.py` records per session whether consent was asked and given, as a
non-blocking audit event. The system never refuses a conversation because
consent is missing — the data controller (the organization deploying the
tool) owns that decision.

**Retention periods and auto-purge:**

`DATA_RETENTION_DAYS` determines how long session data is retained. `0` (default)
disables auto-purge. The actual deletion runs via
`scripts/retention_purge.py` (to be scheduled via cron).

---

## The privacy-tier trade-off

OnCue offers two privacy levels side by side:

**Local-only (Free / Ollama):** nothing leaves the machine. No revocation call
(if no license is active), no central audit chaining. The audit trail
stays exclusively local in SQLite. Fully offline operation possible.

**Pro with central audit:** with Pro, the revocation call activates (once every
7 days, `license_id` only) and `TieredAuditWriter` sends hashes to the
central chain. This increases the auditability of the audit trail, but
introduces two server-side contact moments: the revocation check and each
audit-record hash.

Users who want no phone-home at all stay on Free with Ollama as the
LLM provider. The architecture forces no one into a cloud dependency;
Pro functionality is a deliberate choice, with transparency about what
it sets in motion.

---

## No other telemetry

Apart from the revocation call described above and (for Pro) the audit-hash call,
the codebase contains no phone-home code. No usage data,
crash reports, or diagnostic telemetry is sent to VNX Digital or any
other party.

Verification: search the codebase for `requests.post` and `fetch` calls in
Python files. They do not exist, except for the configured LLM provider calls
and the two license endpoints described in this document.

---

## Frequently asked questions

**May I use OnCue for conversations with customers?**

That depends on your internal policy, the type of conversation, and the applicable law.
In most Dutch B2B sales contexts, use is permitted provided you inform the
prospect about recording. Seek legal advice if in doubt.

**What if the prospect objects to recording?**

Disable recording (`RECORD_AUDIO=false` in `.env`) and restart the system.
Transcription and detection also work without WAV recording.

**Does OnCue comply with the GDPR?**

The tool is designed to facilitate GDPR-compliant deployment, but the organization
deploying it is the data controller. Compliance depends on how you
configure and use the tool. Engage a lawyer for a formal assessment.

**How long are my conversation recordings kept?**

By default, as long as you don't delete them. Set `DATA_RETENTION_DAYS` in `.env`
to activate an automatic retention limit; `scripts/retention_purge.py`
then deletes older session data when run via cron. Manage
`data/sessions/` according to your own policy.

---

## Further reading

- [USE_CASES.md](USE_CASES.md): legal context per deployment scenario
- [FUNCTIONAL_SPEC.md](FUNCTIONAL_SPEC.md): exactly which data is processed
- [INTEGRATIONS.md](INTEGRATIONS.md): LLM provider configuration and Ollama setup
