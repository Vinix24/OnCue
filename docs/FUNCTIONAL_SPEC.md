# Functional specification: OnCue

This document describes what OnCue does from the user's perspective.
It is meant for sales managers, sales professionals, and anyone who wants to assess
whether this product is useful, without needing to know the technical details.

---

## What is OnCue?

OnCue is an AI assistant that listens in live during a video call and
coaches you in real time. It transcribes the conversation and detects pain points and
objections from the prospect, so you can respond at the right moment.

Everything runs on your own computer: macOS (default) or Windows.
Your call recording does not go to an external service unless you configure that yourself.

---

## Before the call

Before you click Start Call, you fill in a short setup via the dashboard:

- **Client** (optional): pick an existing client folder from a list. The prospect
  company, industry, glossary terms, and privacy setting for that client come from
  its `klant.yaml` file, so you don't retype them for a repeat conversation. See
  "Client folders" below.
- **Prospect name and company name** for the report (filled in automatically when
  you pick a client, or typed by hand otherwise)
- **Language** (Dutch, English, or German): the AI uses the correct pain point routes
- **Transcription backend** (whisper.cpp — default, macOS + Windows — or mlx-whisper on Apple Silicon)
- **Modules on/off**: talk-time, detection, suggestions, summary. Detection is
  the AI master switch: with it off the call still records, transcribes and
  tracks talk-time, but no pain points, objections or buying signals are
  detected. Some presets turn it off on purpose — `discovery` and
  `coaching_only` are coaching-only by design — so the dashboard shows a
  standing indicator whenever detection is off for this call, on the setup
  screen and during the call. You should never have to wonder why nothing is
  appearing.
- **Context document** (optional): upload a PDF or text file with background information
  about the prospect. The AI takes this into account when making suggestions.
- **Preset** (optional): save a combination of settings as a preset for
  recurring conversation types.

Click **Start Call** when you're ready. The system preloads the Whisper model
(5 seconds of warmup for the default large-v3-turbo model) and starts listening.

If you would rather check everything is ready before the prospect joins, run
the preflight from the repo:

```bash
bash scripts/oncue_chain.sh check
```

It reports, per line, whether the backend is up and which checkout owns it,
whether your venv carries every command this version declares, whether an MCP
host is registered, whether exactly one meeting app is running
(that gates automatic call start, not the tap itself, which follows the whole
system output mix), whether
detection will be on for the next call, and whether the log files are writable.
Anything that is not OK comes with the command that fixes it. `up` does the
same and then starts what is missing, so the only thing left for you is
starting the call itself.

### Client folders

A client folder is a folder on your own machine (under the configured
`KLANTEN_ROOT`) that can optionally hold a `klant.yaml` file naming the
client's company, industry, contact people, and glossary terms, so you don't
retype them every time you talk to the same client. `klant.yaml` can also set:

- A **privacy ceiling** (`local`, `tenant`, or `public`): if the LLM provider
  configured for this install is more permissive than the client allows, the
  call is refused before anything is captured, rather than silently sending
  that client's conversation somewhere it shouldn't go.
- A **retention window** for that client's own sessions, separate from the
  global retention setting.

Every call linked to a client is archived into a `gesprekken/` (conversations)
subfolder inside that client's folder, so a client's call history lives
together with everything else you keep about them. Deleting a session (the
GDPR purge, see [PRIVACY.md](PRIVACY.md)) removes only that session's own
archived conversation and its own saved notes — never another session's.

If your client folder lives inside a folder that a cloud-sync app (iCloud
Drive, Dropbox, or a similar service) watches, the setup screen warns you: the
content in that folder — including transcripts and contact names — will be
synchronized to that cloud service. This is a warning, not a block; the choice
of where to keep client folders stays yours.

A client folder without a `klant.yaml` keeps working exactly as before: you
type the company name by hand. Client folders are a Free-tier feature.

---

## During the call

### Screen 1: presentation (shared with the prospect)

This is the screen you screen-share. It's a Reveal.js presentation with your
standard sales slides. The prospect sees this screen.

Presentation automation is a Pro feature (`presentation.dynamic_slides`).
Without a Pro license, the presentation stays fully under your manual control.

### Screen 2: coaching dashboard (for you only)

Only you see this screen. Four panels:

**Talk-time bar (top)**

A color-coded bar shows the speaking ratio over the last 2 minutes:

- Green: you're within the target ratio for this phase
- Orange: 5-10% outside the target
- Red: more than 10% outside the target

The bar has a subtle breathing animation when things are going well, so you can
follow it in your peripheral vision without giving it attention.

If you speak continuously for longer than 76 seconds, a subtle
notice appears: "Time to listen". After 5 seconds it disappears again. There is no
sound, no pop-up, no distracting animation.

Phase targets are:

| Phase | You speak | Goal |
|---|---|---|
| Discovery | 30-40% | Listening, asking questions |
| Pitch | 60-70% | Explaining your proposition |
| Closing | 40-50% | Reaching a conclusion together |

You switch between phases manually via the buttons in the dashboard. Automatic
phase recognition can optionally be turned on.

**Detections (center-left)**

Each time a pain point or objection is recognized, a card appears:

- Category (for example "Quoting process") with a confidence score
- The exact spoken sentence that triggered the detection
- Which case slide was injected (if one was available)
- Timestamp

Pain point categories for Dutch B2B conversations (default configuration):
quoting process, capacity, cross-sell, lead generation, knowledge access,
customer service, manual work, data quality, reporting, onboarding,
compliance, cost.

Objection categories: price, timing, competitor, scope, authority.

You can add your own categories or replace the default configuration via
`config/pain_points.yaml` and `config/objections.yaml`.

**Live transcript (center-right)**

The transcript of the conversation appears in real time with speaker attribution
(You / Prospect). The text scrolls along automatically.

Latency from speech to visible transcript: 1-3 seconds, depending on
the buffer setting and model.

**Summary and suggestions (bottom)**

Every 60 seconds the AI generates a 2-sentence summary of the conversation
plus a list of key moments.

Alongside the summary, real-time follow-up question suggestions appear based
on the context. For example: if the prospect mentions capacity problems, a
suggestion might be: "How many hours per week does that task take now?"

---

## After the call

Click **End Call** in the dashboard. The system automatically generates a
report in `data/sessions/<id>/`.

The report contains:

- Full transcript with speaker attribution and timestamps
- All detected pain points with confidence scores and trigger sentences
- All detected objections with timestamps
- Talk-time statistics per minute (chart in JSON format)
- Phase progression (when you moved from discovery to pitch to closing)
- Which case slides were shown and when
- Summary and key moments

The report is available as a JSON file and as a readable Markdown document.

**Processing the transcript afterward**

Via `scripts/label_and_summarize.py` you can run a raw transcript through a
Gemini pipeline after the fact. This produces a structured conversation analysis with
speaker recognition, a summary of the prospect's needs, and recommendations
for next steps.

---

## Privacy: what happens to your audio?

See [PRIVACY.md](PRIVACY.md) for the full explanation.

In short:

- **Transcription runs locally** on your computer (macOS or Windows). Your audio
  does not leave your machine unless you configure a cloud provider for
  transcription (not the default).
- **Pain point and objection detection** uses an LLM provider of your choice.
  No LLM is configured by default (`LLM_PROVIDER=none`); the live cues run
  locally without one. With a provider configured, text is sent per task
  (detection windows, a rolling summary, live suggestions and the whole
  transcript for the post-call report), never audio. See `docs/PRIVACY.md`.
  You can configure Ollama for fully local processing.
- **Session recording is off by default** (`RECORD_AUDIO=false`). If you turn it on
  with `RECORD_AUDIO=true`, recordings are stored as WAV files in
  `data/sessions/` on your machine. They are not synchronized to an
  external service.
- **No telemetry.** The codebase contains no phone-home code.

---

## OSS versus Pro

| Feature | Free (this repo) | Pro |
|---|:---:|:---:|
| Transcription (whisper.cpp + mlx-whisper) | yes | yes |
| Pain point detection (12 default NL categories) | yes | yes |
| Objection detection (5 default NL categories) | yes | yes |
| Buying signal detection | yes | yes |
| Talk-time balance visualization + monologue alert | yes | yes |
| Phase detection | yes | yes |
| Follow-up question suggestions | yes | yes |
| Summary + key moments | yes | yes |
| Coaching dashboard (transcript, detections, talk-time) | yes | yes |
| Post-call report (JSON + Markdown) | yes | yes |
| Multi-language routes (NL/EN/DE) | yes | yes |
| Domain presets (sales/coach/recruitment) | yes | yes |
| Fine-tuning pipeline | yes | yes |
| Local SQLite storage + case database | yes | yes |
| Optional Supabase sync | yes | yes |
| Consent tracking + retention + GDPR purge | yes | yes |
| Client folders (`klant.yaml`): linked prospect info, retention, privacy ceiling | yes | yes |
| Presentation automation (`presentation.dynamic_slides`) | no | yes |
| Telephony audio capture / iPhone relay (`audio.calltap`) | no | yes |
| Central tamper-evident audit, hashes only (`compliance.central_audit`) | no | yes |
| Autostart at login + runtime auto-arm (`system.autostart`) | no | yes |
| Live coaching guidance (`coaching.live`) | no | yes |
| Sales-script / methodology tracking (`coaching.script_tracking`) | no | yes |
| Curated objection-response playbook (`coaching.response_playbook`) | no | yes |
| Report delivery to a webhook/CRM endpoint (`reports.delivery.endpoint`) | no | yes |

Pro is available via [oncueassistant.com](https://oncueassistant.com).
Pro features are proprietary extensions installed as a separate package on
the same open-source core.

---

## Minimum requirements

**macOS (default, production-ready):**

| Requirement | Minimum | Recommended |
|---|---|---|
| macOS | 14.2 (Sonoma) | 15.x or newer |
| Mac | Apple Silicon (M1/M2/M3/M4) | M4 24 GB RAM |
| RAM | 16 GB | 24 GB |
| Screens | 1 (works, but not ideal) | 2 |
| Audio routing | BlackHole (fallback, free, macOS 10.14+) | AudioTee (default, whole-system tap, macOS 14.2+) |

**Windows:**

| Requirement | Minimum | Recommended |
|---|---|---|
| Windows | 10 (build 2004+) | Windows 11 |
| CPU | 64-bit, sufficient for whisper.cpp in real time (no GPU needed) | Newer multi-core CPU |
| RAM | 8 GB | 16 GB |
| Screens | 1 (works, but not ideal) | 2 |
| Audio routing | WASAPI loopback via `soundcard` (automatic, no virtual device needed) | — |

Windows is supported: transcription runs only via whisper.cpp
(mlx-whisper is Apple-only and therefore not available on Windows), and
telephony capture (`CallTapStream`, the iPhone-relay/FaceTime tap) is not
available — WASAPI loopback cannot isolate a single phone call from the
rest of the system audio. Video calling (Teams/Zoom/Meet) works the same as
on macOS.

Linux is on the roadmap but not yet supported.

---

## Installation in 5 minutes

```bash
git clone https://github.com/Vinix24/OnCue.git
cd OnCue
bash scripts/install.sh   # installs dependencies (venv + whisper.cpp)
bash scripts/start.sh        # starts the system
```

This is the macOS installation. Windows has its own
installation path — see the [README](../README.md) for the exact steps.

Then open two browser tabs (or one per screen):

- `dashboard/index.html` for your coaching dashboard
- `presentation/index.html` for the Reveal.js presentation you screen-share

Run into something? Check [FAQ.md](FAQ.md) or [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Further reading

- [ARCHITECTURE.md](ARCHITECTURE.md): technical system overview for developers
- [PRIVACY.md](PRIVACY.md): detailed explanation of data, GDPR, and the AI Act
- [USE_CASES.md](USE_CASES.md): concrete deployment scenarios beyond B2B sales
- [INTEGRATIONS.md](INTEGRATIONS.md): audio routing, LLM providers, and extensions
- TTD: detailed technical design document with API contracts
