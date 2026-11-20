# ADR-004: Consent, one route at a time, and the prompt lifecycle

Status: Accepted. Points 7 and 8 superseded by ADR-005 once the one-recording server is on main.

Date: 2026-11-11

## Context

A prompt "<app> is using your microphone. Record?" with Yes and No must start at most one recording and never a wrong one. It must reach the person even when a page connects late.

OnCue has two routes that both record the same call:

- the copilot route: live analysis, port `WS_HUB_PORT` (default 8760);
- the notetaker route: transcript afterwards, `scripts/record_server.py`, port 8780.

Today the launcher lets both run at once and only shows a notice (`scripts/launcher/oncue-launch.sh`, the "other mode is already running" branch).

The copilot route already has a consent flow: `RuntimeAutostartMonitor` fires `on_consent_required`, the hub broadcasts `autostart_consent_required` on `/ws/system`, and `/api/autostart/confirm` and `/api/autostart/decline` call `confirm_consent` and `decline_consent`. It has no prompt identity and no snapshot for a view that connects late. The notetaker route has none of it.

## Decision

1. **No automatic stop.** A recording started from a prompt stops like any recording: the button, the bar or the hotkey.
   - An end signal is not reliable while it is unknown whether muting releases the microphone. A long muted call would be cut.
   - The measurement (ADR-003) records release times on hang-up and on mute, so a later decision can rest on data.
   - The monitor keeps not observing during a running session (`run_autostart_monitor_loop` skips the tick while `is_call_active` is true).

2. **Prompt identity and validity.** Every prompt has a `prompt_id`. A prompt is valid while its detection runs and the last successful observation is at most 10 s old (ADR-003). An answer counts only for the valid prompt with that `prompt_id`. A late answer, or an answer to an expired prompt, is ignored and logged.

3. **Suppression and its limit.**
   - After No, and after a session is stopped while the app still uses the microphone, there is no new prompt for that detection.
   - A new detection starts only after the app has released the microphone for longer than a recovery window taken from the measurement.
   - If an app releases the microphone on mute and the mute lasts longer than that window, a second prompt can come in the same call. That is safe: a prompt records nothing without Yes.
   - So there is no promise of exactly one prompt per call. The promise is no second prompt while the microphone stays in use or is released for less than the window.

4. **Yes goes through the normal start.**
   - In the notetaker route through the same path as the button (`_handle_start`), which already refuses a second start under its lock.
   - In the copilot route through `apply_start_call` under `_config_lock`, refused when a call is already active.
   - At most one recording, whoever clicks first. A manual start while a prompt is open expires that prompt first.
   - The capture source comes from the app list (ADR-003).

5. **The server knows the open prompt, and every view asks on connect.**
   - The notetaker route carries `pending_consent` in the `state` message it already sends on connect and after every change.
   - The copilot route sends `consent_snapshot` on connect on `/ws/system`.
   - Both hold the open, valid prompt with the `prompt_id` of that prompt, its app and its source, or `null`.
   - A view shows what the latest state says and removes a shown prompt on `null`, also when it missed a `consent_resolved` while away.

6. **Messages.**
   - `consent_required`: `prompt_id`, app, source.
   - Client action `consent`: `prompt_id` and `answer` (`yes` or `no`).
   - `consent_resolved`: `prompt_id` and outcome (`accepted`, `declined` or `expired`).
   - The first valid answer wins.
   - A prompt expires when the detection ends, when the last successful observation is older than 10 s, when a manual recording starts, or when the other route is running.
   - The copilot route keeps `/api/autostart/confirm` and `/api/autostart/decline`, with `prompt_id` added. `prompt_id` changes in one step across the monitor, both endpoints and the dashboard script.

7. **One route at a time.** Both routes record the same call, so running both means two recordings of one call. The launcher allows one route at a time.
   - Starting the other route asks "OnCue is already running as <route>. Switch?" and says so when a recording runs (for the notetaker route via `GET /status`).
   - It stops the running route with SIGTERM so its shutdown transition runs.
   - It waits for that process to exit, by the pid the route wrote at start, and then for its port to be free. A free port alone is not proof: Uvicorn closes the listener before the shutdown that finishes the recording.
   - After 60 s the switch is aborted and the new route is not started.

8. **Safety net in both routes.** Each route writes `data/run/<route>.json` with its pid and the port it actually listens on (including a `--port` override) at start, and removes it on a clean shutdown.
   - The monitor of each route checks at every poll whether the other route runs. Its file names a live pid whose port answers. Without a file, the port from `.env` is used: `WS_HUB_PORT` or `RECORDER_PORT`, default 8780.
   - If the other route runs: skip the poll and log once, expire an open prompt, and refuse a Yes. The refusal is checked when the answer is processed and again just before the recording starts.
   - With both routes running, neither prompts and neither starts from a prompt.
   - A file from a dead process does not count.
   - There is no lock and no handover between routes. After a switch the new route starts without suppression, which is accepted for a deliberate switch.

9. **The notetaker autostart stays off by default** (`AUTOSTART_RECORDER_ENABLED`) until an acceptance run on a real Mac has passed every point.

## Alternatives considered

**Both routes at once, with an ownership lock and handover.** More state and more failure modes, and two recordings of one call have no use.

**Stopping automatically when the microphone is released.** It cuts long muted calls while it is unknown whether muting releases the microphone.

**Treating a free port as a finished shutdown.** It races the finalisation of the recording, because the listener closes before the shutdown transition has finished.

**Pushing prompts only as events, without a snapshot.** A view that connects late or reconnects misses them, and the person never sees the question.

## Consequences

- The launcher changes from "both may run" to "switch".
- A second prompt in a long muted call is possible and documented.
- The WebSocket contract in `docs/TTD.md` section 7 gains the consent messages.
- Each route gains a run file under `data/run/` that becomes part of its observable behaviour.
- The notetaker autostart ships off and is switched on only after the acceptance run.
