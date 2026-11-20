# ADR-005: One recording per call, the notetaker first and the assistant as a layer

Status: Accepted

Date: 2026-11-13

## Context

OnCue has two routes that record the same call:

- the copilot route: live analysis, hub on `WS_HUB_PORT` (default 8760);
- the notetaker route: transcript afterwards, `scripts/record_server.py`, port 8780.

ADR-004 points 7 and 8 keep them apart with a switch in the launcher and a port safety net.

The product is used first as a notetaker. People want every online call written down without starting anything by hand. The live assistant is wanted in a part of those calls, the sales calls.

Calls also run in the browser. The current detection (ADR-003 point 7) treats the browser as optional.

## Decision

1. **The notetaker is the base, the assistant is a layer.**
   - Every call has one recording from its start.
   - Live detection and coaching run as a layer on that same audio.
   - There are no longer two routes that each record.

2. **The choice is made at the start.**
   - The question at the start of a call offers "Alleen notuleren" (notes only) and "Notuleren + sales hulp" (notes plus sales help), next to No.
   - A manual start offers the same choice.

3. **Autostart at every online call.**
   - It applies to the desktop app of a video-call service and to the browser alike.
   - Detection stays on microphone use per process (ADR-003).
   - Browser calls are required, not optional. This supersedes ADR-003 point 7 for the browser.
   - A browser or meeting app that is only open, without using the microphone, is not a call and gives no question.

4. **One server.**
   - The notetaker server and the hub become one server, started at login, so the person never starts OnCue by hand.
   - How the two are merged is the subject of the plan document of the library track.
   - This ADR fixes only that there is one server and one recording.

5. **What stays from ADR-004.**
   - Points 1 to 6 carry over to the one server unchanged: no automatic stop, prompt identity and validity, suppression, Yes through the normal start, the snapshot on connect, the messages.
   - Point 9 stays: the notetaker autostart ships off until the acceptance run on a real Mac passes.

6. **What this ADR supersedes, and when.**
   - ADR-004 point 7 (one route at a time, the switch in the launcher) and point 8 (the run files and the port safety net in both routes) become obsolete with one server.
   - They stay in force until the one-recording server is on main. They are removed only in the same change that lands it.
   - Until then two routes can still run, so the guard against two recordings of one call must remain.

## Alternatives considered

**Keep two routes and let the person pick a mode at launch.** Two programs to start, and the notetaker misses every call the person did not start by hand.

**The assistant as the base with the notetaker as an option.** The common case, notes for every call, becomes the extra step.

**Detect browser calls only when a known meeting site is in the foreground.** It needs reading browser state, which per-process microphone use does not.

## Consequences

- The autostart work for the notetaker route (D6 server, D7 view in the bar and the recording page) becomes autostart in the one server.
- D4 (trigger on microphone use per process) must recognise browsers as call apps.
- The order is: D2 (measurement on a real Mac, including a browser call and a browser open without a call), then D4, then the one server, then D8 (acceptance on a real Mac), then D9 (on by default and docs).
- A login item for the one server is needed. Today only the copilot has one.
- `docs/TTD.md` and the start flow change when the one server lands, not with this ADR.
