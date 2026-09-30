# Changelog

## Unreleased

## [1.0.0-rc.1] — 2026-09-28

First public pre-release since 0.9.0. A full rebrand from "Sales Copilot"
to OnCue across the launcher and dashboard, a client-folder feature that
groups a customer's sessions, dossier and retention policy under one
`klant.yaml`, hardening of the audio tap against silent failure, an
explicit consent gate before an auto-detected call arms a session, and a
post-ASR transcript normalization layer.

### Added

- Pro/Enterprise: a deep-insight lane and an MCP bridge for external AI
  assistants. These are not part of the open-source build.
- Dashboard: a consent prompt that must be explicitly confirmed before an
  auto-detected meeting or phone call arms a session — detection alone
  never starts one.
- Post-call transcription gained a `--model` override so a run can use
  the full accuracy model without editing the live-demo default in
  `.env` and back again.
- Launcher: a record/dashboard mode choice on open, repo-relative
  resolution so the built app still works after being moved out of the
  repo folder, and a valid ad-hoc code signature.
- The detector reports on itself at INFO level: a first-chunk line, a
  bounded heartbeat and a closing summary, each carrying per-reason
  counters, so a detector silently dropping every chunk is no longer
  indistinguishable from one that never started (#203).
- `scripts/oncue_chain.sh check|up`: one command for the whole pre-call
  chain, read-only in `check`, idempotent in `up` (#204, #208).
- An `attached_signal_lost` tap-health state warns and marks the
  transcript when a tap that has already carried audio starts writing
  bit-for-bit zero frames — a stopped tap, not a pause, since a live
  capture of a silent source always carries a noise floor. Found via a
  call that silently lost 23 minutes of prospect audio while every
  existing check still read healthy (#230).
- Client folders ("klantmap"): the `klant.yaml` schema and loader, path
  safety against an escaping slug, a cloud-sync warning for a client root
  that lives under iCloud Drive/Dropbox/other synced storage, the
  server-driven setup-screen client picker and privacy gate, session-to-
  client linking with an automatic archive of transcript + report into
  the client's own folder, and a per-client retention sweep that purges
  only that client's own archived sessions and dossier notes (#244-#249).
- A post-ASR transcript normalization layer: an exact-variant table, an
  adjacent-token merge and a length-gated fuzzy match against a config
  file, measured against a 50-entity tuning set and a held-out corpus of
  real calls before shipping (0 false positives / 10k held-out words,
  7 of 33 known-wrong entities repaired) (#251, #253).
- A headless fixture builder for replay-based detector testing, so a
  reproduction no longer needs a live call and a real audio tap (#241).
- Dashboard: a per-side indicator next to "You"/"Prospect" shows live tap
  health (no tap / attached but silent / attached with signal), not only
  a talk-time percentage (#239).

### Changed

- The two "stop" actions in the dashboard were easy to confuse: ending
  the current call and shutting the whole server down used near-identical
  buttons. Shutting the server down is now a clearly secondary control
  with its own confirmation dialog, replacing a raw browser `confirm()`
  and a page that overwrote itself.
- Windows support documentation now says what was already true: video-call
  capture works like on macOS. Only phone-call capture, which taps a
  macOS-only relay process, stays out of reach on Windows.
- The Dock launcher icon and product name now use OnCue's own mark
  instead of a generic emoji and the old "Sales Copilot" name; the
  start-call button no longer strands a call without an end-call
  control; and the transcription vocabulary was cut from 354 to 200
  tokens to fit whisper's 223-token prompt budget (#207).
- Report delivery to a webhook/CRM endpoint is now gated to the
  Pro/Enterprise tier; delivery to a local directory is unaffected and
  stays available on every tier (#250).

### Fixed

- Public export copies git-tracked files only, and no longer leaves a
  stray `.ruff_cache` directory in the exported snapshot.
- Several dependency floors bumped to close disclosed CVEs (aiohttp,
  cryptography, pip), and a Node version pinned for the license server
  after `better-sqlite3` stopped building on newer Node.
- A pre-push hook resolved the wrong repository root when pushed from a
  worktree, so its check silently ran against the main checkout instead
  of the branch being pushed.
- The AI-detection master switch no longer flips off silently.
  `loadPresets()` re-applied the first preset (`discovery`,
  `pain_points: false`) on every page load, bypassing the guard that only
  ran on the localStorage path. Preset choices are now written once on a
  fresh browser, a deliberate choice always wins, and a call running
  without detection carries a standing indicator (#206).
- `PainPointRouter`'s keyword fast path no longer over-matches. A single
  shared token could produce a high-confidence match, and every hit
  returned a hardcoded 0.95 that downstream thresholds could not act on.
  Confidence is now proportional to overlap and the shared negative class
  is wired in for pain points too. Precision 0.855 -> 0.942, recall
  unchanged at 1.0 (#205).
- An existing install now gets the commands a new version declares.
  `pipx upgrade live-sales-copilot` asks pip, pip compares version
  numbers, and `pyproject.toml` had said 0.9.0 since 2026-04-08 — across
  the v0.10.0 and v1.0.0-rc1 tags. So the documented update route printed
  "already at latest version" and installed nothing: no new code, and no
  `bin/` wrapper for any console script added since, because pip writes
  those at install time only. The release script now refuses a tag whose
  version does not match both declared copies, the pipx update
  instruction is `pipx reinstall`, and `scripts/oncue_chain.sh check`
  gained a step that compares `[project.scripts]` against what is
  actually in the venv's `bin/`.
- Architecture, technical, functional and operator documentation brought
  in line with the fixes above. The operator docs pointed at
  `data/logs/runtime.log`, which is only written when the backend is
  started via `scripts/start.sh` or the launchd agent; anyone launching
  `Start OnCue.app` was being sent to an empty file. They now point at
  `data/logs/copilot.log`, and the FAQ explains all three log files.
- Fixed a native crash roughly a second after start on every Windows
  audio configuration tested in a field test. `soundcard`'s WASAPI
  recorder is a COM object bound to whichever thread creates it, and the
  reader thread that read it never joined that COM apartment.
  `WasapiLoopbackStream` now opens, reads, re-attaches and closes the
  recorder entirely on one COM-joined reader thread. Verified from source
  only — no Windows host was available to confirm it on real hardware
  (#224).
- Warn on a zero-frame session finalize even when no audio stream ever
  attached, not only when one attached and then produced nothing. The
  prior guard was gated on stream bookkeeping that a session which never
  received a single chunk — the likeliest real cause of an empty Windows
  recording — never populated, so the single most common failure shape
  warned nothing (#225).
- Retracted an incorrect Windows telephony claim — a WASAPI level-meter
  movement attributed to OnCue's own tap turned out to be Windows' own
  `mmsys.cpl` meter, with OnCue not running at the time — and brought
  `docs/ARCHITECTURE.md`, `docs/TTD.md`, `INSTALL.md` and
  `KNOWN_ISSUES.md` in line with the Windows fixes above (#226).
- Fixed five reader-thread lifecycle defects in the WASAPI COM fix above,
  found in adversarial review — two of them regressions that fix itself
  introduced (a recorder handle leak on open-failure, and a
  `start()`/`stop()` race after a timed-out join), plus a COM-uninitialize
  call that could run without a matching init, a swallowed open-failure
  that never reached tap health or the dashboard, and a COM join failure
  that could wedge `start()`/`stop()` forever. Not testable on Windows
  here — verified with a mocked `soundcard` and `sys.platform`
  monkeypatched to win32 (#227).
- Made the autostart monitor's video-meeting candidate list
  platform-aware. It held macOS process names only (`Google Chrome`,
  `Microsoft Teams`, `zoom.us`) matched with no fuzzy fallback, so on
  Windows — where the same apps run as `chrome.exe`/`Teams.exe`/
  `ms-teams.exe`/`Zoom.exe` — the monitor polled cleanly but never
  auto-armed from a detected video-meeting app (#228).
- Bumped httpx2/httpcore2 2.9.1 -> 2.12.0, patching six new CVE advisories
  (#229), and anyio 4.13.0 -> 4.14.2, patching CVE-2026-63374 and
  CVE-2026-64847 (#231).
- Bumped pillow 12.2.0 -> 12.3.0 and pytorch-lightning 2.6.5 -> 2.6.6 (six
  and one CVE advisories, both transitive via the `diarization` extra),
  plus h2 4.3.0 -> 4.4.1 and lightning 2.6.5 -> 2.6.6 (#234).
- Stopped the pain-point keyword fast path from short-circuiting the
  embedding router on weak evidence. Measured against a 120-row fixture:
  79/120 utterances were decided by the keyword path, and 21 of those
  (26.6%) were wrong. The keyword path now only bypasses the embedding
  layer on "high"-tier confidence (measured 43/44 correct); "uncertain"-
  tier keyword hits fall through to the embedding layer instead, exactly
  as if no keyword match had been found. A related fix: a short
  live-transcript fragment merely appearing *inside* a longer known route
  utterance no longer counts as an exact quote at confidence 1.0 — only
  the full known phrase said verbatim does. Post-fix: 29/120 utterances
  decided by keyword, 0 wrong; macro precision 0.764 -> 0.859, macro
  recall 0.756 -> 0.819. Both routers built on `PainPointRouter`
  (including `ObjectionRouter`'s always-on objection/buying-signal path)
  share the fix (#235).
- A fresh `pip install -e ".[smart]"` delivers all three declared console
  scripts (`sales-copilot`, `talk-time`, `live-transcriber`) into
  `.venv/bin`, and re-running it after a new script is declared adds the
  new wrapper without removing the others — reproduced live against this
  checkout.
- A live audio tap that keeps delivering exact silence after already
  carrying signal is now reattached automatically, up to three tries
  with increasing spacing, before falling back to a warning (#243).
- Two silent detector code paths — the embedding router finding no route
  at all, and a match resolving to the shared negative class — now log
  the discarded result instead of dropping it without a trace, and the
  transcript-subscription log lines no longer write the hub's auth token
  as part of the logged URL (#240).

### Security

- Client dossier and context-document text is now PII-redacted against
  the provider that actually receives it, not the globally configured
  one (#242).
- A client's privacy ceiling (local / own-tenant / public) is now checked
  against every lane that can see conversation text, not only the live
  detector, and against the provider that will actually run rather than
  an empty default that always read as public (#246).

## [0.9.0] — 2026-07-22

- feat(audio): ship AudioTee source as the OSS/Free default, built locally into `bin/audiotee` — whole-system `tap_all` capture (Teams/Zoom/Meet, no BlackHole, no submodule); only the telephony call-tap stays Pro (#96).
- docs: OSS README rewritten to English, help-wanted section, and local per-job latency benchmarks (`docs/BENCHMARKS.md`) (#97).
- feat(replay): live replay capture path (`AUDIO_CAPTURE_METHOD=replay` + `REPLAY_SESSION_DIR` + `REPLAY_SPEED`) with the one-command runner `scripts/replay_session.py` (#98).
- feat(license): Ed25519 SCP- license keys replacing symmetric HMAC — private key only on Cloudflare Worker, offline client verification with embedded public key, legacy SC- HMAC accepted until 2026-09-01, closes self-minting gap. FeaturePolicy entitlement gating with explicit per-tier feature sets (`audio.calltap`, `compliance.central_audit`); `is_pro()` kept as compatibility wrapper. Offline-first 7-day revocation cache with phone-home to `/license/check` (pseudonymous `license_id` only, `SALES_COPILOT_LICENSE_CHECK_URL`).
- feat(server): Cloudflare Worker license control plane (`server/license-worker/`) — `/license/check`, `/license/issue` (admin-gated), `/webhook/mollie` (server-verified, idempotent, issue-on-paid + revoke-on-refund), `/audit/ingest` (key-gated, client-side hashes, tamper-evident chaining), D1 schema (licenses / payment_events / audit_events). Code complete, deploy pending.
- feat(compliance): central compliance-audit Pro feature — per-session non-blocking consent flag (`CONSENT_TRACKING_ENABLED`), configurable retention + auto-purge (`DATA_RETENTION_DAYS`, `scripts/retention_purge.py`), tiered audit writer (Pro central hashes-only / Free local SQLite).
- chore(release): public-export hardening — `scripts/export_public.sh` fail-closed allowlist with 4 gates (gitleaks 0, dependency-audit, pytest+ruff, relative-link check), `.gitleaks.toml`, `generate_secrets.py` no longer mints a license secret, launcher made self-locating.

- chore(release): fresh-repo-prep — .mcp.json secrets uit git-index, publieke file-set vastgelegd, gitleaks pre-commit hook, docs publiek-klaar (README, BETA_TESTER_GUIDE macOS 14.2+, CONTRIBUTING, INSTALL), stale planning-files gearchiveerd
- feat(release): calltap binary-gating besluit — ADR Accepted (optie C + soft-gate-nudge), bin/audiotee uit git-tracking, README Sales Pro notitie voor telefonie-capture

- feat(dashboard): whisper.cpp als standaard transcript-backend, medium-dropdown (prospect_source per gesprek: Teams/Meet of Telefoon), cache-bust v1.1.0 (PR-V3-UI-MEDIUM)
- docs(analyse): refactor-kaart (`claudedocs/2026-06-06-REFACTOR-KAART.md`, 9 bevindingen met file:regel + S/M/L + gefaseerde volgorde t.o.v. fresh-repo) + ADR calltap pro-gating (`claudedocs/ADR-CALLTAP-PRO-GATING.md`, drie opties + AGPL-weging, aanbeveling C binary-gating) (PR-V3-DEEPDIVE)
- docs(roadmap): V3.3 — telefonie-doorbraak, security verified, release-pad herzien (V3.2 gearchiveerd)

- chore(hygiene): .gitignore aangevuld (*.bak-*, excalidraw.log, .env.verify*, data/tmp/) + calltap experiment WAVs verwijderd uit data/tmp/ (PR-V3-DOCS-C)

- docs: actualiseer naar stand 2026-06-05 — ARCHITECTURE.md (calltap drie-routes + security F01-F04), INTEGRATIONS.md (Vertex AI, Groq quota, whisper.cpp recommended, mlx known issue), FAQ.md (calltap, dashboard file://, mlx issue), README.md (status, PROSPECT_SOURCE config, roadmap), INDEX.md (PR-V3-DOCS-A)
- feat(audio): `PROSPECT_SOURCE=audiotee_call` capture-route voor telefonie (PR-V3-CALLTAP-INT). Nieuwe `CallTapStream` (`src/sales_copilot/audio/calltap.py`) tapt de prospect-audio van een telefoongesprek (iPhone-relay/FaceTime) dat de virtuele audio-devices omzeilt: `pgrep avconferenced` → PID → AudioTee-subprocess `--include-processes <pid> --sample-rate 16000`, stdout-chunks door hetzelfde `AudioStream`-protocol als `BlackHoleStream` (recorder, talk-time VAD en `check_streams_liveness` consumeren het onveranderd). `PROSPECT_SOURCE` (env-default `blackhole`) is de schakelaar, ook per gesprek overschrijfbaar via het start_call-config-veld `prospect_source`; bestaand Teams/Zoom-gedrag via BlackHole verandert niet zonder expliciete keuze. Procesbeheer: supervisor-thread start/stopt de subprocess netjes (geen zombies), stderr → `logger.debug`, crash → één re-resolve + tap-herstart, daarna `audio_warning` (zelfde coaching-kanaal als de liveness-check), nooit de orchestrator omlaag. PID-resolutie-fout (geen `avconferenced`) of ontbrekende binary → `audio_warning` + gedegradeerde stilte, geen crash. Nieuwe env-vars `PROSPECT_SOURCE` + `CALL_PROCESS_NAME`. Docs: `docs/SETUP_EXPECTATIONS.md` sectie "Telefoongesprekken (iPhone-relay)". Bekende grens: een idle `avconferenced` levert stille-maar-lopende chunks, dus de chunk-tellende liveness-check waarschuwt alleen bij volledig ontbrekende audio, niet bij stilte-tijdens-verbinding.
- feat(experiment): `scripts/experiment_call_tap.py` — standalone meetscript dat tijdens een actief telefoongesprek de AudioTee Core-Audio-tap (macOS 14.2+) op kandidaat-processen richt (`FaceTime`, `Phone`, `avconferenced`, `callservicesd`, `coreaudiod` + system-wide referentie) en per kandidaat 10s opneemt naar `data/tmp/calltap_<proc>.wav` (16kHz mono int16). Meet RMS + peak op int16-schaal (drempels: RMS > 500 = call-audio, < 100 = stilte) en schrijft een tabel-rapport naar `data/tmp/calltap_report.txt` met integrale `audiotee --help`. Beantwoordt nuance #10 uit `claudedocs/2026-06-05-deepresearch-telefonie-audio-capture.md` empirisch: tapt de huidige AudioTee-build telefonie-audio? Non-invasief (read-only taps, geen device-claim) — veilig naast een actieve copilot-server. Per-proces timeout 15s, totale runtime < 2,5 min. Geen wijzigingen aan `src/sales_copilot/`. Dry-run rapporteert correct RMS 0 + diagnostische stderr-note (`error: Failed to translate process IDs to audio objects` voor idle FaceTime/coreaudiod) (PR-V3-CALLTAP-EXP).
- fix(audio): re-enumerate PortAudio devices at session start + mic/prospect stream sanity check (PR-V3-AUDIO-DEVICE-FIX). `sd._terminate()`/`sd._initialize()` runs once before stream-open when no streams are open (Bluetooth A2DP↔HFP flaps left stale device indices pointing at dead CoreAudio objects, causing `-10851 Invalid Property Value` and segfaults on end-call→start-call). Stream teardown now swallows dead-device exceptions on `stop()`/`close()` so they never escape to the orchestrator. After warmup the transcriber checks each stream for liveness within 2s; a silent stream broadcasts `{"type":"audio_warning","stream":"self|prospect",...}` on the coaching channel instead of hard-failing the call.
- docs(security): git-history scan rapport `claudedocs/2026-05-20-git-history-scan.md` (PR-V3-SEC-HISTORY) — 5 scan-categorieen op 862 commits, 22 gitleaks-hits geverifieerd, 6 echte secrets gevonden (GitHub PAT, Perplexity, Brave, n8n JWT, License/Audit HMAC + Shutdown Token in commit 080f020 van vandaag, SEOcrawler dev key). Verdict: **REQUIRES_FRESH_REPO**. Kant-en-klare squash + revoke-commands meegeleverd. 996 `.venv/`-files in commit 3293461 bevestigd als bloat (geen extra secrets).
- test(security): integration tests `tests/integration/test_pii_pre_llm_e2e.py` for F01 PII-pre-LLM redactie — 6 nieuwe tests die met BSN + IBAN payload bewijzen dat `DetectionPipeline.process` én `WindowClassifier._call_model_sync` geredacteerde tekst naar router/LLM sturen. Verificatie-evidence in `claudedocs/2026-05-19-sec-verify-evidence.md` (PR-V3-SEC-VERIFY).
- feat(testing): replay audio fixture harness (PR-106). Adds `ReplayAudioStream` — a drop-in sync `AudioStream` implementation that replays a 16 kHz mono WAV at test speed. Adds `WebSocketEventCollector` for asserting hub events. Three test categories: synthetic smoke tests (committed, CI-safe), real-call replay tests (`@pytest.mark.audio_fixture`, skipped in CI), and a sliding-window detector e2e test with mocked `WindowClassifier` that fires `pain_point` events from injected Dutch transcripts. Adds `docs/REPLAY_TESTING.md` and registers `audio_fixture` + `live_llm` pytest markers.
- feat(detector): sliding-window + multi-task LLM classification (PR-105). Replace per-fragment embedding + single-task LLM path with a `SlidingWindowBuffer` (deque of last N prospect chunks, default 5) and a single `WindowClassifier` call that returns all pain_point, objection, buying_signal, and doubt detections in one structured response. Debounces re-classification at 5 s. Phase-aware system prompt (discovery/pitch/closing). 10+ negative examples reduce false positives on backchanneling. Adds `/ws/buying-signals` and `/ws/coaching` (doubt) publish paths. Config: `DETECTOR_WINDOW_SIZE`, `DETECTOR_MIN_CHUNKS`, `DETECTOR_DEBOUNCE_S`.

- feat(transcriber): TRANSCRIBE_SELF_LIVE toggle (default off) — only prospect transcripts live; self appears in post-call report via batch transcription on recorded mic audio. Dashboard setup toggle wired to `transcript.transcribe_self_live` in `start_call` payload. `TranscriptionBackend` Protocol extended with `transcribe_file(path)`. Adds `_run_self_batch_if_needed` to reports module.
- Refactor transcriber to shared inference queue architecture: a single `InferenceWorker` owns the one `TranscriptionBackend` and WebSocket connection; mic and system audio streams become lightweight `AudioBufferer` instances that push `InferenceQueueItem` values onto a bounded `SharedInferenceQueue` with prospect-first priority ordering (priority 0 = HIGH, 1 = LOW). Eliminates concurrent Metal/MLX backend dispatches (root cause of SIGABRT), reduces WebSocket connections from 2 to 1 per call, and makes the pipeline a structural prerequisite for future streaming partial outputs. Enabled by default (`TRANSCRIBER_SHARED_QUEUE=true`); set `false` to fall back to the legacy dual-engine path without redeployment. New env vars: `TRANSCRIBER_SHARED_QUEUE`, `TRANSCRIBER_QUEUE_MAX_SIZE` (default 32), `TRANSCRIBER_SELF_PRIORITY` (default `low`).
- Remove dead WhisperLiveKit dependency from `pyproject.toml`. The `wlk` engine code-paths were removed earlier (only `direct` and `whisper.cpp` engines remain), but the `transcriber` extra still pulled in `whisperlivekit[mlx-whisper,diarization-sortformer]>=0.2.20` and a `transcriber-cpu` extra brought `whisperlivekit[faster-whisper,diarization-sortformer]>=0.2.20` — together ~75 MB of transitive deps (sortformer, ctranslate2, faster-whisper) never imported. The `transcriber` extra now requires only `mlx-whisper>=0.4` (Apple Silicon); the cross-platform path is whisper.cpp via the vendored binary. `transcriber-cpu` extra is dropped (PR-101).
- Switch default Whisper model from `large-v3` to `large-v3-turbo` (mlx-community/whisper-large-v3-turbo). ~50% faster per chunk, warmup 30s→5s, RAM 3 GB→1.6 GB, download 3 GB→800 MB. WER NL +0.5pp (negligible). Override with `WHISPER_MODEL=large-v3` if you need the absolute best accuracy (PR-100).
- Add per-session audio recording: orchestrator owns one `AudioRecorder` per call that the transcriber pushes captured chunks into, producing `data/sessions/<id>/{mic,system}.wav` plus `metadata.json`. Disable with `RECORD_AUDIO=false`. Dashboard setup view shows a Dutch privacy disclaimer (PR-97).
- Fix post-call reports silently dropping transcripts when the publisher emitted `speaker=null` (e.g. diarization fallback): session-tracker now defaults null/missing speakers to `"unknown"` instead of rejecting the entry, drops whitespace-only text, and logs every captured transcript so operators can verify the pipeline from runtime.log (PR-98).
- Add real eager Whisper warmup at orchestrator startup. PR-96.2's `WHISPER_EAGER_WARMUP` only kicked in when the transcriber module spawned at start_call; the orchestrator now pre-loads the model in a background task as soon as the backend boots so the first call is hot. Failures are logged and swallowed so a broken warmup never blocks startup (PR-99).
- Fix talk-time lifecycle mismatch by accepting orchestrator `call_started`/`call_ended` events in addition to `start_call`/`end_call`, add heartbeat and speech-event diagnostics logging, and reset tracker state per session to avoid stale SELF percentages in single-stream mode (PR-93).
- Add dashboard warmup `system_status` banner handling, transcript panel smooth autoscroll improvements, and detector pipeline debug/confirmation runtime logs for operator tracing (PR-92).
- Fix Gemini API key env-name fallback in detector LLM clients by accepting `GOOGLE_API_KEY` or `GEMINI_API_KEY` in pain-point confirmation and phase detection, and document `GEMINI_API_KEY` as preferred in `.env.example` (PR-91a).
- Flip ENABLE_OBJECTION_DETECTION, ENABLE_SUGGESTIONS, ENABLE_SUMMARY, DYNAMIC_SLIDES to default true so first-time users see AI activity out of the box; set =false in .env to opt out (PR-91b).

## Pre-OnCue history (internal numbering, 2026-04-20)
- Add talk-time heartbeat snapshots (`TALK_TIME_HEARTBEAT_MS`) tied to start/end call lifecycle so dashboard timer updates every second even in BlackHole single-stream mode (PR-89).
- Add Dutch self-serve FAQ (`docs/FAQ.md`) with 50+ issue paths, FAQ index, README escalation order, and troubleshooting cross-links.
- Add zero-touch macOS first-run installer (`scripts/first-run.sh`), start/stop wrappers, and honest Dutch setup/troubleshooting docs.
- Fix transcriber `start_call` warmup flow (with coaching `system_status` updates), add single-stream default speaker mapping (`SINGLE_STREAM_SPEAKER_DEFAULT`), and wire talk-time call duration to `start_call`.
- Fix setup-screen Start Call/upload API requests to consistently use resolved API base URL so `file://` dashboard usage hits `http://localhost:8760` instead of relative paths.
- Add an operator-driven manual end-to-end validation plan with scripted NL/EN/DE call fixtures and expected-event checklist artifacts.
- Add AGPL-3.0 licensing/governance docs (`LICENSE`, `LICENSE-COMMERCIAL.md`, `CONTRIBUTING.md`, `CLA.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`) and README policy links for public release.
- Convert whisper.cpp and AudioTee vendor clones to pinned git submodules and add comprehensive OSS attribution in `THIRD_PARTY_LICENSES.md`.
- Add NL/EN/DE pain-point and objection route packs with per-call `CALL_LANGUAGE` routing and automatic Whisper language propagation.
- Add Whisper Dutch sales fine-tuning pipeline scripts, evaluation report flow, and optional MLX adapter loading via `WHISPER_FINE_TUNED_MODEL_PATH`.
- Add optional AI-generated fallback slides (`DYNAMIC_SLIDES`) with slide-control injection when no case match exists.
- Add opt-in real-time follow-up question suggestions with a new dashboard panel and `/ws/suggestions` feed.
- Add optional automatic conversation phase detection with LLM phase classification and auto phase-change events.
- Add objection detection routes, response templates, WebSocket publishing, and dashboard panel support.
- Add opt-in 60-second conversation summaries with key moments, `/ws/summary`, dashboard Samenvatting panel, and post-call report inclusion.
- Add structured runtime logging with configurable `LOG_LEVEL`, stdout output, and rotating file logs.
- Add graceful shutdown hardening: managed audio stream contexts, periodic session checkpoints, async end-call report generation, and hub websocket close-all handling.
- Remove deprecated WhisperLiveKit bridge/transcriber codepaths and keep transcription engines to `direct` + `whisper.cpp`.
- Refactor WebSocket hub into split core, API, upload, and static modules behind a thin wrapper.
- Filter direct-transcriber silence hallucinations (`***`, `...`, subtitle artifacts, symbol-only snippets) before publishing.
- Add per-call transcript backend selection to setup presets and orchestrator config overrides.
- Add transcriber backend abstraction (mlx backend factory) and fix DirectWhisperEngine loop cooperativeness for continuous transcription.
- Add whisper.cpp install/config scaffolding, WAV utilities, chunker, and backend adapter.
- Keep DirectWhisperEngine hub WebSocket open across publishes with pre-connect and auto-reconnect on send failure.
- Add direct mlx-whisper transcription mode with VAD-gated buffering and optional WLK fallback mode switch.
- Switch transcriber startup to a single shared WhisperLiveKit instance with BlackHole-only single-stream fallback.
- Fix Whisper bridge stability by using a single interleaved send/receive loop and add WLK anti-repetition defaults.
- Add audio device health preflight and `verify_audio.py` for mic/BlackHole checks.
- Fix backend start-call flow by normalizing API/WS payloads, preserving orchestrator looping, and reducing detector startup blocking.
- Add Start Call diagnostics, API E2E coverage, and reset hub state to waiting_for_config on end_call.
- Fix dashboard Start Call flow by removing global script collisions and using direct HTTP `/api/start-call`.
- Refactor transcription to dual-stream mic/system Whisper inputs with stream-based speaker tagging.
- Fix critical demo blockers: blackhole talk-time capture, preset contract, config/upload endpoints, configurable WS URLs, end-call UX, and speaker swap flow.
- Fix end-to-end audio pipeline by adding WhisperLiveKit audio bridge and removing dashboard transcript placeholders.
- Apply VNX Digital branding to dashboard and presentation themes.
- Add config E2E test and configuration documentation for setup screen flow.
- Add context doc support for LLM confirmation and reports.
- Add setup screen JS for presets, uploads, and start-call config.
- Add optional module config parameters for orchestrator-driven startup.
- Refactor orchestrator to wait for call config before starting modules.
- Add optional config parameters to module entry points for orchestrator usage.
- Add hub upload, presets, and config endpoints.
- Add dashboard pre-call setup screen with configuration controls.
- Add CallConfig presets and per-call overrides.
- Add Module 4 certification docs, README full setup, and full-system E2E test.
- Add reports module runner and full copilot orchestrator.
- Add dashboard post-call report panel with end-call action and JSON download.
- Fix report generator test imports for session event helpers.
- Add session tracker to collect call data for reports.
- Add post-call report generator with JSON export and summary stats.
- Add Module 3 certification docs and entry point runner.
- Add detector module runner for transcript-driven slide injection.
- Add slide injection orchestrator to emit pain point and slide control events.
- Add dashboard pain point detection panel with live updates.
- Add Reveal.js WebSocket slide control client with hidden case slides.
- Add pain point detection pipeline with debouncer and LLM confirmation flow.
- Add provider-agnostic LLM confirmation client for pain point detection.
- Add semantic router pain point classifier with detector tests.
- Add BlackHole audio capture backend using sounddevice.
- Add Module 2 certification documentation and evidence summary.
- Add live transcript panel with WebSocket feed and auto-scroll controls.
- Add SQLite case database module and seed script updates.
- Add Module 2 transcriber engine wrapper for WhisperLiveKit.
- Add Module 2 architecture contract for Live Transcriber.
- Add Module 1 certification documentation and evidence summary.
- Fix lint issues in Module 1 config, publisher, and VAD modules.
- Add Reveal.js presentation shell with hidden case templates.
- Add responsive dashboard layout with manual dark mode toggle.
- Add phase toggle highlighting and monologue warning display in dashboard UI.
- Add dashboard WebSocket client for live coaching updates.
- Add coaching dashboard HTML skeleton with breathing bar styling.
- Add Silero VAD wrapper for speech event generation.
- Add audio capture streams (MicStream, AudioTeeStream) and DualAudioCapture factory.
- Add talk-time tracker data models, alerting, and rolling window calculations.
- Add talk-time WebSocket publisher for state updates and coaching alerts.
- Add talk-time module runner for standalone execution.
- Update setup script and README quickstart for Module 1 setup.
- Add CI quality gate script for lint/test/import checks.
- Add transcriber module runner entry point.
