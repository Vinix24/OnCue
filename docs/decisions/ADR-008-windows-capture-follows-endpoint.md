# ADR-008: The Windows capture route follows the call's endpoint

Status: Accepted

Amended 2026-11-19: point 7 now says the per-process route is built ahead of the test round, as an opt-in fallback (operator decision).

Date: 2026-11-18

## Context

Today OnCue on Windows opens a WASAPI loopback on the default render endpoint (`src/sales_copilot/audio/wasapi.py`). `AUDIO_WASAPI_ENDPOINT_NAME` overrides this with a named endpoint. While no override is matched, the stream re-attaches when the default output device changes.

A measurement on 2026-09-06 showed the capture following the wrong device. OnCue tapped the default output (the speakers) while Teams played on a Bluetooth headset. Loopback works on every render endpoint, a Bluetooth headset included. What is needed is following the right endpoint, not tapping per process.

Per-process capture was the earlier plan: a C++ helper using process loopback. A comparable local product reported that process loopback in "everything except the recorder" mode heard nothing of the new Teams on part of their PCs. Windows had set the app volume of that capture to 1% and the slider could not be moved. Endpoint loopback worked there. This is one reported case, not our own measurement. It weighs heavily because the per-process route is also the most expensive part: a C++ helper with no Windows runner in CI.

Phone Link plays the call in the normal mix on Windows (measured 2026-11-11), so an endpoint route hears it as well.

## Decision

1. **Follow the endpoint where the call plays.** Every 2 s OnCue picks the target endpoint in this order:
   - the render endpoint on which a process from the app list has an active audio session (`IMMDeviceEnumerator`, per endpoint `IAudioSessionManager2` and its session enumerator, session state active);
   - otherwise the default communications device (`GetDefaultAudioEndpoint(eRender, eCommunications)`);
   - otherwise the default output device (`eConsole`), as today.

   `AUDIO_WASAPI_ENDPOINT_NAME` stays an explicit override and always wins. The app list is data, not code, and it starts from the Windows call-detection list (`_MEETING_APP_CANDIDATES_WIN32`: `chrome.exe`, `Teams.exe`, `ms-teams.exe`, `Zoom.exe`) and adds `msedge.exe`, `WhatsApp.exe` and `PhoneExperienceHost.exe` (the last one for Phone Link). Call detection keeps its own list; this record does not change it.

2. **Tie rule.** If two endpoints have an active session of a listed process, pick the one that is the default communications device at that moment. If neither is, keep the current endpoint and log the doubt. There is no signal on Windows for which process uses the microphone, so that is not part of this contract.

3. **Continuity: what is and is not promised.** A gapless switch cannot be guaranteed. A change is seen at the next poll (at most 2 s), or earlier through `IMMNotificationClient` when the default device changes, and opening a loopback takes time too. Promised:
   - on a change, OnCue opens the loopback on the new endpoint before closing the old one;
   - the tracks stay aligned through silence frames on the fixed time base;
   - one log line per switch with the old endpoint, the new endpoint and the length of the gap;
   - if an endpoint disappears (the headset is switched off), the next one in the order is picked at once; if no endpoint comes back within 10 s, `tap_health` reports `no_tap` and the existing audio warning appears.

   The human test round (D3) measures the gap per switch. A gap longer than 3 s on an ordinary switch fails that point.

4. **No new dependency.** The COM calls go through `ctypes`, like `audio/mic_usage.py` on the Mac and the COM bootstrap `audio/wasapi.py` already has. If `ctypes` turns out unworkable for session enumeration, a new dependency is an operator decision.

5. **Windows 10 and 11.** Endpoint loopback and session enumeration exist on both, so this route supports both.

6. **Rollout behind a switch.** The code lands behind `WINDOWS_ENDPOINT_FOLLOW`, off by default. With the switch off OnCue behaves exactly as today. Turning it on by default is its own PR, and only after a human test round in which every point passed.

7. **Per-process capture is an opt-in fallback route, built ahead of the test round.** Operator decision 2026-11-19: it is built now, so a failed Phone Link test on the endpoint route can switch at once instead of waiting for a build. It is process loopback in exclude mode (everything except OnCue's own process tree) behind `WINDOWS_CAPTURE_ROUTE=process`; `endpoint` stays the default. No C++ helper: it is plain ctypes (`audio/wasapi_process.py`), so point 4 holds. It checks the app volume Windows holds for its own capture and warns below 10% or when muted. Whether it ever becomes a default is decided after the human test round.

8. **The limit, stated in the user docs.** Loopback on an endpoint records everything that plays on that endpoint, notifications and music included. Whoever does not want that puts calls on their own headset. Echo on laptop speakers falls under the echo filter of #319.

## Alternatives considered

**Per-process capture as the default (the earlier plan).** Rejected for the reasons in Context: the one reported silent-capture case and the cost of a C++ helper without a Windows CI runner. Kept as a fallback (point 7).

**Keep the default output only.** Rejected: it is exactly what missed the call on 2026-09-06.

**A microphone-owner signal on Windows to break ties.** Not available, see point 2.

## Consequences

- The capture gains a poll and a switch path, and a human test round on real hardware is needed before the default flips.
- A promise of gapless capture is ruled out. The gap is logged and measured instead.
- The override stays, and with `WINDOWS_ENDPOINT_FOLLOW` off OnCue behaves as it does today.
- The user docs state the limit of point 8 when the switch lands.
- The Windows installer (`product-packaging` phase 2) waits for the PR that turns the switch on by default.
