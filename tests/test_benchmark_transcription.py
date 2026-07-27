"""Tests for scripts/benchmark_transcription.py.

These tests focus on the reference-window alignment needed for a fair WER when
``--wer-max-seconds`` caps the batch audio, plus the ``--threads`` CLI flag
behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import benchmark_transcription as bt


class _StubJiwer:
    """Minimal jiwer stand-in for tests that exercise compute_wer."""

    @staticmethod
    def wer(reference: str, hypothesis: str) -> float:
        return 0.0 if reference == hypothesis else 1.0


@pytest.fixture(autouse=True)
def _stub_jiwer_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure compute_wer can run even when the real jiwer package is absent."""
    monkeypatch.setitem(sys.modules, "jiwer", _StubJiwer())


@pytest.fixture()
def sample_fireflies_md(tmp_path: Path) -> Path:
    path = tmp_path / "fireflies-sample.md"
    path.write_text(
        "**Speaker A** *[00:10]*: first part exactly matches hypothesis\n"
        "**Speaker B** *[01:10]*: extra reference text not in audio\n",
        encoding="utf-8",
    )
    return path


def test_fireflies_reference_text_returns_full_transcript(sample_fireflies_md: Path) -> None:
    text = bt.fireflies_reference_text(sample_fireflies_md)
    assert text == "first part exactly matches hypothesis extra reference text not in audio"


def test_fireflies_reference_text_caps_to_window(sample_fireflies_md: Path) -> None:
    """When max_seconds is supplied, only utterances inside the window are kept."""
    text = bt.fireflies_reference_text(sample_fireflies_md, max_seconds=60)
    assert text == "first part exactly matches hypothesis"


def test_compute_wer_uses_capped_reference_when_audio_is_capped(
    sample_fireflies_md: Path,
) -> None:
    """Regression test for the WER-cap mismatch.

    Before the fix the benchmark scored a capped hypothesis against the full
    Fireflies reference, artificially inflating WER. After the fix the reference
    is capped to the same window, so a correct hypothesis yields zero WER.
    """
    hypothesis = "first part exactly matches hypothesis"

    capped_reference = bt.fireflies_reference_text(sample_fireflies_md, max_seconds=60)
    full_reference = bt.fireflies_reference_text(sample_fireflies_md)

    assert bt.compute_wer(capped_reference, hypothesis) == 0.0
    assert bt.compute_wer(full_reference, hypothesis) == 1.0


@pytest.fixture()
def boundary_fireflies_md(tmp_path: Path) -> Path:
    path = tmp_path / "fireflies-boundary.md"
    path.write_text(
        "**Speaker A** *[00:10]*: inside window\n"
        "**Speaker B** *[01:00]*: exactly on the cap\n"
        "**Speaker C** *[01:01]*: outside window\n",
        encoding="utf-8",
    )
    return path


def test_fireflies_reference_text_excludes_utterance_exactly_on_cap(
    boundary_fireflies_md: Path,
) -> None:
    """Boundary case: a timestamp exactly equal to max_seconds must be excluded.

    The audio slice ``full_audio[: int(max_seconds * SAMPLE_RATE)]`` is half-open,
    so the reference window ``[0, max_seconds)`` must exclude utterances starting
    exactly at max_seconds. Including them would make the reference cover more
    audio than was actually evaluated.
    """
    text = bt.fireflies_reference_text(boundary_fireflies_md, max_seconds=60)
    assert text == "inside window"


def test_timestamp_to_seconds_parses_minutes_and_hours() -> None:
    assert bt._timestamp_to_seconds("00:10") == 10  # noqa: SLF001
    assert bt._timestamp_to_seconds("01:10") == 70  # noqa: SLF001
    assert bt._timestamp_to_seconds("01:30:45") == 5445  # noqa: SLF001


def test_vocabulary_delta_calculation_is_positive_when_biasing_improves_wer() -> None:
    """Delta = WER_without - WER_with; a positive value means the biasing helped."""
    biased = bt.VariantResult(name="whisper.cpp @ 3000ms chunk", wer=0.10)
    unbiased = bt.VariantResult(name="whisper.cpp no vocab", wer=0.15)

    delta = unbiased.wer - biased.wer

    assert delta == pytest.approx(0.05)


def test_vocabulary_delta_calculation_is_negative_when_biasing_hurts_wer() -> None:
    biased = bt.VariantResult(name="mlx-whisper @ 3000ms chunk", wer=0.20)
    unbiased = bt.VariantResult(name="mlx-whisper no vocab", wer=0.18)

    delta = unbiased.wer - biased.wer

    assert delta == pytest.approx(-0.02)


# ---------------------------------------------------------------------------
# --threads CLI flag
# ---------------------------------------------------------------------------


def test_threads_flag_defaults_to_none() -> None:
    """When --threads is not supplied, the argparse default must be None."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=None)
    args = parser.parse_args([])
    assert args.threads is None


def test_threads_flag_parses_integer() -> None:
    """When --threads 8 is given, argparse stores the value."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=None)
    args = parser.parse_args(["--threads", "8"])
    assert args.threads == 8


def test_with_overrides_sets_threads_when_not_none() -> None:
    """with_overrides(..., whisper_cpp_threads=8) must set the field to 8."""
    from sales_copilot.core.config import TranscriberConfig, with_overrides

    cfg = TranscriberConfig(whisper_cpp_threads=4)  # type: ignore[call-arg]
    overridden = with_overrides(cfg, whisper_cpp_threads=8)
    assert overridden.whisper_cpp_threads == 8


def test_with_overrides_preserves_default_threads_when_none() -> None:
    """with_overrides(..., whisper_cpp_threads=None) must keep the existing value."""
    from sales_copilot.core.config import TranscriberConfig, with_overrides

    cfg = TranscriberConfig(whisper_cpp_threads=4)  # type: ignore[call-arg]
    overridden = with_overrides(cfg, whisper_cpp_threads=None)
    assert overridden.whisper_cpp_threads == 4
