# OnCue - Beta Tester Guide

*Version: 0.9.0 (2026-07-22)*

---

## Welcome, beta tester

OnCue is an AI tool that listens along live during sales conversations. It recognizes pain points in the conversation and coaches on talk-time balance - all while the conversation is happening.

The tool runs entirely on your own Mac. Audio never leaves your computer. Transcription happens locally on Apple Silicon.

I'm looking for beta testers who use the tool in at least 3 real sales conversations and submit feedback within 2 weeks. What I ask in return is concrete: real calls, no demo scenarios, and an honest verdict on what does and doesn't work.

Don't expect a perfectly polished product. This is a working beta: the core features work, but you'll run into rough edges. Those rough edges are exactly what's valuable. Bugs and improvement points that testers report go straight into the sprint planning for the next release.

What I give back: a free license key for the Pro features, early access to new features, and a direct line to me for questions and bugs. I usually respond within 24 hours.

---

## What you need

**Hardware and OS:**

- macOS 14.4 or newer recommended for video calls: the copilot taps the entire system output mix through a process tap (AudioTee, `tap_all`), with no audio routing and no matter which or how many apps are open. On older macOS versions (13.0+) you use the manual BlackHole route
- macOS 14.2 or newer required for telephony capture (`PROSPECT_SOURCE=audiotee_call`, iPhone/FaceTime via process tap); this is a Sales Pro feature
- Apple Silicon required: M1, M2, M3, or M4
- 16 GB RAM minimum, 24 GB recommended
- The built-in microphone is sufficient, an external USB microphone gives better results

**Software:**

- Google Meet, Zoom, or Teams for your calls — no extra audio routing needed; the .app taps the entire system output mix, regardless of which app you use
- BlackHole 2ch (free) — only needed as a fallback, see the installation section below
- No Python knowledge needed, the .app handles everything

**Broadband connection:**

- Only needed if you configure an LLM provider (none by default). Transcript text goes to that provider per task, never audio
- Offline mode via Ollama is possible but requires extra setup; I don't cover that in this guide

---

## Installation in 3 steps

### Step 1: Download the .app

Download `Start OnCue.app` via the personal download link I sent you.

Drag the .app into your `/Applications` folder or leave it in your Downloads folder - both work.

### Step 2: Start the app (macOS Gatekeeper)

The first time, macOS shows a warning: "Apple could not verify the developer." This is normal for software that doesn't come from the App Store.

Do this the first time:

1. Don't double-click the app - right-click it instead (or Control-click)
2. Choose "Open" from the menu
3. The warning appears again, but now with an "Open" button
4. Click "Open"

On later launches a normal double-click is enough. macOS remembers that you trust the app.

If you prefer to go through System Settings: Settings > Privacy & Security > click "Open Anyway" next to the notice about OnCue.

### Step 3: Dashboard opens in your browser

OnCue starts a local server and automatically opens your browser at:

```
http://localhost:8760/dashboard
```

In the top right of the dashboard you'll see a status indicator. Wait until it says "Status: connected" - that usually takes 10 to 30 seconds the first time.

**Verification:** Speak a sentence into your microphone. If the transcript panel responds, everything works. If you don't see a transcript, go to the audio routing section below.

---

## System audio: AudioTee (default) and BlackHole (fallback)

**How it works by default:**

OnCue listens to your microphone and to the system audio (the voice of the person you're talking to). For system audio the tool uses AudioTee by default: a macOS Core Audio process tap that taps the entire system output mix (`tap_all`). No virtual audio cable, no Multi-Output Device — you don't have to route anything, and it doesn't matter which or how many apps are open. The only requirement is that the first time you grant the macOS audio recording permission (System Settings > Privacy & Security > Audio Recording).

If you'd rather have fixed, manual routing, you can choose BlackHole (see below). AudioTee does not fall back to it automatically.

**When you need BlackHole yourself:**

- Your Mac runs macOS older than 14.4 (the AudioTee process tap doesn't work then)
- The AudioTee process tap doesn't get audio recording permission on your Mac
- You deliberately want fixed, manual routing

**Installing BlackHole:**

Go to [existential.audio/blackhole](https://existential.audio/blackhole/) and download the 2ch version (free). Install via the .pkg file, then restart your Mac.

**Creating a Multi-Output Device (one-time):**

1. Open `Audio MIDI Setup` (search in Spotlight with Cmd+Space)
2. Click the plus sign (+) in the bottom left
3. Choose "Create Multi-Output Device"
4. Check: "Built-in Speakers" and "BlackHole 2ch"
5. Give the device a name, for example "OnCue Output"

**Set this device as system output:**

1. Go to System Settings > Sound > Output
2. Select "OnCue Output" (or the name you chose)

**Common pitfall:** After you quit OnCue, your output is still set to the Multi-Output Device. Music and video still play fine, but when you're in a meeting without OnCue active, the sound balance may feel different. Set your output back to "Built-in Speakers" when you're not using the tool.

**Check that it works:** Open a YouTube video with audio. Start OnCue. Do you see transcription appear of the spoken text in the video? Then the routing works.

---

## First run in 5 minutes

Do this with a colleague in a real call, or use a YouTube video with spoken Dutch as test material.

**1. Open your meeting app** (Google Chrome with Meet, Teams, or Zoom). AudioTee taps the entire system output mix, so it doesn't matter which or how many apps are open. If you use the manual BlackHole fallback, make sure it's active (System Settings > Sound > Output > OnCue Output).

**2. Start the app** by double-clicking. The dashboard opens in your browser. Wait for "Status: connected."

**3. Start your call** in Google Meet, Zoom, or Teams.

**4. Speak a sentence.** You'll see the transcript appear in the transcript panel on the left. The first appearance is gray and italic (partial transcript), then black for the final transcription. The delay is normally 200 to 500 milliseconds.

**5. Speak a pain-point sentence** such as:
- "I really don't have a good overview of the process"
- "It just takes too much time"
- "Honestly, the quoting process isn't working well for me"

Within 3 to 8 seconds you'll see a notification appear in the right panel.

**6. Check the coaching panel.** At the bottom of the dashboard a suggestion appears for a follow-up question based on what was said.

**7. Stop the tool via the red stop button** in the dashboard. Don't use CMD+Q and don't close the browser tab - the tool won't shut down cleanly then and the next conversation may not start properly.

---

## Troubleshooting top 10

**1. Dashboard doesn't open**

Check whether the app is still starting up. The terminal window behind the dashboard shows the status. Wait 60 seconds. If there's still nothing after 60 seconds, close everything and restart the app.

**2. No transcript visible**

AudioTee taps the entire system output mix, so first check whether system audio is actually playing (the other side is audible) and whether the audio recording permission is on. If you use the manual BlackHole fallback, go to System Settings > Sound > Output and check whether "OnCue Output" (your Multi-Output Device) is selected.

**3. Pain points aren't detected**

The LLM API key is missing or expired. Open `.env` in the project folder (hidden file, use Finder > Go > Go to Folder and type in the path). If you configured a provider, check whether the matching key (for example `GROQ_API_KEY` or `GEMINI_API_KEY`) is filled in. Groq and Gemini both offer a free tier.

**4. .app won't start, "damaged" message**

macOS Gatekeeper is blocking the app. Use right-click > Open for the first time. If that doesn't work: System Settings > Privacy & Security > click "Open Anyway."

**5. Tool responds slowly (transcript lags behind)**

Apple Silicon M1 with 16 GB RAM is the lower bound. If CPU load is high, close other heavy apps (browsers with many tabs, video editing, other AI tools). M2 with 16 GB or M3/M4 give noticeably faster response.

**6. Dashboard shows "License expired" or "License invalid"**

Fill in the waitlist form for a free beta key. The link is in the accompanying email I sent you. If you already have the key but the error persists: copy the key exactly (no spaces before or after) and paste it into the input field.

**7. Transcript is there, but stutters or drops**

Microphone access may be limited. Go to System Settings > Privacy & Security > Microphone and check whether OnCue is listed there with access on.

**8. No more sound in the meeting (BlackHole fallback)**

This only applies if you're on the BlackHole fallback. The Multi-Output Device is selected but BlackHole 2ch isn't in it. Open Audio MIDI Setup again and check whether both channels are checked in your Multi-Output Device.

**9. Tool crashes unexpectedly**

Open the log file at `data/logs/copilot.log`. The first 50 lines are the most informative. Include them in your feedback email or bug report. I can almost always trace the cause from the log.

**10. A feature doesn't work as expected**

That's exactly the information I need. Describe what you expected and what happened instead. Sending along a screenshot or log file helps a lot. Fill in the feedback form or email me directly.

---

## Privacy and data

Audio is processed locally on your Apple Silicon chip via whisper.cpp (the default backend; mlx-whisper is an experimental option). Your conversation audio does not go to an external server.

Transcripts are stored locally on your Mac in `data/sessions/`. I don't see those files unless you send them to me yourself.

LLM features work differently: no LLM is configured by default. If you configure a provider, text goes to it per task: detection windows, a rolling summary, live suggestions and, after the call, the whole transcript for the report. That's text, not audio. Without a provider, or with Ollama, nothing leaves the machine.

For the recruitment use case: an extra PII filter is active that strips personal data from fragments before they reach the API. A local audit log in NDJSON format records which fragments were sent.

I only use the information you share via the feedback form for product development. I don't sell your data and don't share it with third parties.

---

## Giving feedback

The feedback form takes 5 minutes. You may fill it in anonymously.

Feedback form: [PLACEHOLDER - link to follow at beta launch]

Direct contact for questions or bugs:

- Email: info@vincentvandeth.nl
- LinkedIn DM: [Vincent van Deth](https://linkedin.com/in/vincentvandeth)

For bug reports: include the log file (`data/logs/copilot.log`, first 50 lines). That significantly improves the chance of a quick fix.
