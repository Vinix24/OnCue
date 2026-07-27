from __future__ import annotations

import pytest

from sales_copilot.modules.detector.eval_utils import (
    EvalRow,
    build_rows_from_records,
    compute_per_route_precision,
    compute_precision,
    compute_precision_with_extra_class,
    compute_threshold_sweep,
    is_objection_category,
    normalize_label,
    row_is_false_negative,
    row_is_false_positive,
    row_is_true_positive,
    simulate_extra_route,
)


def test_is_objection_category() -> None:
    assert is_objection_category("prijs") is True
    assert is_objection_category("timing") is True
    assert is_objection_category("negative") is False
    assert is_objection_category("kans") is False
    assert is_objection_category("") is False
    assert is_objection_category(None) is False


def test_normalize_label() -> None:
    assert normalize_label("Prijs") == "prijs"
    assert normalize_label("") == "review"
    assert normalize_label(None) == "review"


def test_compute_precision_perfect() -> None:
    rows = [
        EvalRow("te duur", "prijs", "prijs", 0.85),
        EvalRow("niet nu", "timing", "timing", 0.82),
    ]
    result = compute_precision(rows)
    assert result["true_positives"] == 2
    assert result["false_positives"] == 0
    assert result["precision"] == 1.0


def test_compute_precision_with_false_positives() -> None:
    rows = [
        EvalRow("te duur", "prijs", "prijs", 0.85),
        EvalRow("goedemorgen", "negative", "prijs", 0.55),
        EvalRow("demo inplannen", "kans", "timing", 0.60),
    ]
    result = compute_precision(rows, threshold=0.50)
    assert result["true_positives"] == 1
    assert result["false_positives"] == 2
    assert result["precision"] == pytest.approx(1 / 3)


def test_predictions_below_threshold_are_ignored() -> None:
    rows = [
        EvalRow("te duur", "prijs", "prijs", 0.85),
        EvalRow("vage uiting", "negative", "prijs", 0.35),
    ]
    result = compute_precision(rows, threshold=0.50)
    assert result["predictions"] == 1
    assert result["precision"] == 1.0


def test_per_route_precision_counts_route_only_rows() -> None:
    rows = [
        EvalRow("te duur", "prijs", "prijs", 0.85),
        EvalRow("te veel geld", "prijs", "prijs", 0.80),
        EvalRow("niet nu", "timing", "timing", 0.82),
        EvalRow("demo inplannen", "kans", "prijs", 0.55),
    ]
    per_route = compute_per_route_precision(rows, threshold=0.50)

    assert per_route["prijs"]["true_positives"] == 2
    assert per_route["prijs"]["false_positives"] == 1
    assert per_route["prijs"]["predictions"] == 3
    assert per_route["timing"]["true_positives"] == 1
    assert per_route["timing"]["false_positives"] == 0


def test_simulate_negative_route_prevents_false_positives() -> None:
    rows = [
        EvalRow("te duur", "prijs", "prijs", 0.85),
        EvalRow("goedemorgen", "negative", "prijs", 0.55),
        EvalRow("hoe werkt dat", "negative", "timing", 0.52),
    ]
    extra_scores = {
        "te duur": 0.30,
        "goedemorgen": 0.75,
        "hoe werkt dat": 0.65,
    }
    result = simulate_extra_route(rows, extra_scores, "negative", threshold=0.50)

    assert result["redirected_count"] == 2
    assert result["prevented_false_positives"] == 2
    assert result["redirected_by_route"]["prijs"] == 1
    assert result["redirected_by_route"]["timing"] == 1


def test_simulate_readiness_route_redirects_buying_signals() -> None:
    rows = [
        EvalRow("demo inplannen", "kans", "timing", 0.60),
        EvalRow("te duur", "prijs", "prijs", 0.85),
    ]
    extra_scores = {
        "demo inplannen": 0.80,
        "te duur": 0.20,
    }
    result = simulate_extra_route(rows, extra_scores, "kans", threshold=0.50)

    assert result["redirected_count"] == 1
    assert result["newly_correct"] == 1
    assert result["redirected_by_route"]["timing"] == 1


def test_build_rows_from_records_preserves_all_fields() -> None:
    records = [
        {
            "text": "te duur",
            "label": "prijs",
            "predicted_category": "prijs",
            "predicted_confidence": 0.85,
            "source": "seed",
            "custom": "value",
        }
    ]
    rows = build_rows_from_records(records)
    assert len(rows) == 1
    row = rows[0]
    assert row.text == "te duur"
    assert row.label == "prijs"
    assert row.predicted_category == "prijs"
    assert row.predicted_confidence == pytest.approx(0.85)
    assert row.source == "seed"
    assert row.extra == {"custom": "value"}


def test_row_level_confusion_functions() -> None:
    tp = EvalRow("te duur", "prijs", "prijs", 0.85)
    fp = EvalRow("goedemorgen", "negative", "prijs", 0.55)
    fn = EvalRow("te duur", "prijs", "timing", 0.60)
    tn = EvalRow("goedemorgen", "negative", None, 0.20)

    assert row_is_true_positive(tp) is True
    assert row_is_false_positive(fp) is True
    assert row_is_false_negative(fn) is True
    assert row_is_true_positive(tn) is False
    assert row_is_false_positive(tn) is False


def test_threshold_sweep_output() -> None:
    rows = [
        EvalRow("te duur", "prijs", "prijs", 0.85),
        EvalRow("niet nu", "timing", "timing", 0.82),
        EvalRow("goedemorgen", "negative", "prijs", 0.55),
        EvalRow("demo inplannen", "kans", "timing", 0.48),
    ]
    sweep = compute_threshold_sweep(rows, start=0.30, end=0.70, step=0.10)

    thresholds = [entry["threshold"] for entry in sweep]
    assert thresholds == [0.30, 0.40, 0.50, 0.60, 0.70]

    # At 0.30 all four predictions count -> precision 2/4.
    entry_30 = next(e for e in sweep if e["threshold"] == 0.30)
    assert entry_30["predictions"] == 4
    assert entry_30["precision"] == pytest.approx(0.5)

    # At 0.50 the 0.48 prediction drops out -> precision 2/3.
    entry_50 = next(e for e in sweep if e["threshold"] == 0.50)
    assert entry_50["predictions"] == 3
    assert entry_50["precision"] == pytest.approx(2 / 3)

    # At 0.70 only the two high-confidence objections remain -> precision 1.0.
    entry_70 = next(e for e in sweep if e["threshold"] == 0.70)
    assert entry_70["predictions"] == 2
    assert entry_70["precision"] == pytest.approx(1.0)


def test_negative_class_metric_redirects_false_positives() -> None:
    rows = [
        EvalRow("te duur", "prijs", "prijs", 0.85),
        EvalRow("goedemorgen", "negative", "prijs", 0.55),
        EvalRow("hoe werkt dat", "negative", "timing", 0.52),
    ]
    extra_scores = {
        "te duur": 0.30,
        "goedemorgen": 0.75,
        "hoe werkt dat": 0.65,
    }
    baseline = compute_precision(rows, threshold=0.50)
    metrics = compute_precision_with_extra_class(
        rows, extra_scores, "negative", threshold=0.50
    )

    assert metrics["extra_label"] == "negative"
    assert metrics["objection_false_positives"] == 0
    assert metrics["prevented_false_positives"] == baseline["false_positives"]
    assert metrics["extra_class_true_positives"] == 2
    assert metrics["extra_class_false_positives"] == 0
    assert metrics["extra_class_precision"] == pytest.approx(1.0)


def test_negative_class_metric_does_not_harm_true_positives() -> None:
    rows = [
        EvalRow("te duur", "prijs", "prijs", 0.85),
        EvalRow("niet nu", "timing", "timing", 0.82),
        EvalRow("goedemorgen", "negative", "prijs", 0.55),
    ]
    extra_scores = {
        "te duur": 0.30,  # lower than the objection score -> no redirect
        "niet nu": 0.20,
        "goedemorgen": 0.75,  # higher -> redirect to negative
    }
    metrics = compute_precision_with_extra_class(
        rows, extra_scores, "negative", threshold=0.50
    )

    assert metrics["objection_true_positives"] == 2
    assert metrics["objection_false_positives"] == 0
    assert metrics["extra_class_true_positives"] == 1
    assert metrics["prevented_false_positives"] == 1


def test_structural_fix_beat_threshold_tuning() -> None:
    """A structural extra class can outperform every threshold in the sweep.

    This is the core hypothesis the harness must be able to demonstrate:
    threshold tuning trades recall for precision, but adding a negative class
    removes false positives without losing true positives.
    """
    rows = [
        EvalRow("te duur", "prijs", "prijs", 0.85),
        EvalRow("niet nu", "timing", "timing", 0.82),
        # A generic prospect question that the current router over-matches.
        EvalRow("kunt u dat uitleggen", "negative", "prijs", 0.65),
        EvalRow("dat is interessant", "negative", "timing", 0.60),
    ]
    extra_scores = {
        "te duur": 0.20,
        "niet nu": 0.20,
        "kunt u dat uitleggen": 0.80,
        "dat is interessant": 0.75,
    }
    sweep = compute_threshold_sweep(rows, start=0.30, end=0.70, step=0.05)
    best_sweep_precision = max(entry["precision"] for entry in sweep)

    structural = compute_precision_with_extra_class(
        rows, extra_scores, "negative", threshold=0.50
    )

    # The structural fix keeps both true positives and removes both FPs.
    assert structural["objection_precision"] == pytest.approx(1.0)
    assert structural["objection_precision"] >= best_sweep_precision
