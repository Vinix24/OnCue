# ADR-007: Autostart layers per license and three opt-in macOS permissions

Status: Accepted

Date: 2026-11-18

## Context

ADR-005 makes the notetaker the base of every call and the sales assistant a layer on the same recording. The question at the start of a call offers "notes only" (`layer: notes`) and "notes plus sales assistant" (`layer: notes_plus_assist`), next to No. ADR-004 fixes the prompt lifecycle and its messages: `consent_required`, `consent_resolved`, `pending_consent` and `consent_snapshot`.

Three things are not settled by those records:

- **The license.** `RuntimeAutostartMonitor` in `src/sales_copilot/core/autostart_monitor.py` arms only when `FeaturePolicy` allows `FEATURE_AUTOSTART` (`"system.autostart"` in `src/sales_copilot/auth/license_format.py`). The whole autostart is therefore paid, including a start that only takes notes. Live coaching (`FEATURE_LIVE_COACHING`, `"coaching.live"`) has since moved to the free tier (PR #370), so a manual start with the assistant is already free. When the grace window of a license ends, `FeaturePolicy` degrades the key to free (`degraded_to_free`) and the whole autostart switches off.
- **Naming browser calls.** ADR-003 detects a call by microphone use per process. For a browser the prompt can only say the browser's name. ADR-005 makes browser calls required, so a better name is wanted.
- **Seeing the prompt.** The prompt appears in the recording bar (ADR-001) and in the dashboard. A person in another app does not see it.

## Decision

1. **The autostart prompt and the `notes` layer are free.**
   - The question at the start of a call, and the layer "notes only", work without a license.
   - The layer "notes plus sales assistant" (`notes_plus_assist`) through the autostart needs `system.autostart`.
   - Starting a recording with the assistant by hand stays free, because `coaching.live` is in the free tier.

2. **One owner for the layer contract.** When it creates a prompt, the monitor asks `FeaturePolicy` which layers it may offer and stores them as `offered_layers`:
   - no license: `["notes"]`;
   - with `system.autostart`: `["notes", "notes_plus_assist"]`;
   - the standalone copilot route: `["notes_plus_assist"]` with a license, and no prompt at all without one.

   `offered_layers` is part of `consent_required`, `pending_consent` and `consent_snapshot` (the messages from ADR-004). A view shows only the offered layers.

3. **The start path checks again.**
   - A Yes with a layer that is not in `offered_layers` is refused and logged, and `consent_resolved` carries `rejected_layer`.
   - Right before the recording starts, the start path checks the license once more. If `notes_plus_assist` is no longer allowed at that moment (for example the grace window just ran out), nothing starts, the prompt expires (`expired`), and the next poll raises a new prompt with only `notes`.

4. **Degrade-to-free only drops the paid layer.** After the grace window ends, the autostart stays on. Only `notes_plus_assist` disappears from `offered_layers`. Today degrade-to-free switches the whole autostart off; that changes.

5. **Two optional naming signals for browser calls, off by default.** Microphone use per process (ADR-003) stays the only signal that can raise a prompt. Two signals may give the prompt a name, never more:
   - The title of the front window of the browser that uses the microphone, read through Automation (AppleScript for Safari and Chromium browsers). If it contains a known meeting service (Meet, Teams, Zoom, Proton Meet), that becomes the prompt's name. Otherwise the browser's name stays.
   - An item in a chosen local calendar that starts within 10 minutes before or after now and has a call link to `teams.microsoft.com`, `meet.google.com`, `zoom.us` or `meet.proton.me` in its location, notes or URL. With several items, the one that starts closest to now wins. Its title becomes the prompt's name.

   Neither signal ever suppresses a prompt. A call in a background tab leaves a different title in the active tab, and suppressing on that would miss a call in a required browser (ADR-005 point 3).

6. **The prompt also as a macOS notification.**
   - `OnCue Bar.app` (ADR-001) runs as a background agent (`LSUIElement`) from login and shows the prompt through `UNUserNotificationCenter`.
   - There is one notification per `prompt_id`, with that id as identifier. Its buttons are the offered layers plus No.
   - On `consent_resolved`, on expiry and on `pending_consent: null` the agent removes it.
   - If the agent is not running, the server starts it with `open -g`.
   - A python server or `osascript` cannot do this: `display notification` has no buttons and `display dialog` blocks.
   - A signed notification outside the development environment depends on the signed bundle from ADR-006.

7. **Three new macOS permissions, all opt-in, none required.** They are Automation (window title), Calendars (calendar item) and Notifications.
   - Each signal is off until the user switches it on.
   - A refused permission means no name, or no notification, and the prompt goes on exactly as without that signal. A refusal is logged once.
   - A refused or suppressed notification (Do Not Disturb, screen sharing) falls back to the prompt as a window in the recording bar, without stealing focus, and in the dashboard.
   - Detection itself never depends on any of the three.

## Alternatives considered

**Keep the whole autostart Pro (today's state).** The free notetaker should start with every call, so the prompt and the `notes` layer cannot sit behind a license.

**Let tab title or calendar suppress a prompt when they say "no call".** A call in a background tab shows another title in the active tab, and a calendar knows nothing of an unplanned call. Suppressing on either misses a call in a required browser.

**Show the notification from the python server.** `osascript` `display notification` has no buttons, and `display dialog` blocks the caller. Only an app with `UNUserNotificationCenter` can show a notification with the offered layers as buttons.

## Consequences

- The license check now lives per layer, in two places: when the prompt is offered and again when the recording starts. Both must agree, and the second one is the one that counts.
- A silent recording stays ruled out, and no signal decides on its own: window title and calendar only name a prompt, notifications only show it.
- The consent click per call stays (ADR-004). Nothing records without Yes, on any layer.
- A license that lapses between the prompt and the click costs one extra prompt with only `notes`, not a lost recording.
- Three permissions are new, each behind a switch. A person who grants none gets the same detection and the same prompt in the bar and the dashboard.
- A notification outside the development environment waits for the signed bundle from ADR-006.
- No point of ADR-003, ADR-004 or ADR-005 is superseded. `offered_layers` adds a field to the ADR-004 messages and narrows the choice that ADR-005 point 2 describes, so those records stay as they are.
- `docs/TTD.md` section 7 gains `offered_layers` and `rejected_layer` when the code lands, not with this record.
