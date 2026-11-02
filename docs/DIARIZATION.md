# Multi-Speaker Diarization

Speaker diarization identifies who is speaking and when. The sales copilot tags
audio with a speaker ID (`SPEAKER_00`, `SPEAKER_01`, etc.), live with
pyannote.audio 3.1/4.x and after the call with sherpa-onnx or pyannote. It runs
in two places:

| Route | Entry point | What you get |
|---|---|---|
| Live | `DIARIZATION_ENABLED=1` (`modules/diarizer/factory.py`, called from `modules/transcriber/whisper_direct.py`) | A `speaker_segments` WebSocket event per transcribed chunk. The transcript itself is unchanged. |
| Offline, after the call | `scripts/post_process_call.py` with `modules/reports/diarize_merge.py` | A text file with one `SPEAKER_XX:` line per speaker turn |

**What the report files contain.** Neither route changes `full_transcript` in
`*_report.json` or the `*_transcript.md`. Those two keep the channel names
(`self` and `prospect`, or the preset's own names). A transcript with one label
per person exists only as the `-labeled.txt` file from the offline route. No
code consumes the live `speaker_segments` event: the dashboard, the report
builder and the detector ignore it.

This is an **opt-in feature**. The default pipeline is unchanged when
`DIARIZATION_ENABLED=0`.

**Local.** Audio and speaker data stay on your machine. The only network traffic
is the one-time model download: from the sherpa-onnx GitHub releases (no token) or
from Hugging Face (pyannote, needs your token).

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

### 1. HuggingFace token (pyannote only)

The live diarizer and the pyannote backend of the offline route need a token. The
sherpa-onnx backend of the offline route does not (see "Offline route" below).

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

With `DIARIZATION_ENABLED=1` and no token (or a `hf_PLACEHOLDER` token), the live
diarizer logs a warning and stays off (`modules/diarizer/factory.py:23-27`).

`DIARIZATION_BACKEND` (default `sortformer`, `core/config.py:172`) and
`MIN_SPEAKERS` / `MAX_SPEAKERS` are a different setting. The transcriber only
logs the backend name at startup (`modules/transcriber/__main__.py`). The live
diarizer is always `PyannoteV3Diarizer`, and it reads only the
`DIARIZATION_*` variables above.

### 3. Dependencies

All diarization dependencies (`sherpa-onnx`, `pyannote.audio`, `torch`, `torchaudio`)
ship with the project. Install the optional extra if starting fresh:

```bash
pip install "live-sales-copilot[diarization]"
```

On an existing install, pyannote.audio 4.0.4 is already present if you ran
`pip install -e ".[full]"`.

---

## Offline route: speaker-labeled transcript

Record the call with `RECORD_AUDIO=true`. The recorder writes one WAV per stream
under `data/sessions/<session>/`. Run the script on the stream that carries
several voices:

```bash
python scripts/post_process_call.py data/sessions/<session>/<stream>.wav --num-speakers 4
```

Without a token this runs on sherpa-onnx. With a token, `--num-speakers 4` selects
pyannote.

The script transcribes the WAV with the local Whisper backend (or takes
`--transcript-json`), diarizes the whole file, assigns each segment to the speaker
with the greatest overlap and merges consecutive segments of one speaker. The result
is `<wav name>-labeled.txt` (`--output` overrides):

```
SPEAKER_00: Goedemorgen allemaal.
SPEAKER_01: Goedemorgen.
```

### Two backends

`--backend {auto,sherpa,pyannote}` (default `auto`) picks the separation backend. The
script prints which one ran in its closing `[post-process]` line.

| Backend | Needs | Behaviour |
|---|---|---|
| `sherpa` | `sherpa-onnx` (the `diarization` extra), no token | pyannote segmentation-3.0 as ONNX plus the WeSpeaker ResNet34 VoxCeleb embedding, clustered by `--cluster-threshold` (default 0.8). `--num-speakers` is ignored, and the script logs one line saying so. The models (a few tens of MB) download from the sherpa-onnx GitHub releases on first use into the user cache dir. Neither torch nor pyannote is imported. |
| `pyannote` | `pyannote.audio`, torch, a Hugging Face token | Unchanged: `--model`, `--num-speakers` and `--device` apply. |

`auto` decides on `--num-speakers` and the token (`--hf-token`, `HUGGINGFACE_TOKEN`,
`HF_TOKEN` or the cached `huggingface-cli login`):

| `--num-speakers` | Token | Backend | Warning |
|---|---|---|---|
| 2 or less | any | sherpa | no |
| 3 or more, or not given | yes | pyannote | no |
| 3 or more | no | sherpa | yes: likely a group, voices may end up under one label |
| not given | no | sherpa | yes: speaker count unknown, sherpa may put several voices under one label; `--num-speakers 2` or a token avoids that |

Why: measured on our own recordings, on two-person calls sherpa with WeSpeaker and
threshold 0.8 scores a DER of 17% against 25-26% for pyannote; on a single-microphone
two-person recording both split talk time about 63/36. Forcing sherpa to exactly two
clusters gave 97/3, which is wrong, so sherpa clusters by threshold and not by a forced
count. In groups of six or seven people on one microphone, sherpa puts 86-100% of talk
time in one cluster at every threshold while pyannote spreads it over 4 to 6 speakers.
Pyannote stays the choice for groups.

The labels are anonymous. Mapping them to names is not part of this script.

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
The event is published to the WebSocket hub only. Nothing in this repository
stores it or shows it, and it does not influence detection logic in this release.

Each chunk is diarized on its own, so `SPEAKER_00` in one event is not guaranteed
to be the same person as `SPEAKER_00` in the next. Use the offline route when you
need labels that stay consistent over a whole call.

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

## Comparing backends on your own recordings

`scripts/eval_diarization.py` scores diarization backends against each other. It is an
evaluation tool and changes nothing in the product. Contenders: pyannote
`speaker-diarization-3.1`, pyannote `speaker-diarization-community-1` and sherpa-onnx
(pyannote segmentation-3.0 as ONNX plus a speaker-embedding model and sherpa-onnx
clustering).

```bash
uv sync --extra diarization --extra diar-eval      # sherpa-onnx, pyannote.metrics, psutil
export HF_TOKEN=...                                # only for the two pyannote backends
```

Sherpa models (a few tens of MB) are downloaded on first use into the user cache dir
(`~/Library/Caches/oncue/diarization-models` on macOS, `--model-dir` overrides it) and are
never committed.

**Two-track sessions.** The reference comes from the recorder tracks: Silero VAD per track,
`self` for the microphone and `prospect` for the system track, overlap kept. Every backend
only sees the mix of both tracks. Sessions where the microphone carries the other side
through the loudspeakers are skipped and counted: the peak normalised cross-correlation
within +-0.3 s reaches `--bleed-ncc` (0.15), or at least `--bleed-overlap` (0.6) of the
microphone speech lies inside system speech.

```bash
python scripts/eval_diarization.py --sessions-root data/sessions --limit 3 \
    --max-session-minutes 15 --out ~/diar-eval
```

**One room recording.** One microphone, several people. A hand-made RTTM gives DER; without
it you get speaker counts and timings only.

```bash
python scripts/eval_diarization.py --audio meeting.m4a --start 60 --end 360 \
    --expected-speakers 3 --reference meeting.rttm --blind-transcript meeting.json --out ~/diar-eval
```

`--blind` (or `--blind-transcript`) writes `blind/version-A.txt`, `version-B.txt`, ... in
random order, one per backend, each line `[mm:ss] SPEAKER_n: text`, plus `blind/key.json`
(letter to backend). Read the versions first, open the key afterwards. Without a transcript
the fragment is transcribed once with the default transcription backend.

Backends are chosen with `--backends` (comma list, default all). `sherpa:<embedding>` clusters
by threshold, the real-world case where the speaker count is unknown; `@oracle` clusters to the
true count (from the reference or `--expected-speakers`) and so isolates clustering errors from
embedding quality. Embeddings: `eres2net`, `wespeaker`, `titanet`. `--device` is repeatable
(`cpu` default, `mps` and `cuda` for pyannote, `cuda` for sherpa-onnx).

Output in `--out`: one RTTM per backend per input, `results.csv`/`results.md` (also per
input) and `aggregate.csv`/`aggregate.md`. `--out` must lie outside the repository when an
input does; the script refuses otherwise. Logs carry paths and numbers, never transcript text.

| Column | Meaning |
|---|---|
| `der` | Diarization error rate, collar 0.25 s, overlapped speech scored (`pyannote.metrics`) |
| `missed`, `false_alarm`, `confusion` | Components as a share of reference speech |
| `speakers_found`, `speakers_expected` | Speakers in the output; in the reference or `--expected-speakers` |
| `load_s` | Model load and configuration time, not part of `seconds` |
| `seconds`, `rtf` | Wall-clock processing time and the same divided by audio duration |
| `peak_rss_mb` | Peak resident memory of the process during load and processing of one backend (sampled; models are released between backends) |
| `device` | `cpu`, `mps` or `cuda` |

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

The models are downloaded at runtime and are not part of this repository. Licences, checked
on the model cards:

| Model | Used by | Licence |
|---|---|---|
| `pyannote/speaker-diarization-3.1` | pyannote backend, live diarizer | MIT |
| `pyannote/segmentation-3.0` | pyannote backend; also the sherpa-onnx segmentation model | MIT |
| `pyannote/wespeaker-voxceleb-resnet34-LM` | pyannote backend | CC-BY-4.0 |
| `pyannote/speaker-diarization-community-1` | pyannote backend (`--model`) | CC-BY-4.0 |
| sherpa-onnx WeSpeaker VoxCeleb ResNet34 | sherpa backend | CC-BY-4.0 (follows the VoxCeleb dataset licence) |

CC-BY-4.0 requires attribution. A product or redistribution that ships or uses these
models must credit the model authors, name the licence and link to the source. The
attribution list, with source URLs, is in `THIRD_PARTY_LICENSES.md` under "Models
downloaded at runtime".
