"""Routing-quality eval for the embedding layer of the pain-point cascade.

``tests/test_detector_router.py`` exercises individual behaviors of
``PainPointRouter`` (a positive case, a none-case, latency, the keyword-overlap
rules). None of those tests measure whether the routing is actually *correct*
across the full route set in ``config/pain_points.yaml`` -- whether an
utterance that should hit a given route does, and whether utterances that
merely sound similar are correctly kept out.

This module measures exactly that, isolated to layer 2 of the detection
cascade (the semantic-router / sentence-transformers embedding match), the
layer that most pain-point decisions never escalate past. It deliberately
bypasses ``PainPointRouter._keyword_match`` (layer 1's exact/near-exact
fast path) via ``_embedding_only_classify`` below: ``classify()`` tries the
keyword path first, and mixing its hits into this measurement would attribute
keyword-layer behavior to the embedding layer. See
``sales_copilot.modules.detector.cascade_eval._classify_layer`` for the same
``embedding_fast_path`` vs. keyword (``deterministic_accept``) distinction in
the production pipeline.

The embedding model (``paraphrase-multilingual-MiniLM-L12-v2`` via
sentence-transformers) runs fully offline once cached and is deterministic
for a fixed input -- verified manually before writing this: same input
produces byte-identical ``RouteMatch`` output across repeated calls, no
network access, no temperature/sampling involved. That is what makes this a
plain, repeatable pytest instead of a one-off eval script like
``card_selection_eval.py`` (which drives a non-deterministic LLM and is
intentionally not a test).

Fixture: ``tests/fixtures/pain_point_routing_eval.jsonl``, five positive and
five hard-negative utterances per route (60 + 60 = 120 rows), written as
natural Dutch sales-call speech rather than keyword lists. The negatives are
deliberately hard: denials ("dat gaat eigenlijk prima"), backchannel/rambling
("oke duidelijk"), generic questions, and -- the most informative type --
utterances that are true positives for a *different*, semantically close
route (e.g. a `capaciteit` utterance offered as a `lead_generation` foil).

Precision/recall floors below are hardcoded from an actual measurement run
against this fixture (see the module docstring's measurement note in the
dispatch report), each relaxed by tolerating exactly one additional miss on
the observed sample size. They are not aspirational targets: several routes
(`lead_generation`, `cross_sell`, `kosten`) measured well under 0.7 precision
or recall and are flagged as findings in the dispatch report, not silently
propped up by a lenient threshold.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.router import PainPointRouter, RouteMatch

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "pain_point_routing_eval.jsonl"

ROUTE_NAMES = [
    "offerteproces",
    "capaciteit",
    "cross_sell",
    "lead_generation",
    "kennisontsluiting",
    "klantenservice",
    "handmatig_werk",
    "data_kwaliteit",
    "rapportage",
    "onboarding",
    "compliance",
    "kosten",
]

# Measured on this exact fixture (see dispatch report). Floor = observed
# count with one additional miss tolerated, not a re-derivation of the
# measurement itself -- a real regression still trips these.
PRECISION_FLOORS: dict[str, float] = {
    "offerteproces": 0.53,
    "capaciteit": 0.75,
    "cross_sell": 0.50,
    "lead_generation": 0.40,
    "kennisontsluiting": 0.83,
    "klantenservice": 0.85,
    "handmatig_werk": 0.77,
    "data_kwaliteit": 0.88,
    "rapportage": 0.77,
    "onboarding": 0.83,
    "compliance": 0.83,
    "kosten": 0.60,
}
RECALL_FLOORS: dict[str, float] = {
    "offerteproces": 0.75,
    "capaciteit": 0.62,
    "cross_sell": 0.42,
    "lead_generation": 0.42,
    "kennisontsluiting": 0.66,
    "klantenservice": 0.71,
    "handmatig_werk": 0.85,
    "data_kwaliteit": 0.77,
    "rapportage": 0.66,
    "onboarding": 0.57,
    "compliance": 0.80,
    "kosten": 0.33,
}
MACRO_PRECISION_FLOOR = 0.78
MACRO_RECALL_FLOOR = 0.73


def _embedding_only_classify(router: PainPointRouter, text: str) -> RouteMatch | None:
    """Mirror ``PainPointRouter.classify()``'s embedding branch only.

    ``classify()`` tries ``_keyword_match`` first and only falls back to the
    semantic router when that misses. Calling ``_ensure_router()`` directly
    reproduces the exact embedding-branch logic (threshold + negative-class
    filtering) without the keyword pre-filter, so this measures layer 2 in
    isolation. Accessing the private router/negative-set is the same pattern
    already used for ``_keyword_match`` in test_detector_router.py.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    choice = router._ensure_router()(cleaned)  # noqa: SLF001
    if isinstance(choice, list):
        choice = choice[0] if choice else None
    if choice is None or choice.name is None or choice.similarity_score is None:
        return None
    score = float(choice.similarity_score)
    if score < router.config.confidence_threshold_low:
        return None
    if choice.name in router._negative_categories:  # noqa: SLF001
        return None
    return RouteMatch(category=choice.name, confidence=score, tier=router._tier_for_score(score))  # noqa: SLF001


def _load_fixture() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _predict_all(router: PainPointRouter, rows: list[dict[str, Any]]) -> list[str]:
    """Return the predicted category (or ``"negative"`` for no match) per row, in order."""
    predictions = []
    for row in rows:
        match = _embedding_only_classify(router, row["text"])
        predictions.append(match.category if match is not None else "negative")
    return predictions


def _per_route_metrics(
    rows: list[dict[str, Any]], predictions: list[str]
) -> dict[str, dict[str, float | int]]:
    metrics: dict[str, dict[str, float | int]] = {}
    for route in ROUTE_NAMES:
        tp = sum(1 for row, pred in zip(rows, predictions) if row["label"] == route and pred == route)
        fp = sum(1 for row, pred in zip(rows, predictions) if pred == route and row["label"] != route)
        fn = sum(1 for row, pred in zip(rows, predictions) if row["label"] == route and pred != route)
        labeled = tp + fn
        predicted_n = tp + fp
        precision = tp / predicted_n if predicted_n else 0.0
        recall = tp / labeled if labeled else 0.0
        metrics[route] = {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "labeled": labeled,
            "predicted": predicted_n,
            "precision": precision,
            "recall": recall,
        }
    return metrics


@pytest.fixture(scope="module")
def pain_point_router() -> PainPointRouter:
    return PainPointRouter(DetectorConfig.from_env())


@pytest.fixture(scope="module")
def fixture_rows() -> list[dict[str, Any]]:
    return _load_fixture()


def test_fixture_has_five_positive_and_five_negative_per_route(fixture_rows: list[dict[str, Any]]) -> None:
    for route in ROUTE_NAMES:
        positives = [
            r for r in fixture_rows if r["kind"] == "positive" and r["target_route"] == route
        ]
        negatives = [
            r for r in fixture_rows if r["kind"] == "negative" and r["target_route"] == route
        ]
        assert len(positives) >= 5, f"{route}: expected >=5 positive utterances, got {len(positives)}"
        assert len(negatives) >= 5, f"{route}: expected >=5 negative utterances, got {len(negatives)}"
        # Every positive row must actually be labeled as its own route --
        # a labeling mistake here would silently invalid that route's recall.
        assert all(r["label"] == route for r in positives)


def test_embedding_routing_precision_recall_per_route(
    pain_point_router: PainPointRouter, fixture_rows: list[dict[str, Any]]
) -> None:
    predictions = _predict_all(pain_point_router, fixture_rows)
    metrics = _per_route_metrics(fixture_rows, predictions)

    failures = []
    for route in ROUTE_NAMES:
        m = metrics[route]
        if m["precision"] < PRECISION_FLOORS[route]:
            failures.append(
                f"{route}: precision {m['precision']:.3f} < floor {PRECISION_FLOORS[route]} "
                f"(tp={m['tp']} fp={m['fp']})"
            )
        if m["recall"] < RECALL_FLOORS[route]:
            failures.append(
                f"{route}: recall {m['recall']:.3f} < floor {RECALL_FLOORS[route]} "
                f"(tp={m['tp']} fn={m['fn']})"
            )
    assert not failures, "Routing quality regression:\n" + "\n".join(failures)


def test_embedding_routing_macro_precision_recall(
    pain_point_router: PainPointRouter, fixture_rows: list[dict[str, Any]]
) -> None:
    predictions = _predict_all(pain_point_router, fixture_rows)
    metrics = _per_route_metrics(fixture_rows, predictions)

    macro_precision = sum(m["precision"] for m in metrics.values()) / len(metrics)
    macro_recall = sum(m["recall"] for m in metrics.values()) / len(metrics)

    assert macro_precision >= MACRO_PRECISION_FLOOR, (
        f"macro precision {macro_precision:.3f} < floor {MACRO_PRECISION_FLOOR}"
    )
    assert macro_recall >= MACRO_RECALL_FLOOR, (
        f"macro recall {macro_recall:.3f} < floor {MACRO_RECALL_FLOOR}"
    )


def test_embedding_routing_is_deterministic(
    pain_point_router: PainPointRouter, fixture_rows: list[dict[str, Any]]
) -> None:
    """Same fixture, same model, three runs -- byte-identical predictions.

    This is what justifies treating the embedding layer as a plain pytest
    instead of a one-off eval script: no sampling, no network call, no
    hidden state between calls.
    """
    run_1 = _predict_all(pain_point_router, fixture_rows)
    run_2 = _predict_all(pain_point_router, fixture_rows)
    run_3 = _predict_all(pain_point_router, fixture_rows)

    assert run_1 == run_2 == run_3
