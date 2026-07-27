# Setup Expectations (the honest version)

This document is deliberately direct and honest.
If you're setting up OnCue for the first time on macOS, this is not going to be done in 2 minutes.
You're setting up audio routing, permissions, a model download, and local services.
That's powerful, but not frictionless.

## What you get (and what you don't)

### What you do get

- A **one-command installer**: `bash scripts/install.sh`
- Automatic installation of:
  - Homebrew (if it's not there yet)
  - Python 3.11
  - BlackHole 2ch (fallback route for system audio, see below)
  - SwitchAudioSource
  - CMake
  - project dependencies in `.venv`
- Automatic checks on:
  - macOS version
  - Apple Silicon
  - available disk space
- An automatic attempt to create the BlackHole fallback audio routing (Multi-Output). The default capture method is AudioTee: it taps the entire system output mix (`tap_all`) and needs no Multi-Output; this step only prepares the manual fallback.
- A model download step for Whisper
- A controlled start/stop flow (`scripts/start.sh` / `scripts/stop.sh`)

### What you don't get

- No native `.app` installer
- No "click-next" wizard
- No magic bypass of macOS security
- No guarantee that corporate device policy allows everything

## Realistic time estimate

### First time

- **Technically minimal time:** 10-15 minutes
- **Realistic time for most people:** 20-30 minutes
- **If permissions/IT policy get in the way:** 30-60+ minutes

### Second time on the same Mac

- Often 1-3 minutes (most of it is already in place)

## Who shouldn't do this solo

Don't try this **on your own** if one of these points is true:

- You've never used Terminal before
- You work on a tightly managed corporate Mac (MDM, limited admin rights)
- You're not allowed to install audio drivers or system tools
- You have little time and expect to be "live right now" without any technical steps

In those cases: arrange help in advance from IT or someone with macOS developer experience.

## The 3 manual clicks you can't skip

Apple blocks automatic consent for sensitive permissions.
That's why you have to do this manually:

1. **Privacy & Security → Microphone**
- Grant permission to Terminal (or iTerm / your shell app)

2. **Privacy & Security → Screen Recording**
- Needed for some audio capture setups

3. **Privacy & Security → Audio Recording (macOS 14.4+)**
- Needed for the AudioTee process tap (the default audio route). Without this permission the copilot warns you and the prospect stream stays silent until you grant it.
- If you manually choose the BlackHole route (`AUDIO_CAPTURE_METHOD=blackhole`): check that the Sound output is set to the correct Multi-Output.

Note: the installer opens the right System Settings pages for you, but you have to set the toggles yourself.

## Clicks, waiting moments, and admin prompts

### Expected manual actions

- 2-3 permission toggles (Microphone + Audio Recording for AudioTee, possibly Screen Recording)
- 1-2 checks in Sound/Audio MIDI Setup (only needed if you choose the BlackHole fallback)
- 1 admin password prompt for Homebrew/cask installs (depending on system state)

### Target number of manual steps

- Aim: **≤3 permission clicks + 1 admin prompt**
- Sometimes more needed on locked-down devices

## What often goes wrong (and why)

### 1. BlackHole isn't in the audio devices

- Cause: cask install failed, security block, or a reboot is still needed
- Effect: the manual BlackHole fallback doesn't work. For the default AudioTee route (which taps the entire system output mix) this makes no difference; it's only a problem if you deliberately want to use the BlackHole route

### 2. The Multi-Output device doesn't exist or has a different name

- Cause: automatic creation failed (API/UI fallback depends on macOS state)
- Effect: only relevant for the BlackHole fallback; audio routing within that fallback is then unstable

### 3. Microphone stays "silent"

- Cause: permission not granted to the right app (Terminal vs iTerm)
- Effect: only system audio or no audio at all

### 4. Whisper model download takes long or seems stuck

- Default model: `large-v3-turbo` (~800 MB, one-time — was 3 GB with `large-v3`)
- First warmup after download: ~5 seconds (was ~30s with `large-v3`)
- Cause of the delay: network speed or HuggingFace throttling
- Effect: setup does continue, but finishes later

### 5. Start Call fails / no transcript after start

- Cause: port conflict, wrong output, first-run warmup
- Effect: the frontend seems silent or shows an error

## Honest comparison: "Isn't Fireflies/Gong just easier then?"

Yes and no.

### What SaaS tools make easier

- Fast onboarding
- Less local setup
- Less system-permission hassle

### What this stack does better

- Data stays local (or under your own control)
- No vendor lock-in for the core flow
- Full control over routing, models, and logging
- Auditable code + customizable behavior

### Quick comparison

| Topic | OnCue (local) | Fireflies/Gong-like SaaS |
|---|---|---|
| First setup | Harder | Easier |
| Daily use | Fast after setup | Fast |
| Data control | High | Lower |
| Customizability | High | Low to medium |
| Vendor lock-in | Low | High |

## Expectation management per profile

### Profile A: technically skilled (developer / power user)

- Expectation: usually done in one session
- Blockers: permissions, audio naming, model download

### Profile B: sales user with light technical experience

- Expectation: works out with the manual + one round of help
- Blockers: Audio MIDI Setup and understanding permissions

### Profile C: corporate laptop with strict policy

- Expectation: often needs IT help
- Blockers: cask install, driver policy, shell permissions

## What the installer attempts automatically

In order:

1. System checks
2. Homebrew + dependencies
3. Python venv + package install
4. Audio setup helper (with fallback)
5. Model download
6. Open the permissions panel
7. `.env` base config
8. Optional autostart
9. Audio verification

Important: "attempts" is the right word. Some macOS components deliberately can't be fully automated.

## What you have to validate manually after the installer

- Do you see in the output: "PASS: Audio pipeline looks ready."?
- Does your microphone get signal in `scripts/verify_audio.py`?
- Are you using the manual BlackHole fallback? Then check that output is set to "OnCue Output" (or your equivalent). On the default AudioTee route this doesn't apply.
- Does `bash scripts/start.sh` start without an error in the log?

## When you should ask for help right away

Ask for help if one of these is true:

- You've been stuck on the same step for >30 minutes
- You keep getting permission errors despite the toggles
- BlackHole doesn't appear, even after a restart
- `python -m sales_copilot` won't start or stops immediately
- Corporate policy blocks Homebrew/cask

## What info to include when asking for help

Send this along right away, it saves a lot of time:

- macOS version: `sw_vers -productVersion`
- Device type: `uname -m`
- Audio check output: `python scripts/verify_audio.py`
- Latest runtime log lines: `tail -n 100 data/logs/runtime.log`
- Exactly what you've already tried

## Phone calls (iPhone relay)

The default audio route (AudioTee) taps your Mac's entire system output mix (`tap_all`) through a Core Audio process tap. You don't have to select a meeting app and it doesn't matter which or how many apps are open. BlackHole (a virtual audio device) is a manual fallback you can choose yourself. Both routes work for Teams, Zoom, and other video apps that send their sound through the system output.

A real phone call doesn't. If you call via the iPhone relay (Continuity) or FaceTime audio, the caller audio bypasses the normal system output, going straight through macOS's telephony daemon `avconferenced`. Neither the AudioTee system tap nor BlackHole hears that prospect then. This is the well-known reason a call with a Bluetooth headset previously produced no prospect transcript.

The solution is a different kind of process tap: instead of tapping the system output or an audio device, the copilot taps the audio of the `avconferenced` process itself via Core Audio process taps (macOS 14.2+, the same API as the default AudioTee route). That brings the prospect stream back without routing tricks. This is a Sales Pro feature (`FEATURE_CALLTAP`).

**The switch:** set `PROSPECT_SOURCE=audiotee_call` in `.env`. The default stays `blackhole` — which here means "just use your normal video-call route" (the default AudioTee system tap or the manual BlackHole fallback) — so your existing Teams/Zoom setup doesn't change unless you explicitly choose this. You can also pass the choice per conversation via the `prospect_source` field in the start_call config.

**What you can do in this mode:**

- Use a Bluetooth headset. The tap sits on the telephony process, not on your output device, so Bluetooth's A2DP/HFP profile switch no longer matters.
- Open the tap before the call. It then delivers silence (no error) and fills up as soon as the call begins.

**What you need:**

- The AudioTee binary at `AUDIOTEE_BINARY_PATH` (default `./bin/audiotee`) — the same binary as the default system-tap route, part of the OSS/Free install (built by `setup.sh`/`install.sh`).
- Microphone and system audio permissions, just like for the AudioTee/BlackHole route.
- An active call process (`avconferenced` runs as soon as you make a call). If it isn't running, the copilot reports that via an `audio_warning` instead of crashing.

**Honest caveat:** an idle `avconferenced` delivers a silent but running stream. The liveness check counts chunks, not volume, so it only triggers when no audio comes in at all (process absent or tap failed), not on silence-during-connection. So don't expect a warning if you open the tap and aren't calling yet.

Background and the measurement results are in `claudedocs/2026-06-05-deepresearch-telefonie-audio-capture.md`.

## Summary in one sentence

This is a powerful local setup that requires realistic manual steps; with the installer and these docs it's manageable, but not "friction-free".

## Recommended flow (short)

1. Run: `bash scripts/install.sh`
2. Follow the permission prompts genuinely step by step
3. Run: `bash scripts/start.sh`
4. Check the dashboard + transcript within 30-60 seconds
5. If something is broken: go to `docs/TROUBLESHOOTING.md`

## Finally: why this honesty

Here I deliberately choose trust over marketing language.
If something is manual, I say so.
If something is fragile, I call it out.
If something isn't "one-click" yet, I don't pretend otherwise.

That may make onboarding less glossy, but it makes it predictable.
And predictable wins in the long run.
