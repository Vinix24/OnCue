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

WebSocket hub:
- `WS_HUB_HOST` (default `127.0.0.1`)
- `WS_HUB_PORT` (default 8760)

## Report JSON Schema

Generated file: `data/reports/<timestamp>_report.json`

```json
{
  "session_id": "string",
  "call_duration_ms": 90000,
  "phase_timeline": [
    { "phase": "discovery", "duration_ms": 60000 }
  ],
  "per_minute_talk_time": [
    { "minute": 0, "self_pct": 0.5, "prospect_pct": 0.5 }
  ],
  "pain_points_detected": [
    { "category": "offerteproces", "timestamp_ms": 45000, "case_id": "case-017" }
  ],
  "monologue_count": 1,
  "total_self_pct": 0.4,
  "total_prospect_pct": 0.6,
  "full_transcript": [
    { "speaker": "prospect", "text": "text", "start_ms": 1000, "end_ms": 2000 }
  ]
}
```

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
