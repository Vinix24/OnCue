#!/usr/bin/env python3
"""Meetpoort voor contextuele termcorrectie in de uitwerking (objective termenlijst-in-uitwerking, D1).

Meet, voordat er iets in ``enrich_report`` gebouwd wordt, of een model met context kan kiezen
tussen een term uit de termenlijst en een gewoon woord of naam die er net zo uitziet of klinkt
(``ROI`` tegenover de voornaam Roy, ``Teamleader`` tegenover "team leader"), zonder tekst die
al goed stond stuk te maken.

De keten per segment is dezelfde die D2 in ``enrich_report`` kreeg. De voorfilter, de
validatie en de woordverlies-wacht (stap 1, 3 en 4) staan sinds D2 in productie, in
``sales_copilot.modules.reports.term_corrections``; dit harnas importeert ze daaruit. Het
harnas stuurt per aanroep een segment met zijn kandidaten; productie stuurt het hele gesprek
in een aanroep: samen met de rapportvelden, of apart wanneer ``report_terms`` een ander model
heeft dan ``report``.

1. Voorfilter: welke termen uit de lijst lijken op iets in dit segment. Hergebruikt de
   tokenisatie, de begrensde edit-afstand en de ``klant.yaml``-termensyntax
   (``"Term"`` of ``"bron -> Term"``) van ``modules/transcriber/normalize.py``, een stap
   ruimer dan die deterministische laag: die vervangt zelf, dit filter stelt alleen voor.
   Alleen de kandidaten van een segment gaan naar het model, nooit de hele lijst.
2. Het model (taak ``report_terms`` van ``core.llm_routing``) krijgt het segment en zijn kandidaten
   in het gebruikersdeel van de prompt, dus door ``apply_outbound_pii`` met de resolved
   provider. Het noemt per correctie ``segment_index``, ``source``, ``occurrence`` en
   ``target``, nooit een tekenpositie.
3. De code valideert: ``target`` staat in de lijst en was een kandidaat die het model zag,
   ``source`` komt echt voor op die plek, en de positie wordt uitgerekend op het ORIGINELE
   segment. Heeft de PII-strip het aantal voorkomens van ``source`` veranderd, dan is niet
   vast te stellen welk voorkomen het model bedoelde en wordt de correctie geweigerd.

4. Woordverlies-wacht (ronde 2): een correctie mag geen woord weghalen. Tokens die ``source``
   en ``target`` aan begin en eind delen tellen niet mee; blijft er van de target niets over
   terwijl er van de bron wel iets overblijft, of verdwijnt er per saldo een token, dan wordt
   de correctie geweigerd (``4 FTE -> FTE``, ``VWA Excel -> VBA``). Enige uitzondering:
   aangrenzende tokens samenvoegen tot een woord, wanneer de aaneengeschreven bron binnen de
   fuzzy-drempel van de target valt (``Deal Flow -> Dealflow``).

Contextueel is een geval alleen wanneer de deterministische laag het niet al zelf repareert:
een label waarvan ``normalize()`` (de vaste lijst plus deze termen als gesprekstermen) de bron
al in de verwachte term omzet, telt apart als ``deterministic`` en niet mee in de criteria.
Zo'n geval bereikt het rapport in productie nooit in zijn foute vorm.

Twee armen (``--mode``). ``term`` is de lijstgebonden correctie hierboven. ``free`` laat het
model elke verkeerd verstane passage verbeteren, niet alleen lijsttermen, met dezelfde vorm,
dezelfde broncontrole, de positie op het origineel en de woordverlies-wacht; het krijgt elk
segment, ook zonder kandidaten. Die arm wordt gemeten op ``--free-cases``: fouten in lopende
taal die geen lijstterm kan repareren. Per correctie telt een vaste regel of ze inhoud
toevoegt: een nieuw woord dat niet in de bron, de rest van het segment of de kandidaten
staat en geen spellingvariant van een bronwoord is.

``--models`` meet meerdere modellen na elkaar: per model wordt ``REPORT_TERMS_LLM_MODEL`` gezet
en de resolver-taak ``report_terms`` opnieuw opgelost. Die taak erft wat hij zelf niet zet van
``report`` (``REPORT_LLM_*``) en dan van het gesprek, net als in productie. Per aanroep telt
``LLMClient.last_usage`` de tokens; met ``--price`` volgt de prijs per gesprek van 60 minuten.

    # Alleen voorfilter en schaal, geen model, geen netwerk
    python -m scripts.measure_term_corrections --cases cases.json --good-cases lijst.md \\
        --terms termen.yaml --prefilter-only --scale 50,200,1000 --scale-transcript gesprek.txt

    # De meting: 5 runs via het geconfigureerde model (sleutels via de omgeving, nooit geprint)
    python -m scripts.measure_term_corrections --cases cases.json --good-cases lijst.md \\
        --terms termen.yaml --runs 5 --scale 50,200,1000 --scale-transcript gesprek.txt --out result.json

    # Twee modellen, beide armen, met kosten per gesprek
    python -m scripts.measure_term_corrections --cases cases.json --good-cases lijst.md \\
        --terms termen.yaml --free-cases vrij.json --mode term,free --models model-a,model-b \\
        --price model-a=1,5 --price model-b=3,15 --eur-per-usd 0.9 --runs 5 --out result.json

``--cases`` (JSON of YAML) bevat de gelabelde contextuele gevallen:
``{"segments": [{"id", "text", "labels": [{"id", "source", "occurrence", "expected", "name_like"}]}]}``.
``expected`` is de term die er had moeten staan, of ``null`` wanneer ``source`` moet blijven staan.
``--free-cases`` heeft dezelfde vorm; daar is ``expected`` wat er gezegd is, of een lijst van
gelijkwaardige vormen, en nooit ``null``.
``--good-cases`` is de beoordelingslijst; de ``goed``-gevallen daaruit zijn de non-regressieset.

``--probes`` (``tests/fixtures/term_correction_probes.yaml``) meet daarnaast een synthetische
probeset die BUITEN de criteria blijft: de poort-meetset bevat bewust alleen echte gevallen.
Per probe staan de tekst, de termen en per voorkomen de verwachte uitkomst (``correct_to:
<term>`` of ``leave: true``) in het bestand; er is een term tegenover een gewoon woord of een
naam die er hetzelfde uitziet. Per model komen er tellingen en probe-id's uit: goed,
onterecht gecorrigeerd (``leave`` maar toch aangepast), gemist, en modelfouten. Een enkele
doorloop per model, zonder de deterministische laag.

De uitvoer (scherm en ``--out``) bevat alleen tellingen, fracties, tijden, vaste
weigercategorieen en geval-id's. Nooit een zin, een term of een naam uit de invoer. Alleen
``--show-flagged`` schrijft, voor een lokale controle, de correcties die de inhoudregel
markeert met hun tekst naar stderr; nooit naar ``--out``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from sales_copilot.core.config import DetectorConfig, load_env
from sales_copilot.core.llm_routing import ResolvedLLM, build_llm_client, resolve_llm, task_env_keys
from sales_copilot.core.outbound_policy import apply_outbound_pii, resolve_pii_mode
from sales_copilot.core.pii_patterns import NL_VOORNAMEN
from sales_copilot.core.privacy_gate import PrivacyGateError
from sales_copilot.modules.reports.term_corrections import (
    CANDIDATE_SEPARATOR,
    AppliedCorrection,
    TermCorrection,
    TermList,
    TermPrefilter,
    find_occurrences,
    locate_occurrence,
    phonetic_key,
    place_correction,
    validate_corrections,
    word_tokens,
)
from sales_copilot.modules.transcriber.normalize import (
    NormalizationLists,
    _bounded_edit_distance,
    _fuzzy_threshold_for_term,
    load_normalization_lists,
    normalize,
    sanitize_call_terms,
    with_call_terms,
)
from scripts.measure_transcript_normalization import parse_tuning_set

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NORMALIZATION_CONFIG = REPO_ROOT / "config" / "transcript_normalization.yaml"

#: The ``core.llm_routing`` task this measurement resolves its provider, model and timeout from:
#: the term correction's own task, which inherits from ``report`` what it does not set.
TERMS_TASK = "report_terms"

#: Pass criteria, fixed by T0 on 2026-09-30 before the harness existed.
MIN_CONTEXT_CASES = 20
MIN_RECALL_MEDIAN = 0.60
MIN_PREFILTER_RECALL = 0.90
MAX_PREFILTER_MS = 50.0
EXPECTED_GOOD_CASES = 17

#: Pass criteria for the free-correction arm, fixed by T0 on 2026-09-30 before round 2 was built.
EXPECTED_FREE_CASES = 14
MIN_FREE_REPAIRED = 7

#: The two arms: corrections bound to the term list, and free correction of any misheard passage.
MODES = ("term", "free")

#: A 60-minute call at ~150 spoken words per minute.
SIXTY_MINUTE_WORDS = 9000
#: Words per segment when a scale transcript is cut into live-sized segments.
LIVE_SEGMENT_WORDS = 25
SCALE_REPEATS = 5

_TIMESTAMP_PREFIX_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")
_ELLIPSIS_RE = re.compile(r"^\s*(?:\.\.\.|…)\s*|\s*(?:\.\.\.|…)\s*$")
# One segment per request here, so the segment line is always the first line. The
# candidates label starts in lower case on purpose: the PII surname pattern ("Naam van
# Naam") ends on a capitalised word and may span a line break, so a capitalised label could
# be swallowed together with the end of the segment line above it.
_VISIBLE_SEGMENT_RE = re.compile(r"^Segment (\d+): (.*)$")
_VISIBLE_CANDIDATES_RE = re.compile(r"^candidates for segment (\d+): (.*)$")
#: The stand-ins ``pii_filter`` writes for removed personal data ([NAAM], [EMAIL], [DATUM], ...).
_PLACEHOLDER_RE = re.compile(r"\[[A-Z]+\]")

SYSTEM_PROMPT = (
    "You receive numbered segments from the automatic transcript of one sales call. Each "
    "segment is followed by candidate terms: entries from the customer's term list that "
    "resemble something in that segment. Speech recognition sometimes writes such a term as "
    "an ordinary word or a name that looks or sounds alike, for example a product name as two "
    "ordinary words, or an acronym as a first name. Just as often the ordinary word or the name "
    "is exactly what was said.\n\n"
    "For every place where the context makes clear that a candidate term was meant, return one "
    "correction: segment_index, source (the exact text as it appears in that segment, which is "
    "replaced as a whole), occurrence (which occurrence of source in that segment, counting "
    "from 1) and target (the candidate term, written exactly as in the list).\n\n"
    "Correct only when the context shows the term was meant. When a word or a name is plausible "
    "as written, leave it. A person's name stays a name unless the context shows the term was "
    "meant, and a term stays a term unless the context shows a person was meant. Never correct "
    "text that is already right, never rewrite anything else, and never return a target that "
    "is not among the candidates of that segment.\n\n"
    "Text in square brackets, such as [NAAM], stands in for personal data that was removed. "
    "Never correct it and never guess what it replaced. Return an empty list when nothing needs "
    "correcting."
)

FREE_SYSTEM_PROMPT = (
    "You receive numbered segments from the automatic transcript of one sales call. Speech "
    "recognition sometimes mishears a passage: it writes words that sound like what was said "
    "but make no sense in the sentence, splits one word into two, glues words together, or "
    "writes a term or a name as an ordinary word. Each segment is followed by candidate terms: "
    "entries from the customer's term list that resemble something in that segment. That list "
    "may be empty.\n\n"
    "For every passage where the sentence makes clear that something else was said, return one "
    "correction: segment_index, source (the exact text as it appears in that segment, which is "
    "replaced as a whole), occurrence (which occurrence of source in that segment, counting "
    "from 1) and target (what was actually said). The target may be a candidate term or "
    "ordinary words.\n\n"
    "Correct only what was misheard, and only when the sentence shows what was said. The target "
    "must sound like the source: it restores what was spoken and never adds anything that was "
    "not spoken, such as a name, a number, an agreement or a phrase. Never drop a word: source "
    "and target cover the same spoken words. Never correct grammar, word order, filler words, "
    "repetitions or style, because spoken language is not written language. Never correct text "
    "that is already right. When you are not sure, leave it.\n\n"
    "Text in square brackets, such as [NAAM], stands in for personal data that was removed. "
    "Never correct it, never put it in a source or a target, and never guess what it replaced. "
    "Return an empty list when nothing needs correcting."
)


class TermCorrections(BaseModel):
    """The model's answer: only the corrections the context makes clear."""

    corrections: list[TermCorrection] = Field(
        description="Corrections the context makes clear; empty when nothing needs correcting."
    )


class FreeCorrection(TermCorrection):
    """One free correction: the same place anchor, but the target is whatever was said."""

    target: str = Field(description="What was actually said: a candidate term or ordinary words.")
    reason: str = Field(description="A few words on what in the sentence shows what was said.")


class FreeCorrections(BaseModel):
    """The model's answer in the free arm: only the corrections the sentence makes clear."""

    corrections: list[FreeCorrection] = Field(
        description="Corrections the sentence makes clear; empty when nothing needs correcting."
    )


#: Per arm: the system prompt and the structured answer the model must give.
_MODE_PROMPTS: dict[str, tuple[str, type[BaseModel]]] = {
    "term": (SYSTEM_PROMPT, TermCorrections),
    "free": (FREE_SYSTEM_PROMPT, FreeCorrections),
}


@dataclass(frozen=True)
class Label:
    """One labelled place in a segment.

    ``expected`` is the text that should replace ``source`` (a list term, or in the free set
    what was said), or ``None`` when ``source`` must stay as written. ``alternatives`` are
    equally right forms of ``expected`` (free set only). ``occurrence`` is 1-based; ``None``
    protects every occurrence.
    """

    case_id: str
    source: str
    occurrence: int | None
    expected: str | None
    name_like: bool = False
    alternatives: tuple[str, ...] = ()


@dataclass(frozen=True)
class CaseSegment:
    segment_id: str
    text: str
    labels: tuple[Label, ...]
    kind: str  # "context" (--cases), "good" (--good-cases) or "free" (--free-cases)


@dataclass(frozen=True)
class PreparedSegment:
    """A segment ready for the model: what was sent, and what the model saw after PII."""

    segment: CaseSegment
    candidates: tuple[str, ...]
    user_text: str
    visible_text: str
    visible_candidates: tuple[str, ...]


@dataclass(frozen=True)
class ModelRun:
    """The configured model and how often and how wide to call it.

    ``clients`` is a pool with one client per parallel call: a client's ``last_usage`` then
    always belongs to the call that just used it, never to another call finishing at the
    same moment. Its length is the concurrency.
    """

    clients: tuple[Any, ...]
    model: str
    temperature: float
    timeout_s: float
    runs: int
    show_flagged: bool = False


@dataclass(frozen=True)
class Price:
    """What one model costs, in US dollars per million tokens."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float


# ---------------------------------------------------------------------------
# The free arm's validation, applying corrections
# ---------------------------------------------------------------------------
#
# The prefilter, the position anchor and the term arm's validation live in production
# (``sales_copilot.modules.reports.term_corrections``); the free arm is measured here only.


def validate_free_corrections(
    corrections: Sequence[TermCorrection],
    originals: Sequence[str],
    visible: Sequence[str],
) -> tuple[list[AppliedCorrection], Counter[str]]:
    """The free arm: any target, with the same place checks and the word-loss guard.

    There is no list to bind the target to, so what stays is: the segment exists, ``source``
    is really at that place on the original, the PII policy did not change its count, the
    correction removes no word, and the target carries no PII stand-in such as ``[NAAM]``
    (that would write a placeholder into the original).
    """
    accepted: list[AppliedCorrection] = []
    rejected: Counter[str] = Counter()
    taken: dict[int, list[tuple[int, int]]] = {}
    for correction in corrections:
        index = correction.segment_index
        if not 0 <= index < len(originals):
            rejected["segment_out_of_range"] += 1
            continue
        target = correction.target.strip()
        if _PLACEHOLDER_RE.search(target):
            rejected["placeholder_in_target"] += 1
            continue
        placed = place_correction(index, correction.source, correction.occurrence, target, originals, visible, taken)
        if isinstance(placed, str):
            rejected[placed] += 1
            continue
        taken.setdefault(index, []).append(placed)
        accepted.append(AppliedCorrection(segment_index=index, start=placed[0], end=placed[1], target=target))
    return accepted, rejected


def apply_corrections(originals: Sequence[str], accepted: Sequence[AppliedCorrection]) -> list[str]:
    """``originals`` with every accepted correction applied at its own span, nowhere else."""
    result = list(originals)
    for index in range(len(result)):
        for correction in sorted(
            (c for c in accepted if c.segment_index == index), key=lambda c: c.start, reverse=True
        ):
            text = result[index]
            result[index] = text[: correction.start] + correction.target + text[correction.end :]
    return result


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_terms(path: Path) -> tuple[str, ...]:
    """A term list (YAML or JSON): a list, or a mapping with ``termen`` or ``terms``."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("termen", data.get("terms"))
    if not isinstance(data, list) or not data:
        raise ValueError(f"term list must be a non-empty list (or termen/terms): {path}")
    for entry in data:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"every term must be a non-empty string: {path}")
        if "\n" in entry or CANDIDATE_SEPARATOR.strip() in entry:
            raise ValueError(f"a term may not contain a newline or ';': {path}")
    return sanitize_call_terms(data)


def _expected_forms(raw: Any, case_id: str, source: str, free: bool) -> tuple[str, ...]:
    """``expected`` as a tuple of forms: empty for a keep, one form, or (free set) several."""
    if raw is None and not free:
        return ()
    forms = raw if free and isinstance(raw, list) else [raw]
    if not forms or any(not isinstance(form, str) or not form.strip() or form == source for form in forms):
        what = "a non-empty string or list of strings" if free else "null or a term"
        raise ValueError(f"label {case_id}: expected must be {what} different from source")
    return tuple(forms)


def _label(raw: Any, segment_id: str, text: str, free: bool = False) -> Label:
    if not isinstance(raw, dict):
        raise ValueError(f"segment {segment_id}: every label must be a mapping")
    case_id, source, occurrence = raw.get("id"), raw.get("source"), raw.get("occurrence")
    name_like = raw.get("name_like", False)
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"segment {segment_id}: every label needs an id")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"label {case_id}: source must be a non-empty string")
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1:
        raise ValueError(f"label {case_id}: occurrence must be an integer >= 1")
    forms = _expected_forms(raw.get("expected"), case_id, source, free)
    if not isinstance(name_like, bool):
        raise ValueError(f"label {case_id}: name_like must be true or false")
    if locate_occurrence(text, source, occurrence) is None:
        raise ValueError(f"label {case_id}: source does not occur {occurrence} time(s) in its segment")
    return Label(
        case_id=case_id,
        source=source,
        occurrence=occurrence,
        expected=forms[0] if forms else None,
        name_like=name_like,
        alternatives=forms[1:],
    )


def load_cases(path: Path, kind: str = "context") -> list[CaseSegment]:
    """A labelled set (JSON or YAML). Raises ``ValueError`` on any malformed entry.

    ``kind`` ``"context"`` reads the contextual set (``expected`` a term or ``null``);
    ``"free"`` reads the free set, where every label needs what was said, as one form or a
    list of equally right forms.
    """
    if kind not in ("context", "free"):
        raise ValueError(f"unknown case kind {kind!r}")
    free = kind == "free"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw_segments = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError(f"cases file needs a non-empty 'segments' list: {path}")
    segments: list[CaseSegment] = []
    seen: set[str] = set()
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise ValueError("every segment must be a mapping")
        segment_id, text, raw_labels = raw.get("id"), raw.get("text"), raw.get("labels")
        if not isinstance(segment_id, str) or not segment_id.strip() or segment_id in seen:
            raise ValueError(f"segment id missing or duplicated: {segment_id!r}")
        seen.add(segment_id)
        if not isinstance(text, str) or not text.strip() or "\n" in text:
            raise ValueError(f"segment {segment_id}: text must be one non-empty line")
        if not isinstance(raw_labels, list) or not raw_labels:
            raise ValueError(f"segment {segment_id}: needs a non-empty labels list")
        labels = tuple(_label(label, segment_id, text, free) for label in raw_labels)
        for label in labels:
            if label.case_id in seen:
                raise ValueError(f"duplicated id: {label.case_id!r}")
            seen.add(label.case_id)
        segments.append(CaseSegment(segment_id=segment_id, text=text, labels=labels, kind=kind))
    return segments


def check_distinct_ids(*groups: Sequence[CaseSegment]) -> None:
    """Raise ``ValueError`` when two sets share a segment or case id: the outcomes are keyed by id."""
    segment_ids: set[str] = set()
    case_ids: set[str] = set()
    for segments in groups:
        for segment in segments:
            keys = [(segment_ids, segment.segment_id), *((case_ids, label.case_id) for label in segment.labels)]
            for seen, key in keys:
                if key in seen:
                    raise ValueError(f"id {key!r} occurs in more than one set")
                seen.add(key)


def load_good_cases(path: Path) -> tuple[list[CaseSegment], int]:
    """The ``goed`` rows of the beoordelingslijst, each fragment its own segment.

    Returns the segments and the number of ``goed`` rows whose word does not occur in its own
    fragment (those cannot be checked and are reported, not silently dropped).
    """
    segments: list[CaseSegment] = []
    unusable = 0
    goed = [case for case in parse_tuning_set(path) if case.status == "goed"]
    for number, case in enumerate(goed, start=1):
        text = " ".join(_ELLIPSIS_RE.sub("", case.context).split())
        if not find_occurrences(text, case.asr_word):
            unusable += 1
            continue
        case_id = f"good-{number:02d}"
        label = Label(case_id=case_id, source=case.asr_word, occurrence=None, expected=None)
        segments.append(CaseSegment(segment_id=case_id, text=text, labels=(label,), kind="good"))
    return segments, unusable


def load_deterministic_lists(config_path: Path, terms: Sequence[str]) -> NormalizationLists:
    """The deterministic layer as production runs it: the fixed list plus ``terms`` as call terms.

    Refuses a missing config: ``load_normalization_lists`` would silently return a disabled
    layer, and then no case would ever count as deterministic.
    """
    if not config_path.is_file():
        raise ValueError(f"normalization config not found: {config_path}")
    base = load_normalization_lists(config_path)
    if not base.enabled:
        raise ValueError(f"normalization config is disabled: {config_path}")
    return with_call_terms(base, tuple(terms))


def check_expected_in_list(segments: Sequence[CaseSegment], term_list: TermList) -> None:
    """Raise ``ValueError`` when a label expects a term the list does not hold: it could never be hit."""
    for segment in segments:
        for label in segment.labels:
            if label.expected is not None and term_list.canonical(label.expected) != label.expected:
                raise ValueError(f"label {label.case_id}: its expected term is not in the term list")


def load_scale_transcript(paths: Sequence[Path], words: int) -> list[str]:
    """``words`` words of real conversation text, cut into live-sized segments.

    Text files give one segment per line (a leading ``[MM:SS]`` is dropped); JSON files a
    list of strings or of ``{"text": ...}`` segments. Refuses to pad a short transcript by
    repeating it: repeated text would make the token cache look faster than a real call.
    """
    pieces: list[str] = []
    for path in paths:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError(f"scale transcript JSON must be a list: {path}")
            for item in data:
                text = item.get("text") if isinstance(item, dict) else item
                if isinstance(text, str):
                    pieces.append(text)
        else:
            pieces.extend(_TIMESTAMP_PREFIX_RE.sub("", line) for line in path.read_text(encoding="utf-8").splitlines())
    all_words = " ".join(pieces).split()
    if len(all_words) < words:
        raise ValueError(f"scale transcript has {len(all_words)} words; a 60-minute call needs {words}")
    all_words = all_words[:words]
    return [" ".join(all_words[i : i + LIVE_SEGMENT_WORDS]) for i in range(0, len(all_words), LIVE_SEGMENT_WORDS)]


# ---------------------------------------------------------------------------
# Scale: realistic distractor terms (synthetic, no network)
# ---------------------------------------------------------------------------

_PRODUCTS = (
    "Asana", "Monday", "Trello", "Jira", "Confluence", "Notion", "Slack", "Zoom", "Teams", "Outlook",
    "SharePoint", "OneDrive", "Dropbox", "Zendesk", "Freshdesk", "Intercom", "Mailchimp", "ActiveCampaign",
    "Klaviyo", "Moneybird", "Snelstart", "e-Boekhouden", "Yuki", "Nmbrs", "Loket", "Personio", "BambooHR",
    "Recruitee", "Homerun", "Odoo", "Lightspeed", "Shopify", "WooCommerce", "Magento", "Mollie", "Adyen",
    "Stripe", "Zapier", "Make", "Airtable", "Retool", "Power BI", "Tableau", "Looker", "Qlik", "Gripp",
    "Productive", "Harvest", "Toggl", "Clockify", "Calendly", "DocuSign", "Signhost", "Pandadoc",
    "Lusha", "Apollo", "Cognism", "LinkedIn Sales Navigator", "Gong", "Chorus", "Aircall", "Ringover",
    "Voys", "Dialpad", "Trengo", "WhatsApp Business", "Copper", "Zoho", "Insightly", "Close", "Freshsales",
    "SugarCRM", "Microsoft Dynamics", "SAP Business One", "Unit4", "Isah", "Ridder iQ", "King ERP",
    "Syntess", "Bouwsoft", "Kubus", "Relatics", "StabiCAD", "Revit", "SketchUp", "AutoCAD", "Excel Online",
    "Google Sheets", "Looker Studio", "Hotjar", "Semrush", "Ahrefs", "Screaming Frog", "Webflow", "Framer",
    "Canva", "Figma", "Miro", "Lucidchart", "Loom", "Vimeo", "Wistia", "Typeform", "Jotform", "Tally",
)
_ACRONYMS = (
    "KPI", "OKR", "SLA", "API", "SaaS", "B2B", "B2C", "SMB", "ICP", "USP", "CAC", "LTV", "NPS", "CSAT",
    "SDR", "BDR", "AE", "CSM", "MQL", "SQL", "SQO", "ABM", "RFP", "RFQ", "PoC", "MVP", "ETL", "BI", "DWH",
    "WMS", "TMS", "MES", "PLM", "CMS", "DMS", "HRM", "ATS", "CPQ", "PIM", "EDIFACT", "XML", "CSV", "SSO",
    "MFA", "DPIA", "DPA", "ISO", "NEN", "VCA", "BHV", "WKR", "BTW", "KVK", "UWV", "ZZP", "CAO", "OR",
    "RvC", "RvB", "CFO", "CEO", "COO", "CTO", "CIO", "CMO", "HR", "IT", "OT", "IoT", "LLM", "RAG", "NLP",
)
_SURNAMES = (
    "de Jong", "Jansen", "de Vries", "van den Berg", "van Dijk", "Bakker", "Janssen", "Visser", "Meijer",
    "de Boer", "Mulder", "de Groot", "Bos", "Vos", "Peters", "Hendriks", "van Leeuwen", "Dekker", "Brouwer",
    "de Wit", "Dijkstra", "Smits", "de Graaf", "van der Meer", "van der Linden", "Kok", "Jacobs", "de Haan",
    "Vermeulen", "van den Heuvel", "van der Veen", "van den Broek", "de Bruijn", "de Bruin", "van der Heijden",
    "Schouten", "van Beek", "Willems", "van Vliet", "van de Ven", "Hoekstra", "Maas", "Verhoeven", "Koster",
    "van Dam", "van der Wal", "Prins", "Blom", "Peeters", "de Leeuw", "Kuipers", "Veenstra", "Kramer",
    "van den Bosch", "van der Laan", "Hermans", "Postma", "Gerritsen", "Evers", "Bosman",
)
_SECTORS = (
    "Installatietechniek", "Bouw", "Logistiek", "Transport", "Metaal", "Elektrotechniek", "Accountants",
    "Advocaten", "Makelaardij", "Vastgoed", "Groep", "Holding", "Techniek", "Interieur", "Schilders",
    "Dakwerken", "Hoveniers", "Autobedrijf", "Machinebouw", "Verpakkingen",
)
_PLACES = (
    "Amsterdam", "Rotterdam", "Den Haag", "Utrecht", "Eindhoven", "Tilburg", "Groningen", "Almere", "Breda",
    "Nijmegen", "Apeldoorn", "Haarlem", "Arnhem", "Enschede", "Amersfoort", "Zaanstad", "Den Bosch",
    "Zwolle", "Leiden", "Maastricht", "Dordrecht", "Zoetermeer", "Ede", "Alkmaar", "Emmen", "Venlo", "Deventer",
    "Delft", "Helmond", "Hilversum", "Oss", "Roosendaal", "Purmerend", "Schiedam", "Leeuwarden", "Vlaardingen",
    "Veenendaal", "Zeist", "Nieuwegein", "Hoorn", "Heerlen", "Doetinchem", "Veghel", "Uden", "Waalwijk",
    "Harderwijk", "Kampen", "Middelburg", "Goes", "Assen", "Sneek", "Tiel", "Culemborg", "Gorinchem", "Woerden",
)


def distractor_terms(count: int, exclude: Iterable[str], seed: int = 20260930) -> list[str]:
    """``count`` realistic, deterministic distractor terms that do not collide with ``exclude``.

    Products, business acronyms, Dutch first names (the PII filter's own list), places and
    company names built as surname plus sector: the kind of entries a real client list holds.
    """
    rng = random.Random(seed)
    companies = [f"{surname[0].upper()}{surname[1:]} {sector}" for surname in _SURNAMES for sector in _SECTORS]
    rng.shuffle(companies)
    pools = [list(_PRODUCTS), list(_ACRONYMS), sorted(NL_VOORNAMEN), list(_PLACES), companies]
    for pool in pools[:4]:
        rng.shuffle(pool)
    seen = {term.casefold() for term in exclude}
    result: list[str] = []
    while len(result) < count and any(pools):
        for pool in pools:
            while pool:
                term = pool.pop()
                if term.casefold() not in seen:
                    seen.add(term.casefold())
                    result.append(term)
                    break
            if len(result) == count:
                break
    if len(result) < count:
        raise ValueError(f"only {len(result)} distinct distractor terms available, {count} requested")
    return result


def scaled_terms(base: Sequence[str], size: int) -> list[str]:
    """``base`` padded with distractors to exactly ``size`` entries."""
    if size < len(base):
        raise ValueError(f"scale size {size} is smaller than the base list ({len(base)} terms)")
    return [*base, *distractor_terms(size - len(base), base)]


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _label_spans(text: str, label: Label) -> list[tuple[int, int]]:
    spans = find_occurrences(text, label.source)
    if label.occurrence is None:
        return spans
    return [spans[label.occurrence - 1]] if label.occurrence <= len(spans) else []


def deterministic_case_ids(segments: Sequence[CaseSegment], lists: NormalizationLists) -> frozenset[str]:
    """Positive labels the deterministic layer already repairs on its own.

    ``normalize`` runs on the label's source alone: that layer is context-free by design, so
    when it turns the source into the expected term, the model adds nothing for that case.
    """
    return frozenset(
        label.case_id
        for segment in segments
        for label in segment.labels
        if label.expected is not None and normalize(label.source, lists)[0] == label.expected
    )


def build_user_text(texts: Sequence[str], candidates: Sequence[Sequence[str]]) -> str:
    lines: list[str] = []
    for index, (text, terms) in enumerate(zip(texts, candidates, strict=True)):
        lines.append(f"Segment {index}: {text}")
        lines.append(f"candidates for segment {index}: {CANDIDATE_SEPARATOR.join(terms)}")
    return "\n".join(lines)


def parse_visible(visible_text: str, count: int) -> tuple[list[str], list[tuple[str, ...]]]:
    """Split the text the model saw back into its segments and candidate lists."""
    texts: dict[int, str] = {}
    candidates: dict[int, tuple[str, ...]] = {}
    for line in visible_text.split("\n"):
        if match := _VISIBLE_SEGMENT_RE.match(line):
            texts[int(match.group(1))] = match.group(2)
        elif match := _VISIBLE_CANDIDATES_RE.match(line):
            candidates[int(match.group(1))] = tuple(t for t in match.group(2).split(CANDIDATE_SEPARATOR) if t)
    if sorted(texts) != list(range(count)) or sorted(candidates) != list(range(count)):
        raise ValueError("the PII policy changed the segment structure of the request")
    return [texts[i] for i in range(count)], [candidates[i] for i in range(count)]


def prepare(segment: CaseSegment, prefilter: TermPrefilter, provider: str) -> PreparedSegment:
    """Candidates for ``segment`` and the request text before and after the PII policy.

    ``apply_outbound_pii`` is the same call ``LLMClient`` makes on ``user_text``, with the same
    provider, so ``visible_text`` is exactly what the model gets to see.
    """
    candidates = prefilter.candidates(segment.text)
    user_text = build_user_text([segment.text], [candidates])
    visible_text = apply_outbound_pii(user_text, provider=provider, allow_local=True)
    visible_texts, visible_candidates = parse_visible(visible_text, 1)
    return PreparedSegment(
        segment=segment,
        candidates=candidates,
        user_text=user_text,
        visible_text=visible_texts[0],
        visible_candidates=visible_candidates[0],
    )


def pii_effect(prepared: PreparedSegment, label: Label) -> str | None:
    """Why the PII policy made ``label`` unsolvable for the model, or ``None`` when it did not."""
    original = len(find_occurrences(prepared.segment.text, label.source))
    if original != len(find_occurrences(prepared.visible_text, label.source)):
        return "source_redacted"
    if label.expected in prepared.candidates and label.expected not in prepared.visible_candidates:
        return "target_redacted"
    return None


def _folded_words(text: str) -> list[str]:
    return [word.casefold() for word in word_tokens(text)]


def _contains(words: Sequence[str], part: Sequence[str]) -> bool:
    """Whether ``part`` occurs in ``words`` as a run of whole words."""
    size = len(part)
    return size > 0 and any(list(words[i : i + size]) == list(part) for i in range(len(words) - size + 1))


def _region_after(text: str, span: tuple[int, int], accepted: Sequence[AppliedCorrection]) -> str | None:
    """The text over ``span`` and every correction touching it, after those corrections.

    ``None`` when no correction touches ``span``. Accepted corrections never overlap each
    other, so the ones touching ``span`` are all there is to apply inside the region.
    """
    start, end = span
    touching = [c for c in accepted if c.start < end and start < c.end]
    if not touching:
        return None
    start = min(start, *(c.start for c in touching))
    end = max(end, *(c.end for c in touching))
    region = text[start:end]
    for correction in sorted(touching, key=lambda c: c.start, reverse=True):
        region = region[: correction.start - start] + correction.target + region[correction.end - start :]
    return region


def repair_outcome(text: str, label: Label, accepted: Sequence[AppliedCorrection]) -> str:
    """hit/wrong/miss for a correction label in the free arm, where a source may be wider.

    ``hit``: after the corrections touching the label, one of its expected forms stands there
    as whole words and the misheard source is gone (both case-insensitive). ``wrong``: touched
    but not repaired. ``miss``: untouched. A label's source is the whole misheard phrase, so a
    repair the model splits over adjacent corrections still lands inside it.
    """
    after = _region_after(text, _label_spans(text, label)[0], accepted)
    if after is None:
        return "miss"
    words = _folded_words(after)
    source = _folded_words(label.source)
    for form in (label.expected or "", *label.alternatives):
        wanted = _folded_words(form)
        if _contains(words, wanted) and (_contains(wanted, source) or not _contains(words, source)):
            return "hit"
    return "wrong"


def score_segment(
    segment: CaseSegment, accepted: Sequence[AppliedCorrection], mode: str = "term"
) -> tuple[dict[str, str], int]:
    """Outcome per label (hit/wrong/miss for a correction, intact/broken for a keep) and extra edits.

    A keep is broken as soon as an accepted correction overlaps it, in both arms (the round-1
    rule). A correction is a hit in the term arm only on exactly its span with exactly its
    term; in the free arm, where the model may take neighbouring words into its source, by
    ``repair_outcome``.
    """
    outcomes: dict[str, str] = {}
    labelled: list[tuple[int, int]] = []
    for label in segment.labels:
        spans = _label_spans(segment.text, label)
        labelled.extend(spans)
        overlapping = [c for c in accepted for s, e in spans if c.start < e and s < c.end]
        if label.expected is None:
            outcomes[label.case_id] = "broken" if overlapping else "intact"
        elif mode == "free":
            outcomes[label.case_id] = repair_outcome(segment.text, label, accepted)
        elif any((c.start, c.end) == spans[0] and c.target == label.expected for c in overlapping):
            outcomes[label.case_id] = "hit"
        else:
            outcomes[label.case_id] = "wrong" if overlapping else "miss"
    extra = sum(1 for c in accepted if not any(c.start < e and s < c.end for s, e in labelled))
    return outcomes, extra


def _spelling_variant(word: str, other: str) -> bool:
    """``word`` is ``other`` written differently: the same sound-alike key, or within the fuzzy threshold."""
    if word == other:
        return True
    if len(word) >= 3 and len(other) >= 3 and phonetic_key(word) == phonetic_key(other):
        return True
    limit = _fuzzy_threshold_for_term(word)
    return bool(limit) and _bounded_edit_distance(other, word, limit) is not None


def added_words(original: str, correction: AppliedCorrection, candidates: Iterable[str]) -> list[str]:
    """The words of ``correction``'s target that the fixed rule counts as added content.

    A target word is explained when it occurs in the replaced source, elsewhere in the
    segment, or in a candidate term the model saw; when it is a spelling variant of a source
    word (``phonetic_key``, or ``normalize.py``'s fuzzy threshold) or of adjacent source words
    written together; or when it is part of adjacent target words that together are a
    spelling variant of a source word (a split). Every other target word is new: it is in no
    reading of what was said. The rule only flags; each flag is checked by hand.
    """
    source = _folded_words(original[correction.start : correction.end])
    known = set(source)
    known.update(_folded_words(original[: correction.start]), _folded_words(original[correction.end :]))
    for term in candidates:
        known.update(_folded_words(term))
    readings = [*source, *("".join(source[i:j]) for i in range(len(source)) for j in range(i + 2, len(source) + 1))]
    target = _folded_words(correction.target)
    split: set[int] = set()
    for i in range(len(target)):
        for j in range(i + 2, len(target) + 1):
            if any(_spelling_variant("".join(target[i:j]), word) for word in source):
                split.update(range(i, j))
    return [
        word
        for position, word in enumerate(target)
        if word not in known
        and position not in split
        and not any(_spelling_variant(word, reading) for reading in readings)
    ]


def _fraction(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


@dataclass(frozen=True)
class LabelGroups:
    """Every label of the measurement, by the role it plays in the criteria."""

    contextual: tuple[Label, ...]  # corrections only the model can make: criteria 1, 2, 4, 5
    deterministic: tuple[Label, ...]  # corrections normalize() already makes: reported, not counted
    keeps: tuple[Label, ...]  # context-set places that must stay as written
    goods: tuple[Label, ...]  # the beoordelingslijst's goed rows: criterion 3
    free: tuple[Label, ...] = ()  # misheard running language no list term repairs: the free arm

    @classmethod
    def build(cls, segments: Sequence[CaseSegment], deterministic: Collection[str]) -> LabelGroups:
        context = [label for s in segments if s.kind == "context" for label in s.labels]
        return cls(
            contextual=tuple(lb for lb in context if lb.expected is not None and lb.case_id not in deterministic),
            deterministic=tuple(lb for lb in context if lb.expected is not None and lb.case_id in deterministic),
            keeps=tuple(lb for lb in context if lb.expected is None),
            goods=tuple(label for s in segments if s.kind == "good" for label in s.labels),
            free=tuple(label for s in segments if s.kind == "free" for label in s.labels),
        )


async def ask_model(
    llm: Any, model: str, temperature: float, timeout_s: float, user_text: str, mode: str = "term"
) -> TermCorrections | FreeCorrections:
    """One structured call on the ``report_terms`` task. The client applies the PII policy itself."""
    system_prompt, response_model = _MODE_PROMPTS[mode]
    result = await asyncio.wait_for(
        llm.acreate(
            model=model,
            system_prompt=system_prompt,
            user_text=user_text,
            response_model=response_model,
            temperature=temperature,
            allow_local=True,
        ),
        timeout=timeout_s,
    )
    if not isinstance(result, response_model):
        raise TypeError(f"the model returned no {response_model.__name__}")
    return result


#: The token counts ``LLMClient.last_usage`` carries that the cost report adds up.
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens")


def _sent(item: PreparedSegment, mode: str) -> bool:
    """The term arm only asks about segments with candidates; the free arm asks about every segment."""
    return mode == "free" or bool(item.candidates)


async def run_once(
    run: int,
    prepared: Sequence[PreparedSegment],
    groups: LabelGroups,
    term_list: TermList,
    model_run: ModelRun,
    mode: str = "term",
) -> dict[str, Any]:
    """One pass over every segment the arm asks about; scores every label, counts tokens."""
    pool: asyncio.Queue[Any] = asyncio.Queue()
    for client in model_run.clients:
        pool.put_nowait(client)
    rejected: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    usage: Counter[str] = Counter()
    added: Counter[str] = Counter()
    added_segments: set[str] = set()

    def record(item: PreparedSegment, accepted: Sequence[AppliedCorrection], call_usage: Any) -> None:
        if isinstance(call_usage, dict):
            usage["calls"] += 1
            usage["words_sent"] += len(item.segment.text.split())
            usage.update({key: int(call_usage.get(key) or 0) for key in _USAGE_KEYS})
        else:
            usage["calls_without_usage"] += 1
        for correction in accepted:
            new = added_words(item.segment.text, correction, item.visible_candidates)
            if not new:
                continue
            added[item.segment.kind] += 1
            added_segments.add(item.segment.segment_id)
            if model_run.show_flagged:
                source = item.segment.text[correction.start : correction.end]
                print(
                    f"[flagged] {model_run.model} {mode} run {run} {item.segment.segment_id}: "
                    f"{source!r} -> {correction.target!r}, new {new}",
                    file=sys.stderr,
                )

    async def one(item: PreparedSegment) -> tuple[dict[str, str], int]:
        accepted: list[AppliedCorrection] = []
        if _sent(item, mode):
            client = await pool.get()
            try:
                answer = await ask_model(
                    client, model_run.model, model_run.temperature, model_run.timeout_s, item.user_text, mode
                )
                call_usage = getattr(client, "last_usage", None)
            except Exception:  # vnx-silent-except: a failed call counts as an error and as "no correction"
                errors[item.segment.kind] += 1
                answer = None
            finally:
                pool.put_nowait(client)
            if answer is not None:
                if mode == "free":
                    accepted, refused = validate_free_corrections(
                        answer.corrections, [item.segment.text], [item.visible_text]
                    )
                else:
                    accepted, refused = validate_corrections(
                        answer.corrections,
                        [item.segment.text],
                        [item.visible_text],
                        [frozenset(item.visible_candidates)],
                        term_list,
                    )
                rejected.update(refused)
                record(item, accepted, call_usage)
        return score_segment(item.segment, accepted, mode)

    scored = await asyncio.gather(*(one(item) for item in prepared))
    outcomes: dict[str, str] = {}
    extra: Counter[str] = Counter()
    for item, (segment_outcomes, extra_edits) in zip(prepared, scored, strict=True):
        outcomes.update(segment_outcomes)
        extra[item.segment.kind] += extra_edits

    def count(group: Sequence[Label], outcome: str) -> int:
        return sum(1 for label in group if outcomes[label.case_id] == outcome)

    name_like = [label for label in groups.contextual if label.name_like]
    other = [label for label in groups.contextual if not label.name_like]
    every_positive = [*groups.contextual, *groups.deterministic]
    result: dict[str, Any] = {
        "run": run,
        "mode": mode,
        "recall": _fraction(count(groups.contextual, "hit"), len(groups.contextual)),
        "hits": count(groups.contextual, "hit"),
        "positives": len(groups.contextual),
        "recall_name_like": _fraction(count(name_like, "hit"), len(name_like)),
        "recall_other": _fraction(count(other, "hit"), len(other)),
        "recall_incl_deterministic": _fraction(count(every_positive, "hit"), len(every_positive)),
        "wrong_corrections": count(groups.contextual, "wrong"),
        "good_broken": count(groups.goods, "broken"),
        "good_total": len(groups.goods),
        "good_model_errors": errors["good"],
        "keep_broken": count(groups.keeps, "broken"),
        "keep_total": len(groups.keeps),
        "extra_edits_context": extra["context"],
        "extra_edits_good": extra["good"],
        "content_added": sum(added.values()),
        "content_added_by_kind": dict(sorted(added.items())),
        "content_added_segment_ids": sorted(added_segments),
        "rejected": dict(sorted(rejected.items())),
        "model_errors": sum(errors.values()),
        "requests": sum(1 for item in prepared if _sent(item, mode)),
        "usage": {key: usage[key] for key in ("calls", "calls_without_usage", "words_sent", *_USAGE_KEYS)},
    }
    if mode == "free":
        result.update(
            {
                "free_repaired": count(groups.free, "hit"),
                "free_wrong": count(groups.free, "wrong"),
                "free_total": len(groups.free),
                "free_model_errors": errors["free"],
                "extra_edits_free": extra["free"],
            }
        )
    result["outcomes"] = dict(sorted(outcomes.items()))
    return result


def prefilter_report(prepared: Sequence[PreparedSegment], groups: LabelGroups) -> dict[str, Any]:
    """Prefilter recall (target among the candidates) before and after the PII policy."""
    by_id = {label.case_id: item for item in prepared for label in item.segment.labels}
    contextual = groups.contextual
    found = [label.case_id for label in contextual if label.expected in by_id[label.case_id].candidates]
    visible = [label.case_id for label in contextual if label.expected in by_id[label.case_id].visible_candidates]
    every_positive = [*contextual, *groups.deterministic]
    found_all = [label for label in every_positive if label.expected in by_id[label.case_id].candidates]
    effects = {label.case_id: pii_effect(by_id[label.case_id], label) for label in contextual}
    unsolvable = sorted(case_id for case_id, effect in effects.items() if effect)
    name_like = [label for label in contextual if label.name_like]
    context_items = [item for item in prepared if item.segment.kind == "context"]
    return {
        "positives": len(contextual),
        "positives_name_like": len(name_like),
        "positives_other": len(contextual) - len(name_like),
        "recall": _fraction(len(found), len(contextual)),
        "recall_after_pii": _fraction(len(visible), len(contextual)),
        "missed_case_ids": sorted({label.case_id for label in contextual} - set(found)),
        "deterministic": len(groups.deterministic),
        "deterministic_case_ids": sorted(label.case_id for label in groups.deterministic),
        "recall_incl_deterministic": _fraction(len(found_all), len(every_positive)),
        "keeps": len(groups.keeps),
        "unsolvable_by_pii": len(unsolvable),
        "unsolvable_by_pii_name_like": sum(1 for label in name_like if effects[label.case_id]),
        "unsolvable_case_ids": unsolvable,
        "segments": len(context_items),
        "segments_redacted_by_pii": sum(1 for item in context_items if item.visible_text != item.segment.text),
        "segments_with_candidates": sum(1 for item in context_items if item.candidates),
        "avg_candidates_per_segment": round(statistics.fmean(len(item.candidates) for item in context_items), 2)
        if context_items
        else 0.0,
    }


def measure_scale(
    base_terms: Sequence[str],
    sizes: Sequence[int],
    context_segments: Sequence[CaseSegment],
    contextual: Sequence[Label],
    conversation: Sequence[str],
) -> list[dict[str, Any]]:
    """Prefilter recall and cost per 60-minute conversation for each list size. No network."""
    rows: list[dict[str, Any]] = []
    wanted = {label.case_id for label in contextual}
    labelled = [(segment, label) for segment in context_segments for label in segment.labels if label.case_id in wanted]
    for size in sizes:
        entries = scaled_terms(base_terms, size)
        totals: list[float] = []
        builds: list[float] = []
        scans: list[float] = []
        candidate_counts: list[int] = []
        for _ in range(SCALE_REPEATS):
            started = time.perf_counter()
            prefilter = TermPrefilter(entries)
            built = time.perf_counter()
            candidate_lists = prefilter.scan(conversation)
            done = time.perf_counter()
            builds.append((built - started) * 1000)
            scans.append((done - built) * 1000)
            totals.append((done - started) * 1000)
            candidate_counts = [len(c) for c in candidate_lists]
        prefilter = TermPrefilter(entries)
        labelled_candidates = {segment.segment_id: prefilter.candidates(segment.text) for segment, _ in labelled}
        found = sum(1 for segment, label in labelled if label.expected in labelled_candidates[segment.segment_id])
        rows.append(
            {
                "terms": len(entries),
                "prefilter_recall": _fraction(found, len(labelled)),
                "build_ms_median": round(statistics.median(builds), 2),
                "scan_ms_median": round(statistics.median(scans), 2),
                "total_ms_median": round(statistics.median(totals), 2),
                "total_ms_max": round(max(totals), 2),
                "conversation_words": sum(len(s.split()) for s in conversation),
                "conversation_segments": len(conversation),
                "avg_candidates_per_segment": round(statistics.fmean(candidate_counts), 2) if candidate_counts else 0.0,
                "segments_with_candidates": sum(1 for count in candidate_counts if count),
                "avg_candidates_labelled_segment": round(
                    statistics.fmean(len(c) for c in labelled_candidates.values()), 2
                )
                if labelled_candidates
                else 0.0,
            }
        )
    return rows


def per_case(
    prepared: Sequence[PreparedSegment], groups: LabelGroups, runs: Sequence[dict[str, Any]], mode: str = "term"
) -> dict[str, dict[str, Any]]:
    """Per case id: its role, whether the prefilter offered its target, and its outcome per run."""
    roles = {
        **{label.case_id: "contextual" for label in groups.contextual},
        **{label.case_id: "deterministic" for label in groups.deterministic},
        **{label.case_id: "keep" for label in groups.keeps},
        **{label.case_id: "good" for label in groups.goods},
        **{label.case_id: "free" for label in groups.free},
    }
    table: dict[str, dict[str, Any]] = {}
    for item in prepared:
        for label in item.segment.labels:
            row: dict[str, Any] = {"role": roles[label.case_id], "name_like": label.name_like}
            if label.expected is not None:
                row["prefilter"] = label.expected in item.candidates
                row["pii"] = pii_effect(item, label)
            else:
                row["sent_to_model"] = _sent(item, mode)
            row["outcomes"] = [run["outcomes"][label.case_id] for run in runs]
            table[label.case_id] = row
    return dict(sorted(table.items()))


def _good_criterion(result: dict[str, Any], runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """0 of the 17 good cases broken in every run, and no failed call on a good case: both arms."""
    good_broken = [run["good_broken"] for run in runs]
    good_errors = [run["good_model_errors"] for run in runs]
    return {
        "required": f"0 of {EXPECTED_GOOD_CASES} in every run (and no failed call on a good case)",
        "value": good_broken or None,
        "good_total": result["good_cases"]["total"],
        "good_model_errors": good_errors or None,
        "met": None
        if not runs
        else result["good_cases"]["total"] == EXPECTED_GOOD_CASES
        and all(b == 0 for b in good_broken)
        and all(e == 0 for e in good_errors),
    }


def evaluate_free_criteria(result: dict[str, Any]) -> dict[str, Any]:
    """The free arm's three pass criteria T0 fixed on 2026-09-30, each with its measured value.

    ``3_content_added`` is the fixed code rule (``added_words``); it flags, and every flag is
    checked by hand. The count here is what the rule flagged, not the outcome of that check.
    """
    runs = result.get("runs") or []
    repaired = [run["free_repaired"] for run in runs]
    median = statistics.median(repaired) if repaired else None
    free_total = result["free_cases"]["total"]
    added = [run["content_added"] for run in runs]
    return {
        "1_free_repaired": {
            "required": f">= {MIN_FREE_REPAIRED} of {EXPECTED_FREE_CASES} repaired, median over the runs",
            "value": median,
            "free_total": free_total,
            "met": None if median is None else free_total == EXPECTED_FREE_CASES and median >= MIN_FREE_REPAIRED,
        },
        "2_good_cases_broken": _good_criterion(result, runs),
        "3_content_added": {
            "required": "0 corrections adding content (fixed code rule) in every run",
            "value": added or None,
            "met": None if not runs else all(count == 0 for count in added),
        },
    }


def evaluate_criteria(result: dict[str, Any]) -> dict[str, Any]:
    """The pass criteria T0 fixed on 2026-09-30, each with its measured value, for the result's arm."""
    if result.get("config", {}).get("mode", "term") == "free":
        return evaluate_free_criteria(result)
    prefilter = result["prefilter"]
    runs = result.get("runs") or []
    recalls = [run["recall"] for run in runs if run["recall"] is not None]
    median = round(statistics.median(recalls), 3) if recalls else None
    scale = result.get("scale") or []
    return {
        "1_context_set_size": {
            "required": f">= {MIN_CONTEXT_CASES} labelled contextual corrections",
            "value": prefilter["positives"],
            "met": prefilter["positives"] >= MIN_CONTEXT_CASES,
        },
        "2_recall_median": {
            "required": f">= {MIN_RECALL_MEDIAN} median over the runs",
            "value": median,
            "met": None if median is None else median >= MIN_RECALL_MEDIAN,
        },
        "3_good_cases_broken": _good_criterion(result, runs),
        "4_prefilter_recall": {
            "required": f">= {MIN_PREFILTER_RECALL}",
            "value": prefilter["recall"],
            "met": prefilter["recall"] is not None and prefilter["recall"] >= MIN_PREFILTER_RECALL,
        },
        "5_scale": {
            "required": f"prefilter recall >= {MIN_PREFILTER_RECALL} and < {MAX_PREFILTER_MS:.0f} ms "
            "per 60-minute call at every list size",
            "value": [
                {"terms": row["terms"], "recall": row["prefilter_recall"], "ms": row["total_ms_median"]}
                for row in scale
            ]
            or None,
            "met": None
            if not scale
            else all(
                row["prefilter_recall"] is not None
                and row["prefilter_recall"] >= MIN_PREFILTER_RECALL
                and row["total_ms_median"] < MAX_PREFILTER_MS
                for row in scale
            ),
        },
    }


def cost_report(
    runs: Sequence[dict[str, Any]],
    price: Price | None,
    eur_per_usd: float | None,
    words: int = SIXTY_MINUTE_WORDS,
) -> dict[str, Any]:
    """Tokens over every run, per 1000 words sent, and what a call of ``words`` words costs.

    The rate per 1000 words is measured on the segments that went to the model. Every call
    carries the system prompt again, so on these short segments it is an upper bound for one
    batched call over a whole transcript. Input tokens are priced at the full input rate;
    ``cache_read_tokens`` is reported next to it, not discounted.
    """
    totals: Counter[str] = Counter()
    for run in runs:
        totals.update(run.get("usage") or {})
    report: dict[str, Any] = {key: totals[key] for key in ("calls", "calls_without_usage", "words_sent", *_USAGE_KEYS)}
    report["price_usd_per_mtok"] = (
        None if price is None else {"input": price.input_usd_per_mtok, "output": price.output_usd_per_mtok}
    )
    report["eur_per_usd"] = eur_per_usd
    if not totals["words_sent"] or not totals["calls"]:
        report.update(mean_per_call=None, per_1000_words=None, per_60_minute_call=None)
        return report
    input_rate = totals["input_tokens"] * 1000 / totals["words_sent"]
    output_rate = totals["output_tokens"] * 1000 / totals["words_sent"]
    call_input = input_rate * words / 1000
    call_output = output_rate * words / 1000
    usd = (
        None
        if price is None
        else (call_input * price.input_usd_per_mtok + call_output * price.output_usd_per_mtok) / 1_000_000
    )
    report.update(
        mean_per_call={
            "input_tokens": round(totals["input_tokens"] / totals["calls"], 1),
            "output_tokens": round(totals["output_tokens"] / totals["calls"], 1),
            "words": round(totals["words_sent"] / totals["calls"], 1),
        },
        per_1000_words={"input_tokens": round(input_rate, 1), "output_tokens": round(output_rate, 1)},
        per_60_minute_call={
            "words": words,
            "input_tokens": round(call_input),
            "output_tokens": round(call_output),
            "usd": None if usd is None else round(usd, 4),
            "eur": None if usd is None or eur_per_usd is None else round(usd * eur_per_usd, 4),
        },
    )
    return report


def measure(
    context_segments: Sequence[CaseSegment],
    good_segments: Sequence[CaseSegment],
    good_unusable: int,
    base_terms: Sequence[str],
    *,
    provider: str,
    model: str,
    deterministic_lists: NormalizationLists,
    model_run: ModelRun | None,
    scale_sizes: Sequence[int] = (),
    conversation: Sequence[str] = (),
    mode: str = "term",
    free_segments: Sequence[CaseSegment] = (),
    price: Price | None = None,
    eur_per_usd: float | None = None,
) -> dict[str, Any]:
    """The whole measurement of one arm on loaded inputs. ``model_run`` ``None`` measures the prefilter only.

    The term arm runs on the contextual set and the good cases; the free arm adds
    ``free_segments``, which it requires.

    Raises ``ValueError`` for an unknown arm, a free arm without free cases, ids shared
    between the sets, a label that expects a term outside the list, a PII policy that breaks
    the request structure, or a scale size below the base list.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; known modes: {list(MODES)}")
    if mode == "free" and not free_segments:
        raise ValueError("the free arm needs the free cases (--free-cases)")
    check_distinct_ids(context_segments, good_segments, free_segments)
    prefilter = TermPrefilter(base_terms)
    term_list = TermList(prefilter.terms)
    check_expected_in_list(context_segments, term_list)
    segments = [*context_segments, *good_segments, *(free_segments if mode == "free" else ())]
    prepared = [prepare(segment, prefilter, provider) for segment in segments]
    deterministic = deterministic_case_ids(context_segments, deterministic_lists)
    groups = LabelGroups.build([item.segment for item in prepared], deterministic)
    good_items = [item for item in prepared if item.segment.kind == "good"]

    runs: list[dict[str, Any]] = []
    if model_run is not None:
        for run in range(1, model_run.runs + 1):
            runs.append(asyncio.run(run_once(run, prepared, groups, term_list, model_run, mode)))
    result: dict[str, Any] = {
        "config": {
            "task": TERMS_TASK,
            "mode": mode,
            "provider": provider,
            "model": model,
            "pii_mode": resolve_pii_mode(),
            "terms": len(prefilter.terms),
            "runs": len(runs),
            "context_segments": len(context_segments),
        },
        "good_cases": {
            "total": len(good_segments),
            "unusable": good_unusable,
            "sent_to_model": sum(1 for item in good_items if _sent(item, mode)),
        },
        "prefilter": prefilter_report(prepared, groups),
        "runs": runs,
        "per_case": per_case(prepared, groups, runs, mode),
        "cost": cost_report(runs, price, eur_per_usd),
        "scale": measure_scale(base_terms, scale_sizes, context_segments, groups.contextual, conversation)
        if scale_sizes
        else [],
    }
    if mode == "free":
        free_items = [item for item in prepared if item.segment.kind == "free"]
        result["free_cases"] = {
            "total": len(groups.free),
            "segments": len(free_items),
            "redacted_by_pii": sum(1 for item in free_items if item.visible_text != item.segment.text),
            "unsolvable_by_pii": sum(
                1 for item in free_items for label in item.segment.labels if pii_effect(item, label)
            ),
        }
    result["criteria"] = evaluate_criteria(result)
    return result


# ---------------------------------------------------------------------------
# Synthetic probes (outside the gate criteria)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """One synthetic probe: a segment, its own term list and the expected outcome per occurrence."""

    probe_id: str
    terms: tuple[str, ...]
    segment: CaseSegment


def load_probes(path: Path) -> list[Probe]:
    """The probe file (YAML or JSON): ``{"probes": [{id, text, terms, occurrences}]}``.

    Each occurrence names ``source``, ``occurrence`` (1-based, default 1) and exactly one
    expectation: ``correct_to: <term>`` (a term of the probe's list) or ``leave: true``.
    Raises ``ValueError`` for anything malformed, a repeated id, an expected term outside the
    probe's list, or a source that does not occur in the text.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("probes") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"probe file must hold a non-empty 'probes' list: {path}")
    probes: list[Probe] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("every probe must be a mapping")
        probe_id, text, terms = entry.get("id"), entry.get("text"), entry.get("terms")
        if not isinstance(probe_id, str) or not probe_id.strip():
            raise ValueError("every probe needs an id")
        if probe_id in seen:
            raise ValueError(f"probe id {probe_id!r} is used twice")
        seen.add(probe_id)
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"probe {probe_id}: text must be a non-empty string")
        if not isinstance(terms, list) or not terms or not all(isinstance(t, str) and t.strip() for t in terms):
            raise ValueError(f"probe {probe_id}: terms must be a non-empty list of strings")
        term_list = TermList(TermPrefilter(sanitize_call_terms(terms)).terms)
        occurrences = entry.get("occurrences")
        if not isinstance(occurrences, list) or not occurrences:
            raise ValueError(f"probe {probe_id}: occurrences must be a non-empty list")
        labels: list[Label] = []
        for number, raw in enumerate(occurrences, start=1):
            labels.append(_probe_label(probe_id, number, raw, text, term_list))
        segment = CaseSegment(segment_id=probe_id, text=text, labels=tuple(labels), kind="probe")
        probes.append(Probe(probe_id=probe_id, terms=tuple(sanitize_call_terms(terms)), segment=segment))
    return probes


def _probe_label(probe_id: str, number: int, raw: Any, text: str, term_list: TermList) -> Label:
    label_id = f"{probe_id}.{number}"
    if not isinstance(raw, dict):
        raise ValueError(f"probe {probe_id}: every occurrence must be a mapping")
    source, occurrence = raw.get("source"), raw.get("occurrence", 1)
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"probe {label_id}: source must be a non-empty string")
    if not isinstance(occurrence, int) or isinstance(occurrence, bool) or occurrence < 1:
        raise ValueError(f"probe {label_id}: occurrence must be an integer >= 1")
    correct_to, leave = raw.get("correct_to"), raw.get("leave")
    if (correct_to is None) == (leave is None):
        raise ValueError(f"probe {label_id}: give exactly one of correct_to and leave")
    if leave is not None and leave is not True:
        raise ValueError(f"probe {label_id}: leave must be true")
    if correct_to is not None and (not isinstance(correct_to, str) or term_list.canonical(correct_to) != correct_to):
        raise ValueError(f"probe {label_id}: correct_to must be a term of the probe's list")
    if locate_occurrence(text, source, occurrence) is None:
        raise ValueError(f"probe {label_id}: source does not occur {occurrence} time(s) in the text")
    return Label(case_id=label_id, source=source, occurrence=occurrence, expected=correct_to)


def score_probes(
    probes: Sequence[Probe],
    accepted: dict[str, Sequence[AppliedCorrection]],
    errored: Collection[str] = (),
) -> dict[str, Any]:
    """Counts and probe ids for one pass: good, wrongly corrected, missed, model errors.

    A probe is good when every occurrence came out as expected. ``wrongly_corrected``: a
    ``leave`` occurrence that an accepted correction touched. ``missed``: a ``correct_to``
    occurrence nobody repaired, or one repaired to something else. A probe whose call failed
    is listed under ``error_ids`` and is never good. One probe can be in several lists.
    """
    good: list[str] = []
    wrongly: list[str] = []
    missed: list[str] = []
    errors: list[str] = []
    for probe in probes:
        probe_id = probe.probe_id
        if probe_id in errored:
            errors.append(probe_id)
            continue
        outcomes, _ = score_segment(probe.segment, accepted.get(probe_id, ()), "term")
        values = set(outcomes.values())
        if values <= {"hit", "intact"}:
            good.append(probe_id)
        if "broken" in values:
            wrongly.append(probe_id)
        if values & {"miss", "wrong"}:
            missed.append(probe_id)
    return {
        "probes": len(probes),
        "good": len(good),
        "wrongly_corrected": len(wrongly),
        "missed": len(missed),
        "model_errors": len(errors),
        "good_ids": good,
        "wrongly_corrected_ids": wrongly,
        "missed_ids": missed,
        "error_ids": errors,
    }


async def run_probes(probes: Sequence[Probe], model_run: ModelRun, provider: str) -> dict[str, Any]:
    """One pass of the term arm over ``probes``, each with its own term list and prefilter."""
    pool: asyncio.Queue[Any] = asyncio.Queue()
    for client in model_run.clients:
        pool.put_nowait(client)
    accepted: dict[str, Sequence[AppliedCorrection]] = {}
    errored: set[str] = set()

    async def one(probe: Probe) -> None:
        prefilter = TermPrefilter(probe.terms)
        item = prepare(probe.segment, prefilter, provider)
        if not item.candidates:
            return
        client = await pool.get()
        try:
            answer = await ask_model(
                client, model_run.model, model_run.temperature, model_run.timeout_s, item.user_text, "term"
            )
        except Exception:  # vnx-silent-except: a failed call is counted as a model error, never as a pass
            errored.add(probe.probe_id)
            return
        finally:
            pool.put_nowait(client)
        accepted[probe.probe_id], _ = validate_corrections(
            answer.corrections,
            [item.segment.text],
            [item.visible_text],
            [frozenset(item.visible_candidates)],
            TermList(prefilter.terms),
        )

    await asyncio.gather(*(one(probe) for probe in probes))
    result = score_probes(probes, accepted, errored)
    result["model"] = model_run.model
    result["provider"] = provider
    return result


def format_probe_summary(result: dict[str, Any]) -> str:
    return (
        f"probes {result['model']} ({result['provider']}): {result['good']}/{result['probes']} good, "
        f"wrongly corrected {result['wrongly_corrected']} {result['wrongly_corrected_ids']}, "
        f"missed {result['missed']} {result['missed_ids']}, "
        f"model errors {result['model_errors']} {result['error_ids']}"
    )


def format_summary(result: dict[str, Any]) -> str:
    config = result["config"]
    lines = [f"model {config['model']} ({config['provider']}), arm {config['mode']}, {config['runs']} run(s)"]
    lines.append("criterion              required                                      value                 met")
    for name, row in result["criteria"].items():
        lines.append(f"{name:<22} {row['required']:<45} {json.dumps(row['value']):<21} {row['met']}")
    prefilter = result["prefilter"]
    good = result["good_cases"]
    lines.append("")
    lines.append(
        f"prefilter: recall {prefilter['recall']} (after PII {prefilter['recall_after_pii']}) on "
        f"{prefilter['positives']} contextual corrections, missed {prefilter['missed_case_ids']}, "
        f"deterministic (not counted) {prefilter['deterministic_case_ids']}, "
        f"unsolvable by PII {prefilter['unsolvable_by_pii']} (name-like {prefilter['unsolvable_by_pii_name_like']}), "
        f"segments redacted by PII {prefilter['segments_redacted_by_pii']}"
    )
    lines.append(
        f"good cases: {good['total']} usable, {good['unusable']} unusable, {good['sent_to_model']} sent to the model"
    )
    if "free_cases" in result:
        free = result["free_cases"]
        lines.append(
            f"free cases: {free['total']} in {free['segments']} segments, {free['redacted_by_pii']} redacted by PII, "
            f"{free['unsolvable_by_pii']} unsolvable by PII"
        )
    for run in result.get("runs") or []:
        free_part = (
            f"free repaired {run['free_repaired']}/{run['free_total']} (wrong {run['free_wrong']}), "
            if "free_repaired" in run
            else ""
        )
        lines.append(
            f"run {run['run']}: {free_part}recall {run['recall']} ({run['hits']}/{run['positives']}), "
            f"name-like {run['recall_name_like']}, other {run['recall_other']}, "
            f"good broken {run['good_broken']}/{run['good_total']}, keep broken {run['keep_broken']}/"
            f"{run['keep_total']}, extra edits {run['extra_edits_context']}+{run['extra_edits_good']}"
            f"+{run.get('extra_edits_free', 0)}, content added {run['content_added']}, "
            f"rejected {run['rejected']}, errors {run['model_errors']}"
        )
    cost = result.get("cost") or {}
    if cost.get("per_60_minute_call"):
        call = cost["per_60_minute_call"]
        lines.append(
            f"cost: {cost['calls']} calls, {cost['per_1000_words']['input_tokens']} in / "
            f"{cost['per_1000_words']['output_tokens']} out tokens per 1000 words; a {call['words']}-word call "
            f"{call['input_tokens']} in / {call['output_tokens']} out = USD {call['usd']} / EUR {call['eur']}"
        )
    for row in result.get("scale") or []:
        lines.append(
            f"scale {row['terms']} terms: recall {row['prefilter_recall']}, {row['total_ms_median']} ms "
            f"(build {row['build_ms_median']}, scan {row['scan_ms_median']}, max {row['total_ms_max']}), "
            f"{row['avg_candidates_per_segment']} candidates/segment, "
            f"{row['avg_candidates_labelled_segment']} on the labelled segments"
        )
    return "\n".join(lines)


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically: a temporary file next to it, then a rename."""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _parse_sizes(raw: str) -> list[int]:
    try:
        sizes = [int(part) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--scale takes comma-separated integers, got {raw!r}") from exc
    if not sizes or any(size < 1 for size in sizes):
        raise argparse.ArgumentTypeError("--scale needs at least one positive size")
    return sizes


def _parse_names(raw: str) -> list[str]:
    names = [part.strip() for part in raw.split(",") if part.strip()]
    if not names or len(set(names)) != len(names):
        raise argparse.ArgumentTypeError(f"expected a comma-separated list without repeats, got {raw!r}")
    return names


def _parse_modes(raw: str) -> list[str]:
    modes = _parse_names(raw)
    unknown = [mode for mode in modes if mode not in MODES]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown mode(s) {unknown}; choose from {list(MODES)}")
    return modes


def _parse_price(raw: str) -> tuple[str, Price]:
    """``MODEL=IN,OUT``: US dollars per million input and output tokens."""
    model, separator, values = raw.rpartition("=")
    parts = values.split(",")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        numbers = []
    if not separator or not model.strip() or len(numbers) != 2 or any(n < 0 for n in numbers):
        raise argparse.ArgumentTypeError(f"--price takes MODEL=IN,OUT in USD per Mtok, got {raw!r}")
    return model.strip(), Price(input_usd_per_mtok=numbers[0], output_usd_per_mtok=numbers[1])


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", required=True, type=Path, help="labelled contextual set (JSON or YAML)")
    parser.add_argument("--good-cases", required=True, type=Path, help="the beoordelingslijst markdown")
    parser.add_argument("--terms", required=True, type=Path, help="term list (YAML/JSON list, or termen/terms)")
    parser.add_argument("--free-cases", type=Path, help="labelled misheard running language for the free arm")
    parser.add_argument(
        "--mode", type=_parse_modes, default=["term"], help="comma-separated arms: term, free (default term)"
    )
    parser.add_argument(
        "--models",
        type=_parse_names,
        help="comma-separated models, measured one after the other via REPORT_TERMS_LLM_MODEL (default: as resolved)",
    )
    parser.add_argument(
        "--price",
        type=_parse_price,
        action="append",
        default=[],
        help="MODEL=IN,OUT in USD per million tokens (repeatable), for the cost per 60-minute call",
    )
    parser.add_argument("--eur-per-usd", type=float, help="exchange rate for the cost in euro")
    parser.add_argument(
        "--show-flagged",
        action="store_true",
        help="print corrections the content-added rule flags, WITH their text, to stderr for a local check",
    )
    parser.add_argument("--runs", type=int, default=5, help="model runs (default 5)")
    parser.add_argument("--concurrency", type=int, default=4, help="parallel model calls per run (default 4)")
    parser.add_argument("--prefilter-only", action="store_true", help="prefilter and scale only; no model call")
    parser.add_argument("--scale", type=_parse_sizes, help="comma-separated list sizes, e.g. 50,200,1000")
    parser.add_argument(
        "--scale-transcript",
        type=Path,
        action="append",
        default=[],
        help="real conversation text for the timing (repeatable; .txt one segment per line, or .json)",
    )
    parser.add_argument(
        "--scale-words", type=int, default=SIXTY_MINUTE_WORDS, help="words of a 60-minute call (default 9000)"
    )
    parser.add_argument(
        "--normalization-config",
        type=Path,
        default=DEFAULT_NORMALIZATION_CONFIG,
        help="the deterministic layer's fixed list (default config/transcript_normalization.yaml)",
    )
    parser.add_argument(
        "--probes",
        type=Path,
        help="synthetic probe set (e.g. tests/fixtures/term_correction_probes.yaml), reported apart from the criteria",
    )
    parser.add_argument("--out", type=Path, help="write the result as JSON")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.concurrency < 1:
        parser.error("--runs and --concurrency must be at least 1")
    if args.scale and not args.scale_transcript:
        parser.error("--scale needs --scale-transcript")
    if "free" in args.mode and not args.prefilter_only and args.free_cases is None:
        parser.error("--mode free needs --free-cases")
    if args.probes and args.prefilter_only:
        parser.error("--probes needs a model call; it cannot be combined with --prefilter-only")
    if args.eur_per_usd is not None and args.eur_per_usd <= 0:
        parser.error("--eur-per-usd must be positive")
    priced = [model for model, _ in args.price]
    if len(set(priced)) != len(priced):
        parser.error("--price names a model twice")
    if args.models and set(priced) - set(args.models):
        parser.error("--price names a model that is not in --models")
    return args


def resolve_terms_llm(model: str | None, config: DetectorConfig, clients: int) -> tuple[ResolvedLLM, tuple[Any, ...]]:
    """The ``report_terms`` task resolved with ``REPORT_TERMS_LLM_MODEL`` set to ``model``, and ``clients`` clients.

    ``model`` ``None`` keeps whatever the environment says. The variable is restored
    afterwards: a client carries no model of its own, the model is named on every call.
    """
    key = task_env_keys(TERMS_TASK)[1]
    previous = os.environ.get(key)
    if model is not None:
        os.environ[key] = model
    try:
        resolved = resolve_llm(TERMS_TASK, config)
        return resolved, tuple(build_llm_client(resolved) for _ in range(clients))
    finally:
        if model is not None:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        base_terms = load_terms(args.terms)
        context_segments = load_cases(args.cases)
        free_segments = load_cases(args.free_cases, kind="free") if args.free_cases else []
        good_segments, good_unusable = load_good_cases(args.good_cases)
        conversation = load_scale_transcript(args.scale_transcript, args.scale_words) if args.scale else []
        deterministic_lists = load_deterministic_lists(args.normalization_config, base_terms)
        probes = load_probes(args.probes) if args.probes else []
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    load_env()
    config = DetectorConfig.from_env()
    wanted: list[str | None] = [None] if args.prefilter_only or not args.models else list(args.models)
    try:
        chains = [resolve_terms_llm(model, config, 0 if args.prefilter_only else args.concurrency) for model in wanted]
    except (PrivacyGateError, ValueError) as exc:
        print(f"error: the term-correction LLM cannot be used: {exc}", file=sys.stderr)
        return 2
    if not args.prefilter_only and any(resolved.provider == "none" for resolved, _ in chains):
        print("error: the term-correction provider is 'none'; nothing to measure", file=sys.stderr)
        return 2

    prices = dict(args.price)
    measurements: list[dict[str, Any]] = []
    probe_results: list[dict[str, Any]] = []
    for resolved, clients in chains:
        model_run = (
            None
            if args.prefilter_only
            else ModelRun(
                clients=clients,
                model=resolved.model,
                temperature=config.llm_temperature,
                timeout_s=resolved.timeout_ms / 1000,
                runs=args.runs,
                show_flagged=args.show_flagged,
            )
        )
        for mode in ["term"] if args.prefilter_only else args.mode:
            try:
                measurements.append(
                    measure(
                        context_segments,
                        good_segments,
                        good_unusable,
                        base_terms,
                        provider=resolved.provider,
                        model=resolved.model,
                        deterministic_lists=deterministic_lists,
                        model_run=model_run,
                        scale_sizes=args.scale or (),
                        conversation=conversation,
                        mode=mode,
                        free_segments=free_segments,
                        price=prices.get(resolved.model),
                        eur_per_usd=args.eur_per_usd,
                    )
                )
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        if probes and model_run is not None:
            probe_results.append(asyncio.run(run_probes(probes, model_run, resolved.provider)))

    print("\n\n".join(format_summary(result) for result in measurements))
    if probe_results:
        print("\n" + "\n".join(format_probe_summary(result) for result in probe_results))
    if args.out:
        output: dict[str, Any] = {"measurements": measurements}
        if probe_results:
            output["probes"] = probe_results
        write_json(args.out, output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
