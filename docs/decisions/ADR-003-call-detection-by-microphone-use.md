# ADR-003: Detect a call by microphone use per process

Status: Accepted. Point 7 superseded by ADR-005 for the browser.

Date: 2026-11-11

## Context

The autostart recognises a call by a running process. `resolve_meeting_app_target` looks for a meeting app with `pgrep`, and `resolve_telephony_call_target` looks for `avconferenced` for phone calls. Both are polled by `RuntimeAutostartMonitor` in `src/sales_copilot/core/autostart_monitor.py`.

A meeting app and a browser are open all day. `avconferenced` also runs without a call. Process presence says nothing about whether a call is going on.

The notetaker route (`scripts/record_server.py`) is the launcher default and has no autostart at all.

The requirement: when someone calls me, OnCue can record if I want, not only when I call out. Consent per call stays mandatory. Nothing ever records silently.

## Decision

1. **Signal.** From macOS 14.2 Core Audio lists one object per process that uses audio (`kAudioHardwarePropertyProcessObjectList`). Each object carries the pid, the bundle id and whether the process is capturing input right now (`kAudioProcessPropertyIsRunningInput`).
   - A new module `src/sales_copilot/audio/mic_usage.py` reads that list through `ctypes` on `CoreAudio.framework`. No new binary.
   - Selector values come from the SDK headers.
   - OnCue's own processes (itself, its children, AudioTee) are excluded.
   - It reads process ids and bundle ids only, never audio.

2. **Three outcomes, always one of them.**
   - *unavailable*: another platform, or macOS below 14.2.
   - *ok*: a list that may be empty.
   - *read error*: with a reason.

   Only *unavailable* makes the monitor fall back to today's process detection, decided once at start. Windows keeps process detection.

3. **Observation age.** The monitor keeps the time of the last successful observation. A read error is not an observation.
   - When the last successful observation is older than 10 s (five polls at 2 s), a pending prompt expires, the debounce counters reset, and no new prompt comes until a successful observation.
   - A read error never falls back to process detection and never starts anything.
   - After 30 s of errors the monitor logs one error and reports "autostart temporarily unavailable".

4. **One detection is one app.** The app is identified by bundle id, so its helper processes count together, and it has used the microphone without interruption for the debounce period. Two listed apps at once give no prompt, as with two candidates today.

5. **The app list is data, the switches are settings.**
   - `config/autostart_mic_apps.yaml` maps an app's bundle ids to a source (video or phone) and to the capture source (`prospect_process` or the system mix). It follows the pattern of `config/pain_points.yaml`.
   - On/off, debounce and poll interval stay `AUTOSTART_*` settings in `.env` (`AUTOSTART_MONITOR_ENABLED`, `AUTOSTART_POLL_INTERVAL_S`, `AUTOSTART_DEBOUNCE_ARM_POLLS`, `AUTOSTART_DEBOUNCE_DISARM_POLLS`).

6. **Admission is decided by a measurement on a real Mac, before the detection is built.** An app goes on the default list only when the measurement shows all three:
   - during a call it is seen as a microphone user;
   - while it is merely open, without a call and without using the microphone, it is not;
   - a capture source records the other party audibly.

   Using the microphone outside a call in a listed app (a microphone test, a voice memo, dictation) gives a prompt. That is an accepted false positive. It is safe because nothing records without Yes, and No suppresses further prompts (ADR-004). It is measured and documented. An app that uses the microphone while merely open is left off.

7. **Required and optional apps.**
   - The macOS call process and the meeting app the operator uses are required. A browser and any other app are optional.
   - A required app that fails the measurement means this approach cannot meet the requirement. The work stops for a new decision.
   - An optional app that fails is simply left off.
   - If reading the list triggers a macOS permission prompt, the work also stops for a decision.

## Alternatives considered

**Keep process presence.** This is the problem itself: the signal is true all day.

**A separate helper binary that watches audio devices.** More to build, sign and ship, and it reports the same information Core Audio already gives per process.

**Watching audio levels.** It means reading audio, which is a privacy cost, and it gives no app identity. Silence and speech in an unknown app tell nothing about whether it is a call.

**Accepting every app that uses the microphone.** It prompts for dictation and voice memos everywhere. A list per app keeps the prompts to apps that have passed the measurement.

## Consequences

- macOS 14.2 is the floor for the new signal. Below it, and on Windows, process detection stays.
- The measurement gates the build. If a required app fails it, the approach is dropped and a new decision is needed.
- False positives in listed apps (microphone test, voice memo, dictation) are possible and documented.
- Read errors are visible instead of silently weakening detection: a stale observation expires the prompt, and a long run of errors is logged and reported.
- The app list becomes configuration that can change without a code change.
