"""Pure helpers for objection-precision evaluation.

These functions operate on predicted labels, gold labels, and optional scores for
simulated extra routes (negative / readiness). They are model-free so tests can
exercise them with mocked routers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EvalRow:
    """One example in the objection-precision eval set."""

    text: str
    label: str
    predicted_category: str | None = None
    predicted_confidence: float = 0.0
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# Labels that do not represent a real objection category.
_NON_OBJECTION_LABELS = frozenset({
    "negative",
    "kans",
    "near_miss",
    "none",
    "",
    "review",
    # Opportunity / readiness routes loaded when include_opportunities=True.
    # These are distinct prediction classes, not objections.
    "gap-behoefte",
    "koopsignaal",
    "interesse-verdieping",
    "autoriteit-proces",
})


ALL_OBJECTION_ROUTES = frozenset({"prijs", "timing", "concurrent", "scope", "autoriteit"})
OPPORTUNITY_ROUTE_NAMES = frozenset({"gap-behoefte", "koopsignaal", "interesse-verdieping", "autoriteit-proces"})
NEGATIVE_ROUTE_NAMES = frozenset({"negative"})


def is_objection_category(label: str | None) -> bool:
    """Return True when ``label`` is a real objection route name."""
    if not label:
        return False
    return label.strip().lower() not in _NON_OBJECTION_LABELS


def normalize_label(label: str | None) -> str:
    """Return a lowercase label string, defaulting to 'review' when empty."""
    if not label:
        return "review"
    return str(label).strip().lower()


def row_has_prediction(row: EvalRow, threshold: float = 0.50) -> bool:
    """Return True if the row carries an objection prediction at/above threshold."""
    return row.predicted_category is not None and row.predicted_confidence >= threshold


def row_is_true_positive(row: EvalRow, threshold: float = 0.50) -> bool:
    """Prediction matches the gold objection category at/above threshold."""
    if not row_has_prediction(row, threshold):
        return False
    return is_objection_category(row.label) and row.predicted_category == row.label


def row_is_false_positive(row: EvalRow, threshold: float = 0.50) -> bool:
    """An objection prediction was made but the gold label disagrees.

    Non-objection predictions (e.g. opportunity / readiness routes added by the
    structural precision fix) do not count as objection false positives: they
    are routed away from objections into their own class.
    """
    if not row_has_prediction(row, threshold):
        return False
    if not is_objection_category(row.predicted_category):
        return False
    if not is_objection_category(row.label):
        return True
    return row.predicted_category != row.label


def row_is_false_negative(row: EvalRow, threshold: float = 0.50) -> bool:
    """Gold label is a real objection but no matching prediction was made."""
    if not is_objection_category(row.label):
        return False
    if not row_has_prediction(row, threshold):
        return True
    return row.predicted_category != row.label


def compute_precision(rows: list[EvalRow], threshold: float = 0.50) -> dict[str, Any]:
    """Overall precision, TP/FP counts and number of predictions at the given threshold."""
    tp = sum(1 for r in rows if row_is_true_positive(r, threshold))
    fp = sum(1 for r in rows if row_is_false_positive(r, threshold))
    fn = sum(1 for r in rows if row_is_false_negative(r, threshold))
    total_predictions = tp + fp
    precision = tp / total_predictions if total_predictions > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "predictions": total_predictions,
        "precision": precision,
        "recall": recall,
    }


def compute_threshold_sweep(
    rows: list[EvalRow],
    start: float = 0.30,
    end: float = 0.70,
    step: float = 0.05,
) -> list[dict[str, Any]]:
    """Compute precision/recall across a range of thresholds.

    Returns one metric dict per threshold so callers can see whether tuning the
    threshold alone solves precision problems, or whether a structural change
    (e.g. adding a negative/kans class) is required.
    """
    results: list[dict[str, Any]] = []
    threshold = start
    epsilon = 1e-9
    while threshold <= end + epsilon:
        metrics = compute_precision(rows, threshold=threshold)
        results.append(
            {
                "threshold": round(threshold, 2),
                **metrics,
            }
        )
        threshold += step
    return results


def compute_per_route_precision(
    rows: list[EvalRow],
    threshold: float = 0.50,
    routes: frozenset[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Precision broken down by objection route.

    For each route, consider rows whose gold label equals the route OR whose
    prediction equals the route. This yields a route-level confusion matrix.
    """
    route_names = routes or ALL_OBJECTION_ROUTES
    result: dict[str, dict[str, Any]] = {}
    for route in sorted(route_names):
        route_rows = [
            r for r in rows if r.label == route or r.predicted_category == route
        ]
        result[route] = compute_precision(route_rows, threshold)
    return result


def simulate_extra_route(
    rows: list[EvalRow],
    extra_scores: dict[str, float],
    extra_label: str,
    threshold: float = 0.50,
) -> dict[str, Any]:
    """Simulate adding an extra route (e.g. negative or readiness/kans).

    ``extra_scores`` maps the row text to the simulated extra-route confidence.
    If the extra route outranks the existing objection prediction (or there was
    none) and passes ``threshold``, the row is redirected to ``extra_label``.

    Returns counts of redirected rows, prevented false positives, and a per-route
    breakdown of where the redirections came from.
    """
    redirected = 0
    prevented_fps = 0
    newly_correct = 0
    redirected_by_route: dict[str, int] = {}

    for row in rows:
        existing_score = row.predicted_confidence if row.predicted_category else 0.0
        extra_score = extra_scores.get(row.text, 0.0)
        if extra_score < threshold or extra_score <= existing_score:
            continue

        redirected += 1
        if row.predicted_category:
            redirected_by_route[row.predicted_category] = (
                redirected_by_route.get(row.predicted_category, 0) + 1
            )

        if row_is_false_positive(row, threshold):
            prevented_fps += 1
        if normalize_label(row.label) == normalize_label(extra_label):
            newly_correct += 1

    return {
        "extra_label": extra_label,
        "threshold": threshold,
        "redirected_count": redirected,
        "prevented_false_positives": prevented_fps,
        "newly_correct": newly_correct,
        "redirected_by_route": redirected_by_route,
    }


def _row_with_extra_class(
    row: EvalRow,
    extra_scores: dict[str, float],
    extra_label: str,
    threshold: float = 0.50,
) -> EvalRow:
    """Return ``row`` with ``extra_label`` winning when it passes threshold and outranks the existing prediction."""
    existing_score = row.predicted_confidence if row.predicted_category else 0.0
    extra_score = extra_scores.get(row.text, 0.0)
    if extra_score >= threshold and extra_score > existing_score:
        return EvalRow(
            text=row.text,
            label=row.label,
            predicted_category=extra_label,
            predicted_confidence=extra_score,
            source=row.source,
            extra=row.extra,
        )
    return row


def compute_precision_with_extra_class(
    rows: list[EvalRow],
    extra_scores: dict[str, float],
    extra_label: str,
    threshold: float = 0.50,
) -> dict[str, Any]:
    """Compute precision/recall as if ``extra_label`` is a real prediction class.

    This lets the harness compare two hypotheses side by side:

    1. Over-matching can be fixed by threshold tuning alone.
    2. Over-matching is structural and requires an extra negative / readiness
       class that the router can actually predict.

    For each row the extra class replaces the original prediction only when its
    score strictly exceeds the existing objection score. Metrics are reported
    separately for objection predictions, the extra class itself, and the
    combined multi-class view.
    """
    modified_rows = [
        _row_with_extra_class(row, extra_scores, extra_label, threshold=threshold)
        for row in rows
    ]

    # Objection-only precision: among rows still predicted as an objection route.
    objection_predictions = [
        r for r in modified_rows if is_objection_category(r.predicted_category)
    ]
    tp_obj = sum(1 for r in objection_predictions if r.predicted_category == r.label)
    fp_obj = sum(1 for r in objection_predictions if r.predicted_category != r.label)
    obj_precision = tp_obj / (tp_obj + fp_obj) if (tp_obj + fp_obj) > 0 else 0.0

    # Extra-class precision: how often the new class is correct.
    extra_predictions = [
        r for r in modified_rows if r.predicted_category == extra_label
    ]
    extra_tp = sum(
        1 for r in extra_predictions if normalize_label(r.label) == extra_label
    )
    extra_fp = sum(
        1 for r in extra_predictions if normalize_label(r.label) != extra_label
    )
    extra_precision = extra_tp / (extra_tp + extra_fp) if (extra_tp + extra_fp) > 0 else 0.0

    # Multi-class precision/recall over all active predictions.
    all_predictions = [r for r in modified_rows if row_has_prediction(r, threshold)]
    tp_all = sum(1 for r in all_predictions if r.predicted_category == r.label)
    fp_all = sum(1 for r in all_predictions if r.predicted_category != r.label)
    overall_precision = tp_all / (tp_all + fp_all) if (tp_all + fp_all) > 0 else 0.0

    relevant_labels = set(ALL_OBJECTION_ROUTES) | {extra_label}
    relevant_rows = [
        r for r in modified_rows if normalize_label(r.label) in relevant_labels
    ]
    tp_recall = sum(1 for r in relevant_rows if r.predicted_category == r.label)
    fn_recall = sum(1 for r in relevant_rows if r.predicted_category != r.label)
    overall_recall = (
        tp_recall / (tp_recall + fn_recall) if (tp_recall + fn_recall) > 0 else 0.0
    )

    # False positives prevented compared to the baseline at the same threshold.
    baseline_fp = sum(1 for r in rows if row_is_false_positive(r, threshold))
    prevented_fp = baseline_fp - fp_obj

    return {
        "extra_label": extra_label,
        "threshold": threshold,
        "overall_precision": overall_precision,
        "overall_recall": overall_recall,
        "objection_precision": obj_precision,
        "objection_predictions": len(objection_predictions),
        "objection_true_positives": tp_obj,
        "objection_false_positives": fp_obj,
        "extra_class_precision": extra_precision,
        "extra_class_predictions": len(extra_predictions),
        "extra_class_true_positives": extra_tp,
        "extra_class_false_positives": extra_fp,
        "prevented_false_positives": prevented_fp,
    }


def build_rows_from_records(records: list[dict[str, Any]]) -> list[EvalRow]:
    """Convert raw JSONL records into ``EvalRow`` objects."""
    rows: list[EvalRow] = []
    for rec in records:
        rows.append(
            EvalRow(
                text=str(rec.get("text", "")),
                label=normalize_label(rec.get("label")),
                predicted_category=rec.get("predicted_category") or None,
                predicted_confidence=float(rec.get("predicted_confidence", 0.0) or 0.0),
                source=str(rec.get("source", "")),
                extra={k: v for k, v in rec.items() if k not in {
                    "text", "label", "predicted_category", "predicted_confidence", "source"
                }},
            )
        )
    return rows
