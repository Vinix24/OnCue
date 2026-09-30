# Module 4 — Post-Call Reports

Module 4 records live session data, persists it to SQLite, and generates a JSON report at call end. The report summary is surfaced to the dashboard via a WebSocket payload.

## Architecture Overview

Pipeline (runtime order):
- SessionTracker subscribes to `/ws/transcript`, `/ws/pain-points`, `/ws/talk-time`, `/ws/coaching`
- SessionTracker accumulates a session snapshot in memory
- SessionTracker persists the session to `call_sessions` in SQLite
- Report generator composes a `CallReport` JSON file
- Reports module emits `report_ready` summary on `/ws/coaching`

Components and locations:
- SessionTracker: `src/sales_copilot/modules/reports/session.py`
- Report generator: `src/sales_copilot/modules/reports/generator.py`
- Reports runner: `src/sales_copilot/modules/reports/__main__.py`
- Dashboard report panel: `dashboard/js/report.js`

## Data Flow

1. Reports module starts a session and opens WebSocket subscriptions.
2. Incoming events populate transcript, pain points, talk-time snapshots, and phase transitions.
3. On shutdown, the session is persisted and converted into a `CallReport`.
4. The report JSON is written to `data/reports`.
5. A `report_ready` message is broadcast to `/ws/coaching` for the dashboard.

## Configuration (.env)

Reports + DB:
- `CASE_DB_SQLITE_PATH` (default `data/cases.db`) — also stores `call_sessions`.
- `PROSPECT_INDUSTRY` (optional) — stored with session metadata.
- `REPORT_REDACT_PII` (default `false`) — redact prospect name/company and transcript
  text in the persisted (and delivered, see below) report. The in-memory report used
  for the live coaching payload is never affected.

WebSocket hub:
- `WS_HUB_HOST` (default `127.0.0.1`)
- `WS_HUB_PORT` (default 8760)

Report delivery (see "Report Delivery" below):
- `REPORT_DELIVERY_DIR`, `REPORT_DELIVERY_ENDPOINT`, `REPORT_DELIVERY_TIMEOUT_S`,
  `REPORT_DELIVERY_MAX_ATTEMPTS`, `REPORT_DELIVERY_RETRY_BACKOFF_S`.

## Report JSON Schema

Generated file: `data/reports/<timestamp>_<session_id>_report.json`. This is the
normative shape for any downstream consumer, including the delivery sinks below —
the delivered payload is byte-identical to this file (same redaction state, same
fields), never a second, independently-built representation.

```json
{
  "session_id": "session-abc123",
  "call_started_at": "2026-09-06T09:58:12.104531+00:00",
  "call_ended_at": "2026-09-06T10:12:47.881204+00:00",
  "call_duration_ms": 875000,
  "prospect_name": "Robin",
  "prospect_company": "Example BV",
  "context_docs": ["brochure.pdf"],
  "phase_timeline": [
    { "phase": "discovery", "duration_ms": 60000 }
  ],
  "per_minute_talk_time": [
    { "minute": 0, "self_pct": 0.5, "prospect_pct": 0.5 }
  ],
  "pain_points_detected": [
    { "category": "offerteproces", "timestamp_ms": 45000, "case_id": "case-017" }
  ],
  "conversation_summary": "string or null",
  "key_moments": [
    { "type": "pain_point", "timestamp_ms": 45000, "description": "string" }
  ],
  "monologue_count": 1,
  "total_self_pct": 0.4,
  "total_prospect_pct": 0.6,
  "full_transcript": [
    { "speaker": "prospect", "text": "text", "start_ms": 1000, "end_ms": 2000 }
  ],
  "scorecard": { "...": "Free post-call scorecard, or null when unavailable" },
  "insights": [
    {
      "insight_type": "doorvraag",
      "text": "string",
      "grounding": "string",
      "speculation": "laag | midden | hoog",
      "timestamp_ms": 50000,
      "question": "string or null"
    }
  ]
}
```

Field guarantees, for a downstream automation deciding what it can rely on:

| Field | Guarantee |
|---|---|
| `session_id` | Always present; identifies "which call". |
| `call_started_at` / `call_ended_at` | ISO-8601 UTC. Always present on reports produced by the live pipeline (`session.py`'s `SessionData.started_at`/`.ended_at`); `null` only on a `CallReport` constructed directly (e.g. in tests) outside that lifecycle. Answers "when". |
| `call_duration_ms` | Always present; derived, not authoritative for wall-clock time — use the two fields above for that. |
| `prospect_name` / `prospect_company` | Optional (operator-entered); `null` when not provided. Answers "who". |
| `context_docs` | Filenames only (basename, no path). |
| `full_transcript` | Always present (possibly empty list). Entries are in capture order; consult `start_ms`/`end_ms` for exact timing rather than assuming a globally sorted stream. Answers "the transcript itself". |
| `scorecard` | `null` when script tracking is disabled or no checkpoint exists yet. |
| `insights` | Empty list when the deep-insight lane (Pro) is off or not entitled. |
| `pain_points_detected[].case_id` | `null` when no matching case slide fired. |
| `insights[].question` | `null` for non-`antwoord` insight types. |

**PII note:** every field above reflects the SAME redaction state as `REPORT_REDACT_PII`.
Default `false` (raw transcript, since the local report is an owner-only 0600 file and
the delivery sinks are the operator's *own* configured destination). Set it `true` to
redact `prospect_name`/`prospect_company`/transcript text/insight text-grounding-question
in both the local file and anything delivered by the sinks below — there is no separate
redaction toggle for delivery.

## Report Delivery

The customer buys OnCue as a trigger: everything above happens locally, and when the
call ends the finished report can additionally be handed to the customer's own
automation. Both destinations are **optional and independent**, and both **default
off** — a fresh install writes only the local `data/reports/` copy, exactly as before.
That local write is unconditional and unaffected by delivery outcome: it is the
operator's own record and the fallback whenever a delivery fails.

**The HTTP endpoint sink is Pro/Enterprise** (`FEATURE_REPORT_DELIVERY_ENDPOINT`,
operator decision 2026-09-28). The directory sink stays Free. Configuring
`REPORT_DELIVERY_ENDPOINT` is always allowed and validated at startup the same as
before; on a Free tier the endpoint sink is skipped when a report is delivered (one
`WARNING` per process, never an exception) and the directory sink, if configured,
still runs.

Implementation: `src/sales_copilot/modules/reports/delivery.py`, invoked from
`generator.generate_report()` right after the local file is written.

### Configuration (.env)

| Variable | Default | Meaning |
|---|---|---|
| `REPORT_DELIVERY_DIR` | unset (disabled) | Absolute path to a directory — including a mounted network share — that receives a copy of every report. |
| `REPORT_DELIVERY_ENDPOINT` | unset (disabled) | `http(s)://...` URL that receives one `POST` per report, `Content-Type: application/json`, body = the report JSON verbatim. |
| `REPORT_DELIVERY_TIMEOUT_S` | `10.0` | Per-attempt HTTP timeout, in seconds. |
| `REPORT_DELIVERY_MAX_ATTEMPTS` | `3` | Total POST attempts (the first try plus retries), bounded — never an unbounded retry loop. |
| `REPORT_DELIVERY_RETRY_BACKOFF_S` | `2.0` | Linear backoff between attempts: `backoff_s * attempt_number` (2s, then 4s, for the default 3 attempts). |

### Directory sink

Writes the exact report bytes to `REPORT_DELIVERY_DIR/<same filename as the local
report>`, atomically: a temp file is created in the SAME directory and then
`os.replace()`d into place. A directory-watcher (e.g. an n8n "Local File Trigger")
therefore only ever sees the previous file or the fully-written new one — never a
partial write. The delivered copy is written `0644` (world-readable), unlike the
local `0600` report, because the reader is, by design, a different process/user: the
customer's own automation.

**Startup validation.** `REPORT_DELIVERY_DIR` is checked when the reports module
starts (`validate_report_delivery_config`, called from `__main__.main()` before
anything else): the directory must exist and be writable. A missing directory, a
path that is not a directory, or a non-writable directory raises immediately with
the exact path and reason — at startup, not silently discovered at the end of the
first call. This deliberately does NOT auto-create the directory: for a mounted
network share, silently creating a local directory instead of failing would mask a
share that never mounted.

A failure that occurs later (the share drops mid-call) is logged as an `ERROR`
naming the local report path; the local copy is never affected.

### HTTP endpoint sink

**Pro/Enterprise only** (`FEATURE_REPORT_DELIVERY_ENDPOINT`). On Free, a configured
endpoint is skipped with a single `WARNING` naming the feature the first time a report
is delivered per process; every subsequent report in that process is skipped silently
after that first log line, and the directory sink is unaffected.

One `POST` per report. On failure (connection error, timeout, or a non-2xx status)
it retries up to `REPORT_DELIVERY_MAX_ATTEMPTS` times with the linear backoff above,
then gives up and logs a single `ERROR` — never a silently dropped transcript — naming
the endpoint, the session, and the local report path so the operator can redeliver
manually once the endpoint recovers. There is no persistent queue and no retry across
process restarts: an outage that outlasts the bounded retry window for a given call
means that call's delivery is lost, but its local report is not — the intended
recovery path is reading `data/reports/` once the endpoint is back, not an
automatically-replayed backlog. This is a deliberate scope limit, not an oversight —
see the "What this is NOT" framing in the PR that introduced this feature.

Delivery to both sinks runs on a background daemon thread
(`deliver_report_in_background`) so a slow or hung endpoint can never delay the
reports module's own shutdown — the `report_ready` dashboard payload is sent
regardless of delivery outcome.

**No built-in authentication.** This is not a webhook framework: if the endpoint
needs a shared secret, embed it in the URL itself (a common pattern for n8n/Zapier
webhook URLs), e.g. `REPORT_DELIVERY_ENDPOINT=https://n8n.example.com/webhook/<token>`.

### Outbound classification

This is a deliberate, documented THIRD outbound class, distinct from the deep-insight
lane's LLM-provider destination — it does **not** go through `core/outbound_policy.py`.
See `docs/ARCHITECTURE_BOUNDARIES.md` ("Trigger delivery outbound class") for the full
argument.

## Running Module 4

```bash
PYTHONPATH=src python -m sales_copilot.modules.reports
```

The module runs until shutdown (Ctrl+C). On shutdown it generates the JSON report and emits `report_ready`.

## Dashboard Report View

`dashboard/js/report.js` listens on `/ws/coaching` for `report_ready` payloads and renders:
- Call duration
- Talk-time split
- Monologue count
- Pain points list
- Download JSON button

## Limitations

- Session end is currently tied to module shutdown, not a live “end call” button.
- Report generation is synchronous at shutdown; large transcripts increase runtime.

## Certification Evidence (PR-40)

- `ruff check src/`
- `python -m pytest tests/ -v`
- E2E: `tests/test_full_e2e.py`
