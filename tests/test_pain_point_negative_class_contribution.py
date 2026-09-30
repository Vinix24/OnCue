"""Measures whether ``PainPointRouter``'s negative ("none") class earns its keep.

``_configure_negative_class`` loads ``config/negatives.yaml`` as an extra route
that competes against the real pain-point routes inside the same
``SemanticRouter``/keyword-overlap match, and ``classify()`` filters out any
match that resolves to it. It landed as a structural precision fix mirroring
``ObjectionRouter``'s ``INCLUDE_NEGATIVES`` (see the class docstring in
``router.py``), but its actual effect on this fixture had never been measured:
the mechanism existing is not evidence that it changes any outcome, because
``confidence_threshold_low`` already rejects plenty of weak matches on its own.

This module isolates the negative class's contribution from the threshold's by
running the exact same fixture through ``classify()`` twice, with
``include_negatives`` flipped, and by attributing every rejection to one of
two mutually exclusive causes:

- ``"threshold"`` -- nothing (keyword or embedding) cleared
  ``confidence_threshold_high``/``confidence_threshold_low`` at all.
- ``"negative_class"`` -- something matched a real route candidate, but the
  best match resolved to the negative class and was filtered out in
  ``classify()``'s last line.

``_classify_with_reason`` below is not new logic: it is ``classify()``,
copied verbatim, with the reason recorded at each of its two rejection exits
instead of both collapsing to the same ``None``.

## Measured result (see dispatch report for the full table)

The negative class is not inert, on either layer: of the 28
``negative_class``-attributed rejections on the full 120-row fixture, 15 come
from the keyword path and 13 from the embedding path -- roughly even, not
concentrated in one layer as might be assumed from the keyword path's recent
"high"-tier-only short-circuit (#235).

On the 60 hard-negative rows specifically, ``include_negatives=False`` lets
through 14 wrong-route errors (a real route wins the match, but it is not the
row's true label). Adding the negative class back converts 7 of those 14 into
a correct rejection instead. That is the mechanism doing real work, not a
guard that never fires.

It is not free, though: across the full fixture the negative class also
introduces 4 rejections of its own that would otherwise have been correct --
1 genuine positive utterance (a true ``offerteproces`` case) and 3 cross-route
foils (rows whose true label is a *different* real route, e.g.
``lead_generation``, ``data_kwaliteit``, ``kosten``) that the negative class
swallows instead of letting the real-route match through. Net effect on macro
precision/recall: precision 0.818 -> 0.859 (+0.041), recall 0.852 -> 0.819
(-0.033). The trade is a real precision gain at a smaller recall cost, not a
one-sided improvement -- both directions are asserted below.

Regression test below pins the measured catch count on the 60 hard-negative
fixture rows: with the negative class in place,
``NEGATIVE_CLASS_CATCH_FLOOR`` of the wrong-route errors that
``include_negatives=False`` lets through become correct rejections instead.
If a future change makes the negative class inert, this trips.
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

# Measured on this exact fixture with include_negatives=True (production
# default), full classify() path, 60 hard-negative rows (kind="negative").
# Of the 14 wrong-route errors include_negatives=False lets through on those
# 60 rows, 7 are converted into a correct rejection once the negative class is
# back in. Floor tolerates one fewer catch than observed.
NEGATIVE_CLASS_CATCH_FLOOR = 6


def _load_fixture() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _classify_with_reason(router: PainPointRouter, text: str) -> tuple[str, str | None]:
    """Mirror ``PainPointRouter.classify()`` exactly, additionally reporting
    *why* a rejection happened.

    Returns ``(category, reason)`` where ``category`` is the accepted route
    name or the literal string ``"negative"`` for a rejection, and ``reason``
    is ``None`` for an acceptance, ``"threshold"`` when nothing cleared
    ``confidence_threshold_low``, or ``"negative_class"`` when a real
    candidate matched but resolved to the negative class.
    """
    cleaned = text.strip()
    if not cleaned:
        return "negative", "threshold"

    keyword_match = router._keyword_match(cleaned.lower())  # noqa: SLF001
    match = keyword_match if keyword_match is not None and keyword_match.tier == "high" else None
    if match is None:
        choice = router._ensure_router()(cleaned)  # noqa: SLF001
        if isinstance(choice, list):
            choice = choice[0] if choice else None
        if choice is None or choice.name is None or choice.similarity_score is None:
            return "negative", "threshold"
        score = float(choice.similarity_score)
        if score < router.config.confidence_threshold_low:
            return "negative", "threshold"
        match = RouteMatch(category=choice.name, confidence=score, tier=router._tier_for_score(score))

    if match.category in router._negative_categories:  # noqa: SLF001
        return "negative", "negative_class"
    return match.category, None


def _predict_all(router: PainPointRouter, rows: list[dict[str, Any]]) -> tuple[list[str], list[str | None]]:
    predictions: list[str] = []
    reasons: list[str | None] = []
    for row in rows:
        category, reason = _classify_with_reason(router, row["text"])
        predictions.append(category)
        reasons.append(reason)
    return predictions, reasons


def _per_route_metrics(rows: list[dict[str, Any]], predictions: list[str]) -> dict[str, dict[str, float | int]]:
    metrics: dict[str, dict[str, float | int]] = {}
    for route in ROUTE_NAMES:
        tp = sum(1 for row, pred in zip(rows, predictions) if row["label"] == route and pred == route)
        fp = sum(1 for row, pred in zip(rows, predictions) if pred == route and row["label"] != route)
        fn = sum(1 for row, pred in zip(rows, predictions) if row["label"] == route and pred != route)
        labeled = tp + fn
        predicted_n = tp + fp
        precision = tp / predicted_n if predicted_n else 0.0
        recall = tp / labeled if labeled else 0.0
        metrics[route] = {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall}
    return metrics


def _macro(metrics: dict[str, dict[str, float | int]]) -> tuple[float, float]:
    precision = sum(m["precision"] for m in metrics.values()) / len(metrics)
    recall = sum(m["recall"] for m in metrics.values()) / len(metrics)
    return precision, recall


@pytest.fixture(scope="module")
def fixture_rows() -> list[dict[str, Any]]:
    return _load_fixture()


@pytest.fixture(scope="module")
def router_with_negatives() -> PainPointRouter:
    return PainPointRouter(DetectorConfig.from_env(), include_negatives=True)


@pytest.fixture(scope="module")
def router_without_negatives() -> PainPointRouter:
    return PainPointRouter(DetectorConfig.from_env(), include_negatives=False)


def test_negative_class_rejection_breakdown_and_precision_recall(
    router_with_negatives: PainPointRouter,
    router_without_negatives: PainPointRouter,
    fixture_rows: list[dict[str, Any]],
) -> None:
    """The load-bearing measurement: precision/recall for both configurations,
    plus what fraction of ``include_negatives=True``'s rejections are actually
    caused by the negative class (as opposed to the threshold alone, which
    fires with or without it).
    """
    with_preds, with_reasons = _predict_all(router_with_negatives, fixture_rows)
    without_preds, without_reasons = _predict_all(router_without_negatives, fixture_rows)

    with_metrics = _per_route_metrics(fixture_rows, with_preds)
    without_metrics = _per_route_metrics(fixture_rows, without_preds)
    with_macro_p, with_macro_r = _macro(with_metrics)
    without_macro_p, without_macro_r = _macro(without_metrics)

    rejections_with = [r for r in with_reasons if r is not None]
    negative_class_rejections = sum(1 for r in rejections_with if r == "negative_class")
    threshold_rejections = sum(1 for r in rejections_with if r == "threshold")

    # include_negatives=False can never produce a "negative_class" reason --
    # there is no negative class to resolve to.
    assert all(r == "threshold" for r in without_reasons if r is not None)

    detail = (
        f"with negatives: macro precision={with_macro_p:.3f} recall={with_macro_r:.3f}, "
        f"rejections={len(rejections_with)} (negative_class={negative_class_rejections}, "
        f"threshold={threshold_rejections})\n"
        f"without negatives: macro precision={without_macro_p:.3f} recall={without_macro_r:.3f}, "
        f"rejections={sum(1 for r in without_reasons if r is not None)}"
    )

    # The mechanism must fire on at least one row on this fixture, or the
    # whole measurement below is vacuous.
    assert negative_class_rejections > 0, f"negative class never won a match on this fixture\n{detail}"

    # Pins the measured trade-off in both directions: the negative class buys
    # a real macro-precision gain (0.818 -> 0.859) at a real macro-recall cost
    # (0.852 -> 0.819). Floors below tolerate a small margin below each
    # measured value; a regression in either direction (mechanism going
    # inert, or getting meaningfully more harmful) trips one of the four.
    assert with_macro_p >= 0.84, f"with-negatives macro precision regressed\n{detail}"
    assert with_macro_r >= 0.80, f"with-negatives macro recall regressed\n{detail}"
    assert without_macro_p >= 0.80, f"without-negatives macro precision regressed\n{detail}"
    assert without_macro_r >= 0.83, f"without-negatives macro recall regressed\n{detail}"


def test_negative_class_catch_on_hard_negative_rows(
    router_with_negatives: PainPointRouter,
    router_without_negatives: PainPointRouter,
    fixture_rows: list[dict[str, Any]],
) -> None:
    """The specific question this dispatch asks: of the 60 hard-negative
    fixture rows (``kind == "negative"``), how many does the negative class
    catch that the threshold alone would have let through as a wrong route?

    "Caught" means: ``include_negatives=False`` accepts a real-route match
    that is wrong (``prediction != row["label"]``), and on that exact same
    row, ``include_negatives=True`` rejects via ``"negative_class"`` instead
    of repeating (or worsening) the error.
    """
    hard_negative_rows = [row for row in fixture_rows if row["kind"] == "negative"]
    assert len(hard_negative_rows) == 60

    with_preds, with_reasons = _predict_all(router_with_negatives, hard_negative_rows)
    without_preds, _without_reasons = _predict_all(router_without_negatives, hard_negative_rows)

    wrong_without_negatives = [
        idx
        for idx, (row, pred) in enumerate(zip(hard_negative_rows, without_preds))
        if pred != "negative" and pred != row["label"]
    ]
    caught_by_negative_class = [idx for idx in wrong_without_negatives if with_reasons[idx] == "negative_class"]

    detail = (
        f"wrong-route errors without negative class: {len(wrong_without_negatives)}/60, "
        f"of which caught (rejected via negative_class) with it back in: "
        f"{len(caught_by_negative_class)}\n"
        "caught rows: "
        + ", ".join(
            f"{hard_negative_rows[idx]['text']!r} (would-be={without_preds[idx]!r}, "
            f"true label={hard_negative_rows[idx]['label']!r})"
            for idx in caught_by_negative_class
        )
    )

    assert len(caught_by_negative_class) >= NEGATIVE_CLASS_CATCH_FLOOR, (
        f"negative class catches fewer wrong-route errors than the measured floor\n{detail}"
    )

    # Every "catch" must be a strict improvement: the row must not already
    # have been correctly classified by the raw prediction (this fixture asks
    # about ERRORS specifically, per the wrong_without_negatives filter above,
    # so this assertion documents rather than re-derives that invariant).
    for idx in caught_by_negative_class:
        assert with_preds[idx] == "negative"
        assert without_preds[idx] != hard_negative_rows[idx]["label"]
