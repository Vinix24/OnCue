"""Full-cascade (keyword + embedding) routing-quality eval for ``PainPointRouter``.

``tests/test_pain_point_routing_quality.py`` measures layer 2 (the embedding
match) in isolation, deliberately bypassing ``_keyword_match`` via
``_embedding_only_classify`` -- see that module's docstring. It never measures
what ``classify()`` actually returns in production, where the keyword fast
path runs FIRST and can short-circuit the embedding layer entirely.

This module measures the full ``classify()`` path on the same 120-row fixture
(``tests/fixtures/pain_point_routing_eval.jsonl``) and adds an attribution
split on top of precision/recall: how many accepted matches came from
``source="keyword"`` vs ``source="embedding"``, and how many of the
keyword-sourced ones were actually wrong.

## What this measurement found (see dispatch report for the full before/after)

Before the fix in this dispatch, ``classify()`` accepted a keyword match at
ANY tier (``_keyword_match``'s "uncertain" tier included) and never consulted
the embedding layer when the keyword path found anything at all. Measured on
this fixture: 79 of 120 utterances were decided by the keyword path, and 21 of
those 79 (26.6%) were wrong -- a keyword match at "uncertain" tier (partial
word overlap, confidence 0.55-0.84) was right only 31/54 times (~57%), barely
better than a coin flip, yet it fully bypassed both the embedding layer and
any LLM confirmation because ``classify()`` treated "found a keyword match" and
"trust the keyword match" as the same thing.

A second, independent defect lived in ``_keyword_match``'s substring branch:
``text in utterance`` (the live transcript contained inside a stored route
utterance) was treated as an exact quote worth confidence 1.0/"high", when in
fact it means a short or generic transcript fragment ("dashboard", "klanten")
merely happens to be a substring of some longer stored phrase -- containment
of a fragment is not evidence about what the fragment itself carries. This
fixture's substring hits happened to all be the other, safe direction
(``utterance in text``: the full known phrase said verbatim), so this defect
does not move the numbers below, but ``test_short_generic_fragment_is_not_an_exact_quote_match``
below reproduces it directly.

The fix (in ``router.py``): (1) only ``utterance in text`` counts as an exact
quote; the reverse direction falls through to the same token-overlap
evidence as everything else. (2) ``classify()`` only lets the keyword path
short-circuit the embedding layer when ``_keyword_match`` returns "high" tier
(measured 43/44 correct on this fixture, ~98%) -- "uncertain" tier keyword
evidence is discarded and the embedding layer decides instead, exactly as if
the keyword path had found nothing.

Floors below are hardcoded from an actual post-fix measurement run against
this fixture (see the dispatch report), each relaxed by tolerating exactly
one additional miss on the observed sample size -- the same convention
``test_pain_point_routing_quality.py`` uses. They are not aspirational
targets.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.router import PainPointRouter

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

# Measured on this exact fixture, full classify() path (see dispatch report).
# Floor = observed count with one additional miss tolerated.
PRECISION_FLOORS: dict[str, float] = {
    "offerteproces": 0.64,
    "capaciteit": 0.75,
    "cross_sell": 0.64,
    "lead_generation": 0.44,
    "kennisontsluiting": 0.83,
    "klantenservice": 0.86,
    "handmatig_werk": 0.78,
    "data_kwaliteit": 0.88,
    "rapportage": 0.78,
    "onboarding": 0.83,
    "compliance": 0.83,
    "kosten": 0.67,
}
RECALL_FLOORS: dict[str, float] = {
    "offerteproces": 0.78,
    "capaciteit": 0.67,
    "cross_sell": 0.88,
    "lead_generation": 0.50,
    "kennisontsluiting": 0.71,
    "klantenservice": 0.75,
    "handmatig_werk": 0.88,
    "data_kwaliteit": 0.70,
    "rapportage": 0.70,
    "onboarding": 0.62,
    "compliance": 0.83,
    "kosten": 0.57,
}
MACRO_PRECISION_FLOOR = 0.83
MACRO_RECALL_FLOOR = 0.79

# Measured: 0 of 29 keyword-sourced accepts were wrong post-fix (was 21 of 79
# pre-fix). One regression tolerated before this trips.
KEYWORD_SOURCED_WRONG_FLOOR = 1


def _load_fixture() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _predict_all(
    router: PainPointRouter, rows: list[dict[str, Any]]
) -> tuple[list[str], list[str | None]]:
    """Return (predicted category or "negative", match source or None) per row, in order."""
    predictions: list[str] = []
    sources: list[str | None] = []
    for row in rows:
        match = router.classify(row["text"])
        predictions.append(match.category if match is not None else "negative")
        sources.append(match.source if match is not None else None)
    return predictions, sources


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


def _attribution(
    rows: list[dict[str, Any]], predictions: list[str], sources: list[str | None]
) -> dict[str, int]:
    keyword_n = sum(1 for s in sources if s == "keyword")
    embedding_n = sum(1 for s in sources if s == "embedding")
    keyword_wrong = sum(
        1
        for row, pred, src in zip(rows, predictions, sources)
        if src == "keyword" and row["label"] != pred
    )
    return {"keyword": keyword_n, "embedding": embedding_n, "keyword_wrong": keyword_wrong}


@pytest.fixture(scope="module")
def pain_point_router() -> PainPointRouter:
    return PainPointRouter(DetectorConfig.from_env())


@pytest.fixture(scope="module")
def fixture_rows() -> list[dict[str, Any]]:
    return _load_fixture()


def test_full_path_precision_recall_per_route(
    pain_point_router: PainPointRouter, fixture_rows: list[dict[str, Any]]
) -> None:
    predictions, _sources = _predict_all(pain_point_router, fixture_rows)
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
    assert not failures, "Full-path routing quality regression:\n" + "\n".join(failures)


def test_full_path_macro_precision_recall(
    pain_point_router: PainPointRouter, fixture_rows: list[dict[str, Any]]
) -> None:
    predictions, _sources = _predict_all(pain_point_router, fixture_rows)
    metrics = _per_route_metrics(fixture_rows, predictions)

    macro_precision = sum(m["precision"] for m in metrics.values()) / len(metrics)
    macro_recall = sum(m["recall"] for m in metrics.values()) / len(metrics)

    assert macro_precision >= MACRO_PRECISION_FLOOR, (
        f"macro precision {macro_precision:.3f} < floor {MACRO_PRECISION_FLOOR}"
    )
    assert macro_recall >= MACRO_RECALL_FLOOR, (
        f"macro recall {macro_recall:.3f} < floor {MACRO_RECALL_FLOOR}"
    )


def test_keyword_fast_path_false_fire_rate(
    pain_point_router: PainPointRouter, fixture_rows: list[dict[str, Any]]
) -> None:
    """The keyword path must earn its short-circuit: wrong keyword-sourced
    accepts must stay rare. Pre-fix this was 21/79 (26.6%); post-fix 0/29."""
    predictions, sources = _predict_all(pain_point_router, fixture_rows)
    attribution = _attribution(fixture_rows, predictions, sources)

    assert attribution["keyword"] > 0, "expected the keyword path to fire on at least one utterance"
    assert attribution["keyword_wrong"] <= KEYWORD_SOURCED_WRONG_FLOOR, (
        f"keyword-sourced wrong={attribution['keyword_wrong']} of {attribution['keyword']} accepts "
        f"exceeds floor {KEYWORD_SOURCED_WRONG_FLOOR}"
    )


# --- Regression tests for the two specific false-fire shapes ---------------


def test_uncertain_tier_keyword_match_no_longer_short_circuits(
    pain_point_router: PainPointRouter,
) -> None:
    """Structural defect 1: an "uncertain" tier keyword match used to be
    accepted as the final answer, skipping the embedding layer entirely, even
    though that tier measured ~57% precision on the fixture. It must now be
    discarded in favor of the embedding layer's own decision.

    This exact utterance is a real fixture row (label=capaciteit):
    ``_keyword_match`` alone still resolves it to the wrong category
    (lead_generation, "uncertain" tier, partial word overlap on "mensen"/
    "werk") -- that weak signal is not itself removed. What changed is that
    ``classify()`` no longer trusts it: it now defers to the embedding layer,
    which gets this one right.
    """
    text = "We hebben simpelweg te weinig handen aan het bed om alles op te pakken."

    keyword_only = pain_point_router._keyword_match(text.lower())  # noqa: SLF001
    assert keyword_only is not None
    assert keyword_only.tier == "uncertain"
    assert keyword_only.category == "lead_generation"  # the wrong, discarded signal

    full = pain_point_router.classify(text)
    assert full is not None
    assert full.source == "embedding"
    assert full.category == "capaciteit"


def test_uncertain_tier_keyword_match_defers_even_when_embedding_also_misses(
    pain_point_router: PainPointRouter,
) -> None:
    """Same defect, different angle: even when the embedding layer's own
    answer for this utterance is imperfect, ``classify()`` must not fall back
    to the discarded "uncertain" keyword guess -- the source must stay
    "embedding" (or None), never "keyword", once the keyword tier is
    "uncertain".
    """
    text = "We hebben geen goede audit trail van wie wat heeft goedgekeurd."

    keyword_only = pain_point_router._keyword_match(text.lower())  # noqa: SLF001
    assert keyword_only is not None
    assert keyword_only.tier == "uncertain"

    full = pain_point_router.classify(text)
    if full is not None:
        assert full.source != "keyword"


def test_short_generic_fragment_is_not_an_exact_quote_match(
    pain_point_router: PainPointRouter,
) -> None:
    """Structural defect 2: a short/generic live-transcript fragment that
    happens to be a substring of some longer stored route utterance used to
    score confidence 1.0/"high" via the `text in utterance` direction. A
    single generic word is not an exact quote of pain-point content, and this
    fixture never exercised that direction (its substring hits all went the
    other way), so this must be tested directly.
    """
    assert pain_point_router._keyword_match("dashboard") is None  # noqa: SLF001
    assert pain_point_router._keyword_match("klanten") is None  # noqa: SLF001
    assert pain_point_router._keyword_match("compliant") is None  # noqa: SLF001


def test_full_utterance_quote_still_matches_at_full_confidence(
    pain_point_router: PainPointRouter,
) -> None:
    """The safe direction of the substring branch (a full stored route
    utterance said verbatim inside a longer live transcript) must keep
    scoring as an exact quote -- this is real evidence, unlike a short
    fragment merely being contained in a longer stored phrase."""
    match = pain_point_router._keyword_match(  # noqa: SLF001
        "we hebben geen dashboard waarin we in één oogopslag de cijfers zien."
    )
    assert match is not None
    assert match.category == "rapportage"
    assert match.confidence == 1.0
    assert match.tier == "high"


def test_embedding_layer_metrics_are_unaffected_by_the_keyword_path_fix(
    pain_point_router: PainPointRouter, fixture_rows: list[dict[str, Any]]
) -> None:
    """Sanity check that this dispatch's fix lives entirely in the keyword
    path and how ``classify()`` gates it: the embedding-only measurement in
    ``test_pain_point_routing_quality.py`` must not have moved. This does not
    re-run that module (it stays untouched); it independently reproduces its
    isolation call to confirm the same layer-2 predictions this fixture would
    have produced before this dispatch's changes.
    """
    from sales_copilot.modules.detector.router import RouteMatch

    def embedding_only_classify(router: PainPointRouter, text: str) -> RouteMatch | None:
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

    predictions = [
        (embedding_only_classify(pain_point_router, row["text"]) or RouteMatch("negative", 0.0, "none")).category
        for row in fixture_rows
    ]
    metrics = _per_route_metrics(fixture_rows, predictions)
    macro_precision = sum(m["precision"] for m in metrics.values()) / len(metrics)
    macro_recall = sum(m["recall"] for m in metrics.values()) / len(metrics)

    # Same floors test_pain_point_routing_quality.py asserts for the embedding
    # layer -- confirms _keyword_match's changes did not alter layer 2 at all
    # (neither call touches _keyword_match).
    assert macro_precision >= 0.78
    assert macro_recall >= 0.73
