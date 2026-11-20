# ADR-001: Recording bar as a separate macOS app

Status: Accepted

Date: 2026-11-11

## Context

The notetaker route is the launcher default. It runs `scripts/record_server.py` on port 8780 and serves the recording page `dashboard/recorder.html` (record button, timer, one meter per track) in a browser tab.

During a call in a video-call app in full screen, that tab is hidden. You cannot see whether recording runs. A track that goes silent is only noticed afterwards.

What the server already offers:

- It pushes a `level` message every 0.1 s on `/ws`. Per track it carries `db`, `peak_db`, `silent`, `started` and `error`.
- It pushes a `state` message with the phase: `idle`, `recording`, `transcribing`, `ready` or `failed`.

What makes a quit action risky today:

- `AudioRecorder` buffers audio and writes it to the WAV files every `flush_seconds`. Each write also updates the WAV header, so the files on disk stay readable. `metadata.json` is only written in `finalize()`. A process that stops without finalizing loses the audio buffered since the last write, and leaves the session without `metadata.json`.
- The transcription queue in `record_server.py` (`_jobs`) lives in memory and is drained by a daemon thread. A session still waiting for transcription at shutdown never gets one.
- A bar with a quit action and a hotkey makes quitting during a recording more likely than it is today.

## Decision

1. **Packaging.** `native/oncue-bar` is a SwiftPM package with minimum macOS 14.2, the same floor as AudioTee. It builds a small `OnCue Bar.app` with `LSUIElement` and its own bundle id (for logging). It is installed at `bin/OnCue Bar.app`. It is not a bare executable: WKWebView and a global hotkey follow the normal Cocoa model inside a bundle.

2. **Window.** The app shows `http://127.0.0.1:<port>/bar` in a WKWebView inside a borderless, non-activating floating panel (NSPanel) that does not take focus. It is not in the Dock and not in Cmd-Tab. It appears on every Space and above an app in full screen (`canJoinAllSpaces`, `fullScreenAuxiliary`, a window level above normal windows). The port comes in as a launch argument.

3. **Placement and clicks.** A compact rectangular block, top right on the main screen, draggable. The window is exactly the size of the block and the whole block is clickable, so no partial click-through is needed: the window has no empty parts. A click on the logo opens the recording page in the default browser.

4. **Client of the existing server.** The bar uses the same `/ws` stream as `recorder.html`, with the same Host and Origin checks. No new port and no second channel. Start and stop (and later pause and resume) behave the same from the browser, the bar and the hotkey, because all three send the same websocket actions. A `GET /status` route returns the phase, the recording time, the session and the server process id. It serves the launcher and the stop script, is local only and applies the same checks.

5. **Lifecycle.** The bar is its own process and stays when the browser tab closes. The launcher starts it after the server answers, passes the port and writes both process ids. If the bar is missing after installation, OnCue works as today: the launcher logs it and says so once, and the recording page stays the full control.

6. **Shutdown transition, one for every route**, tied to the Uvicorn lifecycle in `scripts/record_server.py`:
   - refuse new commands;
   - let a start or stop in progress finish;
   - finalize a running recording once with `engine.stop()`;
   - every session still waiting for transcription keeps a marker file `.transcribe-pending` in its session folder and is not discarded.

7. **Recovery at server start.**
   - Sessions with `.transcribe-pending` and without `transcript.md` go back into the queue.
   - A session this server started (it carries a `.recording` marker from start until a finished stop) with audio but without `metadata.json` gets a `metadata.json` built from its WAV files, with `"recovered": true`, then goes into the queue. If a WAV header disagrees with the data on disk (a stop between writing data and updating the header), the header is rewritten to match the data. Recovery never changes the audio data. Session folders without that marker, such as recordings by the copilot route in the same folder, are never touched.
   - A session that cannot be recovered gets the reason written in its folder and stays where it is.
   - The marker file goes away only when `transcript.md` exists or the transcription has failed for good. In that case the server writes the reason in the session folder.

8. **Two quit routes, one transition.**
   - "Quit OnCue" in the bar (right-click) asks for confirmation first when a recording runs.
   - `scripts/launcher/stop-oncue.command` stops the recording server and the bar as well, each by the process id the launcher wrote. It reads `/status` first. During a recording it asks for confirmation. When `/status` does not answer it says the status is unknown and asks whether to stop anyway.
   - Stopping is always SIGTERM so the transition runs.
   - The question is a convenience, not a guarantee. The guarantee is the transition.

9. **A lost connection means "status unknown", not "OnCue is not running".** The recording may well continue. The bar turns striped grey with "connection lost", disables its buttons and retries every 2 s. After reconnecting it enables the buttons only once a fresh `state` message has arrived.

10. **Hotkey.** System wide through the Carbon hotkey API, which needs no accessibility permission. In the ready state it starts a recording. During a recording or a pause it stops. The default is Control-Option-R, configurable with `BAR_HOTKEY` in `.env`. If registration fails the bar logs it and works without a hotkey.

11. **Build and distribution.** `scripts/build_macos_binaries.sh` builds `OnCue Bar.app` next to AudioTee, signs it ad hoc, verifies the signature and puts it in the same release tarball (`oncue-macos-arm64.tar.gz`). There is no updater and nothing that removes the quarantine attribute.

12. **Scope.** macOS only. On Windows the recording page in the browser stays the control. The copilot route (port 8760) gets no bar.

## Alternatives considered

**A bare executable instead of an `.app`.** Simpler to build, but there is no bundle id for logging, no `LSUIElement` to keep it out of the Dock, and WKWebView and the global hotkey are meant to live inside a bundle. It lost on fit with the Cocoa model, not on effort.

**A wide strip with click-through on its empty parts.** A thin full-width strip would use less screen area per pixel of content. It needs partial click-through, which means hit-testing on a window with transparent regions, and it puts an invisible layer over a video-call app. A block that is exactly the size of its content has no such zones and no failure mode where a click lands on the wrong app.

**Keeping the recording page as the only view.** No native component to build, sign and ship. But the tab is hidden in exactly the situation where status matters, so a silent track is still found after the call. It stays as the full control and as the fallback, not as the only view.

**Treating a lost connection as "OnCue stopped".** It would let the bar show a calm "not running" state. That is wrong when the server is busy or restarting while a recording continues, and it invites the user to start a second recording. "Status unknown" is the only claim the bar can back up.

**Quitting without a shutdown transition, as today.** Nothing new to build. But a quit during a recording leaves a session without `metadata.json`, and a session waiting for transcription is silently dropped. With a quit action in the bar this becomes a routine event, so it cannot stay as it is.

## Consequences

- OnCue gains a native component that has to be built, signed, installed and shipped. The installer, the bootstrap script and the public export must all carry it.
- Compiling is not proof. The window needs a check on a real Mac, above a video-call app in full screen.
- A hard stop (SIGKILL, power loss) still loses at most the last `flush_seconds` of audio. The docs must say so.
- The shutdown transition and the recovery must exist before any quit route ships.
- `GET /status`, the `.transcribe-pending` and `.recording` markers and the `"recovered": true` field become part of the server's observable contract.
