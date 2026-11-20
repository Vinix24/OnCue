# Changelog

## [Unreleased]

Unreleased changes live in `changelog.d/` until the next release (see `changelog.d/README.md`).

## [1.0.0] — 2026-11-20

### Highlights

- Record and transcribe a call with the local notetaker. Transcription runs on the Mac and the report with the transcript is written locally (rc.4, the notetaker preset; rc.6, the launch dialog's "Record and transcribe"). A recording bar with pause and markers controls it (this section, "A floating recording bar", "Pause, resume and markers").
- Free live coaching during a call: live coaching (`coaching.live`) is part of the free tier, so it works without a license key. Detection and coaching run locally (this section, "Live coaching is part of the free tier"; rc.2, "No LLM is configured by default"; rc.6, the "Live coach" option).
- English is the default language of a new install. Dutch stays fully supported with `LANGUAGE=nl` and `CALL_LANGUAGE=nl` (this section, "English is the default language of a new install").
- With no LLM key configured, no text goes to a model host, and a cached embedding model loads without contacting huggingface.co. A fresh install ships with `LLM_PROVIDER=none`, and every cloud provider is an explicit choice (rc.2, "No LLM is configured by default"; this section, "A cached embedding model loads without contacting huggingface.co").
- The "show me what it does" demo replays an English call on an English install, and the real talk-time tracker raises a coaching alert during it (this section, "The 'show me what it does' demo replays an English call" and "The 'show me what it does' demo drives the real talk-time tracker").
- One-line install on a Mac with Apple silicon and macOS 14.2 or newer, without Xcode command line tools, Homebrew or a password by default (rc.6, "The one-line install needs no Apple command line tools"). The release now also carries the recording bar (this section, "The release build, the installer and the public export carry the recording bar").

### Added

- A floating recording bar. `OnCue Bar.app` (built by `scripts/build_oncue_bar.sh`, source in `native/oncue-bar`) shows the notetaker's compact `/bar` page in a non-activating panel above all windows, full-screen apps included, with a drag handle and a global hotkey (default `ctrl+option+r`, `BAR_HOTKEY`) that starts and stops a recording. Without the notetaker server it shows "connection lost" and retries every 2 seconds. The notetaker server serves `GET /bar` and `GET /status` (phase, elapsed time, session, pid; local only). The OnCue launcher starts the bar after the server answers and only when `bin/OnCue Bar.app` exists; a missing bar is logged every time and notified once, and the browser recording page stays the full control. The bar's right-click menu has **OnCue afsluiten**, which asks first while a recording runs, and the bar opens on the screen with the menu bar. (#343, #346, #354, #355)
- Pause, resume and markers while recording. The engine runs pause, resume and marker on one recording-time clock (wall time minus pauses); audio during a pause is discarded. The bar and the recording page show pause/resume and mark buttons, a paused state (amber, frozen time) and a marker count; `metadata.json` gets optional `markers` and `pauses`. In `transcript.md` each marker is its own line (`Marker mm:ss`, `Markering mm:ss` in Dutch) before the first segment at or after it. A transcript without markers is unchanged. (#345, #348)
- The notetaker finishes a running recording when it is shut down, and keeps pending transcriptions across restarts: a `.recording` marker and a `.transcribe-pending` marker, and at start every folder with the pending marker and no `transcript.md` is queued again. After a hard stop (no clean shutdown), the server rebuilds `metadata.json` (`recovered: true`) for a session folder that has a `.recording` marker and audio, and fixes the WAV header sizes. Audio since the last flush, markers and pauses are still lost in that case. A failed rebuild writes `recovery-failed.txt`. (#347, #352)
- Only one route runs at a time. Starting the copilot or the notetaker while the other runs asks whether to switch; the switch stops the old route with SIGTERM, waits for its pid and port to be free (up to 60 s, `ONCUE_SWITCH_TIMEOUT_S`) and then starts the new one. Both routes write `data/run/<route>.json`. The notetaker port is `RECORDER_PORT` (default 8780). `scripts/launcher/stop-oncue.command` stops the notetaker and the bar with SIGTERM and asks before it interrupts a recording. (#353, #359, #361)
- The release build, the installer and the public export carry the recording bar. The release tarball contains `OnCue Bar.app`, and `bootstrap.sh` and `install.sh` place and check it on both install routes. A missing or unbuildable bar gives one message and never fails the install. (#356)
- A reader for which processes use the microphone (`audio/mic_usage.py`, probe: `scripts/probe_mic_users.py`). It is a building block only: nothing in the app calls it yet, so there is no automatic call detection by microphone use in 1.0.0. (#350)
- The microphone meter is pinned across a device reopen: a lost and reopened microphone is logged at WARNING and the device label shows the device name instead of its index. (#363)
- The call-start monitor on the copilot route has a hardened consent prompt: every prompt carries a `prompt_id`, its `source` (video or telephony) and a `consent_resolved` outcome on `/ws/system`. Yes and No are checked against the open prompt and answer 409 for an unknown, old or expired one; a prompt expires when its source has no fresh observation for over 10 s, on a manual start, and while the other route runs; a Yes during an active call starts nothing. A display that connects gets the open prompt or none. The monitor is on by default (`AUTOSTART_MONITOR_ENABLED=true`) but only prompts on the copilot route, and only with `system.autostart` (Pro or Enterprise, not the free tier) and with the login-autostart wizard step installed with consent tracking on; it never starts a recording without the user's confirmation. The notetaker route does not run this monitor in 1.0.0, so the notetaker does not start by itself when a call begins. (#351, #357, #358, #360, #362)
- OnCue.app downloads the speech model at first start into Application Support, with progress on the start page (texts from the i18n catalogue, English by default and Dutch with `LANGUAGE=nl`), resume from a `.part` file, and a "try again" button after a failure or a full disk; recording stays disabled until the model is there. The app also checks its port first: if an OnCue already runs (for example the one-line install), it opens that one and says from which folder instead of starting a second.
- AudioTee now logs output-device and route changes (default output, system output, device list, aggregate alive) as one `audiotee: device_change ...` line each in recorder.log and copilot.log, plus a start line with the current devices.
- `scripts/build_dmg.sh` signs the app with a Developer ID when `SALES_COPILOT_CERT_ID` is set: inside-out, hardened runtime, secure timestamp, no `--deep` (`scripts/sign_app.py`, entitlements in `scripts/oncue.entitlements`). It then signs, notarizes (`SALES_COPILOT_NOTARY_PROFILE`, a notarytool keychain profile) and staples the .dmg and verifies it with `codesign`, `stapler` and `spctl`. The notarization password route is gone.
- A **Check for updates** button on the recordings start screen. OnCue contacts GitHub
  only when you click it (ADR-006 point 7), and tells you what to do for the app, the
  one-line install or a git checkout.
- The notetaker opens on a library of recordings (newest first, with title, date, duration and status). A recording opens with its transcript and, when a report has one, its summary. The recorder moved to `/record`.
- `scripts/measure_live_detection.py` replays a recorded dialogue through the live path on a scratch hub port with a per-run token and writes per-channel event counts and arrival delays; `docs/BENCHMARKS.md` records the result for a free install without a key.
- OpenRouter calls can fall back to other models: set `LLM_FALLBACK_MODELS` or `<TASK>_LLM_FALLBACK_MODELS`, and a 5xx, 429, timeout or connection error retries the request on the next model at once (no SDK retries on the failing model; the timeout budget is split equally over the attempts).
- A recording stops by itself when both tracks have been quiet for a while: on by default for the notetaker (120 s), off for `scripts/record_call.py`. `RECORDER_AUTOSTOP_SILENCE_SECONDS`, `RECORDER_AUTOSTOP_PROSPECT_DB` and `RECORDER_AUTOSTOP_SELF_DB` (dBFS) set it. The clock starts at the first audible chunk and stands still during a pause. The page and the bar show "stopped after N s of silence" and the transcript gets a marker line.
- `OnCue.app` now contains the recording bar: `scripts/build_dmg.sh` embeds `OnCue Bar.app` in `Contents/Helpers/` (`scripts/embed_bar.py`), the app starts it with the notetaker's port and stops it on quit (a failed start is logged and the notetaker keeps running), `scripts/sign_app.py` signs it before the outer app (`--dry-run` lists the order) and `scripts/smoke_app.sh` checks that it starts and is gone after the app stops. The start dialog and the port messages of the app now come from the i18n catalogue (English by default, Dutch with `LANGUAGE=nl`), and `notify` no longer stops the app from opening a running OnCue when `osascript` cannot be launched.
- `scripts/release_dmg.sh <dmg>` prepares a notarized `OnCue-<version>-arm64.dmg` for the release: it checks the name against the `pyproject.toml` version, refuses a .dmg that fails `xcrun stapler validate` or `spctl --assess`, writes `<dmg>.sha256` and prints the `gh release upload` command without running it. The README and INSTALL.md now put the .dmg download first for macOS, with the one-line install as the second route.
- `scripts/render_vocabulary.py <file>` renders a vocabulary file to the prompt the backends send, counts its tokens against Whisper's 223-token prompt budget and exits non-zero naming the trailing terms over budget. The whisper.cpp and mlx backends log a WARNING once per startup for an over-budget vocabulary instead of truncating silently, naming both the trailing terms to cut and the framing sentence plus leading terms that whisper.cpp drops otherwise; a missing or broken tokenizer only logs an INFO line and never stops startup. `scripts/vocabulary_candidates.py <transcript> <corrected>` lists candidate terms from a correction. `docs/CONFIG.md` explains a vocabulary file per customer. The default vocabulary no longer carries one maker-specific term.
- The report gets an "Open questions" block (`open_questions` in the enrichment: questions still open at the end of the call, PII-redacted like the action items, hidden when empty) in the report page and the Markdown export, in English and Dutch. The report page has a "Download PDF" button that uses the browser's print-to-PDF with an `@media print` stylesheet; no dependency, nothing leaves the machine.
- The installation (scripts/bootstrap.sh) and the setup wizard now measure how fast this computer transcribes, with a short fixed clip through the speech model, and choose the model from that: the default stays on a fast Mac, a smaller model on a medium one, and on a computer too slow for live transcription OnCue says so in plain language and points to the notetaker with transcription afterwards. Both thresholds are settings (`SETUP_SPEED_FAST_MAX_RTF`, `SETUP_SPEED_SLOW_MIN_RTF`), and a `WHISPER_MODEL` you set yourself is kept.
- Documented the speaker boundary: OnCue stores no voice profiles. `docs/ARCHITECTURE_BOUNDARIES.md` gets invariant 7 and separates diarization, per-channel speaker assignment and speaker identification; `docs/PRIVACY.md` and `docs/FAQ.md` point to it, and `tests/test_speaker_boundary_docs.py` fails if the diarization modules write anything to disk besides the model download.
- Record the live suggestion latency measurement (OpenRouter haiku-4.5 against Vertex gemini-2.5-flash, 30 runs per mode) in docs/BENCHMARKS.md, with the raw result in docs/benchmarks/.
- The suggestion latency measurement script writes the fragment path repo-relative, and the recorded result no longer carries a local absolute path.
- Windows (experimental): the loopback can follow the output device where the call plays (ADR-008). With `WINDOWS_ENDPOINT_FOLLOW=true` it picks, every 2 seconds, the endpoint where an app from `config/wasapi_call_apps.yaml` has an active audio session, else the default communications device, else the default output; a matched `AUDIO_WASAPI_ENDPOINT_NAME` still wins. The loopback switches when the choice changes: the new endpoint is opened before the old one is closed, the gap is filled with silence so the tracks stay aligned, and each switch logs the old and new endpoint, the reason and the gap in milliseconds. If an endpoint disappears the next one is tried at once, and after 10 seconds without any endpoint the tap reports `no_tap` with one audio warning. Off by default, and not yet tested on Windows hardware.
- The Windows call-app list is also found in a normal installation (not only in a source checkout), so following the call's output device keeps working after install.
- Windows (experimental): an opt-in fallback capture route, `WINDOWS_CAPTURE_ROUTE=process`, records everything except OnCue's own process tree (process loopback, Windows build 20348 or newer), for a call the endpoint route misses. It warns when Windows has set the volume of OnCue's capture below 10% or muted it. The default stays `endpoint`. Not yet tested on Windows hardware.

### Changed

- Live coaching is part of the free tier. `coaching.live` is in `FREE_FEATURES`, so a user without a license key gets live coaching; every other Pro feature stays Pro. The dashboard copy and the tier tables in the docs follow. (#370)
- The "show me what it does" demo drives the real talk-time tracker (OI-054). The player no longer computes a meter of its own (status always `green`, monologue always 0): it feeds each transcript event to a `TalkTimeTracker` built from `TalkTimeConfig.from_env()`, using the sample's own timestamps, publishes `get_state().to_dict()` on `talk-time` and `check_alerts()` on `coaching`, so the replay speed does not change the result. Both samples now open with a long seller pitch (about 85 s, in nine consecutive segments), so the demo shows a `monologue_warning` and a red talk-time status; the later events moved by the same offset.
- Presets follow the call language. `load_preset(name, language)` reads `config/presets/<name>_<lang>.yaml` when it exists, else the base file, which is Dutch. A base file used for another language no longer applies its inline Dutch routes and `ui_labels`: the language route files (`pain_points_<lang>.yaml`, `objections_<lang>.yaml`) apply instead, and the consent tier, prompt addendum and category identifiers stay. New `config/presets/sales_en.yaml` is the English sales preset, so an English call shows English objection and pain-point labels (`price`, not `prijs`). `acquisitie`, `coach` and `recruitment` have no English file yet and use the English route files on an English call.
- The "show me what it does" demo replays an English call on an English install. New `data/samples/sample-aha_en.json` (a fictional quote-process and price-objection call) is chosen by `CALL_LANGUAGE`: `nl` keeps `sample-aha.json`, every other language gets the English sample. `SAMPLE_AHA_PATH` still wins when set, so `.env.example` no longer sets it.
- English is the default language of a new install. `LANGUAGE`, `CALL_LANGUAGE` and the Whisper language default to `en` (`i18n.DEFAULT_LANGUAGE`, `config.py`, `.env.example`; `WHISPER_LANGUAGE` is commented out and follows `CALL_LANGUAGE`). English is also the fallback catalog, and an English call gets `config/transcription_vocabulary_en.yaml` instead of the Dutch word list. Dutch stays fully supported with `LANGUAGE=nl` and `CALL_LANGUAGE=nl`.
- An update of an existing install keeps it Dutch: `scripts/install.sh` runs the new `scripts/migrate_env_language.sh`, which appends `LANGUAGE=nl` and `CALL_LANGUAGE=nl` to a `.env` that sets neither key and prints one line saying how to switch.
- The OnCue.app dialog and its notification follow the install's `LANGUAGE` (read at runtime by `scripts/launcher/oncue-language.sh`): **Cancel**, **Live coach** and **Record and transcribe** in English, the Dutch labels with `LANGUAGE=nl`. The README opens with the notetaker plus free live coaching, and the docs name the English label.
- OnCue has a new app icon and logo (concept C): a white "O" ring with a conducting
  baton and a red cue dot on a dark tile. The dashboard, the presentation and the
  favicon use the same mark, and loading states show it conducting a bar.
- CI runs the test suite in parallel with pytest-xdist (`-n ${PYTEST_WORKERS:-2}`, `--dist loadgroup`), in `scripts/ci.sh` and in `.github/workflows/ci.yml`.
- The release-route install tests build their release fixture once per module and no longer wait a full second per logged step; the file runs in about a fifth of the time.
- The security audit workflow runs on push and pull request only when `pyproject.toml`, `constraints.txt`, `uv.lock` or the workflow itself change; the daily schedule stays.
- A new push to a pull request cancels the CI run in progress for that pull request; main-branch runs are never cancelled.
- The PRD, technical design, architecture notes and operator test plan now describe the public LLM tier as OpenRouter only (local Ollama, own tenant Azure OpenAI or Vertex AI); the older openai, groq and gemini provider values are mentioned only as still accepted for existing configurations. The doc test covers these four files too.
- The public LLM tier is OpenRouter only in the README, INSTALL, the user-facing docs, `.env.example` and the setup wizard: one key gives access to many models. The first-run script asks for one of three tiers (local, own tenant, public via OpenRouter) and the dashboard setup step lists Ollama, Azure, Vertex and OpenRouter. The openai, groq and gemini code paths in `llm_client.py` are untouched, so existing `.env` files keep working.
- `docs/TTD.md` section 7 now documents the tap messages: `audio_warning` (with the
  `tap_event` and `attempts` fields), `tap_status`, `transcript_marker` and the `tap` object of
  the notetaker's level and state messages. A test keeps those schemas in step with the code.
- The LLM provider row in the technology decisions table no longer lists OpenRouter as a
  rejected alternative.
- The license worker sends a dedicated welcome e-mail to new Windows waitlist leads; every other signup source keeps the Pro-waitlist mail.

### Fixed

- A cached embedding model loads without contacting huggingface.co. When the model is in the Hugging Face cache, `PainPointRouter` builds its encoder with `local_files_only=True`, so no request goes to huggingface.co; `HF_HUB_OFFLINE` is not set process-wide. A model that is not cached keeps the online first-run download and logs one INFO line. (#377)
- Talk-time: the short final chunk of a replayed stream (below 512 samples at 16 kHz) is zero-padded before the voice-activity model scores it, instead of raising. Live capture is unaffected. A test also pins that a `module_failure` event reaches a coaching subscriber on the free tier. (#375)
- The launcher decides that a port is held only for a listening socket (`lsof -nP -iTCP:PORT -sTCP:LISTEN`). A browser tab on OnCue's own port or a stale closed connection no longer makes the launcher call the port foreign. (#366)
- The notetaker transcript uses the same speaker labels as the report (`Me` / `Other`, Dutch `Ik` / `Gesprekspartner`) from one definition. The README and public docs say "in the dashboard" instead of "second screen", and the privacy-ceiling claim in the README and `docs/PRIVACY.md` is limited to the coaching route, with a statement of what the notetaker does. (#368)
- The monologue alert fires only for the seller (OI-058). `TalkTimeTracker.check_alerts` raised "Time to listen" when the prospect talked past `monologue_warning_seconds`, and the call report counted it. Now only a seller monologue raises `monologue_warning` (and the red talk-time status); a prospect monologue raises nothing on that rule, and the alert re-arms when the speaker changes. The ratio rule is unchanged.
- The errors, prompts and buttons of `scripts/launcher/oncue-launch.sh` come from the `launcher:` section of `config/i18n/{en,nl}.yaml`: English by default, Dutch with `LANGUAGE=nl`. `oncue-language.sh` accepts whitespace around `=` (`LANGUAGE = nl`), like the app's dotenv loader and `migrate_env_language.sh`, so such a `.env` no longer gives English launcher text next to a Dutch app.
- The notetaker page (`dashboard/recorder.html`) and the messages `scripts/record_server.py` sends to it are English by default and Dutch with `LANGUAGE=nl`. Their strings live in `config/i18n/{en,nl}.yaml` (`recorder:`); the page loads them with the dashboard's `js/i18n.js` from the recorder server (`/api/config`, `/i18n/<lang>.json`), and `<html lang>` follows the language. The button reads **Show in Finder** in English.
- The Pro-lock teaser on a response slot (OI-045) comes from the i18n catalog (`policy.response_locked_teaser`), so an English call no longer shows Dutch text. The dashboard recognises the teaser in both languages. `en.yaml` and `nl.yaml` carry the same keys.
- The GitHub-hosted macOS jobs (release binaries, CI, Security audit) run on `macos-15` because GitHub retired `macos-14` on 2026-11-02. Binaries still target macOS 14.2.
- Attach-failure details, the phone-call tap and WASAPI warnings, and the remaining Dutch texts in the audio capture now follow `LANGUAGE` (English by default, `nl` as a toggle). The recording bar no longer flashes a raw key such as `recorder.bar_connection_lost` on first paint: it renders once the catalog has loaded, also when loading it fails.
- `scripts/build_dmg.sh` (about 6 GB per run) and `scripts/smoke_app.sh` (about 1.5 GB) no longer leave their temp folders behind: an `EXIT` trap removes them on success, failure and interrupt through a guarded `scripts/safe_tmp_rm.sh` that refuses an empty path, `/`, `$HOME` and anything outside the one folder the callers create theirs in (`${TMPDIR:-/tmp}`, never the folder itself); `smoke_app.sh` installs its trap before the copy, so a failed copy leaves nothing behind. `ONCUE_KEEP_BUILD_TMP=1` and `SMOKE_KEEP=1` keep the folder for debugging and print its path.
- The detection router is built in the background when the detector starts, so the first detection of a call no longer waits about 8 s for the embedding model.
- The "warming up" status shown on the dashboard follows `LANGUAGE` instead of always being Dutch.
- The objection router and, where the tier uses script tracking, the script tracker model are built in the background when the detector starts, so the first detection no longer waits for them (about 2.9 s down to about 2.0 s in the free-tier replay).
- Detector warm-ups run in daemon threads that shutdown stops waiting for, so a slow model load cannot delay process exit.
- The BlackHole attach-failure detail follows `LANGUAGE` instead of always being Dutch.
- The privacy, recruitment-DPIA and license-key docs now describe how OnCue
  behaves today: the notetaker always writes WAV tracks, `DATA_RETENTION_DAYS`
  is off by default and never purges notetaker recordings, legacy `SC-` keys are
  rejected, and the free tier needs no key.
- The telemetry allow-list section now names the speech-model and diarization
  model downloads as the remaining network clients.
- The public export's test gate is green again: the TTD tap-schema guard (which reads
  `docs/TTD.md`) no longer travels into the export, the user-facing docs scan skips the
  docs the export leaves out while still failing on any other missing doc, and the
  insight-routing test skips in a tree without the private deep-insight lane.
- The WASAPI `no_tap` follow test no longer races the 10 s outage grace with a 5 s wait:
  the wall-clock wait is 30 s, enough for the reader thread to run the grace under heavy
  CI/build load, while a real bug still fails because `no_tap` then never comes.
- Fixed: the first-run LLM tier prompt no longer rewrites `.env` when it cannot be read, keeps its file mode, and writes `LLM_MODEL` with the provider (local default `qwen2.5:7b`).
- The start dialog of OnCue.app no longer pins the bundled i18n catalogue for the rest of the process: a catalogue you edited in the app-support folder is used by the recorder and the dashboard again.
- The app smoke test stops an app that ignores SIGTERM after `SMOKE_STOP_TIMEOUT` seconds (default 20) with SIGKILL instead of hanging, and still removes its temp folder.
- `scripts/measure_live_detection.py` no longer reports an incomplete run as a valid measurement: a failed channel subscription is named in `result.json` (`error`, `failed_channels`) and gives a nonzero exit; an exception in the run or a failing child start still writes `events.jsonl` and `result.json` with a structured `failure` (stage, error type, message) and exits nonzero; the child gets the provider key variables set to an empty value so a repo `.env` cannot change a measurement.
- The live copilot now warns the operator as soon as the system tap delivers only zeros while the microphone carries signal, then reattaches, and reports whether the signal came back or the attempts ran out (one warning per outcome, the gave-up warning stays on the dashboard).
- The .dmg no longer fails notarization on Mach-O files inside python312.zip: the packages that carry dylibs (PIL, mlx, sounddevice data, llvmlite, protobuf, a namespace package, with its C extension) ship unzipped so they get signed, build_dmg.sh fails when a zip in the bundle still holds a Mach-O, and the smoke test imports them with the bundle interpreter and only the bundle library path.
- The notetaker recorder page and bar now warn at once when the system audio is lost, show a short note after it is back, and add lost/reattached lines to the transcript.
- A second, independent tap dropout in the same call now gets its own 3 reattach attempts.
- A system track that is exact zero from the start while the microphone carries signal gets a calm `no_system_audio` warning after `AUDIO_TAP_NO_SYSTEM_AUDIO_WARN_SECONDS` (default 120); it never reattaches.
- The tap warning on the recorder page now disappears when the recording stops, and a tap that gave up reconnecting says so (`tap_gave_up`) instead of "Reconnecting".
- The notetaker now notices when the system-audio tap delivers only exact zeros for 30 s while the microphone still carries signal, reattaches it (3 attempts at most), logs each state change to recorder.log, stores `tap_events` in `metadata.json` and puts the tap state in the record_server state payload.
- `scripts/record_call.py` now loads `.env` before it reads the `RECORDER_AUTOSTOP_*` settings, so they work for both recorders without exporting them in the shell. The notetaker server keeps its auto-stop state under its lock.
- The Vertex AI provider no longer falls back to a built-in Google Cloud project: without `GCP_PROJECT` it reports a clear configuration error.
- The recording bar's quit menu and "connection lost" page follow the configured language.
- `.env.example` recommends a report-terms model that `config/llm_models.yaml` offers.
- `WHISPER_MODEL=large-v3-turbo-q8_0`, as written by the Windows installer, is a known model name.
- Removed settings that did nothing: `DIARIZATION_BACKEND`, `MIN_SPEAKERS`, `MAX_SPEAKERS`, `WHISPER_CPP_CHUNK_MS`, `OLLAMA_MODEL`.
- Tap warnings, transcript markers, the recording bar labels and the AudioTee start errors now follow `LANGUAGE` (English by default, `nl` as a toggle) instead of being hard-coded Dutch. The live dashboard also keeps a "lost" tap warning until the outcome arrives and shows the "recovered" message (audio in between is missing from the transcript) instead of clearing it silently.
- `WHISPER_MODEL=large-v3` now works on both backends. One alias table (`modules/transcriber/backends/model_names.py`) maps `tiny`, `base`, `medium`, `large-v3-turbo` and `large-v3` to the mlx repo id and the whisper.cpp ggml file; a full `owner/name` repo id passes through, and an unknown name fails before any download with the list of valid aliases. A missing whisper.cpp model names the download command. The ggml file is resolved only when whisper.cpp is the selected backend, so an mlx config with a full repo id loads, and a missing model at an explicit `WHISPER_CPP_MODEL_PATH` says the file is missing at that path and where the download script writes.
- The setup wizard's model check and `scripts/transcribe_file.py` now use the same alias table, so `detect_model()` finds an mlx model that is in the Hugging Face cache and `transcribe_file.py` builds the `-mlx` repo id. A test scans every `.py` under `src/` and `scripts/` for a hand-built repo id.

### Security

- `tornado` is bumped to 6.6.1 (CVE-2026-126347) and `opentelemetry-api` to 1.45.2 (CVE-2026-126360); `uv.lock` and `constraints.txt` follow. (#367)
- The speech model download is verified: the first-start download in OnCue.app and `scripts/install.sh` check the SHA-256 of `ggml-large-v3-turbo.bin` (pinned to the Hugging Face LFS hash) before the file gets its final name; a mismatch deletes the partial file and the start page offers "try again" (English and Dutch). With a custom `ONCUE_MODEL_URL`, set `ONCUE_MODEL_SHA256` next to it; without it the check is skipped and logged. A resume now sends `If-Range` and continues only on a matching `Content-Range`, otherwise it restarts, and a 206 without `Content-Length` takes its total from `Content-Range`.
- `scripts/sign_app.py` gives the audio-input entitlement only to Mach-O executables (by file type, so the Python framework binary no longer gets it), and a failed `hdiutil detach` after verification now fails the build instead of leaving the image mounted silently.

### Documentation

- New `docs/decisions/` with ADR-001 to ADR-008: the recording bar window (ADR-001), the recording timeline with pause and markers (ADR-002), call detection by microphone use (ADR-003), consent and one route at a time (ADR-004), one recording per call with the notetaker first (ADR-005), macOS `.dmg` packaging (ADR-006), autostart layers and opt-in macOS permissions (ADR-007) and Windows capture following the call's endpoint (ADR-008). They record decisions; they do not mean the described features exist in 1.0.0. (#344, #349, #364, #371, #372, #373)
- `docs/FEATURES.md` describes the recording bar, and `docs/TTD.md` section 7 describes `consent_resolved` and the prompt id. (#356, #357, #360)

## [1.0.0-rc.6] — 2026-11-08

### Documentation

- Install docs for non-developers. `README.md` opens with a short Dutch and English block: what a Mac needs (Apple chip, macOS 14.2, 5 GB free), the one line to paste in Terminal, and a prompt for an AI assistant. The Dutch section and the English `## Install` section follow the same flow, and the "Two ways to use it" table now names **Opnemen en uitschrijven** and `transcript.md`.
- New `docs/INSTALL_WITH_AI.md`: a self-contained guide an AI assistant follows to install OnCue. It covers the preflight, which route runs and what the human must do (password and Apple window on the build route only), verification, first launch with the macOS permissions, a 30-second test recording, troubleshooting and hard rules.
- `INSTALL.md` starts with the one-line install and the measured release-route numbers (about 2.8 GB downloaded, 55 seconds on a fast connection, 4.2 GB on disk). The ZIP route (Path 3) is removed: no release has an asset. macOS 14.2 is the minimum everywhere, and the Gatekeeper steps are gone for the one-line route.
- `KNOWN_ISSUES.md` replaces "No install wizard" with the current state: a one-line installer, and no signed or notarized app or dmg yet. `docs/BETA_TESTER_GUIDE.md`, `docs/SETUP_EXPECTATIONS.md`, `docs/FAQ.md`, `docs/TROUBLESHOOTING.md` and `scripts/launcher/README.md` follow the same facts.
- The two option lines of the launch dialog in `scripts/launcher/build_app.sh` use a colon instead of an em-dash. Button names and behaviour are unchanged.

### Changed

- `scripts/install.sh` requires macOS 14.2 or newer (what AudioTee needs), checks for
  the real command line tools (the `git` and `swift` shims on a Mac without them
  passed the old check) and accepts only Python 3.11, the version CI tests.
- `scripts/install_whisper_cpp.sh` no longer prints the `--help` text of whisper-cli
  and whisper-server.

### Fixed

- Self-hosted CI builds its venv with a pinned `python3.13` (`SELF_HOSTED_PYTHON`)
  instead of the bare `python3`. A Homebrew upgrade moved `python3` to 3.15, which
  broke dependency resolution on every run since 2026-11-07. The step now fails
  with one clear line when the interpreter is missing. The Security audit workflow
  now uses the same pin.
- Browser recorder: a mic or system-audio stream that fails to open is now logged
  (`data/logs/recorder.log`), retried in the background with backoff, shown in red
  in the recorder page ("Microfoon niet gestart") and written to `metadata.json`
  (`failed_streams`, `late_start_ms`). A late track is padded with leading silence
  so both tracks keep the same length.

### Added

- The one-line install needs no Apple command line tools, no Homebrew and no password
  by default. `scripts/bootstrap.sh` now takes a release route: it downloads the
  latest release of Vinix24/OnCue (or `ONCUE_RELEASE=<tag>`), verifies the prebuilt
  `oncue-macos-arm64.tar.gz` against its `.sha256` and places whisper-cli,
  whisper-server and audiotee where the code looks for them. Python 3.11 and the
  packages come from a private uv (pinned version) kept in `<ONCUE_DIR>/.tools`: no
  shell profile or PATH change, and removing the OnCue folder removes everything.
  `scripts/install.sh --prebuilt` then verifies the programs, downloads the speech
  model with curl and builds the app. An existing git checkout keeps the build route;
  a release without prebuilt programs, or a failed download, falls back to it with
  one line. `ONCUE_ROUTE=release|build` forces a route. A re-run with the same tag
  downloads nothing; a newer tag unpacks next to the install, carries over `.env`,
  `data/` and `.tools` and keeps the old version as `<ONCUE_DIR>.previous`. A
  checksum mismatch stops the install and changes nothing.
- Record-and-transcribe is now the default OnCue launch choice. The launcher
  dialog offers "Opnemen en uitschrijven" (default) and "Live coach". After Stop
  the recorder page (`scripts/record_server.py`) transcribes the session in the
  background with the backend `.env` selects (whisper.cpp by default) and writes
  `transcript.md` into the session folder with a header and `[mm:ss] Jij:` /
  `[mm:ss] Ander:` lines (`You` / `Other` when `LANGUAGE=en`). Transcriptions
  queue and run one at a time, a failure is logged to `recorder.log` and shown
  on the page, and the page offers "Open transcript" and, on macOS, "Toon in
  Finder". Every connected page now sees the live levels and state, so
  refreshing during a recording no longer freezes the meters and the timer.
- One-line Mac install: paste `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Vinix24/OnCue/main/scripts/bootstrap.sh)"`
  in Terminal. `scripts/bootstrap.sh` checks the Mac (Apple Silicon, macOS 14.2 or
  newer), installs Apple's command line tools and Homebrew when missing, downloads
  OnCue into `~/OnCue` (or `ONCUE_DIR`), runs `scripts/install.sh` and logs
  everything to `~/Library/Logs/OnCue/install-<timestamp>.log`. Re-running updates
  the checkout with a fast-forward pull and leaves `.env` and `data/` alone.
  `--dry-run` prints every step and changes nothing.
- The installer ends with an installed app: `scripts/install.sh` builds the launcher
  and copies it to `~/Applications/OnCue.app`, and records the checkout in
  `~/.oncue/repo-path`. It also installs `cmake` (needed for the whisper.cpp
  transcriber), prints `[n/9]` step counters with the expected time and size per long
  step, keeps compiler and download output in the log, and closes with a status block
  for transcriber, audio capture and app.
- `scripts/launcher/icon/sales-copilot.icns` is now tracked, so a fresh clone builds
  the app with the OnCue icon without Pillow. `build_app.sh` falls back to the default
  applet icon with a warning when the file is missing.
- Release build of the native Mac programs. `scripts/build_macos_binaries.sh <out_dir>`
  builds `whisper-cli`, `whisper-server` (whisper.cpp, static, Metal embedded, not tuned
  to the build machine's CPU) and `audiotee` for Apple Silicon, checks that they link
  only system libraries, are signed and transcribe `jfk.wav`, and packs them as
  `oncue-macos-arm64.tar.gz` with a `.sha256`. The `release-macos-binaries` workflow runs
  it on a GitHub-hosted runner and attaches both files to each published release.
  Installing from these files needs no compiler on the user's Mac.
- Runtime auto-arm picks the prospect audio source from its trigger. A phone or
  FaceTime call starts the session on the phone-call tap (`audiotee_call`), so a
  call placed from the Mac via the iPhone is captured without editing
  `PROSPECT_SOURCE`. A video call keeps the configured source. Without the
  `audio.calltap` license the session still starts on the configured source and
  logs one warning.
- Microphone lines that are loudspeaker echo of the other side are dropped from
  the report and from the after-the-call transcript. Without a headset the
  microphone picks up the speakers, so every sentence of the other side showed
  up twice and inflated your talk time. Only microphone lines are ever
  removed. Set `TRANSCRIPT_ECHO_FILTER` to turn it off; it is on by default
  (#319).
- `scripts/eval_diarization.py` compares speaker diarization backends on your
  own recordings. It is an evaluation tool only; the product does not change
  (#320).
- `scripts/post_process_call.py` can separate speakers per person without a
  Hugging Face token, with sherpa-onnx. `--backend auto` (the default) picks
  sherpa when `--num-speakers` is 2 or less, and pyannote for a group or an
  unknown count when a token is available. Without a token it uses sherpa and
  logs a warning. `sherpa-onnx` is now part of the `diarization` extra (#323).

### Fixed

- The MCP bridge no longer hands the full transcript to the host on every turn
  when the host restarts the bridge per turn. The hub replays its transcript
  buffer to each new bridge, and the bridge stamped every replayed line with its
  connection time, so `since_ms` filtered nothing. The hub now stamps
  `timestamp_ms` when it buffers a line, so replayed lines keep their original
  time. Detection channels are not replayed and are unaffected.
- `scripts/post_process_call.py` no longer crashes with an AttributeError on a
  WAV without `--transcript-json`. The default whisper.cpp backend now returns
  real per-segment timestamps, so speakers can be assigned per segment. A
  backend that cannot give timestamped segments, or a transcription that
  fails or returns nothing, stops the script with an error instead of
  labeling the whole call as one speaker.
- The live pyannote diarizer loads again on pyannote.audio 4. Before, a
  TypeError was swallowed and live diarization stayed off without notice. A
  rejected token, a missing extra and other load failures now log distinct
  messages (#321).
- macOS: when the system-sound tap (AudioTee or BlackHole) drops out mid-call,
  the system track is filled with silence in real time and the tap is restarted
  or reopened with backoff. The system track stays on the call's timeline like
  the microphone (#316) and the Windows loopback (#317) (#322).
- Microphone: a pause of more than about 1 second (for example Bluetooth
  earbuds switching to the headset profile in a call) is filled with silence,
  so the track stays on the call's timeline. When the input device disappears
  (headset off, USB pulled), OnCue notices after about 2 seconds, fills silence
  in real time and reopens the configured microphone, else the default input.
  On Windows it switches back to the original once that is present again;
  on macOS it stays on the fallback (#316).
- Windows: when the output device vanishes mid-call, the system sound track no
  longer stops. It fills silence in real time, retries every second on the
  current default output (or the pinned endpoint once it is back) and warns once
  per outage (#317).
- Recorder: at stop, every track that received audio is padded with silence to
  the call length, so both WAV files end equally long. The padding is recorded
  per track as `end_padding_ms` in `metadata.json`. A track without any audio
  is not padded, so an empty recording still shows the warning (#315).

### Changed

- Docs: the licensing section of `docs/DIARIZATION.md` is corrected. The
  pyannote models are MIT or CC-BY-4.0, not non-commercial, and CC-BY-4.0
  requires attribution. `THIRD_PARTY_LICENSES.md` lists the models downloaded at
  runtime (#323).
- Docs: Windows is now described as supported instead of experimental
  (`INSTALL.md`, `docs/FAQ.md`, `docs/WINDOWS_TESTPLAN.md`, `KNOWN_ISSUES.md`).
  The README and `docs/DIARIZATION.md` now say what speaker diarization does:
  the live route emits a `speaker_segments` event only, and per-person labels
  in a transcript come from `scripts/post_process_call.py`.

### Security

- pyasn1 0.6.4 to 0.6.5 (CVE-2026-119194, CVE-2026-119195, CVE-2026-119196,
  CVE-2026-119197, fixed in 0.6.5). pyasn1 is a transitive dependency of
  google-auth via pyasn1-modules; the `gemini` extra now requires
  `pyasn1>=0.6.5`.
- tornado 6.5.10 to 6.6 (CVE-2026-116577, CVE-2026-115927, CVE-2026-116578,
  fixed in 6.6). tornado is a transitive dependency of semantic-router and
  aurelio-sdk; the `detector` extra now requires `tornado>=6.6`.
- msgpack 1.2.1 to 1.2.4 (CVE-2026-114613). msgpack is a transitive
  dependency of cachecontrol (pip-audit) and librosa (parakeet extra).
- litellm 1.88.6 to 1.103.9 (GHSA-g5ff-637f-6q2m, fixed in 1.95.0). The `llm`
  extra now requires `litellm>=1.95.0`. litellm 1.104 and later cap filelock
  below 4.0, which would reopen CVE-2026-112940, CVE-2026-112913 and
  CVE-2026-112907, so `[tool.uv]` carries a `filelock>=4.2.1` floor and the lock
  stays on 1.103.9. The new litellm pulls in boto3, botocore, jmespath,
  s3transfer and pydantic-settings.

## [1.0.0-rc.5] — 2026-10-27

Fifth pre-release. Dependency security updates, a privacy fix for the
presentation (no more third-party requests for scripts and fonts) and two
documentation corrections.

### Security

- The presentation no longer loads Reveal.js from jsDelivr or the Inter font
  from Google Fonts. Both ship locally, and the Content Security Policy allows
  only `'self'` for scripts, styles and fonts (#307).
- filelock 3.29.3 to 4.2.4 (CVE-2026-112940, CVE-2026-112913, CVE-2026-112907)
  and setuptools 81 to 84.2.1 (GHSA-xmj3-9gf7-h52w). torch moves from 2.12.0 to
  2.14.1 because torch 2.12 capped setuptools below 82. The CI venv floor is
  now `setuptools>=84.1.0` (#305).

### Changed

- The README demo description no longer mentions case slides.

### Fixed

- Docs corrected: an unset `LLM_PROVIDER` defaults to `none` in the code, not
  `openrouter` (`docs/PRIVACY.md`, `docs/INTEGRATIONS.md`, `docs/MODULE3.md`)
  (#306).

## [1.0.0-rc.4] — 2026-10-23

Fourth pre-release. OnCue now has a second entry route: a local notetaker that
transcribes both sides of a conversation and writes a report afterwards, with
no live coaching and no LLM by default. Every report gets a readable transcript next to it. Windows
has a one-command installer, still experimental. Privacy defaults moved into the
code: without a `.env`, no LLM provider is configured, and the per-conversation
ceiling now also covers the transcription backend.

### Added

- The `notetaker` preset and a notetaker route in the README: it transcribes
  both sides of a conversation locally (system audio and microphone) and
  produces a report with the transcript afterwards. No live coaching, no slides
  and no LLM by default. Your own microphone is also transcribed live while the
  call runs (#292).
- `scripts/install.ps1`, a one-command Windows installer with a `notetaker` and
  a `full` profile, a preflight that changes nothing (`-PreflightOnly`) and an
  `-Uninstall` that removes only what it recorded. Experimental until
  `docs/WINDOWS_TESTPLAN.md` has passed on a clean machine (#293).
- A readable `<prefix>_transcript.md` next to every report in `data/reports/`,
  with speaker labels from the call preset or neutral ones by language. It
  follows `REPORT_REDACT_PII`, and the retention sweep and `purge_session.py`
  remove it with the report (#297).
- `constraints.txt`, derived from `uv.lock`. CI and both installers install
  against it, so a lockless install lands on the tested versions. End users do
  not need `uv` (#301).

### Changed

- A missing optional module (a detector or reports dependency) now shows a
  banner in the dashboard with the install hint, and transcription keeps
  running. Before, the process exited silently with code 0. An unexpected
  startup error now exits with code 1 and a message on stderr (#296).
- Every LLM provider defaults to `none` in the code, not only in
  `.env.example`. Without a `.env`, nothing is sent to a provider (#295).
- Measurement and test tooling, plus the VNX sync, changed without effect on
  users (#285, #288, #289, #290, #298, #299).

### Fixed

- An empty provider or model in the start-call is no override, so
  `LLM_PROVIDER` from `.env` stays in force (#300).
- The report prompt is neutral for the notetaker preset and for any call
  without a sales preset. The vocabulary and normalization files resolve
  through the app's path resolution instead of the working directory (#300).

### Security

- The per-conversation privacy ceiling now covers the transcription backend. At
  start-call, `WHISPER_BACKEND=groq`, whisper.cpp on a non-loopback
  `WHISPER_CPP_SERVER_HOST` and an unknown backend name count as `public` and are
  refused (HTTP 400, lane `transcriptie`) for a client with a `local` or `tenant`
  ceiling. mlx-whisper, parakeet and whisper.cpp on loopback count as `local`
  (#286).
- pillow 13.0.0 (8 CVEs) and certifi 2026.10.19 (PYSEC-2026-4320), with floors in
  `pyproject.toml` (#287).
- opentelemetry 1.45.1 (CVE-2026-114603), with a floor in `pyproject.toml`
  (#291).

### Docs

- The privacy docs match the code: AWS Bedrock is not a provider, reports live in
  `data/reports/`, and the notetaker preset is described (#294).
- A non-loopback `WHISPER_CPP_SERVER_HOST` is documented as what it is:
  whisper-server started on this machine and bound to that address, an open
  unencrypted listener on a network interface. It never sent audio to another
  machine (#286).

## [1.0.0-rc.3] — 2026-10-20

Small pre-release. New installations stopped working when semantic-router 1.0
came out, because nothing capped the dependency: every install without the
lock failed, including `scripts/install.sh`. This release caps it, bumps four
dependencies with known CVEs, and corrects the privacy docs so they match the
two opt-in routes where audio leaves the machine.

### Security

- aiohttp 3.14.5, fsspec 2026.9.0, mcp 2.3.0 and multidict 7.1.0 (#281).

### Fixed

- `semantic-router` is capped at `>=0.1,<1.0`. Version 1.0 broke every
  installation that did not use the lock, `scripts/install.sh` included
  (#281).
- Two tests no longer depend on the machine they run on (#281).

### Docs

- The privacy docs describe the two opt-in routes where audio leaves the
  machine: `WHISPER_BACKEND=groq`, and a remote `WHISPER_CPP_SERVER_HOST`
  over unencrypted HTTP. The per-client ceiling covers LLM tasks only, so it
  does not apply to these routes (#282).
- `scripts/measure_entities_per_model.py`, a measurement harness that scores
  ASR variants on entity recognition after the normalization layer (#283).

## [1.0.0-rc.2] — 2026-10-02

Second pre-release. No LLM is configured by default anymore: a fresh install
runs the live cues fully local, and every cloud provider is an explicit
choice. On top of that, each LLM task can now have its own provider, model and
timeout under a privacy ceiling per conversation, the post-call report is
enriched with a summary, action items and contextual term corrections, and
the privacy docs list per task which text leaves the machine.

### Added

- One LLM route per task. `detector_confirm`, `window_classifier`, `phase`,
  `suggestions`, `summary`, `script_tracking`, `slides` and `report` each read
  `<TASK>_LLM_PROVIDER`, `_MODEL` and `_TIMEOUT_MS`, then a per-task default,
  then the global `LLM_*` values. The privacy level of a client folder is a
  ceiling: a task whose route is above it makes no call at all, both when the
  route is resolved and on every request. The start-call check covers every
  active task (#263).
- Post-call report enrichment: one structured call over the whole transcript
  adds a short summary, an overview, keywords, action items and a flag for
  whether a conversation took place. It fails open to an unenriched report,
  and the local report is written first, atomically and with mode 0600 (#267).
- Calls that were not conversations (silence, a voicemail, only recognizer
  hallucinations) are marked as junk with a reason in the dashboard, and their
  delivery to a webhook or CRM is skipped. The local copy and the client
  archive stay (#269).
- Contextual term correction in the report: a prefilter finds candidate
  segments for a term list, the model may pick a term from that list and
  nothing else, and the report shows the original next to the correction. The
  transcript itself is unchanged. The list is the fixed list, the `termen` of
  the client's `klant.yaml` and an optional `termenlijst.yaml` in the client
  folder (up to 5000 entries). Free-form correction is not built because it
  failed the measurement for every model tried (#274).
- The same client terms now also reach the live transcriber for the duration
  of a call. They take precedence over the fixed list, an exact term is never
  fuzzed into another one, and `SRC -> DST` entries act as exact variants.
  Original text and replacements stay in the local debug log only (#261).
- `report_terms` as its own task (`REPORT_TERMS_LLM_*`), so the term
  correction can run on a different model than the summary. Without its own
  values it inherits from `report` and stays one call (#276).
- `GET /api/reports/{session_id}` behind the dashboard token. The dashboard
  shows a progress line after the report is ready, then the summary, keywords,
  action items and term corrections. The Markdown export carries the enriched
  fields (#276).
- Output limits per task through `<TASK>_LLM_MAX_OUTPUT_TOKENS`, so a live task
  cannot run past its budget (#276).
- Pro/Enterprise, not part of the open-source build: the same routing
  applies to the deep-insight lane.

### Changed

- No LLM is configured by default. `.env.example` ships `LLM_PROVIDER=none` with an
  empty `LLM_MODEL`, and the live cues (detection, coaching, phase) run local
  without one. Summaries, suggestions, slides and report enrichment need a
  provider you choose (#277).
- The privacy docs now list per task what text goes to a configured LLM, which
  speaker lines are included, and where the PII filter stops. README, INSTALL,
  the architecture and module docs no longer name a default cloud provider. The
  central audit hash is described as pseudonymous, and the opt-in telemetry
  signals are listed (#277).
- The report timeout is 300 seconds. The report runs after the call and a
  one-hour call took 54 to 77 seconds on the models measured, so the old 60
  seconds cut slower models off (#276).
- `.env.example` recommends models for the report parts with the measured
  numbers behind them. The report parts have no model default of their own:
  without a value they follow the conversation's provider and model (#276).
- Microphone silence warnings now name microphone causes: a muted microphone,
  the wrong input device and microphone permission. They used to repeat the
  call-audio explanation meant for the prospect side (#264).
- `sanitize_for_outbound` takes the resolved provider, so the PII mode follows
  the route of each task and the own-tenant trust setting (#263).

### Fixed

- The whisper server no longer outlives the app. It starts behind a guard that
  stops it when the parent dies, also on SIGKILL, on macOS, Linux and Windows.
  Servers left over from an earlier crash (same binary, dead parent) are
  cleaned up before the next start (#266).
- Forced tool choice: when a model rejects it, the structured-output client
  falls back instead of failing the call (#273).

### Security

- urllib3 2.7.0 to 2.8.0 (CVE-2026-97687, CVE-2026-97689) (#268).
- litellm 1.88.1 to 1.88.6 (CVE-2026-84377), with the floor raised in the
  `llm` extra (#273).
- sentence-transformers 5.5.1 to 5.6.0 (CVE-2026-68770), with the floor raised
  (#273).
- tornado 6.5.8 to 6.5.10 (GHSA-chx6-46f5-w4vp, GHSA-c2m8-h5v5-343r,
  GHSA-3hv7-mjh2-fv65) (#273).

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
