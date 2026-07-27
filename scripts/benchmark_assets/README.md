# Benchmark assets — bundled mock-speech WAVs

These 8 WAV files (`mock_00.wav` .. `mock_07.wav`) are **synthetic text-to-speech
recordings of invented sentences**, generated with macOS `say` (voice `Xander`,
`nl_NL`) reading the first 8 entries of `_MOCK_SENTENCES` in
`scripts/spark_benchmark.py`.

**Not real audio.** Nobody spoke these words. The sentences are generic,
non-sensitive, invented Dutch sales-call phrases written for this benchmark —
they were never excerpted from a real call, transcript, or recording. No file
in this directory was derived from `data/sessions/` or any other real session
data.

## Why bundle them

`scripts/spark_benchmark.py` measures whisper transcription latency on a
target machine (e.g. an NVIDIA DGX Spark). On macOS it can generate this same
audio on demand via `say`, but `say` doesn't exist on Linux/the Spark itself.
Without a TTS engine, the benchmark previously fell back to a synthetic
amplitude-modulated tone — which under-represents real whisper decode timing
(a tone gives the model far fewer decode steps than actual speech). Bundling
pre-generated speech WAVs makes the benchmark self-contained and representative
everywhere, including machines with no TTS engine at all.

## Regenerating

These files are checked in and not regenerated automatically. To regenerate
them from the current `_MOCK_SENTENCES` list on a Mac with `say` installed:

```bash
.venv/bin/python scripts/spark_benchmark.py --regenerate-bundled-assets
```

## Selection logic

`spark_benchmark.py` prefers these bundled WAVs by default (works everywhere,
including Linux/Spark), falls back to live macOS `say` generation when a
sentence has no bundled WAV (e.g. `--limit` above 8, or a bundled file is
missing), and only falls back further to the synthetic tone when neither a
bundled WAV nor `say` is available.
