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
pipx upgrade live-sales-copilot
```

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

After the script finishes, add your API key:

```bash
nano .env
# Set: GEMINI_API_KEY=your_key_here
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
6. Add your API key to `.env` (free Gemini key: aistudio.google.com/app/apikey)
7. Double-click `Start OnCue.app` to launch

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
- No telephony (phone/FaceTime-relay) capture — that feature is macOS-only (Apple Continuity); Windows covers video-call audio (Zoom/Teams/Meet)
- An NVIDIA GPU with 4+ GB VRAM if you want GPU-accelerated transcription (CPU-only transcription also works, see below)

### Install

The install is a four-step sequence. Running `pip install ".[windows]"` alone is not enough to boot the app — the orchestrator imports the detector and reports modules unconditionally at startup, so those dependencies are always required even for a capture-only run.

```bash
pip install ".[windows]"
pip install "litellm>=1.83.10" --only-binary=:all:
pip install ".[detector]"
pip install aiosqlite --only-binary=:all:
```

**Why this sequence:**

`litellm` cannot build from source on Windows — its sdist requires Rust/Cargo. Forcing `--only-binary=:all:` picks the pure-Python wheel instead, which installs cleanly. `.[detector]` pulls in `semantic-router`, `sentence-transformers`, and `instructor` (which depends on `litellm`, hence step 2 first). `aiosqlite` is needed by the reports/coaching import chain and lives in the `slides` extra, not in `windows` or `detector` — installing it separately closes that gap.

A parallel code change (lazy imports in the orchestrator) will shrink this to `pip install ".[windows]"` for capture-only runs once it lands. Until then, follow the four steps above.

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

**Autostart monitor (`pgrep` crash).** The autostart monitor calls `pgrep` to detect meeting apps, but `pgrep` does not exist on Windows. This throws a `FileNotFoundError` every poll cycle (every 2 seconds by default). The feature is non-functional on Windows. **Workaround:** set `AUTOSTART_MONITOR_ENABLED=false` in `.env`. A code fix (Windows-native process check) is under way.

**WASAPI loopback taps the default output device.** `WasapiLoopbackStream` captures from `default_speaker()` — the Windows default audio output. If your call audio goes to a different device (e.g. a headset selected inside Zoom but not set as the Windows default), loopback captures silence. Set your call-audio device as the Windows default before starting OnCue, or use the dashboard's audio-level indicator to confirm the prospect stream is getting signal.

**First-chunk whisper onset.** The very first partial transcription segment may be garbled before the model settles. This is cosmetic and clears within the first few seconds of a call.

> **Experimental.** Windows support was verified on physical hardware 2026-07-26: WASAPI loopback capture (312/312 real-audio chunks), whisper.cpp GPU transcription (RTX 2050, cuBLAS, large-v3-turbo), and the full dashboard + transcript pipeline all passed end-to-end. See [GitHub issue #148](https://github.com/Vinix24/OnCue/issues/148) for the full test report. The 2026-07-14 VM run confirmed the capture path imports and works; this physical-hardware run confirmed real drivers, real GPU, and real install friction.
>
> What remains untested: device hotplug, day-to-day reliability across diverse hardware, and pain-point detection on machines without TLS inspection. Windows users are welcome to try it and report back on [GitHub Issues](https://github.com/Vinix24/OnCue/issues): what worked, what didn't, and your audio setup. Your results are what move Windows from experimental to fully supported.

---

## Choosing an LLM provider

OnCue uses an LLM only for pain-point confirmation (short transcript fragments, no audio). Set `LLM_PROVIDER` in `.env`:

| Provider | Free tier | Speed | Privacy |
|---|---|---|---|
| `gemini` (default) | Yes, Gemini 2.5 Flash | Fast | Google Cloud |
| `groq` | Yes, 100K tokens/day | Very fast | Groq Cloud |
| `openai` | No | Fast | OpenAI Cloud |
| `ollama` | Yes (local) | Depends on hardware | Fully local |

**Quick pick:** for the lowest latency, use Gemini 2.5 Flash (the default) or Groq. If you want more accuracy on ambiguous fragments and can spare a little latency, `anthropic/claude-haiku-4.5` via OpenRouter is a solid opt-in. Either way the LLM only ever sees a short, PII-redacted transcript fragment.

**Gemini (recommended for beta-testers):**
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
pipx upgrade live-sales-copilot
```

**Path 2 (git clone):**
```bash
cd OnCue
git pull
bash scripts/install.sh
```

**Path 3 (.app):**
Download the new release zip from GitHub Releases and replace the existing .app.

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
- `LLM_PROVIDER` set correctly
- API key present and valid
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
