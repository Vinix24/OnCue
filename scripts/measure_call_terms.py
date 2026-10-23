#!/usr/bin/env python3
"""Per-conversation term lists scored through the post-ASR normalization layer.

Answers: how many entities become correct when OnCue hands the correction layer a
per-conversation term list (``klant.yaml`` ``termen``, passed as ``call_terms``), and how
much of that a list a seller can make before the call delivers, against a list holding
every right answer?

Arms, each scored on already existing transcripts (no re-transcription, the call terms
only act in ``normalize()``):

* ``a``  no list: the baseline of the fixed layer.
* ``b``  realistic list: ``<lists-dir>/b/<fragment>/klant.yaml`` plus ``provenance.json``.
  Every term carries a source and the moment that source file was last saved. A source
  saved at or after the call start is refused (``validate_provenance``); a date without a
  time counts as the end of that day.
* ``c``  upper bound: every correct spelling from the review list as a term.
* ``c-oracle``  the same plus an exact ``<asr word> -> <spelling>`` variant for every
  judged case: what the layer does when it is told each answer by name.
* ``b-contacts``  the ``b`` list with every word of every ``contactpersonen`` entry added
  as a term. The product does NOT do this: ``hub_core._apply_klant_config`` sets
  ``call_terms = klant.termen`` only. The arm shows what wiring contactpersonen in would
  add (a two-word name as one term would never be fuzzy-matched, so it is split).

Own-side arms (deel 3): one fixed list for the own side (own company, colleagues, own
methodology and products), applied to every conversation, on top of the same layer:

* ``e``    the frozen own-side list alone, on every conversation. The list lives outside the
  repo (``--eigen-dir``: ``klant.yaml``, ``provenance.json``, ``freeze.json``). ``freeze.json``
  carries the moment the list was frozen and the sha256 of both files; a file that no longer
  matches its hash is refused, and every term needs a source saved before the freeze.
* ``e+b``  the own-side list plus list ``b`` of that conversation.
* ``g``    ``e`` plus fixed correction pairs (``source -> target``) read from
  ``<eigen-dir>/g-pairs.yaml``, built from OTHER conversations than the measured ones.

Own-side cases are the judged cases whose correct spelling is (a word of) a term of list ``e``.
Because the own-side list applies to every conversation, every arm is also run over the
held-out corpus: replacements there are false corrections, reported per 1000 words, with the
arm-``a`` figure as the baseline.

The derivation mirrors the product: the file is validated with ``KlantConfig``, the
entries go through ``sanitize_call_terms`` and ``with_call_terms``. Entry syntax is
``"Term"`` (fuzzy/merge target, 5+ characters fuzzy) or ``"source -> target"`` (exact
variant). A bare multi-word canonical term is never fuzzy-matched, so the upper bound
writes a multi-word spelling as ``<asr word> -> <spelling>``.

Per arm and transcript the output holds: score before and after the layer, correct
spellings the layer breaks, and replacements outside the judged cases (replacements in
transcript chunks that overlap no judged case window) as a measure of false corrections,
with the baseline-arm figure next to it so the extra false corrections of a list show.

Scoring reuses ``scripts/measure_entities_per_model.score_variant``.

Per-fragment results contain customer names. They go to ``--out-dir`` (expected to be
gitignored) the moment a fragment is done. Stdout carries aggregated counts only.

Run from the repository root as a module (``PYTHONPATH=src python -m
scripts.measure_call_terms ...``).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from sales_copilot.core.klant_config import KlantConfig
from sales_copilot.modules.transcriber.normalize import (
    NormalizationLists,
    load_normalization_lists,
    normalize,
    sanitize_call_terms,
    with_call_terms,
)
from scripts import measure_entities_per_model as m
from scripts import measure_transcript_normalization as base

REPO_ROOT = Path(__file__).resolve().parent.parent
FUZZY_MIN_TERM_LEN = 5
ARMS = ("a", "b", "c", "c-oracle", "b-contacts", "e", "e+b", "g")
OWN_ARMS = ("e", "e+b", "g")
SHARED_LIST_ARMS = ("a", "e", "g")
ARROW = "->"
OWN_WORD_MIN = 4


class ProvenanceError(ValueError):
    """A listed term has no usable source, or a source is not from before the call."""


class FrozenListError(ValueError):
    """The own-side list was changed after it was frozen, or its freeze record is unusable."""


# ---------------------------------------------------------------------------
# Deriving call_terms the way the product does
# ---------------------------------------------------------------------------


def derive_call_terms(klant_yaml: dict[str, Any], *, include_contacts: bool = False) -> tuple[str, ...]:
    """Reproduce ``hub_core._apply_klant_config``: validated ``termen`` -> ``call_terms``.

    ``contactpersonen`` are ignored by the product, so they only count when
    ``include_contacts`` is set (the counterfactual arm): then each word of a name is a term.
    """
    klant = KlantConfig.model_validate(klant_yaml)
    entries = list(klant.termen)
    if include_contacts:
        entries.extend(word for name in klant.contactpersonen for word in name.split())
    return sanitize_call_terms(entries)


def load_klant_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name}: klant.yaml must be a mapping")
    return raw


def canonical_form(entry: str) -> str | None:
    """The spelling an entry steers towards: the term itself, or the target of ``a -> b``."""
    source, arrow, target = entry.partition(ARROW)
    if arrow:
        return target.strip() if source.strip() and target.strip() else None
    return entry.strip() or None


def make_normalize_fn(lists: NormalizationLists) -> base.NormalizeFn:
    def fn(text: str) -> tuple[str, list[tuple[str, str]]]:
        new_text, replacements = normalize(text, lists)
        return new_text, [(src, dst) for src, dst, _rule in replacements]

    return fn


# ---------------------------------------------------------------------------
# Lists: upper bound and provenance
# ---------------------------------------------------------------------------


def upper_bound_terms(cases: list[base.TuningCase], klant_key: str, *, oracle: bool = False) -> list[str]:
    """Every correct spelling of one conversation as a term (``c`` and ``c-oracle`` arms).

    A single-token spelling is a canonical term. A multi-word spelling is written as an
    exact variant from the ASR word of its case, since a bare two-word term is not
    fuzzy-matched. With ``oracle`` every case also gets its exact ``asr -> spelling``
    variant. Duplicates are dropped, order kept.
    """
    entries: dict[str, None] = {}
    for case in cases:
        if m._key(case.klant) != klant_key or not case.target.strip():
            continue
        target = case.target.strip()
        if (oracle or " " in target) and case.asr_word.strip() and case.asr_word.strip() != target:
            entries[f"{case.asr_word.strip()} {ARROW} {target}"] = None
        else:
            entries[target] = None
    return list(entries)


def _moment(value: Any, *, end_of_day: bool) -> datetime.datetime:
    """Parse ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM[:SS]`` as a naive local moment."""
    text = str(value)
    if "T" in text:
        return datetime.datetime.fromisoformat(text)
    day = datetime.date.fromisoformat(text)
    return datetime.datetime.combine(day, datetime.time.max if end_of_day else datetime.time.min)


def validate_provenance(provenance: dict[str, Any], terms: list[str]) -> list[dict[str, str]]:
    """Check every term has a source saved before the call started. Returns the entries.

    ``provenance`` is ``{"call_datetime": "...", "entries": [{"term", "source",
    "source_datetime"}]}``. A date without a time counts as the end of that day for a
    source and the start of that day for the call, so a source on the call day needs a
    time to pass. A term without an entry, a source at or after the call start and an
    unparsable moment all raise: a doubtful source is left out of the list, not kept.
    """
    try:
        call_start = _moment(provenance["call_datetime"], end_of_day=False)
    except (KeyError, ValueError) as exc:
        raise ProvenanceError("provenance needs a call_datetime in YYYY-MM-DD or YYYY-MM-DDTHH:MM") from exc
    entries = provenance.get("entries")
    if not isinstance(entries, list):
        raise ProvenanceError("provenance needs an entries list")
    by_term: dict[str, dict[str, str]] = {}
    for number, entry in enumerate(entries, start=1):
        try:
            term, source = str(entry["term"]), str(entry["source"])
            saved = _moment(entry["source_datetime"], end_of_day=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProvenanceError(f"provenance entry {number} lacks term, source or a valid source_datetime") from exc
        if not source.strip():
            raise ProvenanceError(f"provenance entry {number} has an empty source")
        if saved >= call_start:
            raise ProvenanceError(f"provenance entry {number} is dated at or after the call start")
        by_term[term] = {"term": term, "source": source, "source_datetime": str(entry["source_datetime"])}
    missing = [t for t in terms if t not in by_term]
    if missing:
        raise ProvenanceError(f"{len(missing)} term(s) have no provenance entry")
    return [by_term[t] for t in terms]


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


@dataclass
class Coverage:
    targets: int
    covered: int
    terms: int
    short_terms: int
    variant_terms: int
    inert_terms: int = 0
    covered_usable: int = 0

    @property
    def share(self) -> float:
        return self.covered / self.targets if self.targets else 0.0


def coverage(call_terms: tuple[str, ...], cases: list[base.TuningCase], klant_key: str) -> Coverage:
    """How much of the correct spellings a term list holds.

    A distinct target counts as covered when it equals (case-insensitive) the canonical
    form of an entry, and as usable when that entry is one the layer can act on.
    ``short_terms`` are bare canonical terms under five characters: they are only corrected
    exactly, never fuzzy. ``variant_terms`` are ``a -> b`` entries. ``inert_terms`` are bare
    multi-word terms: the layer never fuzzy-matches those, it only merges their words when
    they are glued together.
    """
    targets = {c.target.strip().casefold() for c in cases if m._key(c.klant) == klant_key and c.target.strip()}
    canon = {form.casefold() for e in call_terms if (form := canonical_form(e))}
    usable = {form.casefold() for e in call_terms if (form := canonical_form(e)) and (ARROW in e or " " not in form)}
    short = sum(1 for e in call_terms if ARROW not in e and len(e.strip()) < FUZZY_MIN_TERM_LEN)
    variants = sum(1 for e in call_terms if ARROW in e)
    inert = sum(1 for e in call_terms if ARROW not in e and " " in e.strip())
    return Coverage(len(targets), len(targets & canon), len(call_terms), short, variants, inert, len(targets & usable))


# ---------------------------------------------------------------------------
# Scoring one arm on one transcript
# ---------------------------------------------------------------------------


@dataclass
class ArmResult:
    arm: str
    transcript_set: str
    fragment: str
    cases_scored: int
    before_hits: int
    after_hits: int
    goed_scored: int
    goed_broken: int
    fout_repaired: int
    fout_scored: int
    replacements_total: int
    replacements_outside: int
    outside_pairs: list[tuple[str, str]] = field(default_factory=list)
    own_scored: int = 0
    own_before: int = 0
    own_after: int = 0


def _case_window_chunks(
    chunks: list[tuple[int, str]],
    cases: list[base.TuningCase],
    fragment: m.Fragment,
    window_s: int,
) -> set[int]:
    """Indices of chunks that overlap the window of any judged case that has a timestamp."""
    covered: set[int] = set()
    for case in cases:
        for center in m._case_centers(case, fragment):
            lo, hi = center - window_s, center + window_s
            for index, (start, _text) in enumerate(chunks):
                if start + m.CHUNK_SECONDS > lo and start < hi:
                    covered.add(index)
    return covered


def outside_replacements(
    text: str,
    cases: list[base.TuningCase],
    fragment: m.Fragment,
    normalize_fn: base.NormalizeFn,
    window_s: int = m.DEFAULT_WINDOW_SECONDS,
) -> list[tuple[str, str]]:
    """Replacements the layer makes in chunks that overlap no judged case window."""
    chunks = m.parse_timestamped_transcript(text)
    inside = _case_window_chunks(chunks, cases, fragment, window_s)
    pairs: list[tuple[str, str]] = []
    for index, (_start, chunk_text) in enumerate(chunks):
        if index not in inside:
            pairs.extend(normalize_fn(chunk_text)[1])
    return pairs


def score_arm(
    arm: str,
    transcript_set: str,
    key: str,
    text: str,
    cases: list[base.TuningCase],
    fragment: m.Fragment,
    lists: NormalizationLists,
    *,
    window_s: int = m.DEFAULT_WINDOW_SECONDS,
    own_targets: frozenset[str] = frozenset(),
) -> ArmResult:
    """Score one fragment transcript with ``lists`` as the layer (reusing ``score_variant``).

    ``own_targets`` are casefolded spellings of the own side: scored outcomes whose target is
    in it are counted separately.
    """
    normalize_fn = make_normalize_fn(lists)
    fragment_cases = [c for c in cases if m._key(c.klant) == key]
    score = m.score_variant(
        m.AsrVariant(arm, "n/a", False), fragment_cases, {key: fragment}, {key: text}, normalize_fn, window_s=window_s
    )
    pairs = outside_replacements(text, fragment_cases, fragment, normalize_fn, window_s)
    own = [o for o in score.outcomes if o.scored and o.target.strip().casefold() in own_targets]
    return ArmResult(
        arm=arm,
        transcript_set=transcript_set,
        fragment=key,
        cases_scored=score.cases_scored,
        before_hits=score.before_hits,
        after_hits=score.after_hits,
        goed_scored=score.goed_scored,
        goed_broken=score.goed_broken,
        fout_repaired=score.fout_repaired,
        fout_scored=score.fout_scored,
        replacements_total=score.replacements_in_fragments,
        replacements_outside=len(pairs),
        outside_pairs=pairs,
        own_scored=len(own),
        own_before=sum(o.present_before for o in own),
        own_after=sum(o.present_after for o in own),
    )


# ---------------------------------------------------------------------------
# Own-side list: frozen file, own-side cases, collisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EigenList:
    """The frozen own-side list: its ``termen`` entries, validated provenance and freeze record."""

    entries: tuple[str, ...]
    provenance: tuple[dict[str, str], ...]
    frozen_at: str
    klant_yaml_sha256: str

    def short_terms(self) -> int:
        return sum(1 for e in self.entries if ARROW not in e and len(e.strip()) < FUZZY_MIN_TERM_LEN)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_eigen_list(directory: Path) -> EigenList:
    """Load the own-side list and prove it is the list that was frozen.

    ``freeze.json`` holds ``frozen_at`` (``YYYY-MM-DDTHH:MM:SS``) and the sha256 of
    ``klant.yaml`` and ``provenance.json``. A changed file raises ``FrozenListError``. Every
    entry (term or ``a -> b`` variant) needs a source saved before ``frozen_at``.
    """
    try:
        freeze = json.loads((directory / "freeze.json").read_text(encoding="utf-8"))
        frozen_at = str(freeze["frozen_at"])
        expected = {"klant.yaml": freeze["klant_yaml_sha256"], "provenance.json": freeze["provenance_sha256"]}
    except (OSError, KeyError, ValueError) as exc:
        raise FrozenListError(f"{directory}: freeze.json missing or without frozen_at and both hashes") from exc
    for name, digest in expected.items():
        if _sha256(directory / name) != digest:
            raise FrozenListError(f"{name} no longer matches the hash it was frozen with")
    klant_yaml = load_klant_yaml(directory / "klant.yaml")
    provenance = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
    entries = derive_call_terms(klant_yaml)
    checked = validate_provenance({**provenance, "call_datetime": frozen_at}, list(entries))
    return EigenList(entries, tuple(checked), frozen_at, str(expected["klant.yaml"]))


def load_pairs(path: Path) -> tuple[str, ...]:
    """Correction pairs for arm ``g``: ``pairs`` is a list of ``{"pair": "a -> b", "source": "..."}``.

    Every pair needs both sides and a non-empty source, otherwise the file is refused.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = raw.get("pairs") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise ProvenanceError(f"{path.name}: needs a pairs list")
    pairs: list[str] = []
    for number, item in enumerate(items, start=1):
        pair = str(item.get("pair", "")) if isinstance(item, dict) else ""
        source, arrow, target = pair.partition(ARROW)
        if not (arrow and source.strip() and target.strip()):
            raise ProvenanceError(f"pair {number} is not 'source -> target'")
        if not str(item.get("source", "")).strip():
            raise ProvenanceError(f"pair {number} has an empty source")
        pairs.append(f"{source.strip()} {ARROW} {target.strip()}")
    return sanitize_call_terms(pairs)


def own_side_targets(entries: tuple[str, ...]) -> frozenset[str]:
    """Casefolded spellings that count as own-side cases: each canonical form and, for a
    multi-word form, each of its words of at least ``OWN_WORD_MIN`` characters."""
    out: set[str] = set()
    for entry in entries:
        form = canonical_form(entry)
        if not form:
            continue
        out.add(form.casefold())
        words = form.split()
        if len(words) > 1:
            out.update(w.casefold() for w in words if len(w) >= OWN_WORD_MIN)
    return frozenset(out)


def collision_probe(
    lists: NormalizationLists, words: list[str], baseline: NormalizationLists | None = None
) -> list[tuple[str, str]]:
    """Words (e.g. customer names or ordinary words) the list makes the layer rewrite.

    A rewrite the fixed layer already makes without the list (``baseline``) is not a collision
    of the list and is left out.
    """

    def rewrites(active: NormalizationLists) -> list[tuple[str, str]]:
        hits: list[tuple[str, str]] = []
        for word in dict.fromkeys(w for w in words if w.strip()):
            hits.extend((src, dst) for src, dst, _rule in normalize(word, active)[1])
        return hits

    already = set(rewrites(baseline)) if baseline is not None else set()
    return [hit for hit in rewrites(lists) if hit not in already]


# ---------------------------------------------------------------------------
# Held-out false corrections
# ---------------------------------------------------------------------------


@dataclass
class HeldOutResult:
    arm: str
    scope: str  # "all" for a list that applies to every conversation, else the fragment key
    words: int
    replacements: int
    pairs: Counter[tuple[str, str]] = field(default_factory=Counter)

    @property
    def per_1000(self) -> float:
        return self.replacements / self.words * 1000 if self.words else 0.0


def held_out_false_corrections(
    arm: str, scope: str, selection: base.HeldOutSelection, lists: NormalizationLists
) -> HeldOutResult:
    """Replacements the layer makes on the held-out corpus. None of them should happen."""
    normalize_fn = make_normalize_fn(lists)
    pairs: Counter[tuple[str, str]] = Counter()
    for session in selection.sessions:
        pairs.update(normalize_fn(session.text)[1])
    return HeldOutResult(arm, scope, selection.total_words, sum(pairs.values()), pairs)


# ---------------------------------------------------------------------------
# Aggregation and reporting
# ---------------------------------------------------------------------------


@dataclass
class Aggregate:
    arm: str
    transcript_set: str
    cases_scored: int = 0
    before_hits: int = 0
    after_hits: int = 0
    goed_scored: int = 0
    goed_broken: int = 0
    fout_repaired: int = 0
    fout_scored: int = 0
    replacements_outside: int = 0
    fragments: int = 0
    own_scored: int = 0
    own_before: int = 0
    own_after: int = 0


def aggregate(results: list[ArmResult]) -> list[Aggregate]:
    """Sum fragment results per (arm, transcript set), arm order kept."""
    sums: dict[tuple[str, str], Aggregate] = {}
    for r in results:
        agg = sums.setdefault((r.arm, r.transcript_set), Aggregate(r.arm, r.transcript_set))
        agg.cases_scored += r.cases_scored
        agg.before_hits += r.before_hits
        agg.after_hits += r.after_hits
        agg.goed_scored += r.goed_scored
        agg.goed_broken += r.goed_broken
        agg.fout_repaired += r.fout_repaired
        agg.fout_scored += r.fout_scored
        agg.replacements_outside += r.replacements_outside
        agg.own_scored += r.own_scored
        agg.own_before += r.own_before
        agg.own_after += r.own_after
        agg.fragments += 1
    return list(sums.values())


def held_out_rate(results: list[HeldOutResult], arm: str) -> tuple[float, int] | None:
    """Mean false corrections per 1000 words and their total count for one arm.

    A list that is the same for every conversation is measured once; per-conversation lists
    (``b``, ``e+b``) are measured per fragment and averaged over the fragments.
    """
    rows = [r for r in results if r.arm == arm]
    if not rows:
        return None
    return sum(r.per_1000 for r in rows) / len(rows), sum(r.replacements for r in rows)


def render_report(aggregates: list[Aggregate], held_out: list[HeldOutResult] | None = None) -> str:
    arms = list(dict.fromkeys(a.arm for a in aggregates))
    sets = list(dict.fromkeys(a.transcript_set for a in aggregates))
    by = {(a.arm, a.transcript_set): a for a in aggregates}
    baseline = {s: by[("a", s)].replacements_outside for s in sets if ("a", s) in by}
    set_cols = " | ".join(f"{s} (after/before)" for s in sets)
    own_cols = " | ".join(f"own {s} (after/scored)" for s in sets)
    header = (
        f"| arm | {set_cols} | mean after | {own_cols} | goed broken (sum) "
        "| outside replacements (sum) | extra vs a | held-out FP/1000 | held-out FP count |"
    )
    lines = [header, "|" + "---|" * (2 * len(sets) + 7)]
    for arm in arms:
        cells = [by[(arm, s)] for s in sets if (arm, s) in by]
        per_set = " | ".join(f"{c.after_hits}/{c.cases_scored} ({c.before_hits})" for c in cells)
        own = " | ".join(f"{c.own_after}/{c.own_scored} ({c.own_before})" for c in cells)
        mean_after = sum(c.after_hits for c in cells) / len(cells)
        broken = sum(c.goed_broken for c in cells)
        outside = sum(c.replacements_outside for c in cells)
        extra = outside - sum(baseline.get(c.transcript_set, 0) for c in cells)
        rate = held_out_rate(held_out or [], arm)
        fp = f"{rate[0]:.3f} | {rate[1]}" if rate else "n/a | n/a"
        lines.append(f"| {arm} | {per_set} | {mean_after:.1f} | {own} | {broken} | {outside} | {extra:+d} | {fp} |")
    return "\n".join(lines)


def render_collisions(held_out: list[HeldOutResult], own_entries: tuple[str, ...], top: int = 15) -> str:
    """Held-out replacements per arm that land on an own-side spelling, plus the most frequent pairs."""
    targets = own_side_targets(own_entries)
    lines = ["| arm | pair (source -> result) | held-out count | lands on own side |", "|---|---|---|---|"]
    for arm in dict.fromkeys(r.arm for r in held_out):
        merged: Counter[tuple[str, str]] = Counter()
        for r in held_out:
            if r.arm == arm:
                merged.update(r.pairs)
        for (src, dst), count in merged.most_common(top):
            lines.append(f"| {arm} | {src} -> {dst} | {count} | {'yes' if dst.casefold() in targets else 'no'} |")
    return "\n".join(lines)


def render_coverage(rows: list[tuple[str, Coverage]]) -> str:
    lines = [
        "| fragment | terms | targets | covered | usable | short (<5) | variants | inert |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key, c in rows:
        lines.append(
            f"| {key} | {c.terms} | {c.targets} | {c.covered} | {c.covered_usable} | "
            f"{c.short_terms} | {c.variant_terms} | {c.inert_terms} |"
        )
    total = Coverage(
        sum(c.targets for _, c in rows),
        sum(c.covered for _, c in rows),
        sum(c.terms for _, c in rows),
        sum(c.short_terms for _, c in rows),
        sum(c.variant_terms for _, c in rows),
        sum(c.inert_terms for _, c in rows),
        sum(c.covered_usable for _, c in rows),
    )
    lines.append(
        f"| total | {total.terms} | {total.targets} | {total.covered} ({total.share:.0%}) | "
        f"{total.covered_usable} | {total.short_terms} | {total.variant_terms} | {total.inert_terms} |"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def parse_transcript_specs(specs: list[str]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for spec in specs:
        label, sep, path = spec.partition("=")
        if not sep or not label or not path:
            raise ValueError(f"--transcripts expects LABEL=DIR, got {spec!r}")
        out[label] = Path(path)
    return out


def arm_call_terms(
    arm: str,
    key: str,
    cases: list[base.TuningCase],
    lists_dir: Path,
    eigen: EigenList | None = None,
    pairs: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    """The call terms of one arm for one fragment, plus its provenance entries (arm b only)."""
    if arm == "a":
        return (), []
    if arm in OWN_ARMS:
        if eigen is None:
            raise FrozenListError(f"arm {arm} needs --eigen-dir")
        if arm == "e":
            return eigen.entries, []
        if arm == "g":
            return sanitize_call_terms([*eigen.entries, *pairs]), []
        klant_yaml = load_klant_yaml(lists_dir / "b" / key / "klant.yaml")
        return sanitize_call_terms([*eigen.entries, *derive_call_terms(klant_yaml)]), []
    if arm in ("c", "c-oracle"):
        entries = upper_bound_terms(cases, key, oracle=arm == "c-oracle")
        write_atomic(
            lists_dir / arm / key / "klant.yaml",
            yaml.safe_dump({"bedrijf": key, "termen": entries}, allow_unicode=True, sort_keys=False),
        )
        return derive_call_terms({"bedrijf": key, "termen": entries}), []
    klant_dir = lists_dir / "b" / key
    klant_yaml = load_klant_yaml(klant_dir / "klant.yaml")
    terms = derive_call_terms(klant_yaml, include_contacts=arm == "b-contacts")
    provenance = json.loads((klant_dir / "provenance.json").read_text(encoding="utf-8"))
    declared = list(klant_yaml.get("termen") or []) + list(klant_yaml.get("contactpersonen") or [])
    return terms, validate_provenance(provenance, declared)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--tuning-set", required=True, type=Path)
    parser.add_argument("--listening-list", required=True, type=Path)
    parser.add_argument(
        "--lists-dir", required=True, type=Path, help="Holds b/<fragment>/klant.yaml; c/ and c-oracle/ are written here"
    )
    parser.add_argument("--transcripts", nargs="+", required=True, metavar="LABEL=DIR")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument(
        "--arms",
        nargs="+",
        choices=ARMS,
        default=None,
        help="Default: every list arm; e and e+b only with --eigen-dir, g only with --pairs as well",
    )
    parser.add_argument(
        "--normalization-config", type=Path, default=REPO_ROOT / "config" / "transcript_normalization.yaml"
    )
    parser.add_argument("--window-seconds", type=int, default=m.DEFAULT_WINDOW_SECONDS)
    parser.add_argument(
        "--eigen-dir", type=Path, default=None, help="Frozen own-side list (klant.yaml, provenance.json, freeze.json)"
    )
    parser.add_argument("--pairs", type=Path, default=None, help="Correction pairs file for arm g")
    parser.add_argument("--held-out-db", type=Path, default=None, help="cases.db for false corrections per 1000 words")
    parser.add_argument("--min-words-per-session", type=int, default=50)
    parser.add_argument("--exclude-session", action="append", default=[], dest="exclude_sessions")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    base_lists = load_normalization_lists(args.normalization_config)
    if not base_lists.enabled:
        print("normalization config missing or disabled: refusing to measure", file=sys.stderr)
        return 2
    if args.arms is None:
        args.arms = [
            arm
            for arm in ARMS
            if arm not in OWN_ARMS or (args.eigen_dir is not None and (arm != "g" or args.pairs is not None))
        ]
    if any(arm in OWN_ARMS for arm in args.arms) and args.eigen_dir is None:
        print("arms e, e+b and g need --eigen-dir", file=sys.stderr)
        return 2
    if "g" in args.arms and args.pairs is None:
        print("arm g needs --pairs", file=sys.stderr)
        return 2
    eigen = load_eigen_list(args.eigen_dir) if args.eigen_dir is not None else None
    pairs = load_pairs(args.pairs) if args.pairs is not None else ()
    own_targets = own_side_targets(eigen.entries) if eigen else frozenset()
    cases = base.parse_tuning_set(args.tuning_set)
    fragments = {m._key(f.name): f for f in m.parse_listening_list(args.listening_list)}
    sets = parse_transcript_specs(args.transcripts)
    selection = None
    if args.held_out_db is not None:
        selection = base.select_held_out_sessions(
            args.held_out_db,
            min_words_per_session=args.min_words_per_session,
            exclude_klanten=base.klant_names_from_tuning_set(args.tuning_set),
            exclude_session_ids=set(args.exclude_sessions),
        )
    results: list[ArmResult] = []
    held_out: list[HeldOutResult] = []
    coverage_rows: list[tuple[str, Coverage]] = []
    collisions: list[tuple[str, str, str]] = []

    for key in sorted(fragments):
        texts = {
            label: (directory / f"{key}.txt").read_text(encoding="utf-8")
            for label, directory in sets.items()
            if (directory / f"{key}.txt").is_file()
        }
        if not texts:
            continue
        fragment_results: list[ArmResult] = []
        for arm in args.arms:
            call_terms, provenance = arm_call_terms(arm, key, cases, args.lists_dir, eigen, pairs)
            lists = with_call_terms(base_lists, call_terms)
            if arm == "b":
                coverage_rows.append((key, coverage(call_terms, cases, key)))
            shared = arm in SHARED_LIST_ARMS
            if selection is not None and not (shared and any(r.arm == arm for r in held_out)):
                held_out.append(held_out_false_corrections(arm, "all" if shared else key, selection, lists))
            if arm in OWN_ARMS and eigen is not None:
                klant_terms = derive_call_terms(load_klant_yaml(args.lists_dir / "b" / key / "klant.yaml"))
                words = [w for term in klant_terms if (form := canonical_form(term)) for w in form.split()]
                hits = collision_probe(lists, words, base_lists)
                collisions.extend((arm, src, dst) for src, dst in hits)
            for label, text in texts.items():
                fragment_results.append(
                    score_arm(
                        arm,
                        label,
                        key,
                        text,
                        cases,
                        fragments[key],
                        lists,
                        window_s=args.window_seconds,
                        own_targets=own_targets,
                    )
                )
            if provenance:
                write_atomic(
                    args.out_dir / "provenance-checked" / f"{key}-{arm}.json",
                    json.dumps(provenance, ensure_ascii=False, indent=2),
                )
        write_atomic(
            args.out_dir / "per-fragment" / f"{key}.json",
            json.dumps([asdict(r) for r in fragment_results], ensure_ascii=False, indent=2),
        )
        results.extend(fragment_results)
        print(f"  fragment {key}: {len(fragment_results)} arm/transcript scores written", flush=True)

    print(render_report(aggregate(results), held_out))
    if selection is not None:
        print(f"\nheld-out corpus: {len(selection.sessions)} sessions, {selection.total_words} words")
        write_atomic(
            args.out_dir / "held-out-pairs.json",
            json.dumps(
                [
                    {
                        "arm": r.arm,
                        "scope": r.scope,
                        "words": r.words,
                        "pairs": [[s, d, n] for (s, d), n in r.pairs.items()],
                    }
                    for r in held_out
                ],
                ensure_ascii=False,
                indent=2,
            ),
        )
        if eigen is not None:
            print()
            print(render_collisions(held_out, eigen.entries))
    if eigen is not None:
        print(
            f"\nlist e: {len(eigen.entries)} entries, {eigen.short_terms()} bare terms under {FUZZY_MIN_TERM_LEN} "
            f"characters, frozen {eigen.frozen_at}, sha256 {eigen.klant_yaml_sha256}"
        )
        if collisions:
            print("\ncollisions with the per-conversation lists (arm, source -> result):")
            for arm, src, dst in dict.fromkeys(collisions):
                print(f"  {arm}: {src} -> {dst}")
    if coverage_rows:
        print()
        print(render_coverage(coverage_rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
