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
  Embedding-based detection and talk-time coaching (live cues)
  Session recordings (data/sessions/<id>/)
  Post-call report
  Case database (SQLite)

         |
         | only if an LLM provider is configured
         | (default: none). Text, per task, through one PII seam
         v

LLM DESTINATION (optional, your choice)
  A local LLM of your choice (for example via Ollama)
  Your own cloud tenant (Azure OpenAI, Vertex AI, AWS Bedrock)
  A public provider with your own key (Gemini, Groq, OpenAI, OpenRouter)

OTHER OPTIONAL DESTINATIONS
  Supabase (case synchronization, optional)
  Report delivery directory or endpoint you name (opt-in)
```

### Configurable privacy tiers

Raw audio is always transcribed locally with whisper.cpp or mlx-whisper and never
leaves the device. Whether any transcript text leaves the device depends on
`LLM_PROVIDER`, which ships as `none`:

1. **No LLM (default).** `LLM_PROVIDER=none`. The live cues (embedding-based
   pain point and objection detection, talk-time coaching) run fully local
   without an LLM. No transcript text is sent anywhere. LLM-backed features
   (rolling summary, live suggestions, window classification, post-call report
   enrichment and term corrections) stay off.
2. **A local LLM of your choice.** For example `LLM_PROVIDER=ollama` with a
   model you run yourself on a loopback address. The text stays on the machine.
3. **Your own cloud tenant.** Azure OpenAI within your own Azure subscription,
   Vertex AI within your own GCP project, or AWS Bedrock. This keeps the text
   within your existing cloud contract/DPA and chosen region, without adding a
   new processor.
4. **A public provider with your own key.** Gemini, Groq, OpenAI or OpenRouter.
   The text is processed under that provider's terms, region settings, and DPA.

No LLM key or model ships with OnCue. The own-tenant tier enables processing
within your existing tenant; the configuration does not automatically make an
organization compliant. The organization deploying OnCue remains the data
controller and remains responsible for the legal basis, configuration, region,
contracts, and use.

### What leaves the machine when an LLM provider is configured

Which text goes out depends on the task. Every task goes through the same seam:
`core/llm_client.py`, which applies the PII policy of `core/outbound_policy.py`
to the text before the call (see "How PII redaction works and where it stops"
below). The destination of each task can be set separately
(`<TASK>_LLM_PROVIDER`), and a per-conversation privacy ceiling refuses a
provider above it.

| Task | What is sent | When |
|---|---|---|
| Pain point and objection detection | A sliding window of the prospect's latest utterances plus the category list from `config/pain_points.yaml` | Provider configured. Only prospect speech is classified (`ONLY_CLASSIFY_PROSPECT=true` default) |
| Rolling conversation summary | Every 60 seconds the last up to 40 transcript lines (maximum 4000 characters) with speaker labels | Provider configured and `ENABLE_SUMMARY=true` (default) |
| Live suggestions | Prospect utterances, plus the context and prep documents you uploaded (`core/context_docs.py`, `core/profile_docs.py`) | Provider configured and `ENABLE_SUGGESTIONS=true` (default) |
| Post-call report enrichment | The whole transcript of the call, numbered per segment, with speaker labels | Provider configured and not switched off with `REPORT_LLM_PROVIDER=none` |
| Post-call term corrections | The same transcript, with the candidate terms from your term list under the segments that have candidates; only when at least one segment has candidates | Provider configured, a term list present, and not switched off with `REPORT_TERMS_LLM_PROVIDER=none` |
| Deep-insight lane (Pro) | The full session transcript and prep documents | Opt-in per conversation, default off (see below) |

The summary and the report get more than a short fragment: up to 40 lines per
minute, and at the end of the call the entire transcript. When the provider is
public, that is the redacted full conversation, not 1-3 sentences.

**Your own speech.** With the default `TRANSCRIBE_SELF_LIVE=false` the microphone
stream is not transcribed during the call. Detection and suggestions only read
prospect speech in any case. Your own words therefore do not reach the live
tasks, including the rolling summary. Two things change that:

- `TRANSCRIBE_SELF_LIVE=true` puts your own lines, labeled as yours, into the
  transcript stream. The summary then receives them as well.
- `RECORD_AUDIO=true` keeps `self.wav`. After the call it is transcribed locally
  in batch and added to the transcript, so the post-call report enrichment
  (and the term corrections) receive your words labeled "Seller". With the
  defaults (no recording, no live self transcription) the report only contains
  prospect speech.

**What never leaves the machine:**

- Raw audio (mic stream and system stream)
- Session recordings (WAV files)
- Embeddings and the case database (unless you configure Supabase)

> **Deep-insight lane (Pro, opt-in, default off).** The deep-insight lane is a Pro feature and a separate outbound class for frontier-grade analysis. The code does not ship in the open source distribution. When an operator explicitly enables it for a specific conversation, the full session transcript is sent to an operator-configured frontier or enterprise destination (BYO-tenant cloud, a public frontier API, or an MCP host). The transcript is always PII-redacted through the existing `outbound_policy` seam before it leaves. The lane is off by default; no conversation uses it unless the operator turned it on for that call. See `docs/ARCHITECTURE_BOUNDARIES.md` ("Deep-insight lane outbound class").

> **Report delivery (opt-in, default off).** OnCue can be used as a trigger for a
> customer's own automation: when a call ends, the finished report can be handed to a
> directory the operator names or an HTTP endpoint the operator names
> (`REPORT_DELIVERY_DIR` / `REPORT_DELIVERY_ENDPOINT`). Both destinations are the
> **operator's own configured infrastructure**, not an OnCue or third-party service,
> which is different in kind from the deep-insight lane above. It carries the same
> content as the local `data/reports/` file, including the full transcript, and by
> default is **not** PII-redacted (`REPORT_REDACT_PII=false`): the point of the
> feature is handing the operator's own automation their own words verbatim so it can
> act on them (e.g. extract a name or company for a CRM). Set `REPORT_REDACT_PII=true`
> to redact both the local file and anything delivered by these sinks. There is no
> separate redaction toggle for delivery. Both sinks are off by default; no report
> leaves the machine via this path unless the operator configured a destination. See
> `docs/ARCHITECTURE_BOUNDARIES.md` ("Trigger delivery outbound class") and
> `docs/MODULE4.md` ("Report Delivery").

**Zero outbound text:** keep `LLM_PROVIDER=none` (the default), or set
`LLM_PROVIDER=ollama` with a model on a loopback address. In both cases no
transcript text leaves the machine through the LLM seam.

---

## How PII redaction works and where it stops

Before text goes to an LLM destination, `core/outbound_policy.py` applies a PII
policy. The redaction itself (`core/pii_filter.py`, `core/pii_patterns.py`) is a
set of regular expressions. There is no named-entity recognition and no model
involved. It is a safety net, not a guarantee.

**What the patterns catch:**

| Pattern | Replaced by | Scope |
|---|---|---|
| Email addresses | `[EMAIL]` | Standard address shape |
| Phone numbers | `[TELEFOON]` | Dutch formats (`+31`, `06`, `0` plus area code) |
| IBAN | `[IBAN]` | Dutch IBANs only (`NL` plus 2 digits, 4 letters, 10 digits) |
| BSN | `[BSN]` | Any 9-digit number, so it also hits other 9-digit numbers |
| Postcode | `[POSTCODE]` | Dutch format (4 digits, 2 letters) |
| Dates | `[DATUM]` | Numeric dates with a 19xx/20xx year and written-out dates |
| Names | `[NAAM]` | Only (a) a fixed list of about 190 Dutch first names, case-sensitive, and (b) the shape "Firstname tussenvoegsel Lastname" |

**What it misses:**

- First names that are not on the list (for example "Heleen" or "Vincent")
- Surnames without a tussenvoegsel
- Company names
- Street addresses
- IBANs from other countries
- Anything that does not look like one of the patterns above

Example: "Ik ben Heleen van Berkel" becomes "[NAAM]" in full. "Ik ben Heleen"
stays unchanged, because "Heleen" is not on the list.

**The three modes (`PII_REDACTION`):**

| Mode | Behavior |
|---|---|
| `cloud_only` (default) | Redact before public providers. Pass the text raw when the destination is Ollama on a loopback address, or an `azure`/`vertex` tenant with `TRUST_OWN_TENANT` set |
| `always` | Redact before every provider, including local and own-tenant ones |
| `off` | Never redact. `ALLOW_RAW_LLM_PII=true` forces this mode |

An unknown value falls back to `cloud_only`. Every time redaction is skipped, a
warning is logged once per process.

**`TRUST_OWN_TENANT`** (default `false`) only has an effect with
`LLM_PROVIDER=azure` or `vertex`. When true, the text goes to your own tenant
unredacted, on the premise that inference stays inside a boundary and DPA you
already govern. It never applies to public providers.

Because the filter is pattern based, treat redaction as one layer. Choose the
tier (no LLM, local LLM, own tenant) by how sensitive the conversation is, not by
how well you trust the patterns.

---

## Which data is processed?

| Data type | Where processed | Retained until |
|---|---|---|
| Audio (mic + system) | Locally on your Mac | Session duration (buffer), WAV in `data/sessions/` if `RECORD_AUDIO=true` |
| Transcript text (live) | Local (Whisper). With an LLM provider configured, to that provider per task: detection window, rolling summary, live suggestions (see the per-task table above). PII policy applies | Session duration for buffers; post-call report in `data/sessions/` |
| Full transcript | Local in post-call report. With an LLM provider configured, the whole transcript goes to the report provider for enrichment and term corrections (PII policy applies); optionally to the deep-insight lane destination (Pro, opt-in, PII-redacted); optionally to an operator-configured report-delivery directory/endpoint (opt-in, unredacted by default, see `REPORT_REDACT_PII`) | Until you delete it (in `data/sessions/`) |
| Your own speech (mic) | Local. Not transcribed live by default. Transcribed after the call from `self.wav` when `RECORD_AUDIO=true`, then part of the report text | Until you delete it |
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

With `LLM_PROVIDER=none` (the default) no LLM provider receives anything. When
you configure one, the system sends the text described in the per-task table
above, after the PII policy of `core/outbound_policy.py` has run. With a public
provider (Gemini, Groq, OpenAI, OpenRouter) that policy redacts by default
(`PII_REDACTION=cloud_only`); `ALLOW_RAW_LLM_PII=true` disables it.

What exactly is sent, per task, is listed in "What leaves the machine when an LLM
provider is configured". In short: a sliding window of prospect utterances for
detection, a rolling summary input of up to 40 lines per minute, prospect
utterances with your uploaded context documents for suggestions, and the whole
transcript after the call for the report. Each prompt also carries the system
instructions of that task, for example the pain point categories from
`config/pain_points.yaml`.

The provider receives no information about who the user is, unless a name
appears in the transcription and the redaction patterns do not catch it (see
"How PII redaction works and where it stops").

**Data minimization:** detection and live suggestions only read prospect speech
(`ONLY_CLASSIFY_PROSPECT=true` is the default for detection). The rolling summary
and the post-call report read every line that is in the transcript, so they
include your own speech whenever it is transcribed (see "Your own speech" above).

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
   conversation is not sent to an external party unless you configure an LLM
   provider. If you do, the provider receives the text of the tasks listed above,
   including the whole transcript for the post-call report.

3. **The cloud receives pseudonymized, not anonymized, text.** Before the text
   leaves, regular-expression redaction removes the patterns it knows. Whether
   the text still contains personal data depends on what the patterns catch
   (see "How PII redaction works and where it stops").

4. **Operation without outbound text is possible.** With `LLM_PROVIDER=none` (the
   default) or `LLM_PROVIDER=ollama` on a loopback address, no transcript text
   leaves the machine through the LLM seam.

**Comparison with cloud-only call-intelligence tools:**

| Aspect | OnCue (local) | Typical cloud suite |
|---|---|---|
| Audio stored at provider | No | Yes (typically 12-36 months) |
| Transcript stored at provider | No stored copy by OnCue; with a configured provider, the provider processes the text under its own terms | Yes |
| DPA with provider required | Only for LLM (optional) | Mandatory for all data |
| Risk of provider data breach | No audio at any provider; transcript text only reaches the provider you configure | Full recordings at the provider |
| Fully offline use possible | Yes (no LLM, or Ollama) | No |

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
compliance audit" section below.

**LLM provider receipt:** there is no automatic log of which text
was sent to which external LLM provider. If your DPIA requires such a log,
there are two options:

1. Keep `LLM_PROVIDER=none` (the default) or use `LLM_PROVIDER=ollama` so there are no external API calls.
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

Only a SHA-256 hash of the record, computed client-side before sending. The
raw record (including any personal data) does not leave the machine. The server
receives a pseudonymous hash plus the license key for authorization; with that
it builds a chained, tamper-resistant audit chain. A hash of a record that
contains personal data is pseudonymisation under the GDPR, not anonymisation,
and the license key identifies the sender. This is on by default for Pro,
because `CONSENT_TRACKING_ENABLED=true` is the default.

**What stays local:**

The full audit record including all fields. The server cannot read the record
from the hash, but it can confirm a record it is given against the hash.

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

**Local-only (Free, no LLM or Ollama):** no transcript text leaves the machine.
No revocation call (if no license is active), no central audit chaining. The audit trail
stays exclusively local in SQLite. Fully offline operation possible.

**Pro with central audit:** with Pro, the revocation call activates (once every
7 days, `license_id` only) and `TieredAuditWriter` sends hashes to the
central chain. This increases the auditability of the audit trail, but
introduces two server-side contact moments: the revocation check and each
audit-record hash.

Users who want no phone-home at all stay on Free with no LLM or Ollama as the
LLM provider. The architecture forces no one into a cloud dependency;
Pro functionality is a deliberate choice, with transparency about what
it sets in motion.

---

## No other telemetry

**The open source version (no license set) makes no call to the maker.** No
revocation call, no central audit, and the measurement signals below are off.

With a Pro license, the only calls to the maker are the revocation call and the
central audit hash described above. No usage data, crash reports, or diagnostic
telemetry is sent to VNX Digital or any other party.

**Opt-in measurement signals** (`core/measurement_signals.py`). All three are off
by default, and the endpoints are unset in `.env.example`:

| Setting | Default | What it does when enabled |
|---|---|---|
| `MEASUREMENT_CORRECTION_OPT_IN` | `false` | Sends aggregated correction counts (predicted and corrected category counts) when you mark a detection as wrong. No transcript or trigger phrase |
| `MEASUREMENT_HEARTBEAT_ENABLED` | `false` | Periodically sends a license fingerprint, a timestamp, and the install code while a valid Pro license is active |
| `INSTALL_CODE` | empty | An attribution code. It is written to the local audit trail, and it is part of the payload of the two signals above when those are enabled |

Nothing is sent for a signal unless its switch is on and an endpoint is set.

Verification: the network clients in the codebase are listed in
`_NETWORK_ALLOWLIST` in `scripts/check_architecture_boundaries.py`, which CI
enforces. A network-client import in any file that is not on that list fails the
check. The list contains the LLM client, the local transcriber backends, the
license and audit calls (`auth/revocation_cache.py`, `core/compliance_audit.py`),
the measurement signals (`core/measurement_signals.py`), report delivery
(`modules/reports/delivery.py`), and the localhost WebSocket modules.

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
