# Entity-recall demo fixture

Worked example for `scripts/eval_entity_recall.py`, used in its dispatch
verification report. Two files:

- `sample.wav` -- clean-path audio, 16kHz mono.
- `sample_degraded.wav` -- the same speech through a simulated bad phone
  line: telephone bandpass (300-3400 Hz), compression, and mixed white noise,
  downsampled to 8kHz mono. `eval_entity_recall.py` upsamples it back to 16kHz
  itself (via `to_wav_16k_mono`), so it can be pointed at directly.
- `ground_truth.yaml` -- the planted entities for both (same ground truth;
  that's what makes `--baseline` a fair clean-vs-degraded comparison).

**Not real audio.** Both are **synthetic macOS `say` text-to-speech** (voice
`Xander`, `nl_NL`) reading one invented sentence written for this fixture.
The name, company, phone number, amount and date are all made up -- nobody
spoke these words, and neither file was derived from `data/sessions/` or any
other real session/call recording. Same convention as
`scripts/benchmark_assets/README.md`.

Planted script (verbatim text given to `say`):

> Goedemiddag, met Bram de Wit van Acme Industries. U kunt mij bereiken op
> nul zes, twaalf, vierendertig, zesenvijftig, achtenzeventig. We hebben een
> investering van twaalfduizend euro besproken, met een vervolgafspraak op
> negen september. Ik stuur u volgende week een voorstel.

## Regenerating

```bash
SENTENCE="Goedemiddag, met Bram de Wit van Acme Industries. U kunt mij bereiken op nul zes, twaalf, vierendertig, zesenvijftig, achtenzeventig. We hebben een investering van twaalfduizend euro besproken, met een vervolgafspraak op negen september. Ik stuur u volgende week een voorstel."

say -v Xander -o tests/fixtures/entity_recall_demo/sample.wav \
    --file-format=WAVE --data-format=LEI16@16000 "$SENTENCE"

ffmpeg -y -i tests/fixtures/entity_recall_demo/sample.wav \
    -af "highpass=f=300,lowpass=f=3400,acompressor,volume=0.6" \
    -ar 8000 -ac 1 /tmp/degraded_8k.wav
ffmpeg -y -i /tmp/degraded_8k.wav \
    -f lavfi -i "anoisesrc=color=white:amplitude=0.045:sample_rate=8000" \
    -filter_complex "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0" \
    tests/fixtures/entity_recall_demo/sample_degraded.wav
```

## Usage

```bash
.venv/bin/python scripts/eval_entity_recall.py \
    tests/fixtures/entity_recall_demo/sample.wav \
    -g tests/fixtures/entity_recall_demo/ground_truth.yaml \
    --threshold 5 -o /tmp/clean_report.json

.venv/bin/python scripts/eval_entity_recall.py \
    tests/fixtures/entity_recall_demo/sample_degraded.wav \
    -g tests/fixtures/entity_recall_demo/ground_truth.yaml \
    --threshold 5 --baseline /tmp/clean_report.json
```
