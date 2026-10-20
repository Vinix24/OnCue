#!/usr/bin/env python3
"""Per-model entity measurement with the post-ASR normalization layer applied.

Answers: which ASR variant (model x vocabulary on/off) yields the most correct
entities *after* ``modules/transcriber/normalize.py``, and how many already
correct spellings does the layer break?

Pipeline per variant:

1. Cut each listening-list fragment (4 minutes) out of the call audio with
   ffmpeg (16 kHz mono, same prep as ``scripts/benchmark_transcription.py``).
2. Transcribe it through the product path (``scripts/transcribe_file.py``
   ``_transcribe``: same backend factory, 30 s chunks, vocabulary prompt from
   the configured vocabulary file).
3. For every reviewed entity case (beoordelingslijst, ``[x]`` rows) take the
   transcript window around the listening-list timestamp and test whether the
   target spelling is present: before the layer (raw ASR) and after
   ``normalize()``.

Scores are reported on the cases whose fragment audio was available. Cases
without audio are listed as a count, never silently folded into the score.

Held-out false positives come from ``measure_transcript_normalization``: the
layer's replacement count on real, unannotated conversations from
``data/cases.db``. That corpus does not depend on the ASR variant, so the rate
is identical for every variant and reported once per table for readability.

Raw transcripts and the per-case outcome file contain customer names. They are
written to ``--out-dir`` (expected to be gitignored) and never printed. Stdout
carries aggregated counts only.

Usage:
    .venv/bin/python scripts/measure_entities_per_model.py \\
        --tuning-set /abs/beoordelingslijst.md \\
        --listening-list /abs/luisterlijst.md \\
        --held-out-db /abs/cases.db \\
        --fireflies-root /abs/data/fireflies \\
        --models-dir /abs/vendor/whisper.cpp/models \\
        --out-dir /abs/data/measurements/entiteiten \\
        --variants turbo-voc turbo-novoc
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sales_copilot.modules.transcriber.normalize import (  # noqa: E402
    load_normalization_lists,
    normalize,
)
from scripts import measure_transcript_normalization as base  # noqa: E402
from scripts.benchmark_transcription import SAMPLE_RATE, ensure_ffmpeg  # noqa: E402

CHUNK_SECONDS = 30
DEFAULT_WINDOW_SECONDS = 30

_FRAGMENT_RE = re.compile(r"^### (?P<name>\S+)\s+\[(?P<start>\d+:\d+(?::\d+)?)–(?P<end>\d+:\d+(?::\d+)?)\]\s*$")
_MENTION_RE = re.compile(r"^\s+\[(?P<ts>\d+:\d+(?::\d+)?)\]\s+'(?P<word>.*)'\s*$")
_TRANSCRIPT_LINE_RE = re.compile(r"^\[(?P<mm>\d+):(?P<ss>\d+)\]\s+(?P<text>.*)$")

TranscribeFn = Callable[[Path, "AsrVariant"], str]


@dataclass(frozen=True)
class AsrVariant:
    label: str
    model: str
    vocabulary: bool


VARIANTS: dict[str, AsrVariant] = {
    "turbo-voc": AsrVariant("turbo-voc", "large-v3-turbo", True),
    "turbo-novoc": AsrVariant("turbo-novoc", "large-v3-turbo", False),
    "v3-voc": AsrVariant("v3-voc", "large-v3", True),
    "v3-novoc": AsrVariant("v3-novoc", "large-v3", False),
}


@dataclass(frozen=True)
class Fragment:
    name: str
    start_s: int
    end_s: int
    mentions: tuple[tuple[int, str], ...]  # (absolute timestamp in seconds, word)


@dataclass
class CaseOutcome:
    klant: str
    asr_word: str
    target: str
    status: str
    scored: bool
    present_before: bool = False
    present_after: bool = False


@dataclass
class VariantScore:
    label: str
    cases_total: int
    cases_scored: int
    cases_unscored: int
    before_hits: int
    after_hits: int
    goed_scored: int
    goed_broken: int
    fout_scored: int
    fout_repaired: int
    replacements_in_fragments: int
    seconds: float
    fragments_transcribed: int
    outcomes: list[CaseOutcome] = field(default_factory=list)

    @property
    def score_before(self) -> float:
        return self.before_hits / self.cases_scored if self.cases_scored else 0.0

    @property
    def score_after(self) -> float:
        return self.after_hits / self.cases_scored if self.cases_scored else 0.0


def _key(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "", name.casefold())


def parse_clock(value: str) -> int:
    parts = [int(p) for p in value.split(":")]
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


# ---------------------------------------------------------------------------
# Listening list, audio lookup, fragment cutting
# ---------------------------------------------------------------------------


def parse_listening_list(path: Path) -> list[Fragment]:
    fragments: list[Fragment] = []
    name = ""
    start = end = 0
    mentions: list[tuple[int, str]] = []

    def flush() -> None:
        if name:
            fragments.append(Fragment(name, start, end, tuple(mentions)))

    for line in path.read_text(encoding="utf-8").splitlines():
        header = _FRAGMENT_RE.match(line.strip())
        if header:
            flush()
            name = header.group("name")
            start = parse_clock(header.group("start"))
            end = parse_clock(header.group("end"))
            mentions = []
            continue
        mention = _MENTION_RE.match(line)
        if mention and name:
            mentions.append((parse_clock(mention.group("ts")), mention.group("word")))
    flush()
    return fragments


def find_audio(fragment_name: str, fireflies_root: Path) -> Path | None:
    """Locate ``audio.mp3`` for a fragment by unique prefix on the folder path.

    ``fragment-name`` and the relative folder path are compared with all
    non-alphanumerics stripped, so a fragment key resolves to
    ``<folder>-<date>/audio.mp3`` or ``<folder>/<subfolder>-<date>/audio.mp3``.
    Zero or several matches return None: guessing which call is meant would
    score the wrong audio.
    """
    key = _key(fragment_name)
    matches = [
        audio
        for audio in sorted(fireflies_root.rglob("audio.mp3"))
        if _key(str(audio.parent.relative_to(fireflies_root))).startswith(key)
    ]
    return matches[0] if len(matches) == 1 else None


def cut_fragment(audio: Path, start_s: int, end_s: int, dst: Path) -> Path:
    """Cut [start_s, end_s) to a 16 kHz mono WAV (cached on ``dst``)."""
    if dst.exists():
        return dst
    ensure_ffmpeg()
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", str(start_s), "-t", str(end_s - start_s),
        "-i", str(audio),
        "-ac", "1", "-ar", str(SAMPLE_RATE), str(dst),
    ]
    subprocess.run(cmd, check=True)
    return dst


# ---------------------------------------------------------------------------
# Transcript windows and scoring
# ---------------------------------------------------------------------------


def parse_timestamped_transcript(text: str) -> list[tuple[int, str]]:
    """Parse ``[MM:SS] text`` lines (as written by ``_transcribe``) into chunks."""
    chunks: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = _TRANSCRIPT_LINE_RE.match(line.strip())
        if match:
            chunks.append((int(match.group("mm")) * 60 + int(match.group("ss")), match.group("text")))
        elif chunks and line.strip():
            # whisper-server returns multi-line text per chunk; keep it with its chunk.
            start, previous = chunks[-1]
            chunks[-1] = (start, f"{previous} {line.strip()}")
    return chunks


def window_text(chunks: list[tuple[int, str]], center_s: int, span_s: int = DEFAULT_WINDOW_SECONDS) -> str:
    """Join the chunks overlapping [center - span, center + span]."""
    lo, hi = center_s - span_s, center_s + span_s
    return " ".join(
        text for start, text in chunks if start + CHUNK_SECONDS > lo and start < hi
    )


def target_present(text: str, target: str) -> bool:
    if not target.strip():
        return False
    pattern = rf"(?<!\w){re.escape(target.strip())}(?!\w)"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def _case_centers(case: base.TuningCase, fragment: Fragment) -> list[int]:
    word = case.asr_word.casefold()
    return [ts - fragment.start_s for ts, w in fragment.mentions if w.casefold() == word]


def score_variant(
    variant: AsrVariant,
    cases: list[base.TuningCase],
    fragments: dict[str, Fragment],
    transcripts: dict[str, str],
    normalize_fn: base.NormalizeFn,
    *,
    seconds: float = 0.0,
    window_s: int = DEFAULT_WINDOW_SECONDS,
) -> VariantScore:
    """Score one variant. ``transcripts`` is keyed by fragment key; a missing key = no audio."""
    parsed = {key: parse_timestamped_transcript(text) for key, text in transcripts.items()}
    outcomes: list[CaseOutcome] = []
    replacements = 0
    for text in transcripts.values():
        replacements += len(normalize_fn(text)[1])

    for case in cases:
        key = _key(case.klant)
        chunks = parsed.get(key)
        fragment = fragments.get(key)
        outcome = CaseOutcome(case.klant, case.asr_word, case.target, case.status, scored=False)
        if chunks is not None and fragment is not None:
            centers = _case_centers(case, fragment)
            if centers:
                raw = " ".join(window_text(chunks, c, window_s) for c in centers)
            else:
                raw = " ".join(text for _, text in chunks)
            normalized, _ = normalize_fn(raw)
            outcome.scored = True
            outcome.present_before = target_present(raw, case.target)
            outcome.present_after = target_present(normalized, case.target)
        outcomes.append(outcome)

    scored = [o for o in outcomes if o.scored]
    goed = [o for o in scored if o.status == "goed"]
    fout = [o for o in scored if o.status == "fout"]
    return VariantScore(
        label=variant.label,
        cases_total=len(cases),
        cases_scored=len(scored),
        cases_unscored=len(cases) - len(scored),
        before_hits=sum(o.present_before for o in scored),
        after_hits=sum(o.present_after for o in scored),
        goed_scored=len(goed),
        goed_broken=sum(o.present_before and not o.present_after for o in goed),
        fout_scored=len(fout),
        fout_repaired=sum(not o.present_before and o.present_after for o in fout),
        replacements_in_fragments=replacements,
        seconds=seconds,
        fragments_transcribed=len(transcripts),
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------
# Transcription through the product path
# ---------------------------------------------------------------------------


def make_product_transcriber(
    models_dir: Path, binary: str | None = None, server_binary: str | None = None
) -> TranscribeFn:
    """Transcribe via ``scripts/transcribe_file._transcribe`` with env-driven variant settings."""
    from scripts.transcribe_file import _transcribe

    def transcribe(wav: Path, variant: AsrVariant) -> str:
        os.environ["WHISPER_BACKEND"] = "whisper.cpp"
        os.environ["WHISPER_VOCABULARY_ENABLED"] = "true" if variant.vocabulary else "false"
        os.environ["WHISPER_CPP_MODEL_PATH"] = str(models_dir / f"ggml-{variant.model}.bin")
        if binary:
            os.environ["WHISPER_CPP_BINARY"] = binary
        if server_binary:
            os.environ["WHISPER_CPP_SERVER_BINARY"] = server_binary
        return asyncio.run(
            _transcribe(wav, backend_name="whisper.cpp", chunk_seconds=CHUNK_SECONDS,
                        include_timestamps=True, model_name=variant.model)
        )

    return transcribe


def transcribe_variant(
    variant: AsrVariant,
    fragments: dict[str, Fragment],
    audio_by_key: dict[str, Path],
    out_dir: Path,
    transcribe_fn: TranscribeFn,
) -> tuple[dict[str, str], float]:
    """Transcribe every fragment that has audio. Raw text is cached under ``out_dir``."""
    variant_dir = out_dir / variant.label
    variant_dir.mkdir(parents=True, exist_ok=True)
    transcripts: dict[str, str] = {}
    seconds = 0.0
    for key, audio in audio_by_key.items():
        fragment = fragments[key]
        cache = variant_dir / f"{key}.txt"
        timing = variant_dir / f"{key}.seconds"
        if cache.exists():
            transcripts[key] = cache.read_text(encoding="utf-8")
            if timing.exists():
                seconds += float(timing.read_text(encoding="utf-8"))
            continue
        wav = cut_fragment(audio, fragment.start_s, fragment.end_s, out_dir / f"{key}.wav")
        started = time.monotonic()
        text = transcribe_fn(wav, variant)
        elapsed = time.monotonic() - started
        seconds += elapsed
        cache.write_text(text, encoding="utf-8")
        timing.write_text(f"{elapsed:.3f}", encoding="utf-8")
        transcripts[key] = text
        print(f"  [{variant.label}] {len(transcripts)}/{len(audio_by_key)} fragments, {seconds:.0f}s", flush=True)
    return transcripts, seconds


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_table(scores: list[VariantScore], fp_per_1000: float, held_out_words: int) -> str:
    lines = [
        "| variant | scored | before | after | goed broken | fout repaired | FP/1000 words | seconds |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(
            f"| {s.label} | {s.cases_scored}/{s.cases_total} | "
            f"{s.before_hits}/{s.cases_scored} = {s.score_before:.3f} | "
            f"{s.after_hits}/{s.cases_scored} = {s.score_after:.3f} | "
            f"{s.goed_broken}/{s.goed_scored} | {s.fout_repaired}/{s.fout_scored} | "
            f"{fp_per_1000:.3f} | {s.seconds:.0f} |"
        )
    lines.append("")
    lines.append(f"Held-out corpus: {held_out_words} words (same for every variant).")
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--tuning-set", required=True, type=Path)
    parser.add_argument("--listening-list", required=True, type=Path)
    parser.add_argument("--held-out-db", required=True, type=Path)
    parser.add_argument("--fireflies-root", required=True, type=Path)
    parser.add_argument("--models-dir", required=True, type=Path, help="Directory holding ggml-<model>.bin files")
    parser.add_argument(
        "--out-dir", required=True, type=Path, help="Gitignored directory for raw transcripts and outcomes"
    )
    parser.add_argument("--variants", nargs="+", choices=sorted(VARIANTS), required=True)
    parser.add_argument("--whisper-cpp-binary", default=None)
    parser.add_argument(
        "--whisper-cpp-server-binary", default=None, help="whisper-server used by the product path (else one-shot CLI)"
    )
    parser.add_argument(
        "--normalization-config", type=Path, default=REPO_ROOT / "config" / "transcript_normalization.yaml"
    )
    parser.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--min-words-per-session", type=int, default=50)
    return parser


def main(argv: list[str] | None = None, transcribe_fn: TranscribeFn | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    lists = load_normalization_lists(args.normalization_config)

    def normalize_fn(text: str) -> tuple[str, list[tuple[str, str]]]:
        new_text, replacements = normalize(text, lists)
        return new_text, [(src, dst) for src, dst, _rule in replacements]

    cases = base.parse_tuning_set(args.tuning_set)
    fragments = {_key(f.name): f for f in parse_listening_list(args.listening_list)}
    audio_by_key: dict[str, Path] = {}
    missing_keys: list[str] = []
    for key, fragment in fragments.items():
        audio = find_audio(fragment.name, args.fireflies_root)
        if audio is None:
            missing_keys.append(key)
        else:
            audio_by_key[key] = audio

    selection = base.select_held_out_sessions(
        args.held_out_db,
        min_words_per_session=args.min_words_per_session,
        exclude_klanten=base.klant_names_from_tuning_set(args.tuning_set),
        exclude_session_ids=set(),
    )
    held_out = base.measure_held_out(selection, normalize_fn)
    fp_per_1000 = held_out.false_positive_count / held_out.word_count * 1000 if held_out.word_count else 0.0

    transcribe = transcribe_fn or make_product_transcriber(
        args.models_dir, args.whisper_cpp_binary, args.whisper_cpp_server_binary
    )
    scores: list[VariantScore] = []
    for label in args.variants:
        variant = VARIANTS[label]
        transcripts, seconds = transcribe_variant(variant, fragments, audio_by_key, args.out_dir, transcribe)
        score = score_variant(variant, cases, fragments, transcripts, normalize_fn,
                              seconds=seconds, window_s=args.window_seconds)
        scores.append(score)
        (args.out_dir / f"{label}-outcomes.json").write_text(
            json.dumps([asdict(o) for o in score.outcomes], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(render_table(scores, fp_per_1000, held_out.word_count))
    print(
        f"\nFragments in listening list: {len(fragments)}; "
        f"with audio: {len(audio_by_key)}; without audio: {len(missing_keys)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
