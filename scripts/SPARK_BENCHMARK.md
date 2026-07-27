# Spark benchmark kit

`scripts/spark_benchmark.py` measures whisper transcription latency, and optionally
local-LLM generation latency, on a TARGET machine -- in particular an NVIDIA DGX Spark
or other CUDA box -- using only non-sensitive **mock audio** bundled with the script.

**No private data ever leaves this checkout.** The script never reads `data/sessions/`
or any other real recording. Every audio segment it transcribes is one of, in preference
order: a **pre-generated WAV bundled in `scripts/benchmark_assets/`** (8 clips, checked
into the repo -- itself macOS `say` reading one of the ~14 neutral, invented Dutch sales
sentences in the script), live macOS `say` TTS for any sentence beyond the bundled set,
or (when neither a bundled WAV nor a TTS engine is available) a synthetic tone of
representative duration. Latency is hardware-bound, not content-bound, so this is a fair
benchmark without a single real conversation involved.

Because the kit ships its own bundled mock-speech WAVs, a Spark/Linux run is
**representative out of the box** -- it uses real-speech-like audio, not the synthetic
tone, with no TTS engine required on the target machine. See
`scripts/benchmark_assets/README.md` for exactly what's bundled and why it's safe
(invented sentences, not real recordings).

## Why

The Spark question is whether fast local hardware can close the local-vs-cloud latency
gap that made the live copilot's LLM path settle on cloud/BYO-tenant (see
`claudedocs/` local-inference-speedup writeups -- best locally-measured structured-output
call on a 24GB Mac was ~2815ms p50, far above the live ≤300ms target). This kit lets a
Spark owner run the same measurement shape on their box and send back a report, without
Vincent handing over any call recordings.

## Running on the Spark

1. **Get the code onto the Spark.** Clone/copy this repository (or the subset needed to
   run `scripts/spark_benchmark.py`: `src/sales_copilot/`, `scripts/`, `pyproject.toml`,
   `uv.lock`). Install Python deps the usual way (`uv sync` or `pip install -e .`).

2. **Build whisper.cpp with CUDA.** This repo's `scripts/install_whisper_cpp.sh` is
   macOS-only (`-DWHISPER_METAL=ON`). On the Spark, build the vendored submodule (or your
   own whisper.cpp checkout) with CUDA instead:

   ```bash
   git submodule update --init --recursive vendor/whisper.cpp
   cmake -S vendor/whisper.cpp -B vendor/whisper.cpp/build -DGGML_CUDA=on
   cmake --build vendor/whisper.cpp/build --config Release --target whisper-cli whisper-server
   bash vendor/whisper.cpp/models/download-ggml-model.sh large-v3-turbo
   ```

   Requires the NVIDIA driver + CUDA toolkit already set up on the box (standard on a
   DGX Spark image). See the [whisper.cpp CUDA docs](https://github.com/ggml-org/whisper.cpp#nvidia-gpu-support)
   if `-DGGML_CUDA=on` fails to configure.

3. **Point the config at the CUDA build** (env vars, no code changes -- see
   `.env.example`):

   ```bash
   export WHISPER_CPP_BINARY=./vendor/whisper.cpp/build/bin/whisper-cli
   export WHISPER_CPP_SERVER_BINARY=./vendor/whisper.cpp/build/bin/whisper-server
   export WHISPER_CPP_MODEL_PATH=./vendor/whisper.cpp/models/ggml-large-v3-turbo.bin
   ```

4. **Run the benchmark:**

   ```bash
   .venv/bin/python scripts/spark_benchmark.py --llm
   ```

   `--llm` additionally probes the configured `LLM_PROVIDER` (e.g. `LLM_PROVIDER=ollama`
   with Ollama running locally on the Spark) for one representative structured-output
   call, reporting generation latency + time-to-first-token. Omit `--llm` to measure
   whisper only. Omit nothing else -- the mock audio and hardware fingerprint are
   automatic.

5. **Send the report back.** Two files are written (`--report-md` / `--report-json` to
   override the paths): a markdown report and its JSON twin, both self-describing (mock
   audio source, hardware fingerprint, whisper latency p50/p95/mean, optional LLM probe).
   Neither file contains any audio or transcript content beyond the bundled mock
   sentences.

## CUDA-specific notes

- `-DGGML_CUDA=on` requires the CUDA toolkit's `nvcc` on `PATH` and a matching NVIDIA
  driver. If cmake can't find CUDA, set `CUDACXX=/usr/local/cuda/bin/nvcc` (or your
  toolkit's path) before running cmake.
- The hardware fingerprint in the report calls `nvidia-smi` if present. If `nvidia-smi`
  isn't on `PATH` (unusual on a DGX image but possible in a container), the report falls
  back to `"unknown"` for the GPU field -- the benchmark still runs.
- `whisper-server` binds `WHISPER_CPP_SERVER_HOST`/`WHISPER_CPP_SERVER_PORT`
  (default `127.0.0.1` / an OS-assigned free port) -- no inbound network exposure needed.

## Off-Spark (macOS dev machine)

Running `python scripts/spark_benchmark.py` on the same Mac used for
`scripts/stt_compare.py` measures the Metal/CPU backend instead of CUDA -- useful to sanity
check the script and to compare against the reference numbers already measured
(local whisper.cpp ~990ms p50, Groq cloud ~209ms p50) before shipping the kit. By default
this reuses the bundled WAVs (fast, deterministic); pass `--no-bundled-assets --no-tts` to
force the synthetic-tone fallback instead.

## Bundled mock-speech WAVs

`scripts/benchmark_assets/` ships 8 pre-generated WAVs (macOS `say`, voice `Xander`,
reading the first 8 `_MOCK_SENTENCES` in `spark_benchmark.py`) so the kit is
self-contained and representative on any machine, TTS engine or not. Details, safety
notes, and how to regenerate them after editing `_MOCK_SENTENCES` are in
`scripts/benchmark_assets/README.md` -- in short:

```bash
.venv/bin/python scripts/spark_benchmark.py --regenerate-bundled-assets
```

Flags controlling audio source selection:

- Default (no flags): prefer bundled WAVs, fall back to live `say`, then the tone.
- `--no-bundled-assets`: skip the bundled WAVs even when present.
- `--no-tts`: skip live `say` generation for any sentence with no bundled WAV.
- `--no-bundled-assets --no-tts` together: force the synthetic tone for every segment.
