# Troubleshooting (macOS)

Use this document when the setup or live run doesn't work as expected.
All examples assume the project root as the current directory.

<a id="problem-blackhole-verschijnt-niet-in-audio-midi-setup"></a>
## Problem: BlackHole doesn't appear in Audio MIDI Setup

See also FAQ: [The install script fails on BlackHole](FAQ.md#faq-het-installatiescript-faalt-op-blackhole-unidentified-developer)

BlackHole is the fallback route (the default is AudioTee, which needs no audio device); the installer sets it up regardless so the fallback is available.

**Symptom:** you don't see "BlackHole 2ch" in the input/output lists.
**Diagnosis:** `brew list --cask blackhole-2ch && system_profiler SPAudioDataType | rg -i blackhole`
**Fix:**
1. `brew reinstall --cask blackhole-2ch`
2. Restart macOS
3. Check again in Audio MIDI Setup
**Why this happens:** a driver/cask install isn't always immediately visible without a restart, or was blocked by system policy.

## Problem: Multi-Output Device creation fails (Core Audio error)

See also FAQ: [Which multi-output device should I choose in Audio MIDI Setup?](FAQ.md#faq-welk-multi-output-device-moet-ik-kiezen-in-audio-midi-setup)

**Symptom:** the installer reports that automatic audio setup failed.
**Diagnosis:** `python scripts/create_audio_setup.py --dry-run`
**Fix:**
1. Open Audio MIDI Setup
2. Manually create a Multi-Output Device
3. Name it exactly `OnCue Output`
4. Check Speakers + BlackHole 2ch
**Why this happens:** macOS aggregate device APIs and UI scripting are fragile and depend on permissions, language settings, and OS state.

<a id="problem-microphone-permission-denied-voor-terminalpython"></a>
## Problem: Microphone permission denied for Terminal/Python

See also FAQ: [My microphone isn't detected by OnCue](FAQ.md#faq-mijn-microfoon-wordt-niet-gedetecteerd-door-sales-copilot)

**Symptom:** `scripts/verify_audio.py` shows the mic as SILENT/FAIL despite a working microphone.
**Diagnosis:** `python scripts/verify_audio.py --skip-tone`
**Fix:**
1. Open `x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone`
2. Turn on Terminal (or iTerm)
3. Quit Terminal completely and restart it
**Why this happens:** permission applies per app binary; switching between Terminal/iTerm requires granting it again.

## Problem: Whisper model download stalls or fails

See also FAQ: [Script hangs at "Downloading Whisper model"](FAQ.md#faq-script-hangt-bij-downloading-whisper-model)

**Symptom:** the installer hangs for a long time or ends with a download error.
**Diagnosis:** `python scripts/install_whisper_model.py --model large-v3-turbo`
**Fix:**
1. Check network/VPN/proxy
2. Try again with a smaller model variant (`--model base`)
3. Switch back to `large-v3-turbo` later when the network is stable
**Why this happens:** model files are large and depend on external hosting (Hugging Face).

## Problem: Port 8760 already in use

See also FAQ: ["Port 8760 already in use" — what now?](FAQ.md#faq-port-8760-already-in-use-wat-nu)

**Symptom:** the backend won't start or the log reports a bind/listen error on port 8760.
**Diagnosis:** `lsof -nP -iTCP:8760 -sTCP:LISTEN`
**Fix:**
1. Stop old processes: `bash scripts/stop.sh`
2. If needed, forcefully: `pkill -f "python -m sales_copilot"`
3. Start again: `bash scripts/start.sh`
**Why this happens:** a previous run or another service is using the same port.

<a id="problem-dashboard-start-call-mislukt"></a>
## Problem: Dashboard "Start Call failed"

See also FAQ: [The "Start Call" button shows an error](FAQ.md#faq-start-call-knop-geeft-een-foutmelding)

**Symptom:** the setup screen shows a start error right after clicking Start Call.
**Diagnosis:** `tail -n 120 data/logs/runtime.log`
**Fix:**
1. Pull the latest main (historical issue fixed in PR-84)
2. Restart the backend via `bash scripts/stop.sh && bash scripts/start.sh`
3. Check that the dashboard uses the correct base URL to localhost
**Why this happens:** an old frontend/backend mismatch or an outdated API endpoint configuration.

## Problem: No transcription appearing after 30s

See also FAQ: [Transcription doesn't appear (or only after 30 seconds)](FAQ.md#faq-transcriptie-verschijnt-niet-of-pas-na-30-seconden)

**Symptom:** the call is running, but the transcript stays empty.
**Diagnosis:**
- `python scripts/verify_audio.py`
- `tail -n 120 data/logs/runtime.log | rg -i "transcrib|whisper|audio"`
**Fix:**
1. Wait 30-60s (first whisper warmup)
2. The default AudioTee route taps the entire system output mix (`tap_all`) — you don't have to select a meeting app and no output routing is needed. Check that system audio is actually playing (the other side is audible).
3. If you manually use the BlackHole route (`AUDIO_CAPTURE_METHOD=blackhole`): check that output is set to `OnCue Output` and that BlackHole is receiving audio.
4. Restart the run after the fix
**Why this happens:** usually warmup delay or (on the manual BlackHole route) a wrong output route — not an actual model failure.

## Problem: Transcription labels YouTube audio as "JIJ"

See also FAQ: [YouTube audio gets tagged as "JIJ" while I'm not talking](FAQ.md#faq-youtube-audio-wordt-getagged-als-jij-terwijl-ik-niet-praat)

**Symptom:** system audio is labeled as your own speaker.
**Diagnosis:** `tail -n 200 data/logs/runtime.log | rg -i "single-stream|fallback|speaker"`
**Fix:**
1. Make sure dual-stream capture is active
2. Avoid the single-stream fallback (check the audio device setup)
3. Check the PR-85 related config/defaults in the current branch
**Why this happens:** in single-stream mode speaker separation is limited; labeling can shift.

<a id="problem-timer-in-dashboard-loopt-niet-terwijl-ik-wl-audio-hoor"></a>
## Problem: Timer in the dashboard doesn't run even though I do hear audio

See also FAQ: [Talk-time timer gets stuck while audio is running](FAQ.md#faq-talk-time-timer-blijft-hangen-terwijl-audio-wel-loopt)

**Symptom:** you hear audio/transcription but the talk-time timer stays still or updates too slowly.
**Diagnosis:** `tail -f data/logs/runtime.log | rg "talk_time_snapshot"`
**Fix:**
1. Check `.env` for `TALK_TIME_HEARTBEAT_MS=1000` (or another desired value).
2. Restart the backend with `bash scripts/stop.sh && bash scripts/start.sh`.
3. Start the call again and verify that `talk_time_snapshot` keeps arriving every second.
**Why this happens:** in single-stream BlackHole mode VAD events are sometimes sparse; heartbeat events keep the timer and ratio updates running between speech events.

## Problem: Module crashes silently

See also FAQ: [`data/logs/runtime.log` is empty / contains errors](FAQ.md#faq-data-logs-runtime-log-is-leeg-bevat-errors)

**Symptom:** the UI seems silent, but there's no clear error in the browser.
**Diagnosis:** `tail -n 200 data/logs/runtime.log`
**Fix:**
1. Look at the stacktrace in the runtime log
2. Remove stale PIDs/processes with `bash scripts/stop.sh`
3. Start again with `bash scripts/start.sh`
4. If needed: run foreground `python -m sales_copilot`
**Why this happens:** a background run sends errors to the log file; without checking the log file it seems "silent".

## Problem: Corporate Mac blocks installation

See also FAQ: [My work Mac refuses the installation (corporate MDM)](FAQ.md#faq-mijn-werk-mac-weigert-de-installatie-corporate-mdm)

**Symptom:** brew/cask install fails or permissions can't be changed.
**Diagnosis:**
- `brew doctor`
- `profiles status -type enrollment`
- error message text in the installer output
**Fix:**
1. Ask IT for temporary or permanent permission for:
   - Homebrew packages/casks
   - Audio driver install (BlackHole)
   - Microphone + Screen Recording permissions for Terminal/iTerm
2. Have IT run the installer together with you
**Why this happens:** MDM/security policies block local developer-like behavior on managed devices.

<a id="problem-wasapi-loopback-capture-geeft-geen-audio-windows"></a>
## Problem: WASAPI loopback capture produces no audio (Windows)

See also FAQ: [Does this work on Windows / Linux / iPad?](FAQ.md#faq-werkt-dit-op-windows-linux-ipad)

Windows support is experimental and uses WASAPI loopback (`soundcard`) instead of AudioTee/BlackHole. The stream never fails hard: any setup error lets the prospect stream keep running silently with an `audio_warning` on the dashboard instead of crashing the call.

**Symptom:** the prospect stream stays silent (no transcript from the other side), the dashboard shows an `audio_warning`.
**Diagnosis:** `tail -n 120 data/logs/runtime.log` (or the `audio_warning` panel on the dashboard) and match the exact text:
- `"Python-package 'soundcard' ontbreekt"` → package not installed.
- `"Geen standaard audio-uitvoerapparaat gevonden"` → Windows has no default playback device.
- `"Kon geen WASAPI-loopback-apparaat openen"` → the default device doesn't support loopback recording.
- `"WASAPI-loopback-apparaat gaf een fout of verdween tijdens opname"` → the device was switched/disconnected during the call.

Reproducible on its own without starting a call:
```bash
.venv\Scripts\python -c "import soundcard as sc; s=sc.default_speaker(); print(s); print(sc.get_microphone(s.id, include_loopback=True))"
```

**Fix:**
1. Package missing: `pip install ".[windows]"` (installs `soundcard>=0.4.6`).
2. No default device: set a default playback device via Windows Settings > System > Sound, and start the call again.
3. Device switched/disconnected during a call (e.g. Bluetooth headphones connected or disconnected): stop and start the call again — that reopens the WASAPI stream against the (new) default device.
4. Check that `AUDIO_CAPTURE_METHOD` is set to `wasapi`, or is empty (that's the Windows default — see `.env.example`).
**Why this happens:** `WasapiLoopbackStream` is "degrade-not-raise": any setup error logs a warning and leaves the stream silent instead of crashing the background task. So the cause is literally in the warning text.

## Problem: I hear myself back / echo during the call (Windows, WASAPI)

See also FAQ: [Does this work on Windows / Linux / iPad?](FAQ.md#faq-werkt-dit-op-windows-linux-ipad)

**Symptom:** you hear your own voice back with a delay, or the transcription contains duplicate/repeated sentences.
**Diagnosis:** are you playing the call through speakers instead of a headset/headphones?
**Fix:**
1. Use a headset or headphones, not speakers.
2. WASAPI loopback taps the entire system output channel (whole-endpoint) — unlike AudioTee on macOS, it can't isolate a single app. With speakers your microphone picks that output channel back up (self-capture/echo).
**Why this happens:** inherent to whole-endpoint loopback, not a bug. See the [Windows section in INSTALL.md](../INSTALL.md#windows).

## Extra: quick health scan

Run this set as first triage:

```bash
python scripts/verify_audio.py
lsof -nP -iTCP:8760 -sTCP:LISTEN
bash scripts/stop.sh
bash scripts/start.sh
```

If this isn't enough, share the output + `data/logs/runtime.log` in GitHub Discussions or with the support channel.
