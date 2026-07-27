# Whisper Fine-Tuning Guide (Dutch Sales)

This guide explains how to fine-tune the MLX Whisper backend for Dutch sales-specific vocabulary like `offerte`, `propositie`, and domain-specific pain-point language.

> **Before you fine-tune:** try the lighter "data moat" vocabulary-biasing feature first. It feeds domain terms to whisper.cpp / mlx-whisper via the `initial_prompt` mechanism without training. See `config/transcription_vocabulary.yaml` and `.env.example`. Measure the WER delta with `scripts/benchmark_transcription.py --measure-vocabulary-delta`. Only move to LoRA fine-tuning when biasing no longer yields sufficient gains.

## Why Fine-Tune

Generic Whisper models usually perform well on common Dutch. In sales calls they often miss company-specific terms, product names, and jargon. Fine-tuning on your own corrected calls can lower Word Error Rate (WER) on this domain.

## Collect Training Data

1. Record and store at least 10 real calls in `data/sessions/<session_id>/`.
2. Each session should contain:
- `audio.wav`
- `transcript.json` (auto transcript)
3. Manually correct transcripts and save as `transcript_corrected.json` to override auto text.
4. Continue collecting until you have diverse accents, call lengths, and objection patterns.

## Prepare Dataset

Run:

```bash
python scripts/finetune/prepare_dataset.py \
  --sessions-dir data/sessions \
  --out data/finetune/dataset.jsonl
```

Output format (JSONL per line):

```json
{"audio_path":"/abs/path/to/audio.wav","text":"...","language":"nl"}
```

## Train LoRA Adapter

Run:

```bash
python scripts/finetune/train_whisper.py \
  --dataset data/finetune/dataset.jsonl \
  --epochs 3 \
  --lr 1e-4 \
  --adapter-out data/finetune/adapter \
  --allow-stub-adapter
```

Notes:
- The script logs baseline WER and WER per epoch on a held-out split.
- Adapter artifacts are stored in `data/finetune/adapter/`.
- `--allow-stub-adapter` is useful for dry-run pipeline validation when no local MLX LoRA trainer is wired yet.

## Evaluate Base vs Fine-Tuned

Run:

```bash
python scripts/finetune/evaluate.py \
  --dataset data/finetune/dataset.jsonl \
  --adapter data/finetune/adapter \
  --out data/finetune/eval_report.json
```

The report contains:
- `baseline_wer`
- `finetuned_wer`
- `delta` (positive means improvement)

## Use Adapter in Live Transcriber

Set in `.env`:

```bash
WHISPER_FINE_TUNED_MODEL_PATH=data/finetune/adapter
```

Behavior:
- If path exists and adapter kwargs are supported by `mlx_whisper.transcribe`, the adapter is loaded.
- If path is invalid, transcriber logs a warning and falls back to the base model.

## Retraining Cadence

Retrain when:
- Around every 50 new calls are added.
- Vocabulary shifts (new product line, vertical, objection patterns, or competitors).
- WER regresses after introducing new team members/regions.
