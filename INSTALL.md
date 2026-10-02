# OnCue — Installation Guide

Three install paths for macOS (the stable, primary platform), plus a Windows path (experimental — see [Windows](#windows) below).

**System requirements (macOS, Paths 1-3):**
- macOS 13 (Ventura) or newer for video calls; macOS 14.2 or newer required for telephony capture (`PROSPECT_SOURCE=audiotee_call`, a Sales Pro feature)
- Apple Silicon (M1/M2/M3/M4) — Intel Macs work but MLX transcription is unavailable
- 8 GB RAM minimum, 16 GB recommended
- BlackHole 2ch for system audio capture (free), only needed if you use the manual `blackhole` fallback — the default AudioTee tap needs no virtual device

---

## Path 1: Power-user (pipx)

Best for developers who want a clean, isolated install that stays out of the system Python.

**Prerequisites:** `pipx` installed (`brew install pipx`)

```bash
pipx install "git+https://github.com/Vinix24/OnCue.git#egg=live-sales-copilot[smart]"
sales-copilot start
```

`sales-copilot start` launches the local hub and opens the dashboard in your default browser. It drives two UIs:
- `dashboard/index.html` — private coaching UI (second screen), opened for you
- `presentation/index.html` — Reveal.js deck to screen-share to your prospect (open this tab yourself and share it)

If the browser doesn't open, go to `http://localhost:8760` manually.
To keep the browser from opening automatically, set `SALES_COPILOT_NO_BROWSER=1`.

**To update:**
```bash
pipx reinstall live-sales-copilot
```

Use `reinstall`, not `upgrade`. This install tracks the git branch, whose version
number changes per release, not per commit. `pipx upgrade` asks pip, pip sees the
same version it already has, and stops — it prints "already at latest version"
and installs nothing. That is silent for new code and silent in a second way for
new commands: the `bin/` wrappers for anything under `[project.scripts]` are
written at install time only, so a command added since your install never
appears, with no error to notice. `pipx reinstall` rebuilds from the branch head
and writes them.

**To uninstall:**
```bash
pipx uninstall live-sales-copilot
```

**Limitation:** the Whisper model download and AudioTee build happen on first `sales-copilot start`. The first run takes 3-5 minutes on a clean machine.

---

## Path 2: Beta-tester (one-shot script)

Best for technical sales coaches and recruiters who are comfortable with a terminal. The script is idempotent — safe to run twice.

```bash
git clone https://github.com/Vinix24/OnCue.git
cd OnCue
bash scripts/install.sh
```

The installer checks and installs:
1. macOS version (13+)
2. Homebrew
3. Python 3.11+ (installs via Homebrew if missing)
4. git (Xcode command line tools)
5. Virtual environment at `.venv/`
6. All Python dependencies (`pip install -e .[smart]`)
7. whisper.cpp backend — binary compiled from the vendored submodule + its GGML model (large-v3-turbo, ~800 MB)
8. `.env` from `.env.example` if not already present
9. Local data directories

The installer builds the AudioTee binary locally from the OSS-vendored Swift
source at `vendor/audiotee/` (requires Xcode command line tools; `xcode-select --install`
if missing). If the build is skipped or fails, system-audio capture falls back
to BlackHole (see the BlackHole section below). Telephony/per-process capture
(`PROSPECT_SOURCE=audiotee_call`) uses the same binary but is a Sales Pro feature.

After the script finishes, optionally add an LLM provider and its API key (OnCue runs the live cues without one; see [Choosing an LLM provider](#choosing-an-llm-provider)):

```bash
nano .env
# Optional: LLM_PROVIDER=gemini and GEMINI_API_KEY=your_key_here
```

Then start:

```bash
.venv/bin/sales-copilot start
# or
.venv/bin/python -m sales_copilot
```

---

## Path 3: Non-technical user (ZIP download)

Best for sales coaches and recruiters who do not use a terminal. No Python knowledge needed. A Terminal window opens once during setup to show install progress — you don't type anything into it.

**Download**

Get the latest release ZIP from GitHub Releases (link provided in the beta invitation). The ZIP contains everything you need, including both installer and launcher apps.

The default AudioTee tap needs no audio setup. If you use the manual `blackhole` fallback, the step-by-step setup is in the **Optional fallback: BlackHole audio routing** section below.

**Quick steps:**

1. Download and unzip the release file
2. Move the unzipped folder to your Desktop or Documents (not iCloud Drive)
3. Right-click `Setup OnCue.app` > **Open** > **Open** (Gatekeeper bypass, one-time only)
4. A Terminal window opens showing install progress — leave it open and wait 3-5 minutes for it to finish (you don't type anything into it)
5. (Optional) The default AudioTee tap needs no audio setup. Only if you use the manual BlackHole fallback, follow the **Optional fallback: BlackHole audio routing** section below
6. (Optional) Add an LLM provider and its API key to `.env`. Without one, the live cues run fully local. Example: a free Gemini key from aistudio.google.com/app/apikey
7. Double-click `Start OnCue.app` to launch

**Checking your setup before a real call.** From a checkout of the repo you can
run the preflight, which changes nothing and starts nothing:

```bash
bash scripts/oncue_chain.sh check
```

It answers, per line, whether the backend is up and which copy of OnCue owns it,
whether your install carries every command this version declares, whether
exactly one meeting app is running (which gates automatic call start; the tap
itself follows the whole system mix and needs no meeting app), whether AI
detection will be on for your
next call, and whether the logs are writable. Anything that is not OK comes
with the command that fixes it. `bash scripts/oncue_chain.sh up` runs the same
checks and then starts what is missing, so the only step left is starting the
call.

**Updating:**

Download the new release ZIP from GitHub Releases and replace the existing folder.

---

## Optional fallback: BlackHole audio routing

BlackHole is an **optional** fallback. The default AudioTee tap captures whole-system audio with no virtual device, so most users can skip this section. Set it up only if the AudioTee build failed or you explicitly run `AUDIO_CAPTURE_METHOD=blackhole`.

BlackHole 2ch is a free virtual audio driver that lets OnCue capture system audio (what your prospect says via Teams/Zoom/Meet) without recording to a file.

**Install:**
```bash
brew install --cask blackhole-2ch
```

**Configure (one-time, takes 2 minutes):**

1. Open **Audio MIDI Setup** (Spotlight: "Audio MIDI Setup")
2. Click `+` in the bottom left > **Create Multi-Output Device**
3. Check both your speakers/headphones and **BlackHole 2ch**
4. Name it "OnCue Output"
5. Right-click the new Multi-Output Device > **Use This Device For Sound Output**
6. Set **System Preferences > Sound > Output** to "OnCue Output"

Full walkthrough with screenshots: [docs/BETA_TESTER_GUIDE.md](docs/BETA_TESTER_GUIDE.md)

---

## Windows

OnCue supports Windows via a WASAPI loopback capture path (no virtual-audio-device install needed, unlike the macOS BlackHole route).

**Windows requirements:**
- Windows 10 2004+ or Windows 11 (WASAPI loopback + per-process loopback API)
- Python 3.11+ (3.13 verified working as of 2026-07-26)
- A headset — WASAPI loopback captures the whole system-output endpoint, not a single app; speakers cause echo/self-capture
- Telephony (phone-relay) audio is confirmed recordable on Windows via Stereo Mix, but whether it reaches the render pipeline OnCue's WASAPI tap reads from is unconfirmed — Stereo Mix and WASAPI loopback tap different points in the audio stack and can diverge; you must also start the session manually, since automatic call detection is macOS-only; see **Telephony capture** under Known Windows caveats below
- An NVIDIA GPU with 4+ GB VRAM if you want GPU-accelerated transcription (CPU-only transcription also works, see below)

### Install

```bash
pip install ".[windows]"
```

That's enough to boot a capture-only run — talk-time tracking and live transcription, no pain-point detection. The orchestrator (`src/sales_copilot/__main__.py`) only imports the detector and reports modules from inside `_run_call()`, once a call actually enables them, so a plain `.[windows]` install never needs `litellm`, `instructor`, `semantic-router`, `sentence-transformers`, or `aiosqlite` just to start.

**Full install, with pain-point detection:**

```bash
pip install ".[windows]"
pip install "litellm>=1.83.10" --only-binary=:all:
pip install ".[detector]"
pip install aiosqlite --only-binary=:all:
```

**Why the four-step sequence for the full install:**

`litellm` cannot build from source on Windows — its sdist requires Rust/Cargo. Forcing `--only-binary=:all:` picks the pure-Python wheel instead, which installs cleanly. `.[detector]` pulls in `semantic-router`, `sentence-transformers`, and `instructor` (which depends on `litellm`, hence step 2 first). `aiosqlite` is needed by the reports/coaching import chain and lives in the `slides` extra, not in `windows` or `detector` — installing it separately closes that gap.

**Audio capture config:**
```env
AUDIO_CAPTURE_METHOD=wasapi
```
`wasapi` is the default on Windows — you can leave `AUDIO_CAPTURE_METHOD` unset and the copilot picks it automatically.

### Add transcription (prebuilt whisper.cpp binaries)

The `vendor/whisper.cpp` submodule is not pulled by `gh repo clone` or `git clone`, and building it on Windows requires a full C++ toolchain (CMake + MSVC). The simpler route is to download prebuilt binaries — no submodule, no build.

**GPU (cuBLAS) route:**

1. Download the **whisper.cpp v1.9.1 cuBLAS binaries** from the [ggml-org whisper.cpp releases](https://github.com/ggml-org/whisper.cpp/releases/tag/v1.9.1): `whisper-cublas-12.4.0-bin-x64.zip`. This bundle includes the CUDA runtime DLLs — no separate CUDA toolkit install needed.
2. Download the model file `ggml-large-v3-turbo.bin` (~1.55 GB) from the same release.
3. Extract both to a folder of your choice, then point OnCue at them in `.env`:

```env
WHISPER_BACKEND=whisper.cpp
WHISPER_MODEL=large-v3-turbo
WHISPER_CPP_BINARY=C:\path\to\whisper-bin\cublas\Release\whisper-cli.exe
WHISPER_CPP_SERVER_BINARY=C:\path\to\whisper-bin\cublas\Release\whisper-server.exe
WHISPER_CPP_MODEL_PATH=C:\path\to\whisper-bin\models\ggml-large-v3-turbo.bin
```

With a GPU, model warm-up takes ~3 seconds (tested on an RTX 2050 4 GB, large-v3-turbo loaded fully in VRAM).

**CPU-only route:**

Use the non-CUDA binaries from the same v1.9.1 release instead of the cuBLAS zip. All other steps are identical — set the same env vars, pointing `WHISPER_CPP_BINARY` and `WHISPER_CPP_SERVER_BINARY` at the CPU-only executables. Transcription will be slower but needs no GPU.

### Add an LLM provider for pain-point detection

```bash
pip install ".[gemini]"
```

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key_here
```

Swap `gemini` for `groq` or `openai` if you prefer that provider. See [Choosing an LLM provider](#choosing-an-llm-provider) below. Only add `.[llm]` on Windows if you already have Rust/Cargo installed — without it, `pip install ".[llm]"` (or `.[smart]`/`.[full]`, which both include `llm`) fails with a `metadata-generation-failed` error on `litellm`.

### TLS / HTTPS inspection pitfall (corporate machines)

Machines behind a corporate firewall or antivirus that injects a root CA will hit download failures on every OpenSSL/schannel path:

- Model download via `curl` → `CRYPT_E_NO_REVOCATION_CHECK`. Workaround: `curl --ssl-no-revoke`.
- Python `urllib` downloads (including the embedding model for pain-point detection) → `CERTIFICATE_VERIFY_FAILED`. `pip` itself is unaffected (it uses `certifi`), but the embedding-model download at first startup will fail on these machines.
- `litellm` runtime model-cost fetch → `CERTIFICATE_VERIFY_FAILED` (non-fatal; falls back to a local backup).

Until an offline/bundled embedding model or a certifi-based downloader lands, pain-point detection cannot warm up on machines with TLS inspection. Tracked as a known gap.

### Known Windows caveats

**Telephony capture.** *A 2026-09-06 note in this section claimed the WASAPI level meter itself showed a relayed call — that was wrong. What moved was Windows' own `mmsys.cpl` level meter, not OnCue's, because OnCue was not running at the time.* Below is what is actually established, measured 2026-09-07 on Windows 11 build 26200.9278.

Measured: a phone call relayed over Bluetooth to the PC *is* recordable on that machine — captured via Stereo Mix (host MME), confirmed by WAV analysis (exact digital silence from 0-21s, then speech peaking at 0.69).

Not established: whether that audio ever passes through the render pipeline WASAPI loopback taps. Stereo Mix is a Realtek codec feature — it captures the codec's own output mix. WASAPI loopback taps a different point: the Windows audio engine's render pipeline. Those are usually the same signal, but not necessarily: if the Bluetooth stack mixes relayed call audio directly into the codec's mix without routing it through the audio engine, Stereo Mix would still catch it while WASAPI loopback would not, with nothing broken on either side. Which of the two is happening here is unknown, and it isn't a minor detail — Stereo Mix is off by default on most Windows machines and isn't available on every codec, so an OnCue that depended on it wouldn't behave the same across hardware. One measurement would resolve it: change the Windows default output device mid-call and check whether the relayed audio follows. If it does, it's routed through the audio engine and WASAPI loopback should see it; if it stays on the Realtek endpoint, it doesn't, and telephony audio won't reach OnCue's own tap on setups like this one.

Not established either: whether OnCue's own WASAPI loopback tap (`WasapiLoopbackStream`) catches that same relayed call. On this machine OnCue crashed about a second after start (root cause fixed in #224 — see below — but not re-verified on real Windows hardware), and a separate WASAPI-loopback probe in Audacity returned `-9996 (Invalid device)` on this Realtek endpoint. For a call carried by a PC app instead of a relayed phone (WhatsApp), OnCue's WASAPI loopback did capture signal — so the whole-endpoint tap works on this hardware for at least one audio source; telephony specifically remains unverified through OnCue itself.

What Windows does *not* have, independent of the above: (1) automatic call detection — the `avconferenced` process check the runtime autostart monitor uses on macOS to arm a session on its own has no Windows equivalent, since `avconferenced` is a macOS daemon — and (2) the per-process telephony tap (`PROSPECT_SOURCE=audiotee_call`, Sales Pro) that isolates call audio from the rest of the system mix on macOS; an explicit `audiotee_call` on Windows normalizes back to `blackhole`/WASAPI with a logged warning instead of failing. In practice: on Windows, start OnCue's capture manually before the call. Whether the relayed call ends up in the transcript is, per the above, not yet confirmed — treat it as untested until someone reports back on real hardware.

**Native crash ~1s after start — fixed in code, not yet re-verified on real hardware.** The 2026-09-06 field test hit a native crash (no Python traceback) about a second after "Audio transcriber started", on every audio configuration tried on that machine. Root cause: `soundcard`'s WASAPI recorder is a COM object bound to whichever thread creates it, and the reader thread that read it never joined that COM apartment — a cross-apartment violation. Fixed in #224 by moving the recorder's entire lifecycle (open, read, periodic re-attach, close) onto one COM-joined reader thread. A follow-up adversarial review (#227) found and fixed five further reader-thread lifecycle defects in that same fix — two of them regressions #224 itself introduced (a recorder handle leak on open-failure, and a `start()`/`stop()` race after a timed-out join), plus a COM-uninitialize call that could run without a matching init, a swallowed open-failure that never reached `tap_health` or the dashboard, and a COM join failure that could wedge `start()`/`stop()` forever. Both #224 and #227 are verified from source only, since no Windows host was available to confirm either fix on real hardware. The same field test found and fixed five other real-install defects (#223): the wheel didn't ship `dashboard/`/`presentation/`/`config/` for a plain (non-editable) `pip install` — this also affected the documented macOS pipx path — `httpx` was missing from the `windows` extra so transcription silently fell back to a backend that cannot run outside Apple Silicon, a blank `MIC_INPUT_DEVICE` crashed PortAudio, the shipped `.env.example` carried an override that defeated Windows auto-detection, and a zero-frame recording finalized silently. All five are fixed on `main`.

**Autostart monitor: the `pgrep` crash is fixed, and video-app auto-detection is now Windows-aware.** Process lookups dispatch on `sys.platform` (`_find_pid_win32` in `audio/capture.py`, backed by `tasklist`), so the monitor no longer throws `FileNotFoundError` polling on Windows. The built-in video-meeting candidate list is now platform-aware too (`_default_meeting_app_candidates()` in `audio/capture.py`): `chrome.exe`, `Teams.exe`, `ms-teams.exe` (both Teams client generations are checked), and `Zoom.exe` on Windows, versus `Google Chrome`/`Microsoft Teams`/`zoom.us` on macOS. Matching stays *exact* on both platforms, deliberately with no fuzzy fallback (see `resolve_meeting_app_target()`). Telephony-based auto-arm is still moot on Windows for the reason above (no `avconferenced`) — only the video-app trigger works there.

**WASAPI loopback follows the default output device — or a device you pin.** By default `WasapiLoopbackStream` follows whatever Windows currently calls the default playback device, and re-attaches automatically if that default changes mid-call (e.g. a Bluetooth headset connecting or disconnecting), logging the switch. If your call audio instead goes to a device that is *not* the Windows default (e.g. a headset selected inside Zoom but never set as the system default), you have two options: set that device as the Windows default before starting OnCue, or set `AUDIO_WASAPI_ENDPOINT_NAME` in `.env` to a substring of the device name (e.g. `Realtek` or `Headset`) to pin the tap to it regardless of what Windows calls the default — a pinned endpoint is never displaced by a later default-device change. Either way, use the dashboard's audio-level indicator to confirm the prospect stream is getting signal.

**First-chunk whisper onset.** The very first partial transcription segment may be garbled before the model settles. This is cosmetic and clears within the first few seconds of a call.

> **Experimental.** Windows support was verified on physical hardware 2026-07-26: WASAPI loopback capture (312/312 real-audio chunks), whisper.cpp GPU transcription (RTX 2050, cuBLAS, large-v3-turbo), and the full dashboard + transcript pipeline all passed end-to-end for a PC-app call. See [GitHub issue #148](https://github.com/Vinix24/OnCue/issues/148) for the full test report. The 2026-07-14 VM run confirmed the capture path imports and works; this physical-hardware run confirmed real drivers, real GPU, and real install friction. None of that 2026-07-26 run touched telephony/Bluetooth-relayed audio — that was first tried on 2026-09-06, on a different machine (see **Telephony capture** above).
>
> That 2026-09-06/07 field test (Windows 11 build 26200.9278) hit a native crash the 2026-07-26 run did not report, plus five install defects — see **Native crash** and **Telephony capture** above. A follow-up code review (#227) found and fixed five further defects in the native-crash fix itself, two of them regressions the original fix (#224) introduced. All are fixed on `main`; none of the fixes has been re-verified on real Windows hardware since landing.
>
> What remains untested: the #223, #224 and #227 fixes on real hardware, telephony audio through OnCue's own WASAPI tap, device hotplug, day-to-day reliability across diverse hardware, and pain-point detection on machines without TLS inspection. Windows users are welcome to try it and report back on [GitHub Issues](https://github.com/Vinix24/OnCue/issues): what worked, what didn't, and your audio setup. Your results are what move Windows from experimental to fully supported.

---

## Choosing an LLM provider

An LLM is optional. `LLM_PROVIDER=none` is the default: the live cues (embedding detection and talk-time) run fully local and no transcript text is sent anywhere. With a provider set, OnCue also uses it for window classification, a rolling summary, live suggestions and the post-call report. Never audio. What is sent per task is listed in [docs/PRIVACY.md](docs/PRIVACY.md). Set `LLM_PROVIDER` in `.env`:

| Provider | Free tier | Speed | Privacy |
|---|---|---|---|
| `none` (default) | n/a | n/a | Nothing is sent |
| `gemini` | Yes, Gemini 2.5 Flash | Fast | Google Cloud |
| `groq` | Yes, 100K tokens/day | Very fast | Groq Cloud |
| `openai` | No | Fast | OpenAI Cloud |
| `ollama` | Yes (local) | Depends on hardware | Fully local |

**Quick pick:** for the lowest latency, use Gemini 2.5 Flash or Groq. `anthropic/claude-haiku-4.5` via OpenRouter is another opt-in. Public providers receive the text after the pattern-based PII filter, which is a safety net and not a guarantee (see [docs/PRIVACY.md](docs/PRIVACY.md)).

**Gemini (example public provider):**
```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key_here
```
Get key: https://aistudio.google.com/app/apikey

**Groq (fastest free option):**
```env
LLM_PROVIDER=groq
GROQ_API_KEY=your_key_here
```
Get key: https://console.groq.com

Note: Groq free tier has a 100K tokens/day limit. For production use (multiple calls/day) or team deployments, use Gemini or a paid Groq tier.

**Fully local (Ollama):**
```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen2.5:7b
```
Requires Ollama running locally. The `/v1` suffix is required (the OpenAI-compatible adapter POSTs to `<base>/v1/chat/completions`). Pain-point latency will be higher (2-8s vs 1-3s with cloud).

---

## Update

**Path 1 (pipx):**
```bash
pipx reinstall live-sales-copilot
```
`pipx upgrade` short-circuits on an unchanged version number and then installs
nothing at all — see [Path 1](#path-1-power-user-pipx) above.

**Path 2 (git clone):**
```bash
cd OnCue
git pull
bash scripts/install.sh
```
The second step is not optional bookkeeping. `git pull` updates the source; only
the reinstall writes the `bin/` wrappers for commands the new version declares.

**Path 3 (.app):**
Download the new release zip from GitHub Releases and replace the existing .app.

**Checking that an update actually landed:** from a checkout,
`bash scripts/oncue_chain.sh check` compares the commands declared in
`pyproject.toml` against the ones present in `.venv/bin` and names any that are
missing (line `1b. Entrypoints`).

---

## Troubleshooting

**"sales-copilot: command not found" after pipx install**

Add pipx bin dir to your PATH:
```bash
pipx ensurepath
source ~/.zshrc
```

**Microphone permission denied**

macOS requires explicit permission. Go to **System Settings > Privacy & Security > Microphone** and enable it for Terminal (or whichever app you use).

**"BlackHole not found" on startup**

Confirm BlackHole 2ch is installed (`brew install --cask blackhole-2ch`) and the Multi-Output Device is set as your system output. See the BlackHole section above.

**"No module named sales_copilot"**

You are running the wrong Python. Use `.venv/bin/python` or `.venv/bin/sales-copilot`, not the system `python3`.

**Pain points not detected during call**

Check your `.env`:
- Without an LLM (`LLM_PROVIDER=none`) only the local embedding path runs; set `LLM_PROVIDER` to enable LLM classification
- If a provider is set: API key present and valid
- `PAIN_POINTS_ENABLED=1` (default)

Then check the dashboard for error messages — they show the exact failure cause.

**Very high latency (>10s) on pain-point detection**

Switch to Gemini or Groq for faster responses. Ollama on 8 GB RAM with a 7B model typically adds 4-8s of latency per detection.

---

## Developer mode

If you want to modify the code and contribute, use development mode with the full dev dependencies:

```bash
git clone https://github.com/Vinix24/OnCue.git
cd OnCue
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full,dev]"
pytest tests/
```

Run tests:
```bash
pytest tests/ -x
```

Lint:
```bash
ruff check src/ tests/
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the technical design.
