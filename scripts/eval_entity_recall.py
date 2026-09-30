#!/usr/bin/env python3
"""Entity-recall harness for transcription over a (possibly degraded) audio path.

Sits alongside ``scripts/benchmark_transcription.py`` (Dutch WER on Fireflies
calls) but answers a different question. WER measures "how many words are
right"; this measures whether the *specific things downstream automation
needs* survive the audio path: company names, person names, phone numbers,
amounts, dates and agreements. A transcript that mangles filler words but
gets every name and number right is a pass here even with a mediocre WER. One
with excellent WER that turns a phone number into prose is a fail.

Reuses ``scripts/benchmark_transcription.py``'s ffmpeg/WAV prep
(``ensure_ffmpeg`` / ``to_wav_16k_mono``) and ``scripts/transcribe_file.py``'s
``_transcribe`` -- the same batch-transcription entry point the CLI script
uses -- so this measures what the product's own transcription path actually
produces, not a bespoke call into whisper.

Ground truth is a YAML file listing planted entities (see
``tests/fixtures/entity_recall_demo/ground_truth.yaml`` for an example):

    entities:
      - type: phone
        value: "06 12 34 56 78"
        variants: ["0612345678", "06-12-34-56-78"]
        critical: true
      - type: amount
        value: "12.000 euro"
        variants: ["twaalfduizend euro"]
        critical: false

``type`` is one of company/person/phone/amount/date/agreement. ``variants``
are additional accepted forms for the SAME planted entity -- meaning-based
accepted forms belong here, not in matching code. ``critical`` (default
true) marks a load-bearing entity so the report can separate "missed a
filler date" from "missed the candidate's name".

Matching is exact after normalisation (case, punctuation, whitespace, Dutch
number words vs. digits) against ``value`` and every ``variants`` entry --
never fuzzy similarity as the primary test, so a wrong surname one character
off is a miss, not a hit. A fuzzy "closest text" search runs only to explain
a miss (--fuzzy-threshold controls when it's flagged as a near-miss) and
never contributes to the hit count.

Usage:
    .venv/bin/python scripts/eval_entity_recall.py call.wav \\
        --ground-truth truth.yaml --threshold 5 --output clean_report.json

    # Score a degraded path against a clean-path reference:
    .venv/bin/python scripts/eval_entity_recall.py call_degraded.wav \\
        --ground-truth truth.yaml --baseline clean_report.json

No network calls, no LLM -- this is a transcription measurement, reusing the
configured local Whisper backend (WHISPER_BACKEND / WHISPER_CPP_BINARY /
WHISPER_CPP_MODEL_PATH etc. from .env), same as ``scripts/transcribe_file.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark_transcription import ensure_ffmpeg, to_wav_16k_mono  # noqa: E402
from scripts.transcribe_file import _transcribe  # noqa: E402

_ALLOWED_TYPES = ("company", "person", "phone", "amount", "date", "agreement")
_TMP_DIR_NAME = "sc_eval_entity_recall"


# ---------------------------------------------------------------------------
# Dutch number-word <-> digit normalisation
# ---------------------------------------------------------------------------

_UNITS = {
    "nul": 0, "een": 1, "één": 1, "twee": 2, "drie": 3, "vier": 4,
    "vijf": 5, "zes": 6, "zeven": 7, "acht": 8, "negen": 9,
}
_TEENS = {
    "tien": 10, "elf": 11, "twaalf": 12, "dertien": 13, "veertien": 14,
    "vijftien": 15, "zestien": 16, "zeventien": 17, "achttien": 18, "negentien": 19,
}
_TENS = {
    "twintig": 20, "dertig": 30, "veertig": 40, "vijftig": 50,
    "zestig": 60, "zeventig": 70, "tachtig": 80, "negentig": 90,
}
# Checked largest-first: the first scale word found in a compound splits it
# into (multiplier before) x (scale) + (remainder after), recursively.
_SCALES: tuple[tuple[str, int], ...] = (
    ("miljoen", 1_000_000),
    ("duizend", 1_000),
    ("honderd", 100),
)


def _parse_dutch_number_word(word: str) -> int | None:
    """Parse a single Dutch number word/compound (e.g. "twaalfduizend") to an int.

    Returns None when ``word`` is not a recognised Dutch number word at all.
    Unaccented "een" is ambiguous between the numeral 1 and the indefinite
    article ("a euro" vs "one euro") -- this resolves it as 1, which is the
    reading that matters for amount entities.
    """
    word = word.strip()
    if not word:
        return None
    if word in _UNITS:
        return _UNITS[word]
    if word in _TEENS:
        return _TEENS[word]
    if word in _TENS:
        return _TENS[word]
    for tens_word, tens_val in _TENS.items():
        if word.endswith(tens_word) and len(word) > len(tens_word):
            prefix = word[: -len(tens_word)]
            if prefix.endswith("en"):
                unit_val = _UNITS.get(prefix[:-2])
                if unit_val:
                    return unit_val + tens_val
    for scale_word, scale_val in _SCALES:
        idx = word.find(scale_word)
        if idx == -1:
            continue
        prefix, suffix = word[:idx], word[idx + len(scale_word):]
        multiplier = 1
        if prefix:
            parsed = _parse_dutch_number_word(prefix)
            if parsed is None:
                continue
            multiplier = parsed
        remainder = 0
        if suffix:
            parsed = _parse_dutch_number_word(suffix)
            if parsed is None:
                continue
            remainder = parsed
        return multiplier * scale_val + remainder
    return None


def _dutch_words_to_digits(text: str) -> str:
    """Replace maximal runs of Dutch number-word tokens with digit strings.

    Handles both a single compound token ("twaalfduizend" -> "12000") and a
    run of separate tokens ("twaalf duizend" or digit-by-digit "nul zes
    twaalf" -> "0 6 12", left for ``_merge_adjacent_digit_groups`` to fuse).
    """
    tokens = [t for t in text.split(" ") if t]
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        if _parse_dutch_number_word(tokens[i]) is not None:
            run = [tokens[i]]
            j = i + 1
            while j < n:
                if tokens[j] == "en" and j + 1 < n and _parse_dutch_number_word(tokens[j + 1]) is not None:
                    run.extend([tokens[j], tokens[j + 1]])
                    j += 2
                elif _parse_dutch_number_word(tokens[j]) is not None:
                    run.append(tokens[j])
                    j += 1
                else:
                    break
            merged = "".join(t for t in run if t != "en")
            value = _parse_dutch_number_word(merged)
            if value is not None:
                out.append(str(value))
            else:
                # The run as a whole isn't a single valid number (e.g. digits
                # read one at a time) -- convert each morpheme independently.
                out.extend(
                    t if t == "en" else str(_parse_dutch_number_word(t))
                    for t in run
                )
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


def _merge_adjacent_digit_groups(text: str) -> str:
    """Fuse whitespace-separated pure-digit tokens into one run.

    Handles both a thousands-formatted amount ("12 000" -> "12000") and a
    spaced/hyphenated phone number ("06 12 34 56 78" -> "0612345678") with the
    same rule: digits next to digits belong together, anything else doesn't.
    """
    out: list[str] = []
    buffer = ""
    for tok in text.split(" "):
        if tok.isdigit():
            buffer += tok
        else:
            if buffer:
                out.append(buffer)
                buffer = ""
            if tok:
                out.append(tok)
    if buffer:
        out.append(buffer)
    return " ".join(out)


def normalize(text: str) -> str:
    """Case/punctuation/whitespace/number-word-insensitive normalisation."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    text = _dutch_words_to_digits(text)
    text = _merge_adjacent_digit_groups(text)
    return text


# ---------------------------------------------------------------------------
# Ground truth
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entity:
    type: str
    value: str
    variants: tuple[str, ...] = ()
    critical: bool = True

    @property
    def candidates(self) -> tuple[str, ...]:
        return (self.value, *self.variants)


def load_ground_truth(path: Path) -> list[Entity]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not data:
        return []
    raw_entities = data.get("entities") if isinstance(data, dict) else data
    if not isinstance(raw_entities, list):
        raise ValueError(f"{path}: expected a top-level 'entities' list")

    entities: list[Entity] = []
    for i, raw in enumerate(raw_entities):
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: entity #{i} is not a mapping")
        entity_type = str(raw.get("type", "")).strip().lower()
        if entity_type not in _ALLOWED_TYPES:
            raise ValueError(
                f"{path}: entity #{i} has invalid type {entity_type!r}; "
                f"expected one of {_ALLOWED_TYPES}"
            )
        value = str(raw.get("value", "")).strip()
        if not value:
            raise ValueError(f"{path}: entity #{i} ({entity_type}) is missing a non-empty 'value'")
        variants_raw = raw.get("variants", []) or []
        if not isinstance(variants_raw, list):
            raise ValueError(f"{path}: entity #{i} ({entity_type}) 'variants' must be a list")
        variants = tuple(str(v).strip() for v in variants_raw if str(v).strip())
        critical = bool(raw.get("critical", True))
        entities.append(Entity(type=entity_type, value=value, variants=variants, critical=critical))
    return entities


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class EntityResult:
    type: str
    value: str
    critical: bool
    found: bool
    matched_form: str | None = None
    context_text: str | None = None
    context_similarity: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "value": self.value,
            "critical": self.critical,
            "found": self.found,
            "matched_form": self.matched_form,
            "context_text": self.context_text,
            "context_similarity": self.context_similarity,
        }


def _contains_normalized(transcript_norm: str, candidate_norm: str) -> bool:
    """Word-boundary-safe substring check (so "jan" doesn't match inside "januari")."""
    if not candidate_norm:
        return False
    return f" {candidate_norm} " in f" {transcript_norm} "


def _best_context(
    transcript_tokens: list[str], candidate_norm: str, *, pad: int = 6
) -> tuple[str | None, float | None]:
    """Find the transcript window most similar to ``candidate_norm``.

    Used only to explain a miss ("what did it hear instead") and, above
    ``--fuzzy-threshold``, to flag a near-miss. Never used to decide a hit.
    """
    if not transcript_tokens or not candidate_norm:
        return None, None
    candidate_len = max(1, len(candidate_norm.split()))
    n = len(transcript_tokens)
    best_ratio = -1.0
    best_start = 0
    for start in range(n):
        end = min(n, start + candidate_len)
        window_norm = normalize(" ".join(transcript_tokens[start:end]))
        ratio = SequenceMatcher(None, window_norm, candidate_norm).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start
    ctx_start = max(0, best_start - pad)
    ctx_end = min(n, best_start + candidate_len + pad)
    context = " ".join(transcript_tokens[ctx_start:ctx_end])
    return context, best_ratio


def match_entities(entities: list[Entity], transcript: str, *, pad: int = 6) -> list[EntityResult]:
    transcript_norm = normalize(transcript)
    transcript_tokens = transcript.split()
    results: list[EntityResult] = []
    for entity in entities:
        found = False
        matched_form: str | None = None
        for candidate in entity.candidates:
            candidate_norm = normalize(candidate)
            if _contains_normalized(transcript_norm, candidate_norm):
                found = True
                matched_form = candidate
                break

        context_text: str | None = None
        context_similarity: float | None = None
        if not found:
            context_text, context_similarity = _best_context(
                transcript_tokens, normalize(entity.value), pad=pad
            )

        results.append(
            EntityResult(
                type=entity.type,
                value=entity.value,
                critical=entity.critical,
                found=found,
                matched_form=matched_form,
                context_text=context_text,
                context_similarity=context_similarity,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def summarize(results: list[EntityResult]) -> dict[str, Any]:
    total = len(results)
    hits = sum(1 for r in results if r.found)
    by_type: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_type.setdefault(r.type, {"total": 0, "hits": 0})
        bucket["total"] += 1
        bucket["hits"] += int(r.found)
    critical_total = sum(1 for r in results if r.critical)
    critical_hits = sum(1 for r in results if r.critical and r.found)
    return {
        "total": total,
        "hits": hits,
        "by_type": by_type,
        "critical_total": critical_total,
        "critical_hits": critical_hits,
    }


def report_to_dict(
    *,
    audio_path: Path,
    ground_truth_path: Path,
    threshold: int | None,
    transcript: str,
    results: list[EntityResult],
) -> dict[str, Any]:
    return {
        "audio_path": str(audio_path),
        "ground_truth_path": str(ground_truth_path),
        "threshold": threshold,
        "transcript": transcript,
        "entities": [r.to_dict() for r in results],
        "summary": summarize(results),
    }


def load_report_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def render_baseline_delta(results: list[EntityResult], baseline: dict[str, Any]) -> list[str]:
    """Score the current (typically degraded-path) run against a baseline report."""
    lines: list[str] = []
    base_entities = {(e["type"], e["value"]): bool(e["found"]) for e in baseline.get("entities", [])}
    base_summary = baseline.get("summary", {})
    base_hits = base_summary.get("hits", sum(1 for v in base_entities.values() if v))
    base_total = base_summary.get("total", len(base_entities))
    cur_hits = sum(1 for r in results if r.found)
    cur_total = len(results)

    lines.append(f"Baseline report: {baseline.get('audio_path', '(unknown)')}")
    lines.append(
        f"  baseline: {base_hits}/{base_total} found   "
        f"current: {cur_hits}/{cur_total} found   "
        f"delta: {cur_hits - base_hits:+d} entities"
    )

    regressions, improvements, missing = [], [], []
    for r in results:
        key = (r.type, r.value)
        if key not in base_entities:
            missing.append(r)
            continue
        was_found = base_entities[key]
        if was_found and not r.found:
            regressions.append(r)
        elif not was_found and r.found:
            improvements.append(r)

    if regressions:
        lines.append(
            f"  regressed ({len(regressions)}): "
            + ", ".join(f"{r.type}:{r.value!r}" for r in regressions)
        )
    if improvements:
        lines.append(
            f"  improved ({len(improvements)}): "
            + ", ".join(f"{r.type}:{r.value!r}" for r in improvements)
        )
    if missing:
        lines.append(
            "  WARNING: not present in baseline report (different ground truth?): "
            + ", ".join(f"{r.type}:{r.value!r}" for r in missing)
        )
    return lines


def render_report(
    *,
    audio_path: Path,
    ground_truth_path: Path,
    transcript: str,
    results: list[EntityResult],
    threshold: int | None,
    fuzzy_threshold: float,
    baseline: dict[str, Any] | None,
) -> str:
    lines: list[str] = []
    summary = summarize(results)
    hits, total = summary["hits"], summary["total"]

    if threshold is not None:
        passed = hits >= threshold
        lines.append(f"THRESHOLD: {threshold} entities required to pass")
        lines.append(f"RESULT: {'PASS' if passed else 'FAIL'} ({hits}/{total} entities found)")
        lines.append("")

    lines.append(f"Audio:        {audio_path}")
    lines.append(f"Ground truth: {ground_truth_path}")
    lines.append(f"Transcript:   {len(transcript)} chars, {len(transcript.split())} words")
    lines.append("")
    lines.append("Per-entity results:")

    near_misses: list[EntityResult] = []
    for r in results:
        status = "HIT " if r.found else "MISS"
        crit = "critical" if r.critical else "optional"
        lines.append(f"  [{status}] {r.type:<9} ({crit}) expected: {r.value!r}")
        if r.found:
            if r.matched_form is not None and r.matched_form != r.value:
                lines.append(f"           matched via variant: {r.matched_form!r}")
        elif r.context_text:
            similarity = r.context_similarity or 0.0
            lines.append(f'           heard instead (similarity {similarity:.2f}): "{r.context_text}"')
            if similarity >= fuzzy_threshold:
                near_misses.append(r)
                lines.append(
                    f"           NOTE: fuzzy near-miss above threshold {fuzzy_threshold:.2f} "
                    "-- NOT counted as a hit"
                )
        else:
            lines.append("           heard instead: (empty transcript)")

    lines.append("")
    lines.append("Summary:")
    pct = (hits / total * 100) if total else 0.0
    lines.append(f"  {hits}/{total} entities found ({pct:.0f}%)")
    lines.append(f"  critical: {summary['critical_hits']}/{summary['critical_total']} found")
    lines.append("  by type:")
    for etype, bucket in summary["by_type"].items():
        lines.append(f"    {etype:<10} {bucket['hits']}/{bucket['total']}")

    if near_misses:
        lines.append("")
        lines.append(f"Fuzzy near-misses (similarity >= {fuzzy_threshold:.2f}, NOT counted as hits):")
        for r in near_misses:
            lines.append(f'  - {r.type}: {r.value!r} ~ "{r.context_text}" ({r.context_similarity:.2f})')

    if baseline is not None:
        lines.append("")
        lines.append("Baseline comparison:")
        lines.extend(render_baseline_delta(results, baseline))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Transcription (reuse the product's own batch-transcription pipeline)
# ---------------------------------------------------------------------------


def transcribe_via_pipeline(audio_path: Path) -> str:
    """Batch-transcribe ``audio_path`` through the configured product backend.

    Delegates to ``scripts/transcribe_file.py``'s ``_transcribe`` (the same
    ``TranscriberConfig.from_env()`` + backend construction the CLI script
    uses) so this measures the product's real transcription path, not a
    bespoke call into whisper. Any ffmpeg-decodable format is accepted; the
    16kHz-mono conversion mirrors ``benchmark_transcription.to_wav_16k_mono``.
    """
    ensure_ffmpeg()
    tmp_dir = Path(tempfile.gettempdir()) / _TMP_DIR_NAME
    tmp_dir.mkdir(exist_ok=True)
    resolved = audio_path.resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:16]
    wav_path = to_wav_16k_mono(resolved, tmp_dir / f"{digest}.wav")
    return asyncio.run(_transcribe(wav_path, backend_name=None, chunk_seconds=30, include_timestamps=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("audio_path", type=Path, help="Existing WAV (or ffmpeg-decodable) recording, no live capture")
    parser.add_argument(
        "--ground-truth", "-g", type=Path, required=True, help="YAML file listing planted entities"
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=None,
        help="Number of entities that must be found to pass (default: no threshold, exit 0 regardless)",
    )
    parser.add_argument(
        "--baseline", type=Path, default=None, help="A previously saved --output report to diff this run against"
    )
    parser.add_argument(
        "--output", "-o", type=Path, default=None, help="Write the JSON report here (usable later as --baseline)"
    )
    parser.add_argument(
        "--fuzzy-threshold",
        type=float,
        default=0.75,
        help="Similarity (0-1) above which a miss's closest transcript text is flagged as a fuzzy "
        "near-miss -- informational only, never counted as a hit (default: 0.75)",
    )
    parser.add_argument(
        "--context-words",
        type=int,
        default=6,
        help="Words of transcript padding shown around a miss's closest match (default: 6)",
    )
    args = parser.parse_args(argv)

    if not args.audio_path.exists():
        print(f"FATAL: audio file not found: {args.audio_path}", file=sys.stderr)
        return 1
    if not args.ground_truth.exists():
        print(f"FATAL: ground truth file not found: {args.ground_truth}", file=sys.stderr)
        return 1

    try:
        entities = load_ground_truth(args.ground_truth)
    except ValueError as exc:
        print(f"FATAL: invalid ground truth file: {exc}", file=sys.stderr)
        return 1
    if not entities:
        print(f"FATAL: ground truth file has no entities: {args.ground_truth}", file=sys.stderr)
        return 1

    baseline: dict[str, Any] | None = None
    if args.baseline is not None:
        if not args.baseline.exists():
            print(f"FATAL: baseline report not found: {args.baseline}", file=sys.stderr)
            return 1
        baseline = load_report_file(args.baseline)

    try:
        transcript = transcribe_via_pipeline(args.audio_path)
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: transcription failed: {exc}", file=sys.stderr)
        return 1

    results = match_entities(entities, transcript, pad=args.context_words)

    report_text = render_report(
        audio_path=args.audio_path,
        ground_truth_path=args.ground_truth,
        transcript=transcript,
        results=results,
        threshold=args.threshold,
        fuzzy_threshold=args.fuzzy_threshold,
        baseline=baseline,
    )
    print(report_text)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report_dict = report_to_dict(
            audio_path=args.audio_path,
            ground_truth_path=args.ground_truth,
            threshold=args.threshold,
            transcript=transcript,
            results=results,
        )
        with args.output.open("w", encoding="utf-8") as handle:
            json.dump(report_dict, handle, indent=2, ensure_ascii=False)
        print(f"\nReport written to {args.output}", file=sys.stderr)

    if args.threshold is not None:
        return 0 if summarize(results)["hits"] >= args.threshold else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
