# Setup Expectations (the honest version)

This document is deliberately direct and honest.
If you're setting up OnCue for the first time on macOS, the one-line install does most of the work.
You still download a 1.5 GB speech model and grant two macOS permissions yourself.
That's simple, but not magic.

## What you get (and what you don't)

### What you do get

- A **one-line installer** you paste in Terminal (see [INSTALL.md](../INSTALL.md#one-line-for-everyone)), or an AI assistant that follows [INSTALL_WITH_AI.md](INSTALL_WITH_AI.md). It downloads OnCue into `~/OnCue` and ends with the app `~/Applications/OnCue.app`
- On the release route (when a release carries prebuilt programs): no compiler, no Homebrew, no password. A private Python 3.11 (uv) and the Python packages go inside `~/OnCue`
- On the build route (the fallback): Apple's command line tools, Homebrew, Python 3.11 and CMake, and a local build of whisper.cpp and AudioTee
- Automatic checks on macOS version (14.2 or newer) and Apple Silicon
- The speech model download (`large-v3-turbo`, 1.5 GB)
- A status block at the end that says `OK` or `NOT OK` with the fix, for the transcriber, audio capture and the app
- Manual BlackHole routing is not installed. It is an optional fallback you set up yourself (see [INSTALL.md](../INSTALL.md#optional-fallback-blackhole-audio-routing))

### What you don't get

- No signed or notarized `.app` or `.dmg` download, and no "click-next" wizard (the app is built on your Mac by the installer)
- No magic bypass of macOS security
- No guarantee that corporate device policy allows everything

## Realistic time estimate

### First time

- **Release route:** 55 seconds on a fast connection in one measurement. The download of about 2.8 GB dominates: on a home connection expect several minutes, up to about 15
- **Build route (fallback):** about 5 to 15 minutes, longer when Apple's tools and Homebrew must be installed first
- **If permissions/IT policy get in the way:** 30-60+ minutes

### Second time on the same Mac

- About 6 seconds on the release route: nothing is downloaded again

## Who shouldn't do this solo

Don't try this **on your own** if one of these points is true:

- You've never used Terminal before and don't want to paste one line (an AI assistant can do it for you, see [INSTALL_WITH_AI.md](INSTALL_WITH_AI.md))
- You work on a tightly managed corporate Mac (MDM, limited admin rights)
- You're not allowed to install audio drivers or system tools
- You have little time and expect to be "live right now" without any technical steps

In those cases: arrange help in advance from IT or someone with macOS developer experience.

## The permission clicks you can't skip

Apple blocks automatic consent for sensitive permissions. macOS asks the first time you open OnCue.app. If you dismissed a prompt, set it in **System Settings > Privacy & Security**:

1. **Microphone**
- Grant permission to `OnCue` (listed as `applet`), or to Terminal when you start OnCue from there

2. **Audio Recording** (macOS 14.4+), or **System Audio Recording Only** (Dutch: *Alleen systeemaudio-opname*) on newer macOS
- Needed for the AudioTee process tap (the default audio route). Without this permission the other side of the call is silent, without an error.
- If you manually choose the BlackHole route (`AUDIO_CAPTURE_METHOD=blackhole`): check that the Sound output is set to the correct Multi-Output.

The installer does not open these pages and does not set the toggles for you.

## Clicks, waiting moments, and admin prompts

### Expected manual actions

- 2 permission prompts (Microphone and Audio Recording for AudioTee)
- 1-2 checks in Sound/Audio MIDI Setup (only needed if you choose the BlackHole fallback)
- On the build route only: one click in Apple's command line tools window and one Mac password prompt for Homebrew

### Target number of manual steps

- Aim: **2 permission clicks, no admin prompt** on the release route
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

- Expectation: works out with the one-line install, or with an AI assistant doing it
- Blockers: understanding the permission prompts, and (build route) Apple's window and the password prompt

### Profile C: corporate laptop with strict policy

- Expectation: often needs IT help
- Blockers: cask install, driver policy, shell permissions

## What the installer attempts automatically

In order (build route, 9 steps; the release route has 4 to 5):

1. System checks
2. Apple command line tools
3. Homebrew
4. Python 3.11 and CMake
5. Python venv + package install
6. AudioTee build
7. whisper.cpp build and the speech model
8. `.env` base config and data folders
9. The OnCue app in `~/Applications`

Important: "attempts" is the right word. Some macOS components deliberately can't be fully automated.

## What you have to validate manually after the installer

- Does the status block say `Transcriber OK`, `Audio capture OK` and `App OK`?
- Does OnCue.app open, and do both level meters move in a short test recording?
- Does `transcript.md` appear after Stop?
- Are you using the manual BlackHole fallback? Then check that output is set to "OnCue Output" (or your equivalent). On the default AudioTee route this doesn't apply.

## When you should ask for help right away

Ask for help if one of these is true:

- You've been stuck on the same step for >30 minutes
- You keep getting permission errors despite the toggles
- BlackHole doesn't appear, even after a restart
- OnCue won't start or stops immediately (`data/logs/launcher-record.log`)
- Corporate policy blocks Homebrew or Apple's command line tools (only the build route needs them)

## What info to include when asking for help

Send this along right away, it saves a lot of time:

- macOS version: `sw_vers -productVersion`
- Device type: `uname -m`
- The newest install log from `~/Library/Logs/OnCue/`
- Latest runtime log lines: `tail -n 100 ~/OnCue/data/logs/copilot.log`
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

1. Paste the one-line install in Terminal (see [INSTALL.md](../INSTALL.md#one-line-for-everyone))
2. Open `~/Applications/OnCue.app` and choose **Opnemen en uitschrijven**
3. Click Allow on the macOS permission prompts
4. Record 30 seconds, press Stop and check that `transcript.md` appears
5. If something is broken: go to `docs/TROUBLESHOOTING.md`

## Finally: why this honesty

Here I deliberately choose trust over marketing language.
If something is manual, I say so.
If something is fragile, I call it out.
If something isn't "one-click" yet, I don't pretend otherwise.

That may make onboarding less glossy, but it makes it predictable.
And predictable wins in the long run.
