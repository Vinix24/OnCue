# Multi-Speaker Diarization

Speaker diarization identifies who is speaking and when. The sales copilot uses
pyannote.audio 3.1/4.x to tag each audio segment with a speaker ID
(`SPEAKER_00`, `SPEAKER_01`, etc.).

This is an **opt-in feature** — the default pipeline is unchanged when
`DIARIZATION_ENABLED=0`.

---

## When to use it

**Remote calls (dual-stream):** diarization is unnecessary. The existing dual-stream
setup (AudioTee by default, BlackHole as fallback) already separates mic (self) from
system audio (prospect). Use the standard configuration.

**In-room coaching or training sessions:** one microphone captures everyone in the room.
Diarization separates speakers from the mixed audio stream. Activate with
`DIARIZATION_ENABLED=1`.

**Monday-morning meetings (8-10 known speakers):** diarization assigns IDs to turns;
speaker enrollment (see `ADR-SPEAKER-ENROLLMENT.md`) maps IDs to names. Requires
GDPR consent for each participant — see the opt-in flow in the ADR.

---

## Setup

### 1. HuggingFace token

Create an account at [huggingface.co](https://huggingface.co) and generate a token at
`Settings > Access Tokens`.

Accept the model card for each required model:

- `pyannote/speaker-diarization-3.1`
- `pyannote/segmentation-3.0`
- `pyannote/embedding`

All three require explicit acceptance on the HuggingFace model page before the
pipeline can download.

### 2. Environment variables

Add to your `.env` file:

```env
HUGGINGFACE_TOKEN=hf_your_actual_token
DIARIZATION_ENABLED=1
DIARIZATION_MIN_SPEAKERS=1   # lower bound for clustering
DIARIZATION_MAX_SPEAKERS=10  # upper bound; set to expected speaker count for best accuracy
```

### 3. Dependencies

All diarization dependencies (`pyannote.audio`, `torch`, `torchaudio`) ship with the
project. Install the optional extra if starting fresh:

```bash
pip install "live-sales-copilot[diarization]"
```

On an existing install, pyannote.audio 4.0.4 is already present if you ran
`pip install -e ".[full]"`.

---

## Shadow-mode vs production-mode

### Shadow-mode (`DIARIZATION_ENABLED=0`, default)

The diarizer is never created. The transcription pipeline runs exactly as before.
No WS events, no overhead, no model download.

### Production-mode (`DIARIZATION_ENABLED=1`)

1. On session start, `PyannoteV3Diarizer.load()` downloads the model to
   `~/.cache/huggingface/` on first run (~500 MB total).
2. After each transcribed audio chunk, `diarize_chunk()` runs in a thread pool
   executor (non-blocking).
3. A `speaker_segments` WS event is emitted alongside the regular `transcript` event:

```json
{
  "type": "speaker_segments",
  "channel": "transcript",
  "start_ms": 1200,
  "end_ms": 3800,
  "segments": [
    {"speaker_id": "SPEAKER_00", "start_ms": 1200, "end_ms": 2100, "confidence": 1.0},
    {"speaker_id": "SPEAKER_01", "start_ms": 2300, "end_ms": 3800, "confidence": 1.0}
  ]
}
```

The pain point **detector continues to receive the unchanged `transcript` event**.
Speaker data is available to the dashboard and audit ledger for future use, but
does not influence detection logic in this release.

---

## Performance on Apple Silicon

The pipeline runs on MPS (Apple Metal) by default. Set `device` in the factory to
`"cpu"` if you encounter MPS instability.

| Scenario | Audio | Expected time |
|---|---|---|
| 2.5s chunk, MPS (M2 Pro) | 2.5s | 0.3-0.8s |
| 2.5s chunk, CPU | 2.5s | 2-4s |
| 29 min file, CPU | 1740s | 15-30 min |

The first `load()` call takes 5-15 seconds to initialise the pipeline (model is
cached after the first run).

---

## Edge cases

**Short chunks (<2s):** pyannote requires context to distinguish speakers reliably.
Chunks shorter than ~1s frequently return a single segment or empty output. The 2.5s
default buffer (`WHISPER_MAX_BUFFER_SECONDS=2.5`) is the minimum viable chunk length;
longer buffers improve diarization accuracy at the cost of transcript latency.

**Overlapping speech:** `pyannote/speaker-diarization-3.1` does not support
overlapping speech detection by default. Overlapping segments are assigned to a
single speaker. Use `pyannote/overlapped-speech-detection` as a preprocessing step
if overlap is frequent in your use case.

**Accented speech:** speaker embeddings are language-agnostic (WeSpeaker ECAPA-TDNN
backbone). Accent does not affect diarization accuracy significantly; it affects
transcription accuracy (Whisper handles this independently).

**Single speaker:** with one speaker, pyannote returns all segments as `SPEAKER_00`.
`DIARIZATION_MIN_SPEAKERS=1` allows this. If you set `DIARIZATION_MIN_SPEAKERS=2`,
pyannote forces two clusters even for solo recordings.

---

## Licensing

pyannote.audio models on HuggingFace are licensed for **non-commercial use only**.
Commercial deployment under a paid SaaS model requires a separate agreement with
Hi! Paris / CNRS. Contact `gabriel.huang@hi-paris.fr` before a production release.

See `claudedocs/ADR-PYANNOTE-DIARIZATION.md` — Consequences section for the full
licensing analysis and alternative embedding models under Apache 2.0.
