"""Transcript parsing and seed-set generation for objection-precision mining.

These helpers are dependency-light so tests can run without loading the full
detector stack or embedding model.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from sales_copilot.core.paths import resolve_app_path

REPO_ROOT = resolve_app_path(".")

_SPEAKER_LINE = re.compile(r"^\*\*(.+?)\*\*\s*\*\[\d+:\d+\]\*:\s*(.*)$")

# Sales reps whose lines should never be mined as prospect utterances. Real
# Fireflies transcripts label speakers by their actual name (not a generic
# "Prospect" placeholder), so every other speaker on the call counts as the
# prospect unless a more specific --prospect-name is given. No names are
# hardcoded: the caller passes them (CLI flag or EVAL_EXCLUDED_SPEAKERS env var).
_DEFAULT_EXCLUDED_SPEAKERS: tuple[str, ...] = ()


def _normalize_speaker_name(name: str) -> str:
    """Strip the Fireflies `` | Company`` suffix and normalize for comparison."""
    return name.split("|", 1)[0].strip().lower()


class _Classifier(Protocol):
    """Minimal protocol for the classifier used during mining."""

    def classify(self, text: str) -> Any: ...


# Representative near-misses: semantically close to an objection route but not
# actually an objection. These are the cases where a router without a negative
# class is expected to over-match.
_REPRESENTATIVE_NEAR_MISSES: list[dict[str, str]] = [
    # Prijs-adjacent but not a price objection
    {"text": "we hebben dit jaar een ruim budget", "label": "negative", "source": "seed"},
    {"text": "de kosten zijn voor ons niet het probleem", "label": "negative", "source": "seed"},
    {"text": "het budget is goedgekeurd", "label": "negative", "source": "seed"},
    # Timing-adjacent
    {"text": "we zijn juist nu aan het opschalen", "label": "negative", "source": "seed"},
    {"text": "dit kwartaal hebben we volle focus", "label": "negative", "source": "seed"},
    # Concurrent-adjacent
    {"text": "we werken al jaren samen met onze huidige partner", "label": "concurrent", "source": "seed"},
    {"text": "onze leverancier doet dit grotendeels", "label": "concurrent", "source": "seed"},
    # Scope-adjacent
    {"text": "we willen eerst een grotere pilot", "label": "scope", "source": "seed"},
    {"text": "dit is precies de omvang die we zoeken", "label": "negative", "source": "seed"},
    # Autoriteit-adjacent
    {"text": "mijn manager is hier direct bij betrokken", "label": "negative", "source": "seed"},
]

# Pure negatives: generic prospect speech with no relation to an objection.
_REPRESENTATIVE_NEGATIVES: list[dict[str, str]] = [
    {"text": "kunt u dat nog eens uitleggen", "label": "negative", "source": "seed"},
    {"text": "wat zijn de volgende stappen", "label": "negative", "source": "seed"},
    {"text": "ik begrijp het idee", "label": "negative", "source": "seed"},
    {"text": "hoe zit het met de implementatie", "label": "negative", "source": "seed"},
    {"text": "kunnen we een demo inplannen", "label": "kans", "source": "seed"},
    {"text": "dit klinkt als een goede oplossing", "label": "kans", "source": "seed"},
    {"text": "waarom zou ik nu moeten veranderen", "label": "negative", "source": "seed"},
]


def _load_route_utterances(path: str | Path) -> list[dict[str, str]]:
    """Load utterances from any ``routes:`` yaml file and label them with their route.

    Domain-agnostic: shared by the objection and pain-point seed builders below.
    """
    from sales_copilot.core.config import load_yaml

    data = load_yaml(Path(path))
    rows: list[dict[str, str]] = []
    for route in data.get("routes", []):
        name = route["name"]
        for utterance in route.get("utterances", []):
            rows.append({"text": utterance, "label": name, "source": "seed"})
    return rows


def _load_objection_utterances(config_path: str | Path = "") -> list[dict[str, str]]:
    """Load the canonical objection utterances and label them with their route."""
    path = config_path or resolve_app_path("config/objections.yaml")
    return _load_route_utterances(path)


def _load_pain_point_utterances(config_path: str | Path = "") -> list[dict[str, str]]:
    """Load the canonical pain-point utterances and label them with their route."""
    path = config_path or resolve_app_path("config/pain_points.yaml")
    return _load_route_utterances(path)


# Representative near-misses for PainPointRouter: semantically close to a
# pain-point route but not actually that pain point. Mirrors
# _REPRESENTATIVE_NEAR_MISSES for the ObjectionRouter finding.
_REPRESENTATIVE_PAIN_POINT_NEAR_MISSES: list[dict[str, str]] = [
    # rapportage-adjacent but not a reporting complaint
    {"text": "we hebben er al drie jaar geleden naar gekeken", "label": "negative", "source": "seed"},
    # capaciteit-adjacent
    {"text": "we hebben er net twee mensen bij aangenomen", "label": "negative", "source": "seed"},
    # handmatig_werk-adjacent
    {"text": "dat doen we grotendeels al geautomatiseerd", "label": "negative", "source": "seed"},
    # kosten-adjacent
    {"text": "het budget hebben we er al voor gereserveerd", "label": "negative", "source": "seed"},
    # onboarding-adjacent
    {"text": "nieuwe mensen zijn hier na twee weken al zelfstandig", "label": "negative", "source": "seed"},
]

# Pure negatives / neutral filler for the pain-point domain: the live
# over-match this eval script was built to catch. "een maand of vijf, zes"
# was tagged `rapportage` at a hardcoded 0.95 on 2026-09-05 via a single
# shared 7+ char token clearing the old keyword score>=2 bar. Numbers, time
# spans, confirmations, and questions are what an actual Dutch sales
# conversation is full of without carrying a real pain-point signal.
_REPRESENTATIVE_PAIN_POINT_NEGATIVES: list[dict[str, str]] = [
    {"text": "een maand of vijf, zes", "label": "negative", "source": "seed"},
    {"text": "dat is ongeveer drie kwartier werk", "label": "negative", "source": "seed"},
    {"text": "over een week of twee weten we meer", "label": "negative", "source": "seed"},
    {"text": "ja dat klopt helemaal", "label": "negative", "source": "seed"},
    {"text": "precies dat is ook wat ik bedoel", "label": "negative", "source": "seed"},
    {"text": "wat bedoel je daar precies mee", "label": "negative", "source": "seed"},
    {"text": "kun je dat nog even toelichten", "label": "negative", "source": "seed"},
    {"text": "hoeveel mensen zitten er ongeveer in dat team", "label": "negative", "source": "seed"},
]


def seed_records() -> list[dict[str, str]]:
    """Build a small, representative seed eval set for ObjectionRouter."""
    records = _load_objection_utterances()
    records.extend(_REPRESENTATIVE_NEAR_MISSES)
    records.extend(_REPRESENTATIVE_NEGATIVES)
    return records


def pain_point_seed_records() -> list[dict[str, str]]:
    """Build a small, representative seed eval set for PainPointRouter.

    Mirrors ``seed_records()`` for the ObjectionRouter finding, scoped to the
    pain-point routes and their near-miss/negative traps.
    """
    records = _load_pain_point_utterances()
    records.extend(_REPRESENTATIVE_PAIN_POINT_NEAR_MISSES)
    records.extend(_REPRESENTATIVE_PAIN_POINT_NEGATIVES)
    return records


def utterances_from_markdown(
    md_path: Path,
    exclude_speakers: Sequence[str] | None = None,
    prospect_name: str | None = None,
) -> list[dict[str, str]]:
    """Extract prospect utterances from a Fireflies-style markdown transcript.

    Every speaker line matches ``**Name** *[mm:ss]*: text``. If ``prospect_name``
    is given, only lines from that speaker are mined. Otherwise every speaker
    NOT in ``exclude_speakers`` (default: none; pass the sales reps) counts as the
    prospect. Speaker names are matched case-insensitively and after stripping
    a Fireflies `` | Company`` suffix.
    """
    excluded = {
        _normalize_speaker_name(name)
        for name in (exclude_speakers if exclude_speakers is not None else _DEFAULT_EXCLUDED_SPEAKERS)
    }
    normalized_prospect = _normalize_speaker_name(prospect_name) if prospect_name else None

    spoken: list[dict[str, str]] = []
    for line in md_path.read_text(encoding="utf-8").splitlines():
        match = _SPEAKER_LINE.match(line.strip())
        if not match:
            continue
        speaker = _normalize_speaker_name(match.group(1))
        text = match.group(2).strip()

        if normalized_prospect is not None:
            if speaker != normalized_prospect:
                continue
        elif speaker in excluded:
            continue

        # Skip very short filler utterances for a fair eval set.
        if len(text) >= 10:
            spoken.append({"text": text, "label": "review", "source": md_path.stem})
    return spoken


def utterances_from_jsonl(jsonl_path: Path) -> list[dict[str, str]]:
    """Read a JSONL transcript with ``text`` and optional ``speaker`` fields."""
    spoken: list[dict[str, str]] = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        text = obj.get("text", "").strip()
        if len(text) >= 10:
            spoken.append({
                "text": text,
                "label": obj.get("label", "review"),
                "source": jsonl_path.stem,
            })
    return spoken


def load_jsonl_records(path: Path) -> list[dict[str, Any]]:
    """Read any JSONL file into a list of dicts."""
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def load_records(
    input_path: Path,
    exclude_speakers: Sequence[str] | None = None,
    prospect_name: str | None = None,
) -> list[dict[str, str]]:
    """Load raw records from a transcript file."""
    if input_path.suffix.lower() == ".jsonl":
        return utterances_from_jsonl(input_path)
    if input_path.suffix.lower() in {".md", ".markdown"}:
        return utterances_from_markdown(
            input_path, exclude_speakers=exclude_speakers, prospect_name=prospect_name
        )
    raise ValueError(f"Unsupported transcript format: {input_path.suffix}")


def classify_records(
    records: list[dict[str, str]],
    router: _Classifier,
    threshold: float = 0.50,
) -> list[dict[str, Any]]:
    """Run every record through the router and enrich with predictions."""
    enriched: list[dict[str, Any]] = []
    for rec in records:
        text = rec["text"]
        match = router.classify(text)
        predicted_category = getattr(match, "category", None) if match else None
        predicted_confidence = getattr(match, "confidence", 0.0) if match else 0.0

        label = rec.get("label", "review")
        # When the transcript comes from a real call we do not know the true
        # label yet; keep it as review. For seed data the label is already set.
        if label == "review" and predicted_category and predicted_confidence >= threshold:
            label = "review"

        enriched.append({
            "id": str(uuid.uuid4()),
            "text": text,
            "source": rec.get("source", ""),
            "predicted_category": predicted_category,
            "predicted_confidence": round(predicted_confidence, 6),
            "label": label,
        })
    return enriched
