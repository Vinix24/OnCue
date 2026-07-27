# Integrations

This document covers how OnCue connects to external systems, how to
configure audio routing on macOS, and how to extend the system with custom
backends.

**Last updated:** 2026-07-22

---

## Audio routing on macOS

OnCue captures two separate audio streams:

1. **Microphone** (your voice): captured directly via `sounddevice`.
2. **Prospect audio** (other party): four routes depending on call type.

### Understanding the four prospect-audio routes

| Route | Env | Call type | macOS requirement |
|---|---|---|---|
| AudioTee whole-system tap (default) | `AUDIO_CAPTURE_METHOD=audiotee` | Video calls: Teams, Zoom, Meet (via Google Chrome) and any other app — whole-system output, no manual routing | 14.2+ (Core Audio process taps) |
| BlackHole (manual fallback) | `AUDIO_CAPTURE_METHOD=blackhole` | Video calls, when you prefer a virtual-device route instead of the AudioTee tap | 10.14+ (BlackHole driver) |
| AudioTee call-tap | `PROSPECT_SOURCE=audiotee_call` | Telephony: iPhone-relay (Continuity), FaceTime audio — Pro | 14.2+ (Core Audio process taps) |
| Single-stream (mic only) | `SINGLE_STREAM_SPEAKER_DEFAULT=prospect` | No system audio access | Any |

**Critical macOS limitation:** iPhone/FaceTime phone calls bypass all virtual audio
devices. The beller-audio travels through the macOS telephony daemon `avconferenced`
directly, never reaching the system output mix. Using BlackHole for phone calls
captures only near-silence (RMS ~60-96 instead of ~3000 for real audio). This is
a documented macOS constraint — not a configuration issue. Use the call-tap route
for telephony.

### Route A: AudioTee whole-system tap — video calls (default)

`AUDIO_CAPTURE_METHOD=audiotee` (the default in `.env.example`) taps the whole
macOS system audio output directly via the Core Audio Taps API (`tap_all=True`).
No virtual audio device, no Multi-Output Device, no manual routing, and no
meeting-app detection: whatever plays through your system output is captured, so
any video-call app (Microsoft Teams, Zoom, Google Meet via Chrome, and others)
works out of the box.

```bash
# Set in .env (already the default)
AUDIO_CAPTURE_METHOD=audiotee
```

Requires the macOS Core Audio process-tap permission (System Settings > Privacy &
Security > Audio-opname / Audio Recording, macOS 14.4+). A missing permission or
missing `bin/audiotee` binary produces an actionable warning instead of a silent
failure.

Because the tap captures the whole system-output mix, other audio (notifications,
music) is also captured during the call — keep notifications muted.

**Optional per-app targeting (legacy).** You can narrow the tap to a single
process instead of the whole-system output by setting `TARGET_PROCESS_NAME` to a
meeting-app process name (for example `Google Chrome`, `Microsoft Teams`, or
`zoom.us`). This is optional and off by default; the whole-system tap needs no
such configuration.

```bash
# Optional: narrow the tap to one app instead of whole-system output
TARGET_PROCESS_NAME=Microsoft Teams
```

### Route B: AudioTee call-tap — telephony (iPhone-relay, Pro)

[AudioTee](https://github.com/makeusabrew/audiotee) uses the same Core Audio Taps
API to capture audio from the telephony daemon process (`avconferenced`) directly.
The tap bypasses device routing entirely — it does not matter which audio output
device is selected, including Bluetooth headsets.

```bash
# Set in .env
PROSPECT_SOURCE=audiotee_call
CALL_PROCESS_NAME=avconferenced   # default, usually correct for iPhone-relay
```

AudioTee itself is OSS (MIT, vendored at `vendor/audiotee/`, built locally by
`scripts/setup.sh` / `scripts/install.sh` into `./bin/audiotee`) — Route A's
whole-system tap uses that same binary and is free/OSS. This call-tap route
additionally requires a Pro license (`FEATURE_CALLTAP`), gated in the Python
layer (`TelephonyTapGuard`), not in the AudioTee binary itself.

Minimum macOS requirement: 14.2 (Sonoma). This is a Core Audio process taps API
requirement, not an AudioTee limitation.

**What works in call-tap mode:**
- Bluetooth headset (A2DP/HFP profile switching is irrelevant — tap is on the process)
- Opening the tap before the call starts (delivers silence until the call begins, no error)
- Per-call override: pass `prospect_source: "audiotee_call"` in the `start_call` payload

**Honest limitation:** an idle `avconferenced` delivers silent-but-running chunks.
The liveness-check counts chunks, not volume, so it only alerts when audio is
completely absent (process missing or tap failed), not during silence mid-call.

### Route C: BlackHole — video calls (manual fallback)

[BlackHole](https://github.com/ExistentialAudio/BlackHole) is a virtual audio
driver. It is a manual fallback: use it only when you explicitly set
`AUDIO_CAPTURE_METHOD=blackhole`, for example on hardware without Core Audio
process taps or when you prefer a virtual-device route. Route your video-call
application's audio output to BlackHole, and OnCue reads it from there.

Setup steps:

1. Install BlackHole 2ch from [existential.audio](https://existential.audio/blackhole/).
2. Open Audio MIDI Setup (Applications/Utilities).
3. Create a Multi-Output Device: check your speakers AND BlackHole 2ch.
4. Set this Multi-Output Device as the default system output.
5. In your video-call app, set the output to the Multi-Output Device (or leave at
   system default).

```bash
# Set in .env to force this route explicitly
AUDIO_CAPTURE_METHOD=blackhole
PROSPECT_SOURCE=blackhole   # default value; also covers Route A's audiotee whole-system tap
```

BlackHole captures the system audio mix (all apps). Other audio (notifications,
music) will also be captured during the call — keep notifications muted.

### Route D: Single-stream mode

If you cannot configure a virtual audio device, run with microphone only.

```bash
# Set in .env
AUDIO_CAPTURE_METHOD=blackhole          # or audiotee
SINGLE_STREAM_SPEAKER_DEFAULT=prospect  # all speech attributed to prospect
```

In single-stream mode, talk-time tracking continues to work on the mic stream.
Speaker attribution defaults all transcribed speech to "prospect" for detection
purposes. This reduces detection accuracy for phrases you speak.

### macOS Multi-Output Device (for the BlackHole fallback)

To hear audio in your headphones AND capture it in BlackHole simultaneously:

1. Open Audio MIDI Setup.
2. Click the `+` button at the bottom left and choose "Create Multi-Output Device".
3. Check both your headphones/speakers and BlackHole 2ch.
4. Set this Multi-Output Device as the system output in System Settings.
5. You will need to control volume via the individual devices in Audio MIDI Setup,
   not via the system volume keys.

---

## LLM providers

All LLM calls go through the `instructor` library for structured output. The
provider is selected by `LLM_PROVIDER` in `.env`.

**Default provider:** the shipped `.env.example` sets `LLM_PROVIDER=gemini`, so a
fresh checkout runs on Gemini out of the box. If `LLM_PROVIDER` is unset entirely,
the `config.py` dataclass falls back to `openrouter`. Both are documented below.

**Dependency pins:** `instructor>=1.7,<2.0` and `google-genai>=1.0,<2.0` are
pinned to prevent silent breakage on major-bump updates.

| Provider | Value | Notes |
|---|---|---|
| Google Gemini | `gemini` | `gemini-2.5-flash`. Uses `instructor.from_genai(GENAI_STRUCTURED_OUTPUTS)`. Shipped `.env.example` default. |
| OpenRouter | `openrouter` | `anthropic/claude-haiku-4.5` default. Routes through OpenRouter's public API, not the direct Anthropic API — no Anthropic SDK involved. `config.py` fallback default. |
| Google Vertex AI | `vertex` | Same Gemini models via GCP. Requires GCP billing enabled. BYO-tenant. |
| Azure OpenAI | `azure` | OpenAI models within your own Azure subscription. BYO-tenant. |
| Groq | `groq` | Fast inference. Free tier: 100K tokens/day limit (TPD). Production requires Dev tier. |
| OpenAI | `openai` | Use `gpt-4o-mini` for cost-effective classification. |
| Ollama | `ollama` | Fully local. No text leaves the machine. |

### Gemini configuration (shipped default)

```bash
LLM_PROVIDER=gemini
LLM_MODEL=gemini-2.5-flash
GEMINI_API_KEY=your-key-here    # or GOOGLE_API_KEY — both accepted
```

Get a key at [aistudio.google.com](https://aistudio.google.com). The free tier
is sufficient for personal use.

### OpenRouter configuration (config.py fallback)

```bash
LLM_PROVIDER=openrouter
LLM_MODEL=anthropic/claude-haiku-4.5
OPENROUTER_API_KEY=your-key-here
```

Get a key at [openrouter.ai/keys](https://openrouter.ai/keys). Winner of the
fase-B B3 suggestion/rebuttal eval (p50 3.1s / p95 4.3s, Claude-tier quality,
$1/$5 per 1M tokens — list prices, not re-verified against the live OpenRouter
feed).

### Vertex AI configuration

```bash
LLM_PROVIDER=vertex
LLM_MODEL=gemini-2.5-flash
GOOGLE_CLOUD_PROJECT=your-project-id
```

Requires `gcloud auth application-default login` or a service account key.
GCP billing must be enabled — Vertex AI does not have a free tier.

### Groq configuration

```bash
LLM_PROVIDER=groq
LLM_MODEL=llama-3.3-70b-versatile
GROQ_API_KEY=your-key-here
```

**Groq free-tier quota:** 100K tokens per day (TPD). A full sales call (18
pain-point events, summaries, suggestions) can hit 80-120K tokens. For
production use, upgrade to a paid Dev tier or use Gemini as fallback.

### OpenAI configuration

```bash
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
OPENAI_API_KEY=your-key-here
```

### Ollama (fully local)

```bash
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1
OLLAMA_MODEL=qwen2.5:7b
```

Install Ollama from [ollama.com](https://ollama.com), then pull a model:

```bash
ollama pull qwen2.5:7b
```

`qwen2.5:7b` is recommended for Dutch classification. `llama3.2:3b` works on
machines with less RAM but with lower accuracy.

With `LLM_PROVIDER=ollama`, no data leaves your Mac at any point in the pipeline.
Transcription (Whisper) and LLM classification both run locally.

---

## Transcription backends

Two backends are available. Select via `WHISPER_BACKEND` in `.env`.

### whisper.cpp (recommended, cross-platform)

```bash
WHISPER_BACKEND=whisper.cpp
WHISPER_CPP_BINARY=./vendor/whisper.cpp/build/bin/whisper-cli
WHISPER_CPP_MODEL_PATH=./vendor/whisper.cpp/models/ggml-large-v3-turbo.bin
```

`whisper.cpp` is the recommended and stable backend. It is included as a vendored
git submodule. Build and download a model:

```bash
bash scripts/install_whisper_cpp.sh
python scripts/install_whisper_model.py --model large-v3-turbo
```

`whisper.cpp` runs a subprocess per audio chunk (temp WAV files in
`data/runtime/transcriber/<session-id>/`). Works on any hardware that can run
the `whisper.cpp` binary.

See [WHISPER_CPP_DESIGN.md](WHISPER_CPP_DESIGN.md) for the full backend design.

### mlx-whisper (Apple Silicon, known issue)

```bash
WHISPER_BACKEND=mlx-whisper
WHISPER_MODEL=large-v3-turbo   # or: large-v3, medium
WHISPER_LANGUAGE=nl            # or: en, auto
```

Uses Apple's MLX framework for inference on the Apple Neural Engine and GPU.
`large-v3-turbo`: approximately 1.6 GB RAM, 5-second warmup, 50% faster per
chunk than `large-v3`.

**Known issue:** the live-pad path may silently deliver zero transcript segments
on some macOS/MLX version combinations. If you see an active call with no
transcription output despite valid audio, switch to `WHISPER_BACKEND=whisper.cpp`
as a workaround. This is an open item.

Models are downloaded from Hugging Face on first use and cached in `~/.cache/huggingface/`.

---

## Case database

Case slides are stored in a SQLite database by default. An optional Supabase
backend is available for cloud sync.

### SQLite (default)

```bash
CASE_DB_TYPE=sqlite
CASE_DB_SQLITE_PATH=data/cases.db
```

Seed the database with example cases:

```bash
python scripts/seed_cases.py
```

To add your own cases, either edit `scripts/seed_cases.py` or insert rows directly:

```sql
INSERT INTO cases (title, industry, pain_point, slide_html, metrics)
VALUES (
    'Klant X: 70% snellere offertes',
    'manufacturing',
    'offerteproces',
    '<section><h2>Klant X</h2><p>70% snellere offertes</p></section>',
    '70% snellere offertes, ROI in 3 maanden'
);
```

`pain_point` must match a route name in `config/pain_points.yaml`.

### Supabase (optional sync)

```bash
CASE_DB_TYPE=supabase
CASE_DB_URL=https://your-project.supabase.co
CASE_DB_KEY=your-anon-key
```

The Supabase schema is in TTD section 2.5. Run it via the Supabase
SQL editor to create the `cases` and `call_sessions` tables.

---

## CRM integrations

No out-of-the-box CRM integration is included in the open-source version.

The `post-call report` (JSON in `data/sessions/<id>/`) contains all structured
data needed to push to a CRM manually or via a custom script:

- Pain points detected (category, confidence, trigger phrase, timestamp)
- Objections raised
- Talk-time statistics
- Transcript with speaker attribution

**Webhook / custom export:** you can wrap the report generator in a custom script
that posts to your CRM's API. The report schema is documented in TTD
section 2.7.

**HubSpot sync** is available in the Pro tier. Other CRM integrations (Pipedrive,
Salesforce, Teamleader) are on the Enterprise roadmap.

---

## Extending with custom backends

OnCue uses Python Protocols for all swappable components. Adding a new
implementation requires implementing the protocol and registering it in the factory.

### Custom transcription backend

Implement the `TranscriptionBackend` Protocol from
`src/sales_copilot/modules/transcriber/backends/base.py`:

```python
from sales_copilot.modules.transcriber.backends.base import TranscriptionBackend

class MyBackend:
    async def start(self, stop_event: asyncio.Event) -> None:
        ...

    async def warmup(self) -> None:
        ...

    async def transcribe(self, audio: np.ndarray) -> str:
        ...

    async def stop(self) -> None:
        ...
```

Register it in `src/sales_copilot/modules/transcriber/backends/__init__.py` and
add the backend name to the `WHISPER_BACKEND` config key.

### Custom audio capture backend

Implement the `AudioStream` Protocol from
`src/sales_copilot/audio/capture.py`:

```python
class AudioStream(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read(self) -> np.ndarray | None: ...
```

### Custom case storage backend

Implement a case store class that matches the interface in
`src/sales_copilot/modules/slides/case_db.py` and register it via
`CASE_DB_TYPE` in `.env`.

---

## Environment variable reference

Full schema is in `.env.example`. Key variables:

```bash
# LLM
LLM_PROVIDER=gemini|openrouter|vertex|azure|groq|openai|ollama   # .env.example ships gemini; config.py fallback is openrouter
LLM_MODEL=gemini-2.5-flash                                       # shipped default (matches LLM_PROVIDER=gemini)
LLM_TIMEOUT_MS=3000

# Audio — capture method
AUDIO_CAPTURE_METHOD=mic|audiotee|blackhole|wasapi|replay   # default: audiotee (whole-system tap)
TARGET_PROCESS_NAME=                       # optional: narrow the AudioTee tap to one app (default: whole-system)

# Audio — prospect source
PROSPECT_SOURCE=blackhole|audiotee_call   # blackhole=video apps, audiotee_call=telephony
CALL_PROCESS_NAME=avconferenced          # process to tap when PROSPECT_SOURCE=audiotee_call

# Transcription
WHISPER_BACKEND=whisper.cpp|mlx-whisper  # whisper.cpp is recommended/stable
WHISPER_MODEL=large-v3-turbo|large-v3|medium
WHISPER_LANGUAGE=nl|en|auto

# Detection
DETECTOR_WINDOW_SIZE=5
DETECTOR_MIN_CHUNKS=2
DETECTOR_DEBOUNCE_S=5
DEBOUNCE_SECONDS=45
ONLY_CLASSIFY_PROSPECT=true

# Transcriber queue
TRANSCRIBER_SHARED_QUEUE=true
TRANSCRIBER_QUEUE_MAX_SIZE=32
TRANSCRIBER_SELF_PRIORITY=low

# Features
RECORD_AUDIO=false   # opt-in; recording is off by default
TRANSCRIBE_SELF_LIVE=false
ENABLE_OBJECTION_DETECTION=true
ENABLE_SUGGESTIONS=true
ENABLE_SUMMARY=true

# Hub
WS_HUB_HOST=127.0.0.1
WS_HUB_PORT=8760

# Case database
CASE_DB_TYPE=sqlite|supabase
CASE_DB_SQLITE_PATH=data/cases.db
```
