# Session Persistence

## Purpose

The session persistence module provides a SQLite-backed audit trail for all active sales sessions. Its primary purpose is crash recovery: if the server crashes during a call, the next startup detects unfinished sessions and sends a `session_resume_available` WebSocket event.

Secondarily, the store forms a lightweight audit trail of all pain-point detections per session, independent of the call_sessions table managed by `SQLiteCaseDB`.

## Location

Database: `.vnx-data/sessions.db`

The `.vnx-data/` folder is runtime state and is listed in `.gitignore`. The directory is created automatically on first use.

## Schema

### Table `sessions`

| Column | Type | Description |
|---|---|---|
| `id` | TEXT PK | UUID of the session |
| `start_ts` | REAL | Unix timestamp of start |
| `end_ts` | REAL (nullable) | Unix timestamp of end; NULL = session was still running |
| `profile` | TEXT | Preset profile: `sales`, `coach`, `recruitment` |
| `transcript_path` | TEXT (nullable) | Path to the audio recording if present |

### Table `detections`

| Column | Type | Description |
|---|---|---|
| `session_id` | TEXT FK | References `sessions.id` |
| `ts` | REAL | Unix timestamp of the detection |
| `type` | TEXT | Type: `pain_point`, `coaching_alert`, etc. |
| `subcat` | TEXT (nullable) | Subcategory (e.g. `capaciteit`, `prijs`) |
| `confidence` | REAL | Confidence score 0.0-1.0 |
| `text` | TEXT | Transcribed text that triggered the detection |

Index on `detections(session_id)` for efficient per-session lookups.

## Resume flow

1. Server is running, a session starts → `SessionStore.create_session()` writes a record with `end_ts = NULL`
2. Pain-point detection → `SessionStore.append_detection()` writes a detections record
3. Server crashes (SIGKILL, OOM, etc.) → `end_ts` stays NULL
4. Server restarts → `_orchestrate()` in `__main__.py` calls `store.find_unfinished_sessions()`
5. If there are unfinished sessions (<24h old): a `session_resume_available` event via the `/ws/config` channel
6. The dashboard shows a "Resume session?" prompt with session IDs
7. Normal shutdown: `SessionTracker.end_session()` → `store.end_session()` sets `end_ts`

## GC policy

Sessions older than 24 hours are deleted via `gc_old_sessions()`. This also deletes the associated detections. The 24-hour retention is tuned to a workday; for compliance use cases the threshold can be raised by adjusting `_GC_THRESHOLD_SECONDS` or by adding a Supabase sync.

`gc_old_sessions()` is currently not called automatically. It should be called manually or via a periodic task at a suitable moment (e.g. at server startup after the recovery check).

## Backward compatibility

`SessionTracker` accepts an optional `session_store: SessionStore | None` parameter. Without a store, everything works exactly as before this addition: no SQLite writes to `.vnx-data/sessions.db`, no behavior change.

## Sample queries

View unfinished sessions:

```sql
SELECT id, datetime(start_ts, 'unixepoch') AS started, profile
FROM sessions
WHERE end_ts IS NULL
ORDER BY start_ts DESC;
```

Detections per session:

```sql
SELECT type, subcat, confidence, text
FROM detections
WHERE session_id = '<uuid>'
ORDER BY ts;
```
