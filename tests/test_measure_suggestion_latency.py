"""Unit tests for scripts/measure_suggestion_latency.py: statistics, argument parsing, fragment loading.

No network: only the pure helpers are exercised.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_suggestion_latency.py"
_spec = importlib.util.spec_from_file_location("measure_suggestion_latency", _SCRIPT)
assert _spec is not None and _spec.loader is not None
msl = importlib.util.module_from_spec(_spec)
sys.modules["measure_suggestion_latency"] = msl
_spec.loader.exec_module(msl)


def test_median_odd_even_and_empty() -> None:
    assert msl.median([3.0, 1.0, 2.0]) == 2.0
    assert msl.median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert msl.median([]) is None


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 21)]  # 1..20
    assert msl.percentile(values, 95) == 19.0
    assert msl.percentile(values, 50) == 10.0
    assert msl.percentile(values, 100) == 20.0
    assert msl.percentile([7.0], 95) == 7.0
    assert msl.percentile([], 95) is None


def test_percentile_p95_of_thirty_is_29th_value() -> None:
    values = [float(v) for v in range(1, 31)]
    assert msl.percentile(values, 95) == 29.0


def test_percentile_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        msl.percentile([1.0], 0)
    with pytest.raises(ValueError):
        msl.percentile([1.0], 101)


def test_error_rate() -> None:
    assert msl.error_rate(3, 30) == pytest.approx(0.1)
    assert msl.error_rate(0, 0) == 0.0


def test_mode_stats_summary_counts_errors_by_type_and_excludes_them_from_latency() -> None:
    stats = msl.ModeStats("haiku", "streaming", attempts=5)
    stats.results = [
        msl.RunResult(latency_ms=100.0, ttft_ms=40.0),
        msl.RunResult(latency_ms=300.0, ttft_ms=60.0),
        msl.RunResult(latency_ms=200.0, ttft_ms=50.0),
        msl.RunResult(error_type="TimeoutError", error_message="slow"),
        msl.RunResult(error_type="TimeoutError", error_message="slow"),
    ]
    summary = stats.summary()
    assert summary["ok"] == 3
    assert summary["errors"] == 2
    assert summary["error_rate"] == pytest.approx(0.4)
    assert summary["error_types"] == {"TimeoutError": 2}
    assert summary["median_ms"] == 200.0
    assert summary["p95_ms"] == 300.0
    assert summary["ttft_median_ms"] == 50.0
    assert summary["measured"] is True


def test_mode_stats_ttft_only_reported_for_streaming() -> None:
    stats = msl.ModeStats("haiku", "non-streaming", attempts=1)
    stats.results = [msl.RunResult(latency_ms=100.0, ttft_ms=None)]
    assert stats.summary()["ttft_median_ms"] is None


def test_mode_stats_all_failed_is_not_measured() -> None:
    stats = msl.ModeStats("qwen", "streaming", attempts=2)
    stats.results = [msl.RunResult(error_type="AuthenticationError", error_message="401")] * 2
    summary = stats.summary()
    assert summary["measured"] is False
    assert summary["median_ms"] is None
    assert summary["error_rate"] == 1.0
    assert "NIET GEMETEN" in msl.format_table([summary])


def test_scrub_masks_credentials_and_truncates() -> None:
    message = "401 for key sk-or-v1-abcdef0123456789 and Bearer abc.def"
    cleaned = msl.scrub(message)
    assert "sk-or-v1" not in cleaned
    assert "abc.def" not in cleaned
    assert len(msl.scrub("x" * 500)) == 200


def test_parse_args_defaults() -> None:
    args = msl.parse_args([])
    assert args.runs == 30
    assert args.candidates == ["haiku", "qwen", "gemini"]
    assert args.fragment == msl.DEFAULT_FRAGMENT
    assert args.out is None


def test_parse_args_custom(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    args = msl.parse_args(["--runs", "3", "--candidates", "qwen, haiku,qwen", "--out", str(out)])
    assert args.runs == 3
    assert args.candidates == ["qwen", "haiku"]
    assert args.out == out


@pytest.mark.parametrize("argv", [["--runs", "0"], ["--candidates", "nope"], ["--candidates", ""]])
def test_parse_args_rejects_invalid(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        msl.parse_args(argv)


def test_load_fragment_takes_prospect_lines_only(tmp_path: Path) -> None:
    script = tmp_path / "dialogue.md"
    script.write_text(
        "S: verkoper zegt iets\n\nP: eerste   prospect regel\n[PP:x]\nP: tweede regel\nS: nog iets\n",
        encoding="utf-8",
    )
    assert msl.load_fragment(script) == ["eerste prospect regel", "tweede regel"]


def test_load_fragment_caps_lines(tmp_path: Path) -> None:
    script = tmp_path / "dialogue.md"
    script.write_text("\n".join(f"P: regel {i}" for i in range(20)), encoding="utf-8")
    assert len(msl.load_fragment(script)) == msl.SUGGESTIONS_MAX_CONTEXT_LINES


def test_load_fragment_without_prospect_lines_raises(tmp_path: Path) -> None:
    script = tmp_path / "dialogue.md"
    script.write_text("S: alleen verkoper\n", encoding="utf-8")
    with pytest.raises(ValueError):
        msl.load_fragment(script)


def test_default_fragment_loads() -> None:
    lines = msl.load_fragment(msl.DEFAULT_FRAGMENT)
    assert 1 <= len(lines) <= msl.SUGGESTIONS_MAX_CONTEXT_LINES


def test_pin_provider_merges_into_existing_extra_body() -> None:
    seen: dict = {}

    def create(**kwargs):
        seen.update(kwargs)

    msl._pin_provider(create, ["DeepInfra"])(
        model="m", extra_body={"chat_template_kwargs": {"enable_thinking": False}}
    )
    assert seen["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["extra_body"]["provider"] == {"order": ["DeepInfra"], "allow_fallbacks": False}


def test_pin_provider_without_existing_extra_body() -> None:
    seen: dict = {}
    msl._pin_provider(lambda **kw: seen.update(kw), ["DeepInfra"])(model="m")
    assert seen["extra_body"] == {"provider": {"order": ["DeepInfra"], "allow_fallbacks": False}}
