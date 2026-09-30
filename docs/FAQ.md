# FAQ — Solve problems yourself (macOS)

This document is self-serve. For each problem you get a concrete path to recovery without a support ticket.

Order of operations:
1. This FAQ
2. [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
3. [SETUP_EXPECTATIONS.md](SETUP_EXPECTATIONS.md)
4. GitHub Discussions
5. Email: `info@vincentvandeth.nl`

## Category 1 — Installation & first start

<a id="faq-het-installatiescript-faalt-op-blackhole-unidentified-developer"></a>
### Q: The installation script fails on BlackHole ("unidentified developer")

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — BlackHole does not appear in Audio MIDI Setup](TROUBLESHOOTING.md#problem-blackhole-verschijnt-niet-in-audio-midi-setup).

<a id="faq-homebrew-commando-vraagt-om-password-is-dat-veilig"></a>
### Q: Homebrew command asks for a password (is that safe?)

**What you see:** You are experiencing: **Homebrew command asks for a password (is that safe?)**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** This is usually a combination of macOS security, dependency installation, or permissions on your machine.
**Fix it yourself:**
1. Restart from the project root with `bash scripts/first-run.sh` and read the first real error line.
2. Fix the specific dependency/permission problem and then run exactly the same step again.
3. Verify it works with:
```bash
bash scripts/first-run.sh --dry-run
```
**When this does NOT work:** Look at [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md). If it's a managed work Mac: email `info@vincentvandeth.nl`.

<a id="faq-python-3-11-not-found-tijdens-setup"></a>
### Q: "Python 3.11 not found" during setup

**What you see:** You are experiencing: **"Python 3.11 not found" during setup**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** This is usually a combination of macOS security, dependency installation, or permissions on your machine.
**Fix it yourself:**
1. Restart from the project root with `bash scripts/first-run.sh` and read the first real error line.
2. Fix the specific dependency/permission problem and then run exactly the same step again.
3. Verify it works with:
```bash
bash scripts/first-run.sh --dry-run
```
**When this does NOT work:** Look at [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md). If it's a managed work Mac: email `info@vincentvandeth.nl`.

<a id="faq-script-hangt-bij-downloading-whisper-model"></a>
### Q: Script hangs at "Downloading Whisper model"

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — Whisper model download stalls or fails](TROUBLESHOOTING.md#problem-whisper-model-download-stalls-or-fails).

<a id="faq-na-installatie-kan-ik-geen-geluid-meer-horen-van-mijn-mac"></a>
### Q: After installation I can no longer hear sound from my Mac

**What you see:** You are experiencing: **After installation I can no longer hear sound from my Mac**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** This is usually a combination of macOS security, dependency installation, or permissions on your machine.
**Fix it yourself:**
1. Restart from the project root with `bash scripts/first-run.sh` and read the first real error line.
2. Fix the specific dependency/permission problem and then run exactly the same step again.
3. Verify it works with:
```bash
bash scripts/first-run.sh --dry-run
```
**When this does NOT work:** Look at [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md). If it's a managed work Mac: email `info@vincentvandeth.nl`.

<a id="faq-mijn-werk-mac-weigert-de-installatie-corporate-mdm"></a>
### Q: My work Mac refuses the installation (corporate MDM)

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — Corporate Mac blocks installation](TROUBLESHOOTING.md#problem-corporate-mac-blocks-installation).

## Category 2 — Audio problems

By default, OnCue taps the entire system output mix via AudioTee: a Core Audio process-tap over the full output (`tap_all`). You don't have to select a meeting app and no audio routing is needed — it doesn't matter which or how many apps are open. BlackHole/Multi-Output ("OnCue Output") is a manual fallback you can choose yourself; it does not kick in automatically. The steps below that refer to "OnCue Output" are about that manual fallback.

<a id="faq-bluetooth-headset-en-telefoongesprek-geen-transcript"></a>
### Q: Bluetooth headset + iPhone relay call produces no prospect transcript

**What you see:** You call via iPhone Continuity or FaceTime audio, OnCue is running, but the caller's transcript stays empty.
**Why this happens:** BlackHole cannot capture telephony audio. Caller audio flows through `avconferenced` (the macOS telephony daemon) and never reaches the system output device. This is a known macOS limitation, not a configuration error.
**Fix it yourself:**
1. Set `PROSPECT_SOURCE=audiotee_call` in `.env`.
2. Restart the backend with `bash scripts/stop.sh && bash scripts/start.sh`.
3. Verify with:
```bash
grep -i "calltap\|audiotee_call\|avconferenced" data/logs/copilot.log | tail -20
```
**When this does NOT work:** If `avconferenced` is not found, is iPhone Continuity actually calling actively (not WiFi-call emulation)? A Bluetooth headset is not an obstacle in this mode.

<a id="faq-mijn-microfoon-wordt-niet-gedetecteerd-door-sales-copilot"></a>
### Q: My microphone is not detected by OnCue

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — Microphone permission denied for Terminal/Python](TROUBLESHOOTING.md#problem-microphone-permission-denied-voor-terminalpython).

<a id="faq-ik-hoor-mijn-eigen-stem-dubbel-echo-tijdens-de-call"></a>
### Q: I hear my own voice doubled / echo during the call

**What you see:** You are experiencing: **I hear my own voice doubled / echo during the call**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Audio routing on macOS is strict: input, output, and permissions all three have to be right.
**Fix it yourself:**
1. AudioTee taps the entire system output mix by default (`tap_all`) — no meeting-app selection or audio routing needed; do set your input to your real microphone in macOS Sound. If you use the BlackHole route manually, set output to `OnCue Output`.
2. Run a full audio check and fix the first FAIL you see first.
3. Verify it works with:
```bash
python scripts/verify_audio.py
```
**When this does NOT work:** If routing keeps going wrong: follow the audio section in [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

<a id="faq-ik-hoor-de-prospect-niet-door-mijn-koptelefoon"></a>
### Q: I can't hear the prospect through my headphones

**What you see:** You are experiencing: **I can't hear the prospect through my headphones**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Audio routing on macOS is strict: input, output, and permissions all three have to be right.
**Fix it yourself:**
1. AudioTee taps the entire system output mix by default (`tap_all`) — no meeting-app selection or audio routing needed; do set your input to your real microphone in macOS Sound. If you use the BlackHole route manually, set output to `OnCue Output`.
2. Run a full audio check and fix the first FAIL you see first.
3. Verify it works with:
```bash
python scripts/verify_audio.py
```
**When this does NOT work:** If routing keeps going wrong: follow the audio section in [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

<a id="faq-mijn-externe-usb-microfoon-werkt-niet-airpods-jabra-shure-mv7-etc"></a>
### Q: My external USB microphone doesn't work (AirPods, Jabra, Shure MV7, etc.)

**What you see:** You are experiencing: **My external USB microphone doesn't work (AirPods, Jabra, Shure MV7, etc.)**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Audio routing on macOS is strict: input, output, and permissions all three have to be right.
**Fix it yourself:**
1. AudioTee taps the entire system output mix by default (`tap_all`) — no meeting-app selection or audio routing needed; do set your input to your real microphone in macOS Sound. If you use the BlackHole route manually, set output to `OnCue Output`.
2. Run a full audio check and fix the first FAIL you see first.
3. Verify it works with:
```bash
python scripts/verify_audio.py
```
**When this does NOT work:** If routing keeps going wrong: follow the audio section in [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

<a id="faq-bluetooth-koptelefoon-werkt-eerst-wel-dan-niet-meer"></a>
### Q: Bluetooth headphones work at first, then stop working

**What you see:** You are experiencing: **Bluetooth headphones work at first, then stop working**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Audio routing on macOS is strict: input, output, and permissions all three have to be right.
**Fix it yourself:**
1. AudioTee taps the entire system output mix by default (`tap_all`) — no meeting-app selection or audio routing needed; do set your input to your real microphone in macOS Sound. If you use the BlackHole route manually, set output to `OnCue Output`.
2. Run a full audio check and fix the first FAIL you see first.
3. Verify it works with:
```bash
python scripts/verify_audio.py
```
**When this does NOT work:** If routing keeps going wrong: follow the audio section in [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

<a id="faq-audio-crackles-slechte-kwaliteit-op-blackhole"></a>
### Q: Audio crackles / poor quality on BlackHole

**What you see:** You are experiencing: **Audio crackles / poor quality on BlackHole**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Audio routing on macOS is strict: input, output, and permissions all three have to be right.
**Fix it yourself:**
1. AudioTee taps the entire system output mix by default (`tap_all`) — no meeting-app selection or audio routing needed; do set your input to your real microphone in macOS Sound. If you use the BlackHole route manually, set output to `OnCue Output`.
2. Run a full audio check and fix the first FAIL you see first.
3. Verify it works with:
```bash
python scripts/verify_audio.py
```
**When this does NOT work:** If routing keeps going wrong: follow the audio section in [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

<a id="faq-youtube-spotify-geluid-komt-niet-door-blackhole"></a>
### Q: YouTube/Spotify sound doesn't come through BlackHole

**What you see:** You are experiencing: **YouTube/Spotify sound doesn't come through BlackHole**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Audio routing on macOS is strict: input, output, and permissions all three have to be right.
**Fix it yourself:**
1. AudioTee taps the entire system output mix by default (`tap_all`) — no meeting-app selection or audio routing needed; do set your input to your real microphone in macOS Sound. If you use the BlackHole route manually, set output to `OnCue Output`.
2. Run a full audio check and fix the first FAIL you see first.
3. Verify it works with:
```bash
python scripts/verify_audio.py
```
**When this does NOT work:** If routing keeps going wrong: follow the audio section in [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

<a id="faq-welk-multi-output-device-moet-ik-kiezen-in-audio-midi-setup"></a>
### Q: Which multi-output device should I choose in Audio MIDI Setup?

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — Multi-Output Device creation fails (Core Audio error)](TROUBLESHOOTING.md#problem-multi-output-device-creation-fails-core-audio-error).

<a id="faq-wat-als-ik-headset-met-microfoon-en-speakers-in-een-device-heb"></a>
### Q: What if I have a headset with microphone and speakers in one device?

**What you see:** You are experiencing: **What if I have a headset with microphone and speakers in one device?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Audio routing on macOS is strict: input, output, and permissions all three have to be right.
**Fix it yourself:**
1. AudioTee taps the entire system output mix by default (`tap_all`) — no meeting-app selection or audio routing needed; do set your input to your real microphone in macOS Sound. If you use the BlackHole route manually, set output to `OnCue Output`.
2. Run a full audio check and fix the first FAIL you see first.
3. Verify it works with:
```bash
python scripts/verify_audio.py
```
**When this does NOT work:** If routing keeps going wrong: follow the audio section in [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

<a id="faq-hoe-test-ik-of-audio-correct-gerouteerd-is-voor-een-echte-call"></a>
### Q: How do I test whether audio is routed correctly BEFORE a real call?

**What you see:** You are experiencing: **How do I test whether audio is routed correctly BEFORE a real call?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Audio routing on macOS is strict: input, output, and permissions all three have to be right.
**Fix it yourself:**
1. AudioTee taps the entire system output mix by default (`tap_all`) — no meeting-app selection or audio routing needed; do set your input to your real microphone in macOS Sound. If you use the BlackHole route manually, set output to `OnCue Output`.
2. Run a full audio check and fix the first FAIL you see first.
3. Verify it works with:
```bash
python scripts/verify_audio.py
```
**When this does NOT work:** If routing keeps going wrong: follow the audio section in [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Category 3 — Dashboard & UI

<a id="faq-dashboard-opent-niet-via-file-dubbel-klik"></a>
### Q: Dashboard doesn't open when I double-click the HTML file (file://)

**What you see:** You open `dashboard/index.html` directly via Finder or the browser's open-file, and the dashboard shows a CORS error or doesn't work.
**Why this happens:** The backend only allows CORS for `http://localhost:8760`. Access via `file://` is deliberately blocked (security fix F02) — otherwise any local HTML page could drive the hub.
**Solution:**
1. Make sure the backend is running (`bash scripts/start.sh`).
2. Open the dashboard via: `http://localhost:8760/dashboard`
3. Save this address as a browser bookmark.
**When this does NOT work:** If the backend is running but `http://localhost:8760/dashboard` gives a 404, check `WS_HUB_PORT` in `.env`.



<a id="faq-dashboard-toont-verbonden-met-backend-maar-er-gebeurt-niks"></a>
### Q: Dashboard shows "Connected to backend" but nothing happens

**What you see:** You are experiencing: **Dashboard shows "Connected to backend" but nothing happens**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** The UI depends on realtime events from backend modules; if one link drops out, the dashboard appears silent.
**Fix it yourself:**
1. Do a clean restart with `bash scripts/stop.sh && bash scripts/start.sh`.
2. Then start the call again from setup (not via browser back/forward).
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'start_call|transcrib|talk|pain|websocket'
```
**When this does NOT work:** Does this keep happening on main? Add a log + timestamp in GitHub Discussions or email `info@vincentvandeth.nl`.

<a id="faq-start-call-knop-geeft-een-foutmelding"></a>
### Q: "Start Call" button gives an error

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — Dashboard "Start Call failed"](TROUBLESHOOTING.md#problem-dashboard-start-call-mislukt).

<a id="faq-talk-time-balk-beweegt-niet-tijdens-de-call"></a>
### Q: Talk-time bar doesn't move during the call

**What you see:** You are experiencing: **Talk-time bar doesn't move during the call**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** The UI depends on realtime events from backend modules; if one link drops out, the dashboard appears silent.
**Fix it yourself:**
1. Do a clean restart with `bash scripts/stop.sh && bash scripts/start.sh`.
2. Then start the call again from setup (not via browser back/forward).
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'start_call|transcrib|talk|pain|websocket'
```
**When this does NOT work:** Does this keep happening on main? Add a log + timestamp in GitHub Discussions or email `info@vincentvandeth.nl`.

<a id="faq-timer-blijft-op-00-00-staan"></a>
### Q: Timer stays at 00:00

**What you see:** Talk-time percentages sometimes do move, but the timer stays at **00:00**.
**Why this happens:** This is a state-sync edge case from the operator E2E (2026-04-19): `start_call` and timer state fall out of sync.
**Fix it yourself:**
1. Do a clean restart with `bash scripts/stop.sh && bash scripts/start.sh`.
2. Then start the call again from setup (not via browser back/forward).
3. Verify it works with:
```bash
tail -n 150 data/logs/copilot.log | rg -i 'start_call|call_start|talk-time'
```
**When this does NOT work:** Does this keep happening on main? Add a log + timestamp in GitHub Discussions or email `info@vincentvandeth.nl`.

<a id="faq-talk-time-timer-blijft-hangen-terwijl-audio-wel-loopt"></a>
### Q: Talk-time timer hangs while audio is running

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — Timer in the dashboard doesn't run while I do hear audio](TROUBLESHOOTING.md#problem-timer-in-dashboard-loopt-niet-terwijl-ik-wl-audio-hoor).

<a id="faq-transcriptie-verschijnt-niet-of-pas-na-30-seconden"></a>
### Q: Transcription doesn't appear (or only after 30 seconds)

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — No transcription appearing after 30s](TROUBLESHOOTING.md#problem-no-transcription-appearing-after-30s).

<a id="faq-browser-tab-freezet-tijdens-de-call"></a>
### Q: Browser tab freezes during the call

**What you see:** You are experiencing: **Browser tab freezes during the call**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** The UI depends on realtime events from backend modules; if one link drops out, the dashboard appears silent.
**Fix it yourself:**
1. Do a clean restart with `bash scripts/stop.sh && bash scripts/start.sh`.
2. Then start the call again from setup (not via browser back/forward).
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'start_call|transcrib|talk|pain|websocket'
```
**When this does NOT work:** Does this keep happening on main? Add a log + timestamp in GitHub Discussions or email `info@vincentvandeth.nl`.

<a id="faq-dashboard-verliest-verbinding-na-mac-uit-slaap-komt"></a>
### Q: Dashboard loses connection after Mac wakes from sleep

**What you see:** You are experiencing: **Dashboard loses connection after Mac wakes from sleep**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** The UI depends on realtime events from backend modules; if one link drops out, the dashboard appears silent.
**Fix it yourself:**
1. Do a clean restart with `bash scripts/stop.sh && bash scripts/start.sh`.
2. Then start the call again from setup (not via browser back/forward).
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'start_call|transcrib|talk|pain|websocket'
```
**When this does NOT work:** Does this keep happening on main? Add a log + timestamp in GitHub Discussions or email `info@vincentvandeth.nl`.

<a id="faq-pijnpunten-paneel-is-leeg-ondanks-dat-prospect-ze-noemt"></a>
### Q: Pain points panel is empty even though the prospect mentions them

**What you see:** You are experiencing: **Pain points panel is empty even though the prospect mentions them**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** The UI depends on realtime events from backend modules; if one link drops out, the dashboard appears silent.
**Fix it yourself:**
1. Do a clean restart with `bash scripts/stop.sh && bash scripts/start.sh`.
2. Then start the call again from setup (not via browser back/forward).
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'start_call|transcrib|talk|pain|websocket'
```
**When this does NOT work:** Does this keep happening on main? Add a log + timestamp in GitHub Discussions or email `info@vincentvandeth.nl`.

<a id="faq-slide-verschijnt-niet-ondanks-gedetecteerd-pijnpunt"></a>
### Q: The presentation panel does not update during the call

**What you see:** You are experiencing: **The presentation panel does not update during the call**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** The UI depends on realtime events from backend modules; if one link drops out, the dashboard appears silent.
**Fix it yourself:**
1. Do a clean restart with `bash scripts/stop.sh && bash scripts/start.sh`.
2. Then start the call again from setup (not via browser back/forward).
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'start_call|transcrib|talk|pain|websocket'
```
**When this does NOT work:** Does this keep happening on main? Add a log + timestamp in GitHub Discussions or email `info@vincentvandeth.nl`.

<a id="faq-end-call-gaat-terug-naar-setup-in-plaats-van-rapport-tonen"></a>
### Q: End Call returns to setup instead of showing the report

**What you see:** After End Call you return to setup instead of directly to the report view.
**Why this happens:** Historical UI-flow mismatch; the report is often written but not opened automatically.
**Fix it yourself:**
1. Do a clean restart with `bash scripts/stop.sh && bash scripts/start.sh`.
2. Then start the call again from setup (not via browser back/forward).
3. Verify it works with:
```bash
ls -lt data/reports | head -n 5
```
**When this does NOT work:** Does this keep happening on main? Add a log + timestamp in GitHub Discussions or email `info@vincentvandeth.nl`.

## Category 4 — Speaker recognition (YouTube test scenario)

<a id="faq-youtube-audio-wordt-getagged-als-jij-terwijl-ik-niet-praat"></a>
### Q: YouTube audio is tagged as "JIJ" while I'm not talking

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — Transcription labels YouTube audio as "JIJ"](TROUBLESHOOTING.md#problem-transcription-labels-youtube-audio-as-jij).

<a id="faq-prospect-en-ik-worden-omgedraaid-in-talk-time"></a>
### Q: Prospect and I are swapped in talk-time

**What you see:** You are experiencing: **Prospect and I are swapped in talk-time**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Speaker labeling becomes less reliable once the system falls into single-stream fallback or sources get mixed up.
**Fix it yourself:**
1. First check whether dual-stream is active (mic + system separated).
2. Restore audio routing and start a short 20-30 second test call.
3. Verify it works with:
```bash
tail -n 200 data/logs/copilot.log | rg -i 'single-stream|fallback|speaker|prospect|jij'
```
**When this does NOT work:** If labels keep flipping despite good routing: email `info@vincentvandeth.nl` with a log fragment.

<a id="faq-twee-sprekers-in-youtube-video-worden-als-een-gezien"></a>
### Q: Two speakers in a YouTube video are seen as one

**What you see:** You are experiencing: **Two speakers in a YouTube video are seen as one**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Speaker labeling becomes less reliable once the system falls into single-stream fallback or sources get mixed up.
**Fix it yourself:**
1. First check whether dual-stream is active (mic + system separated).
2. Restore audio routing and start a short 20-30 second test call.
3. Verify it works with:
```bash
tail -n 200 data/logs/copilot.log | rg -i 'single-stream|fallback|speaker|prospect|jij'
```
**When this does NOT work:** If labels keep flipping despite good routing: email `info@vincentvandeth.nl` with a log fragment.

<a id="faq-diarization-werkt-slecht-bij-vlaams-engels-accenten"></a>
### Q: Diarization works poorly with Flemish/English/accents

**What you see:** You are experiencing: **Diarization works poorly with Flemish/English/accents**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Speaker labeling becomes less reliable once the system falls into single-stream fallback or sources get mixed up.
**Fix it yourself:**
1. First check whether dual-stream is active (mic + system separated).
2. Restore audio routing and start a short 20-30 second test call.
3. Verify it works with:
```bash
tail -n 200 data/logs/copilot.log | rg -i 'single-stream|fallback|speaker|prospect|jij'
```
**When this does NOT work:** If labels keep flipping despite good routing: email `info@vincentvandeth.nl` with a log fragment.

<a id="faq-hoe-test-ik-of-single-stream-fallback-actief-is"></a>
### Q: How do I test whether single-stream fallback is active?

**What you see:** You are experiencing: **How do I test whether single-stream fallback is active?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Speaker labeling becomes less reliable once the system falls into single-stream fallback or sources get mixed up.
**Fix it yourself:**
1. First check whether dual-stream is active (mic + system separated).
2. Restore audio routing and start a short 20-30 second test call.
3. Verify it works with:
```bash
tail -n 200 data/logs/copilot.log | rg -i 'single-stream|fallback|speaker|prospect|jij'
```
**When this does NOT work:** If labels keep flipping despite good routing: email `info@vincentvandeth.nl` with a log fragment.

## Category 5 — Whisper & transcription

<a id="faq-mlx-whisper-levert-geen-transcripten-tijdens-call"></a>
### Q: mlx-whisper backend is running but no transcripts appear (call active, audio OK)

**What you see:** The call is running, audio indicators show signal, but the transcript panel stays empty. The logs show no errors.
**Why this happens:** The mlx-whisper live path has a known issue where the backend silently delivers no segments on certain macOS/MLX version combinations. This is an open item.
**Fix it yourself:**
1. Switch to whisper.cpp: set `WHISPER_BACKEND=whisper.cpp` in `.env`.
2. Restart the backend.
3. Verify with:
```bash
grep -i "whisper\|transcrib\|segment\|backend" data/logs/copilot.log | tail -30
```
**When this does NOT work:** If whisper.cpp also delivers no transcripts, the cause is audio (empty stream) rather than the backend. Follow the audio steps in Category 2.

<a id="faq-whisper-warmup-duurt-te-lang-30-seconden"></a>
### Q: Whisper warmup takes too long (30 seconds+)

**What you see:** You are experiencing: **Whisper warmup takes too long (30 seconds+)**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Whisper behavior depends directly on model choice, warmup state, and the quality of incoming audio.
**Fix it yourself:**
1. Warm up the pipeline before your real call (short test sentence + system audio).
2. Check the model choice and temporarily switch to `large-v3-turbo` as a baseline.
3. Verify it works with:
```bash
tail -n 150 data/logs/copilot.log | rg -i 'whisper|warmup|transcrib|model|chunk'
```
**When this does NOT work:** If transcription stays structurally poor: follow [docs/FINETUNING_GUIDE.md](FINETUNING_GUIDE.md).

<a id="faq-transcriptie-mist-de-eerste-zin-van-de-call"></a>
### Q: Transcription misses the first sentence of the call

**What you see:** The very first sentence is missing, after that transcription runs fine.
**Why this happens:** Cold-start/warmup was still in progress when the call already started.
**Fix it yourself:**
1. Warm up the pipeline before your real call (short test sentence + system audio).
2. Check the model choice and temporarily switch to `large-v3-turbo` as a baseline.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'warmup|first|transcrib'
```
**When this does NOT work:** If transcription stays structurally poor: follow [docs/FINETUNING_GUIDE.md](FINETUNING_GUIDE.md).

<a id="faq-transcriptie-bevat-hallucinaties-uitgevonden-zinnen-tijdens-stilte"></a>
### Q: Transcription contains "hallucinations" (invented sentences during silence)

**What you see:** You are experiencing: **Transcription contains "hallucinations" (invented sentences during silence)**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Whisper behavior depends directly on model choice, warmup state, and the quality of incoming audio.
**Fix it yourself:**
1. Warm up the pipeline before your real call (short test sentence + system audio).
2. Check the model choice and temporarily switch to `large-v3-turbo` as a baseline.
3. Verify it works with:
```bash
tail -n 150 data/logs/copilot.log | rg -i 'whisper|warmup|transcrib|model|chunk'
```
**When this does NOT work:** If transcription stays structurally poor: follow [docs/FINETUNING_GUIDE.md](FINETUNING_GUIDE.md).

<a id="faq-welk-whisper-model-moet-ik-kiezen-base-medium-large-v3-turbo"></a>
### Q: Which Whisper model should I choose: base / medium / large-v3 / turbo?

**What you see:** You are experiencing: **Which Whisper model should I choose: base / medium / large-v3 / turbo?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Whisper behavior depends directly on model choice, warmup state, and the quality of incoming audio.
**Fix it yourself:**
1. `large-v3-turbo` is the default on Apple Silicon — leave it unless you have a reason to switch.
2. Only go to `large-v3` if quality matters more than latency (heavier model, ~3 GB RAM, ~30s warmup).
3. Verify it works with:
```bash
tail -n 150 data/logs/copilot.log | rg -i 'whisper|warmup|transcrib|model|chunk'
```
**When this does NOT work:** If transcription stays structurally poor: follow [docs/FINETUNING_GUIDE.md](FINETUNING_GUIDE.md).

<a id="faq-hoe-switch-ik-van-mlx-whisper-naar-whisper-cpp-of-andersom"></a>
### Q: How do I switch from mlx-whisper to whisper.cpp (or vice versa)?

**What you see:** You are experiencing: **How do I switch from mlx-whisper to whisper.cpp (or vice versa)?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Whisper behavior depends directly on model choice, warmup state, and the quality of incoming audio.
**Fix it yourself:**
1. Warm up the pipeline before your real call (short test sentence + system audio).
2. Check the model choice and temporarily switch to `large-v3-turbo` as a baseline.
3. Verify it works with:
```bash
tail -n 150 data/logs/copilot.log | rg -i 'whisper|warmup|transcrib|model|chunk'
```
**When this does NOT work:** If transcription stays structurally poor: follow [docs/FINETUNING_GUIDE.md](FINETUNING_GUIDE.md).

<a id="faq-model-download-faalt-geen-internet-huggingface-rate-limit"></a>
### Q: Model download fails (no internet / HuggingFace rate limit)

**What you see:** You are experiencing: **Model download fails (no internet / HuggingFace rate limit)**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Whisper behavior depends directly on model choice, warmup state, and the quality of incoming audio.
**Fix it yourself:**
1. Warm up the pipeline before your real call (short test sentence + system audio).
2. Check the model choice and temporarily switch to `large-v3-turbo` as a baseline.
3. Verify it works with:
```bash
tail -n 150 data/logs/copilot.log | rg -i 'whisper|warmup|transcrib|model|chunk'
```
**When this does NOT work:** If transcription stays structurally poor: follow [docs/FINETUNING_GUIDE.md](FINETUNING_GUIDE.md).

<a id="faq-transcriptie-is-traag-lagging-achter-audio"></a>
### Q: Transcription is slow (lagging behind audio)

**What you see:** You are experiencing: **Transcription is slow (lagging behind audio)**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Whisper behavior depends directly on model choice, warmup state, and the quality of incoming audio.
**Fix it yourself:**
1. Warm up the pipeline before your real call (short test sentence + system audio).
2. Check the model choice and temporarily switch to `large-v3-turbo` as a baseline.
3. Verify it works with:
```bash
tail -n 150 data/logs/copilot.log | rg -i 'whisper|warmup|transcrib|model|chunk'
```
**When this does NOT work:** If transcription stays structurally poor: follow [docs/FINETUNING_GUIDE.md](FINETUNING_GUIDE.md).

<a id="faq-engels-wordt-getranscribeerd-terwijl-gesprek-in-nederlands-is"></a>
### Q: English is transcribed while the conversation is in Dutch

**What you see:** You are experiencing: **English is transcribed while the conversation is in Dutch**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Whisper behavior depends directly on model choice, warmup state, and the quality of incoming audio.
**Fix it yourself:**
1. Warm up the pipeline before your real call (short test sentence + system audio).
2. Check the model choice and temporarily switch to `large-v3-turbo` as a baseline.
3. Verify it works with:
```bash
tail -n 150 data/logs/copilot.log | rg -i 'whisper|warmup|transcrib|model|chunk'
```
**When this does NOT work:** If transcription stays structurally poor: follow [docs/FINETUNING_GUIDE.md](FINETUNING_GUIDE.md).

## Category 6 — LLM problems

<a id="faq-gemini-api-call-faalt-401-unauthorized"></a>
### Q: Gemini API call fails (401 unauthorized)

**What you see:** You are experiencing: **Gemini API call fails (401 unauthorized)**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** LLM errors usually come from a key/provider mismatch, latency, or a model choice that doesn't fit your account.
**Fix it yourself:**
1. Check the provider + model combination in setup and `.env`.
2. Test with a known working combination and then restart the backend.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'provider|model|llm|timeout|401|unauthorized'
```
**When this does NOT work:** If the API provider keeps failing: temporarily test Ollama locally or email `info@vincentvandeth.nl`.

<a id="faq-llm-detectie-is-traag-timeout-na-3s"></a>
### Q: LLM detection is slow / times out after 3s

**What you see:** You are experiencing: **LLM detection is slow / times out after 3s**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** LLM errors usually come from a key/provider mismatch, latency, or a model choice that doesn't fit your account.
**Fix it yourself:**
1. Check the provider + model combination in setup and `.env`.
2. Test with a known working combination and then restart the backend.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'provider|model|llm|timeout|401|unauthorized'
```
**When this does NOT work:** If the API provider keeps failing: temporarily test Ollama locally or email `info@vincentvandeth.nl`.

<a id="faq-ik-wil-lokale-llm-via-ollama-gebruiken-hoe"></a>
### Q: I want to use a local LLM via Ollama — how?

**What you see:** You are experiencing: **I want to use a local LLM via Ollama — how?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** LLM errors usually come from a key/provider mismatch, latency, or a model choice that doesn't fit your account.
**Fix it yourself:**
1. Check the provider + model combination in setup and `.env`.
2. Test with a known working combination and then restart the backend.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'provider|model|llm|timeout|401|unauthorized'
```
**When this does NOT work:** If the API provider keeps failing: temporarily test Ollama locally or email `info@vincentvandeth.nl`.

<a id="faq-gemma-qwen-llama-welke-werkt-het-best-voor-nederlands"></a>
### Q: Gemma / Qwen / Llama — which works best for Dutch?

**What you see:** You are experiencing: **Gemma / Qwen / Llama — which works best for Dutch?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** LLM errors usually come from a key/provider mismatch, latency, or a model choice that doesn't fit your account.
**Fix it yourself:**
1. Check the provider + model combination in setup and `.env`.
2. Test with a known working combination and then restart the backend.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'provider|model|llm|timeout|401|unauthorized'
```
**When this does NOT work:** If the API provider keeps failing: temporarily test Ollama locally or email `info@vincentvandeth.nl`.

<a id="faq-llm-kosten-lopen-op-hoe-monitor-ik-dit"></a>
### Q: LLM costs are rising — how do I monitor this?

**What you see:** You are experiencing: **LLM costs are rising — how do I monitor this?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** LLM errors usually come from a key/provider mismatch, latency, or a model choice that doesn't fit your account.
**Fix it yourself:**
1. Check the provider + model combination in setup and `.env`.
2. Test with a known working combination and then restart the backend.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'provider|model|llm|timeout|401|unauthorized'
```
**When this does NOT work:** If the API provider keeps failing: temporarily test Ollama locally or email `info@vincentvandeth.nl`.

<a id="faq-welke-modellen-uit-de-dropdown-zijn-beter-voor-nederlands"></a>
### Q: Which models from the dropdown are better for Dutch?

**What you see:** You are experiencing: **Which models from the dropdown are better for Dutch?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** LLM errors usually come from a key/provider mismatch, latency, or a model choice that doesn't fit your account.
**Fix it yourself:**
1. Check the provider + model combination in setup and `.env`.
2. Test with a known working combination and then restart the backend.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'provider|model|llm|timeout|401|unauthorized'
```
**When this does NOT work:** If the API provider keeps failing: temporarily test Ollama locally or email `info@vincentvandeth.nl`.

## Category 7 — Pre-call setup

<a id="faq-ik-moet-elke-keer-dezelfde-instellingen-invullen-kan-dat-niet-slimmer"></a>
### Q: I have to enter the same settings every time — can't that be smarter?

**What you see:** You are experiencing: **I have to enter the same settings every time — can't that be smarter?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Pre-call setup is flexible, but without fixed presets you quickly get manual work and inconsistency.
**Fix it yourself:**
1. Define fixed call profiles (e.g. discovery/demo) and use them consistently.
2. After Start Call, check the logs to confirm the chosen config was actually applied.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'config|start_call|modules|provider|model'
```
**When this does NOT work:** If setup state doesn't stay consistent: report the exact steps in GitHub Discussions.

<a id="faq-context-docs-uploaden-faalt"></a>
### Q: Context-docs upload fails

**What you see:** You are experiencing: **Context-docs upload fails**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Pre-call setup is flexible, but without fixed presets you quickly get manual work and inconsistency.
**Fix it yourself:**
1. Define fixed call profiles (e.g. discovery/demo) and use them consistently.
2. After Start Call, check the logs to confirm the chosen config was actually applied.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'config|start_call|modules|provider|model'
```
**When this does NOT work:** If setup state doesn't stay consistent: report the exact steps in GitHub Discussions.

<a id="faq-ik-wil-een-preset-opslaan-voor-discovery-calls-vs-demo-calls"></a>
### Q: I want to save a preset for "discovery calls" vs "demo calls"

**What you see:** You are experiencing: **I want to save a preset for "discovery calls" vs "demo calls"**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Pre-call setup is flexible, but without fixed presets you quickly get manual work and inconsistency.
**Fix it yourself:**
1. Define fixed call profiles (e.g. discovery/demo) and use them consistently.
2. After Start Call, check the logs to confirm the chosen config was actually applied.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'config|start_call|modules|provider|model'
```
**When this does NOT work:** If setup state doesn't stay consistent: report the exact steps in GitHub Discussions.

<a id="faq-hoe-voeg-ik-eigen-case-slides-toe"></a>
### Q: How do I add my own case slides?

**What you see:** You are experiencing: **How do I add my own case slides?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Pre-call setup is flexible, but without fixed presets you quickly get manual work and inconsistency.
**Fix it yourself:**
1. Define fixed call profiles (e.g. discovery/demo) and use them consistently.
2. After Start Call, check the logs to confirm the chosen config was actually applied.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'config|start_call|modules|provider|model'
```
**When this does NOT work:** If setup state doesn't stay consistent: report the exact steps in GitHub Discussions.

<a id="faq-hoe-voeg-ik-eigen-pijnpunten-toe-bovenop-de-standaard-12"></a>
### Q: How do I add my own pain points (on top of the standard 12)?

**What you see:** You are experiencing: **How do I add my own pain points (on top of the standard 12)?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Pre-call setup is flexible, but without fixed presets you quickly get manual work and inconsistency.
**Fix it yourself:**
1. Define fixed call profiles (e.g. discovery/demo) and use them consistently.
2. After Start Call, check the logs to confirm the chosen config was actually applied.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'config|start_call|modules|provider|model'
```
**When this does NOT work:** If setup state doesn't stay consistent: report the exact steps in GitHub Discussions.

<a id="faq-llm-provider-model-zijn-twee-velden-zou-een-moeten-zijn"></a>
### Q: LLM Provider + Model are two fields (should be one)

**What you see:** You first choose Provider and then Model; that feels redundant and error-prone.
**Why this happens:** Model options are provider-dependent; that's why these are two fields (operator feedback from the PR-86 context).
**Fix it yourself:**
1. Define fixed call profiles (e.g. discovery/demo) and use them consistently.
2. After Start Call, check the logs to confirm the chosen config was actually applied.
3. Verify it works with:
```bash
tail -n 80 data/logs/copilot.log | rg -i 'provider|model|config'
```
**When this does NOT work:** If setup state doesn't stay consistent: report the exact steps in GitHub Discussions.

## Category 8 — Privacy & security

<a id="faq-gaat-mijn-audio-naar-een-cloud"></a>
### Q: Does my audio go to a cloud?

**What you see:** You are experiencing: **Does my audio go to a cloud?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Privacy/security depends not only on tooling, but also on your process choices and provider choice.
**Fix it yourself:**
1. Explicitly decide whether you use a cloud LLM or run fully local.
2. Record the retention period and consent text in your own sales process.
3. Verify it works with:
```bash
ls -la data/sessions data/reports && tail -n 80 data/logs/copilot.log | rg -i 'provider|ollama|gemini|openai|groq'
```
**When this does NOT work:** For legally binding statements: consult your privacy lawyer.

<a id="faq-kan-ik-100-lokaal-draaien-geen-internet"></a>
### Q: Can I run 100% locally (no internet)?

**What you see:** You are experiencing: **Can I run 100% locally (no internet)?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Privacy/security depends not only on tooling, but also on your process choices and provider choice.
**Fix it yourself:**
1. Explicitly decide whether you use a cloud LLM or run fully local.
2. Record the retention period and consent text in your own sales process.
3. Verify it works with:
```bash
ls -la data/sessions data/reports && tail -n 80 data/logs/copilot.log | rg -i 'provider|ollama|gemini|openai|groq'
```
**When this does NOT work:** For legally binding statements: consult your privacy lawyer.

<a id="faq-is-dit-avg-compliant-voor-nederlandse-klanten"></a>
### Q: Is this GDPR-compliant for Dutch customers?

**What you see:** You are experiencing: **Is this GDPR-compliant for Dutch customers?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Privacy/security depends not only on tooling, but also on your process choices and provider choice.
**Fix it yourself:**
1. Explicitly decide whether you use a cloud LLM or run fully local.
2. Record the retention period and consent text in your own sales process.
3. Verify it works with:
```bash
ls -la data/sessions data/reports && tail -n 80 data/logs/copilot.log | rg -i 'provider|ollama|gemini|openai|groq'
```
**When this does NOT work:** For legally binding statements: consult your privacy lawyer.

<a id="faq-hoe-informeer-ik-de-prospect-over-opname-transcriptie-praktijken"></a>
### Q: How do I inform the prospect about recording/transcription practices?

**What you see:** You are experiencing: **How do I inform the prospect about recording/transcription practices?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Privacy/security depends not only on tooling, but also on your process choices and provider choice.
**Fix it yourself:**
1. Explicitly decide whether you use a cloud LLM or run fully local.
2. Record the retention period and consent text in your own sales process.
3. Verify it works with:
```bash
ls -la data/sessions data/reports && tail -n 80 data/logs/copilot.log | rg -i 'provider|ollama|gemini|openai|groq'
```
**When this does NOT work:** For legally binding statements: consult your privacy lawyer.

<a id="faq-kan-ik-call-data-delen-met-mijn-crm-zonder-cloud"></a>
### Q: Can I share call data with my CRM without cloud?

**What you see:** You are experiencing: **Can I share call data with my CRM without cloud?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Privacy/security depends not only on tooling, but also on your process choices and provider choice.
**Fix it yourself:**
1. Explicitly decide whether you use a cloud LLM or run fully local.
2. Record the retention period and consent text in your own sales process.
3. Verify it works with:
```bash
ls -la data/sessions data/reports && tail -n 80 data/logs/copilot.log | rg -i 'provider|ollama|gemini|openai|groq'
```
**When this does NOT work:** For legally binding statements: consult your privacy lawyer.

## Category 9 — Productivity & integration

<a id="faq-hoe-integreer-ik-dit-met-hubspot-pipedrive-verwijs-naar-pro"></a>
### Q: How do I integrate this with HubSpot / Pipedrive?

**What you see:** You are experiencing: **How do I integrate this with HubSpot / Pipedrive?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** There is no built-in CRM connection; the open-source core writes reports locally to `data/reports/` and you use those as the source for your own export. A ready-made CRM integration is on the roadmap, not in the current release.
**Fix it yourself:**
1. Use `data/reports/` as the source for exports/integrations.
2. Only then automate with your own scripts/webhooks based on those exports.
3. Verify it works with:
```bash
ls -lt data/reports | head -n 5
```
**When this does NOT work:** For direct commercial integration paths: email `info@vincentvandeth.nl`.

<a id="faq-werkt-dit-met-zoom-teams-meet-bluejeans"></a>
### Q: Does this work with Zoom / Teams / Meet / BlueJeans?

**What you see:** You are experiencing: **Does this work with Zoom / Teams / Meet / BlueJeans?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Integrations differ per stack; the open-source core mainly provides local output as a basis.
**Fix it yourself:**
1. Use `data/reports/` as the source for exports/integrations.
2. Only then automate with your own scripts/webhooks based on those exports.
3. Verify it works with:
```bash
ls -lt data/reports | head -n 5
```
**When this does NOT work:** For direct commercial integration paths: email `info@vincentvandeth.nl`.

<a id="faq-werkt-dit-op-windows-linux-ipad"></a>
### Q: Does this work on Windows / Linux / iPad?

**What you see:** You want to know whether OnCue also runs outside macOS.
**Why this happens:** macOS is the stable main platform; Windows is a separate track with its own audio-capture path (WASAPI instead of AudioTee/BlackHole). Linux and iPad/iOS are not (yet) on the map.
**Status per platform:**
1. **Windows:** yes, via WASAPI loopback capture — no virtual audio device needed, unlike BlackHole on macOS. `pip install ".[windows]"` gives you only the audio capture; for LLM-driven pain-point detection you also need a provider extra, e.g. `pip install ".[windows,detector,gemini]"` (`litellm` recently moved behind the separate `[llm]` extra because it needs Rust/Cargo to build on Windows). Verified 2026-07-14 in a Windows 11 VM with a real audio-hardware probe (95/95 real frames, not mocked) — not yet tested on physical Windows hardware during a live call. See [INSTALL.md#windows](../INSTALL.md#windows).
2. **Linux:** on the roadmap, no supported audio-capture backend yet.
3. **iPad / iOS:** not planned — the architecture (macOS Core Audio process-tap, Windows WASAPI loopback, local whisper.cpp) requires a desktop OS.
**When this does NOT work:** For Windows-specific problems: see [TROUBLESHOOTING.md](TROUBLESHOOTING.md#problem-wasapi-loopback-capture-geeft-geen-audio-windows) or open a [GitHub issue](https://github.com/Vinix24/OnCue/issues). For macOS-only features (telephony capture) that you're missing on Windows: that's deliberate, see the Architecture section in the README.

<a id="faq-kan-ik-meerdere-calls-per-dag-doen-zonder-telkens-opnieuw-opstarten"></a>
### Q: Can I do multiple calls a day without restarting each time?

**What you see:** You are experiencing: **Can I do multiple calls a day without restarting each time?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Integrations differ per stack; the open-source core mainly provides local output as a basis.
**Fix it yourself:**
1. Use `data/reports/` as the source for exports/integrations.
2. Only then automate with your own scripts/webhooks based on those exports.
3. Verify it works with:
```bash
ls -lt data/reports | head -n 5
```
**When this does NOT work:** For direct commercial integration paths: email `info@vincentvandeth.nl`.

<a id="faq-hoe-export-ik-call-rapporten-naar-notion-obsidian-email"></a>
### Q: How do I export call reports to Notion / Obsidian / email?

**What you see:** You are experiencing: **How do I export call reports to Notion / Obsidian / email?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Integrations differ per stack; the open-source core mainly provides local output as a basis.
**Fix it yourself:**
1. Use `data/reports/` as the source for exports/integrations.
2. Only then automate with your own scripts/webhooks based on those exports.
3. Verify it works with:
```bash
ls -lt data/reports | head -n 5
```
**When this does NOT work:** For direct commercial integration paths: email `info@vincentvandeth.nl`.

## Category 10 — Error messages & diagnostics

<a id="faq-port-8760-already-in-use-wat-nu"></a>
### Q: "Port 8760 already in use" — what now?

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — Port 8760 already in use](TROUBLESHOOTING.md#problem-port-8760-already-in-use).

<a id="faq-data-logs-runtime-log-is-leeg-bevat-errors"></a>
### Q: The log is empty / contains errors — and which log is it?

There are two, and they are not interchangeable.

- **`data/logs/copilot.log`** is the application log. Every module writes to it
  through `core/logging.py`, on every launch route, always. This is the one you
  want in almost every case.
- **`data/logs/runtime.log`** is only the captured stdout of the backend
  process, and only when you started it with `bash scripts/start.sh` or through
  the launchd autostart agent. Start OnCue by double-clicking `Start OnCue.app`
  and this file is never written at all, so an empty or months-old
  `runtime.log` is normal rather than a symptom.
- **`data/logs/mcp-bridge.log`** exists once you have used the MCP bridge (Pro).

If `copilot.log` itself is empty, the backend never started. Run
`bash scripts/oncue_chain.sh check`: it reports whether the hub is up, which
checkout owns it, and whether these log files are present and writable.

Step-by-step diagnosis and fix: [TROUBLESHOOTING.md — Module crashes silently](TROUBLESHOOTING.md#problem-module-crashes-silently).

<a id="faq-websocket-connection-keeps-closing"></a>
### Q: Websocket connection keeps closing

**What you see:** You are experiencing: **Websocket connection keeps closing**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** These errors are usually startup state, a port conflict, or a provider/model mismatch after earlier tests.
**Fix it yourself:**
1. First make a clean run with the stop/start scripts.
2. Read the runtime log from the first ERROR and fix exactly that cause.
3. Verify it works with:
```bash
lsof -nP -iTCP:8760 -sTCP:LISTEN && tail -n 150 data/logs/copilot.log
```
**When this does NOT work:** If you can't get it stable within 15 minutes: email `info@vincentvandeth.nl` with the log and steps.

<a id="faq-model-switch-failed-bij-opstarten"></a>
### Q: "Model switch failed" at startup

**What you see:** You are experiencing: **"Model switch failed" at startup**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** These errors are usually startup state, a port conflict, or a provider/model mismatch after earlier tests.
**Fix it yourself:**
1. Choose a model that is visibly supported for your chosen provider.
2. Fully restart the backend so old model state doesn't linger.
3. Verify it works with:
```bash
lsof -nP -iTCP:8760 -sTCP:LISTEN && tail -n 150 data/logs/copilot.log
```
**When this does NOT work:** If you can't get it stable within 15 minutes: email `info@vincentvandeth.nl` with the log and steps.

<a id="faq-hoe-reset-ik-alles-naar-een-schone-staat"></a>
### Q: How do I reset everything to a clean state?

**What you see:** You are experiencing: **How do I reset everything to a clean state?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** These errors are usually startup state, a port conflict, or a provider/model mismatch after earlier tests.
**Fix it yourself:**
1. First make a clean run with the stop/start scripts.
2. Read the runtime log from the first ERROR and fix exactly that cause.
3. Verify it works with:
```bash
lsof -nP -iTCP:8760 -sTCP:LISTEN && tail -n 150 data/logs/copilot.log
```
**When this does NOT work:** If you can't get it stable within 15 minutes: email `info@vincentvandeth.nl` with the log and steps.

<a id="faq-gpt-5-2-codex-model-is-not-supported-chatgpt-account"></a>
### Q: gpt-5.2-codex model is not supported (ChatGPT account)

**What you see:** You see an error that `gpt-5.2-codex` is not supported in your account.
**Why this happens:** Historical account/model mismatch: not every model ID is available in every environment.
**Fix it yourself:**
1. Choose a model that is visibly supported for your chosen provider.
2. Fully restart the backend so old model state doesn't linger.
3. Verify it works with:
```bash
tail -n 120 data/logs/copilot.log | rg -i 'not supported|model|provider'
```
**When this does NOT work:** If you can't get it stable within 15 minutes: email `info@vincentvandeth.nl` with the log and steps.

<a id="faq-model-switch-loop-na-new"></a>
### Q: Model-switch loop after /new

**What you see:** After `/new` you get stuck in a model-switch loop and can't get stably into call flow.
**Why this happens:** State reset + model choice fall out of sync; old session state keeps carrying over.
**Fix it yourself:**
1. First make a clean run with the stop/start scripts.
2. Read the runtime log from the first ERROR and fix exactly that cause.
3. Verify it works with:
```bash
tail -n 150 data/logs/copilot.log | rg -i 'model switch|/new|provider|state'
```
**When this does NOT work:** If you can't get it stable within 15 minutes: email `info@vincentvandeth.nl` with the log and steps.

## Category 11 — Upgrade & maintenance

<a id="faq-hoe-update-ik-naar-een-nieuwe-versie"></a>
### Q: How do I update to a new version?

**What you see:** You are experiencing: **How do I update to a new version?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Maintenance works reliably if you do updates deliberately and always finish with test evidence.
**Fix it yourself:**
1. Update in a controlled way, not ad-hoc right before a customer call.
2. After every update, run at least lint + tests + a short audio sanity check.
3. Verify it works with:
```bash
ruff check src/ && python -m pytest tests/ -v
```
**When this does NOT work:** If an update causes regressions: roll back to the previous working commit and document the issue.

<a id="faq-waar-staat-mijn-data-opgeslagen-voor-backups"></a>
### Q: Where is my data stored? (for backups)

**What you see:** You are experiencing: **Where is my data stored? (for backups)**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Maintenance works reliably if you do updates deliberately and always finish with test evidence.
**Fix it yourself:**
1. Update in a controlled way, not ad-hoc right before a customer call.
2. After every update, run at least lint + tests + a short audio sanity check.
3. Verify it works with:
```bash
ruff check src/ && python -m pytest tests/ -v
```
**When this does NOT work:** If an update causes regressions: roll back to the previous working commit and document the issue.

<a id="faq-kan-ik-oude-calls-opschonen-om-ruimte-te-besparen"></a>
### Q: Can I clean up old calls to save space?

**What you see:** You are experiencing: **Can I clean up old calls to save space?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Maintenance works reliably if you do updates deliberately and always finish with test evidence.
**Fix it yourself:**
1. Update in a controlled way, not ad-hoc right before a customer call.
2. After every update, run at least lint + tests + a short audio sanity check.
3. Verify it works with:
```bash
ruff check src/ && python -m pytest tests/ -v
```
**When this does NOT work:** If an update causes regressions: roll back to the previous working commit and document the issue.

<a id="faq-hoe-werkt-de-whisper-submodule-update"></a>
### Q: How does the Whisper submodule update work?

**What you see:** You are experiencing: **How does the Whisper submodule update work?**. This behavior blocks a normal call flow or makes the output unreliable.
**Why this happens:** Maintenance works reliably if you do updates deliberately and always finish with test evidence.
**Fix it yourself:**
1. Update in a controlled way, not ad-hoc right before a customer call.
2. After every update, run at least lint + tests + a short audio sanity check.
3. Verify it works with:
```bash
ruff check src/ && python -m pytest tests/ -v
```
**When this does NOT work:** If an update causes regressions: roll back to the previous working commit and document the issue.
