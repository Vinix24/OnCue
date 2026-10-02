# OnCue features

OnCue is a real-time coaching assistant for sales and recruitment calls. It runs on macOS (Windows is supported via WASAPI loopback) and keeps the audio layer local. During a call it transcribes speech, detects pain points and objections, tracks talk-time ratios, and shows coaching suggestions on a private dashboard. Pro adds a set of licensed capabilities (see the tier matrix below).

The primary users are Dutch B2B sales professionals, recruiters, and coaches who want live call support without sending conversations to a third-party SaaS.

---

## Tier matrix

Feature entitlement is enforced in code by `FeaturePolicy` (`src/sales_copilot/auth/feature_policy.py`). The table below reflects the actual feature IDs checked with `allows()` in the current codebase.

| Capability | Free (AGPL-3.0, self-host) | Pro (commercial license) |
|---|---|---|
| Live transcription (whisper.cpp, on-device) | Yes | Yes |
| Talk-time balance visualization + monologue alerts | Yes | Yes |
| Pain-point, objection, and buying-signal detection | Yes | Yes |
| Coaching dashboard shell (transcript, detections, talk-time) | Yes | Yes |
| Rolling summary + post-call report | Yes | Yes |
| Follow-up question suggestions | Yes | Yes |
| Sample case-slide content (`scripts/seed_cases.py`) | Yes | Yes |
| Multi-language routes (NL, EN, DE) | Yes | Yes |
| Vakgebied-presets (sales, coach, recruitment) | Yes | Yes |
| Local SQLite session + case database | Yes | Yes |
| Local NDJSON audit log (Free) / HMAC-signed audit (recruitment) | Yes | Yes |
| Per-session consent tracking (`CONSENT_TRACKING_ENABLED`) | Yes | Yes |
| Configurable retention + auto-purge (`DATA_RETENTION_DAYS`) | Yes | Yes |
| GDPR Art. 17 purge-session CLI | Yes | Yes |
| Client folders (`klant.yaml`): linked prospect info, per-client retention, privacy ceiling, cloud-sync-folder warning | Yes | Yes |
| Report delivery to a local directory (`REPORT_DELIVERY_DIR`) | Yes | Yes |
| Presentation automation (`presentation.dynamic_slides`) | No | Yes |
| Telephony audio capture, iPhone-relay / FaceTime (`audio.calltap`) | No | Yes |
| Central tamper-evident audit with hashes only (`compliance.central_audit`) | No | Yes |
| Autostart on login + runtime auto-arm (`system.autostart`) | No | Yes |
| Live coaching guidance (`coaching.live`) | No | Yes |
| Sales-script / methodology tracking (`coaching.script_tracking`) | No | Yes |
| Curated objection-response playbook (`coaching.response_playbook`) | No | Yes |
| Report delivery to a webhook/CRM endpoint (`reports.delivery.endpoint`) | No | Yes |
| HubSpot / CRM sync | Roadmap | Roadmap |
| Team dashboard + shared case library | Roadmap | Roadmap |

> **Note:** Consent tracking, retention/auto-purge, and the purge-session CLI are currently env-configurable and work in both tiers.

**Verified Pro feature IDs in code:**

- `audio.calltap` — telephony process-tap (`src/sales_copilot/audio/telephony_guard.py`, `src/sales_copilot/audio/capture.py`).
- `compliance.central_audit` — central hash-only audit endpoint (`src/sales_copilot/core/compliance_audit.py`).
- `presentation.dynamic_slides` — presentation automation (Pro-gated).
- `system.autostart` — launch at login via wizard autostart-step (`src/sales_copilot/wizard/cli.py`), **and** the runtime auto-arm monitor (`src/sales_copilot/core/autostart_monitor.py`) that detects a video call *or* a phone/FaceTime call starting while the app is already running. These are two distinct capabilities sharing one feature-gate: the wizard step controls whether the app starts at macOS login; the runtime monitor controls whether an already-running app auto-arms a call session, from either trigger source.
- `coaching.live` — live coaching-guidance channel; suppressed without entitlement (`src/sales_copilot/websocket/hub_core.py`, `src/sales_copilot/auth/feature_policy.py`).
- `coaching.script_tracking` — sales-script / methodology tracking; gated WebSocket channel (`src/sales_copilot/websocket/hub_core.py`).
- `coaching.response_playbook` — curated objection-response playbook; the Free tier falls back to a locked teaser (`src/sales_copilot/auth/feature_policy.py`).
- `reports.delivery.endpoint` — HTTP delivery of the finished post-call report to a customer-configured webhook/CRM endpoint (`src/sales_copilot/modules/reports/delivery.py`). Delivery to a local directory (`REPORT_DELIVERY_DIR`) is a separate sink and stays Free in both tiers; on Free, a configured endpoint is skipped with one `WARNING` per process and the directory sink still runs.

A future curated content pack and shared team case library are on the roadmap but are not license-gated today.

---

## What the Free tier includes

The Free tier is the full open-source copilot engine. You get:

- Local audio capture for mic + system audio (AudioTee whole-system tap by default; BlackHole is a manual fallback).
- whisper.cpp large-v3-turbo transcription on your Mac.
- Silero VAD talk-time tracking with a breathing bar on your second screen.
- Detection of 12+ Dutch pain-point categories, 5 objection categories, and buying signals.
- A private coaching dashboard with live transcript, detections, and follow-up suggestions.
- Post-call JSON and Markdown reports.
- Sample case slides via `scripts/seed_cases.py`.
- Sales, coach, and recruitment presets.
- Client folders (`klant.yaml`): couple a client's company, industry, contact people
  and glossary terms to a folder, with an optional per-client privacy ceiling
  (`local`/`tenant`/`public`) enforced against the configured LLM provider before a
  call starts, an optional per-client retention window, a warning when the client
  folder root lives under a cloud-synced location (iCloud Drive, Dropbox, or a
  File-Provider cloud-storage mount), and an automatic `gesprekken/` call archive.
- No LLM by default (`LLM_PROVIDER=none`); the live cues run local without one. Optional providers: Gemini, OpenRouter, Groq, OpenAI, Ollama, Vertex AI, Azure OpenAI, including fully local Ollama.

The Free tier is AGPL-3.0. Self-hosting is required; there is no managed cloud service.

---

## What Pro adds

Pro unlocks the license-gated capabilities enforced by `FeaturePolicy`:

1. **Presentation automation** (`presentation.dynamic_slides`). Pro-gated presentation control during the call.
2. **Telephony capture** (`audio.calltap`). Route iPhone-relay and FaceTime audio via `PROSPECT_SOURCE=audiotee_call` without virtual audio devices.
3. **Central tamper-evident audit** (`compliance.central_audit`). The raw record never leaves the machine. The server receives a pseudonymous SHA-256 hash of each record plus the license key. It is on by default for Pro because `CONSENT_TRACKING_ENABLED=true`.
4. **Autostart on login** (`system.autostart`). The app starts automatically when you log in to macOS, configured through the setup wizard.
5. **Runtime auto-arm (video calls)** (`system.autostart`). While the app is already running, Pro installs detect a supported video meeting app (Google Chrome / Microsoft Teams / zoom.us) becoming active and surface a consent-tick prompt to arm the call session automatically -- the manual Start Call button stays available in every tier.
6. **Runtime auto-arm (phone calls)** (`system.autostart`). The same monitor also detects an active macOS phone/FaceTime call via the `avconferenced` daemon and surfaces the same consent-tick prompt. The copilot only *detects* an in-progress call -- macOS places the underlying call itself via Continuity/iPhone-relay; this is not a dialer or CRM click-to-call integration.
7. **Live coaching guidance** (`coaching.live`). Live in-call coaching prompts on a dedicated channel, suppressed without entitlement.
8. **Sales-script / methodology tracking** (`coaching.script_tracking`). Tracks adherence to a configured sales script or methodology.
9. **Curated objection-response playbook** (`coaching.response_playbook`). Curated responses to detected objections; the Free tier shows a locked teaser instead.
10. **Report delivery to a webhook/CRM endpoint** (`reports.delivery.endpoint`). Sends the finished post-call report to a customer-configured webhook or CRM endpoint; delivery to a local directory stays Free in both tiers.

Pro is a commercial license. Contact info@vincentvandeth.nl for terms.

### Runtime auto-arm details (video + telephony)

The runtime auto-arm monitor (`src/sales_copilot/core/autostart_monitor.py`) polls two independent trigger sources every cycle, each debounced separately, feeding the same detect -> consent-arm -> session-arm state machine:

- **Video.** Reuses the same meeting-app process detection built for the audio-onboarding path (`resolve_meeting_app_target` in `src/sales_copilot/audio/capture.py`).
- **Telephony.** Reuses the `avconferenced` process resolution built for the telephony-tap guard (`resolve_telephony_call_target` in `src/sales_copilot/audio/telephony_guard.py`) -- the same daemon macOS routes phone/FaceTime call audio through.

Both sources share the same consent-arm gate as the login-autostart wizard step (`detect_autostart_consent_arm`) and the same `system.autostart` entitlement -- there is one auto-arm capability with two ways to trigger it. It never starts recording silently:

1. **Detect.** A supported video meeting app, or `avconferenced`, must be the *only* candidate process running for its source, confirmed over `AUTOSTART_DEBOUNCE_ARM_POLLS` consecutive polls (default 2) -- a flapping process, or more than one candidate open at once, never triggers auto-arm. If both sources cross their debounce threshold on the same poll, the video path is checked first (an edge case with no behavioural preference either way, since only one real call is ever actually in progress).
2. **Consent-arm.** The install must be Pro/Enterprise-entitled (`system.autostart`) and the consent-arm gate must currently pass (the wizard's login-autostart step must be installed with `CONSENT_TRACKING_ENABLED=true` and `CONSENT_TIER` not `off`). Only then does the monitor surface a consent-tick prompt (`autostart_consent_required` on the `system` WebSocket channel, with `process` set to the detected process name -- a meeting-app name for video, `avconferenced` for telephony).
3. **Session-arm.** The session only starts once the user explicitly confirms via `POST /api/autostart/confirm` -- the per-call consent tick. Declining (`POST /api/autostart/decline`) or the triggering process disappearing before confirmation (debounced over `AUTOSTART_DEBOUNCE_DISARM_POLLS` polls, default 2) resets the prompt without starting anything. Only the source that triggered the prompt is watched for that disappearance -- e.g. a video meeting app opening in the background while a phone-call prompt is pending does not affect it, and vice versa.

Free tier: the monitor still polls both sources, but detection is a no-op without `system.autostart` -- the manual Start Call button is the only way to start a session. Configuration: `AUTOSTART_MONITOR_ENABLED`, `AUTOSTART_POLL_INTERVAL_S`, `AUTOSTART_DEBOUNCE_ARM_POLLS`, `AUTOSTART_DEBOUNCE_DISARM_POLLS` in `.env.example` (shared by both trigger sources -- there is no separate telephony debounce knob).

Detecting and arming is a separate concern from which audio stream the resulting session actually captures. An auto-armed session (from either trigger) starts with whatever `PROSPECT_SOURCE` is already configured -- to have a telephony-triggered session capture the phone-call audio itself, the install still needs `PROSPECT_SOURCE=audiotee_call` and `audio.calltap` entitlement, exactly as for a manually started phone-call session. This slice only adds the trigger; it does not change how prospect-audio routing is chosen.

**Real-device verification is an operator step, not covered by CI.** The mocked unit tests (`tests/test_autostart_monitor.py`) inject both `detect_fn` and `telephony_detect_fn`, so CI never shells out to `pgrep` or requires a real call. Confirming the telephony trigger against a real `avconferenced` process requires placing an actual phone or FaceTime call through Continuity/iPhone-relay on a real Mac and watching the consent prompt fire -- there is no way to simulate a genuine Continuity call in CI, so this step must be run manually by whoever ships a change to this path.

---

## Compliance angle for recruitment

Recruitment use of AI-driven decision support is classified as high-risk under EU AI Act Annex III, section 4. This makes an auditable processing log legally relevant, not only a privacy nice-to-have.

OnCue supports this through:

- A recruitment preset with PII redaction before any LLM call.
- A local append-only audit log with optional HMAC signing.
- Pro-only central hash-chain audit (`compliance.central_audit`) that sends tamper-evident hashes, not transcript content, to a central endpoint.
- Per-session consent tracking (`CONSENT_TRACKING_ENABLED`) and a GDPR Art. 17 purge-session CLI, both env-configurable and available in all tiers today.

These features support compliance work. They do not guarantee compliance by themselves. The deploying organization remains the controller and is responsible for lawful basis, retention policy, DPA coverage, and human oversight. See [DPIA_RECRUITMENT.md](DPIA_RECRUITMENT.md) for the full DPIA template.

---

## Pricing

Pricing is not published in this repository. Public plan bands are on the [landing page](https://oncueassistant.com). For Pro and Enterprise licensing, contact info@vincentvandeth.nl.
