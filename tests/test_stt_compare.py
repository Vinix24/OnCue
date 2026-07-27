"""Tests for scripts/stt_compare.py.

Two tiers, mirroring tests/test_e2e_latency_harness.py:

- Pure unit tests (WER/CER math, aggregation, rendering, CLI early-exit) that run
  unconditionally, no fixtures, no whisper.cpp binary, no network needed.
- An end-to-end segment-collection test driving the real ``AudioBufferer`` against a
  synthetic (generated, not fixture) WAV, with fully mocked local/cloud candidates --
  no live whisper.cpp/Groq/OpenAI call anywhere in this file.
"""

from __future__ import annotations

import asyncio
import sys
import unittest.mock
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import stt_compare as sc  # noqa: E402

from sales_copilot.modules.transcriber.audio_bufferer import AudioBufferer  # noqa: E402
from tests.conftest_replay import make_synthetic_wav  # noqa: E402

# ---------------------------------------------------------------------------
# WER / CER
# ---------------------------------------------------------------------------


def test_normalize_text_strips_punctuation_and_collapses_whitespace() -> None:
    assert sc.normalize_text("Hallo,  wereld!!") == "hallo wereld"


def test_normalize_text_lowercases() -> None:
    assert sc.normalize_text("OFFERTES kosten TIJD") == "offertes kosten tijd"


def test_word_error_rate_identical_is_zero() -> None:
    assert sc.word_error_rate("het maken van offertes", "het maken van offertes") == 0.0


def test_word_error_rate_empty_reference_returns_none() -> None:
    assert sc.word_error_rate("", "iets") is None
    assert sc.word_error_rate("   ", "iets") is None


def test_word_error_rate_counts_substitutions_over_reference_length() -> None:
    # reference has 3 words, hypothesis substitutes 1 -> WER = 1/3
    assert sc.word_error_rate("een twee drie", "een twee vier") == pytest.approx(1 / 3)


def test_word_error_rate_ignores_punctuation_differences() -> None:
    assert sc.word_error_rate("Hallo, wereld!", "hallo wereld") == 0.0


def test_char_error_rate_identical_is_zero() -> None:
    assert sc.char_error_rate("offerte", "offerte") == 0.0


def test_char_error_rate_empty_reference_returns_none() -> None:
    assert sc.char_error_rate("", "x") is None


def test_char_error_rate_counts_edits_over_reference_length() -> None:
    # "kat" -> "hat": 1 substitution / 3 chars
    assert sc.char_error_rate("kat", "hat") == pytest.approx(1 / 3)


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def test_discover_sessions_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert sc.discover_sessions(tmp_path / "does-not-exist") == []


def test_discover_sessions_only_returns_dirs_with_prospect_wav(tmp_path: Path) -> None:
    (tmp_path / "session-a").mkdir()
    (tmp_path / "session-a" / "prospect.wav").write_bytes(b"")
    (tmp_path / "session-b").mkdir()  # no prospect.wav -- must be excluded

    found = sc.discover_sessions(tmp_path)

    assert [p.name for p in found] == ["session-a"]


# ---------------------------------------------------------------------------
# Segment collection -- real AudioBufferer against a synthetic WAV
# ---------------------------------------------------------------------------


async def _collect_from_synthetic_session(tmp_path: Path, *, duration_s: float = 2.0) -> list[sc.Segment]:
    session_dir = tmp_path / "synthetic-session"
    session_dir.mkdir()
    # RMS ~0.35, well above AudioBufferer.min_rms -- registers as speech without
    # needing the Silero VAD model (same fixture helper e2e_latency_harness's tests use).
    make_synthetic_wav(session_dir / "prospect.wav", duration_s=duration_s)

    with unittest.mock.patch.object(AudioBufferer, "_load_vad", return_value=None):
        return await sc._collect_session_segments(session_dir, remaining_budget=5)


def test_collect_session_segments_returns_real_audio(tmp_path: Path) -> None:
    segments = asyncio.run(_collect_from_synthetic_session(tmp_path))

    assert len(segments) >= 1
    seg = segments[0]
    assert seg.session == "synthetic-session"
    assert seg.index == 0
    assert isinstance(seg.audio, np.ndarray)
    assert seg.audio.size > 0
    assert seg.end_ms > seg.start_ms
    assert seg.duration_s > 0


def test_collect_session_segments_respects_remaining_budget(tmp_path: Path) -> None:
    session_dir = tmp_path / "synthetic-session"
    session_dir.mkdir()
    make_synthetic_wav(session_dir / "prospect.wav", duration_s=2.0)

    with unittest.mock.patch.object(AudioBufferer, "_load_vad", return_value=None):
        segments = asyncio.run(sc._collect_session_segments(session_dir, remaining_budget=0))

    assert segments == []


def test_collect_segments_across_sessions_stops_at_top_n(tmp_path: Path) -> None:
    for name in ("session-a", "session-b"):
        session_dir = tmp_path / name
        session_dir.mkdir()
        make_synthetic_wav(session_dir / "prospect.wav", duration_s=2.0)

    with unittest.mock.patch.object(AudioBufferer, "_load_vad", return_value=None):
        segments, sessions_scanned = asyncio.run(sc.collect_segments(tmp_path, top_n=1))

    assert sessions_scanned == 2
    assert len(segments) == 1


# ---------------------------------------------------------------------------
# run_candidate -- mocked candidates, no live network
# ---------------------------------------------------------------------------


class _FixedTextCandidate:
    """Fake STT candidate returning a fixed transcript after an artificial delay."""

    name = "fake"

    def __init__(self, text: str, delay_s: float = 0.0) -> None:
        self._text = text
        self._delay_s = delay_s

    async def transcribe(self, segment: sc.Segment) -> str:  # noqa: ARG002
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return self._text


class _FailingCandidate:
    """Fails on a specific segment index, succeeds otherwise."""

    name = "flaky"

    def __init__(self, fail_index: int) -> None:
        self._fail_index = fail_index

    async def transcribe(self, segment: sc.Segment) -> str:
        if segment.index == self._fail_index:
            raise RuntimeError("simulated provider error")
        return "ok"


def _make_segments(n: int) -> list[sc.Segment]:
    return [
        sc.Segment(
            session="s", index=i, start_ms=i * 1000, end_ms=i * 1000 + 900, audio=np.zeros(16000, dtype=np.float32)
        )
        for i in range(n)
    ]


def test_run_candidate_records_text_and_latency() -> None:
    segments = _make_segments(2)
    results = asyncio.run(sc.run_candidate(_FixedTextCandidate("hallo wereld"), segments, timeout_s=5.0))

    assert len(results) == 2
    for r in results:
        assert r.error is None
        assert r.text == "hallo wereld"
        assert r.latency_ms is not None
        assert r.latency_ms >= 0


def test_run_candidate_one_failing_segment_does_not_crash_the_run() -> None:
    segments = _make_segments(3)
    results = asyncio.run(sc.run_candidate(_FailingCandidate(fail_index=1), segments, timeout_s=5.0))

    assert len(results) == 3
    assert results[0].error is None and results[0].text == "ok"
    assert results[1].error is not None and "simulated provider error" in results[1].error
    assert results[2].error is None and results[2].text == "ok"


def test_run_candidate_timeout_is_recorded_as_error_not_raised() -> None:
    segments = _make_segments(1)
    slow = _FixedTextCandidate("too slow", delay_s=0.2)

    results = asyncio.run(sc.run_candidate(slow, segments, timeout_s=0.01))

    assert len(results) == 1
    assert results[0].error is not None
    assert results[0].text == ""


# ---------------------------------------------------------------------------
# LocalCandidate.server_mode()
# ---------------------------------------------------------------------------


class _StubBackendWithServerFlag:
    def __init__(self, server_started: bool | None) -> None:
        self._server_started = server_started


def test_server_mode_reports_resident_server_when_started() -> None:
    candidate = sc.LocalCandidate("local", _StubBackendWithServerFlag(True))
    assert "resident whisper-server" in candidate.server_mode()


def test_server_mode_flags_one_shot_cli_as_unfair() -> None:
    candidate = sc.LocalCandidate("local", _StubBackendWithServerFlag(False))
    mode = candidate.server_mode()
    assert "whisper-cli" in mode
    assert "NOT a fair cloud comparison" in mode


def test_server_mode_reports_not_applicable_for_non_whisper_cpp_backend() -> None:
    candidate = sc.LocalCandidate("local", _StubBackendWithServerFlag(None))
    assert candidate.server_mode() == "n/a (not whisper.cpp)"


# ---------------------------------------------------------------------------
# Aggregation: build_backend_summary / build_report / render_markdown
# ---------------------------------------------------------------------------


def test_build_backend_summary_local_is_free_and_has_no_quality_metrics() -> None:
    results = [
        sc.TranscriptResult(backend="local", session="s", index=0, text="hallo", latency_ms=100.0, duration_s=1.0)
    ]

    summary, diffs = sc.build_backend_summary("local", "local", results, reference=None)

    assert summary.cost_usd == 0.0
    assert summary.cost_note == "own hardware"
    assert summary.mean_wer is None
    assert diffs == []


def test_build_backend_summary_cloud_computes_wer_against_reference() -> None:
    results = [
        sc.TranscriptResult(
            backend="groq:x", session="s", index=0, text="een twee vier", latency_ms=50.0, duration_s=1.0
        ),
        sc.TranscriptResult(backend="groq:x", session="s", index=1, text="drie", latency_ms=60.0, duration_s=1.0),
    ]
    reference = {("s", 0): "een twee drie", ("s", 1): "drie"}

    summary, diffs = sc.build_backend_summary("groq:x", "cloud", results, reference=reference)

    assert summary.n_compared == 2
    assert summary.mean_wer == pytest.approx((1 / 3 + 0.0) / 2)
    # every compared segment becomes a diff candidate, sorted highest-WER first
    assert [d.index for d in diffs] == [0, 1]
    assert diffs[0].wer == pytest.approx(1 / 3)


def test_build_backend_summary_missing_reference_key_is_skipped() -> None:
    results = [
        sc.TranscriptResult(backend="groq:x", session="s", index=0, text="hallo", latency_ms=50.0, duration_s=1.0)
    ]
    reference: dict[tuple[str, int], str] = {}  # local produced nothing for this segment

    summary, diffs = sc.build_backend_summary("groq:x", "cloud", results, reference=reference)

    assert summary.n_compared == 0
    assert summary.mean_wer is None
    assert diffs == []


def test_build_backend_summary_records_failed_segments_and_error_sample() -> None:
    results = [
        sc.TranscriptResult(backend="openai:whisper-1", session="s", index=0, error="HTTP 401", duration_s=1.0),
        sc.TranscriptResult(
            backend="openai:whisper-1", session="s", index=1, text="ok", latency_ms=10.0, duration_s=1.0
        ),
    ]

    summary, _ = sc.build_backend_summary("openai:whisper-1", "cloud", results, reference=None)

    assert summary.n_ok == 1
    assert summary.n_failed == 1
    assert summary.errors_sample == ["HTTP 401"]


def test_build_report_produces_verdict_for_each_backend_and_flags_local_reference() -> None:
    results_by_backend = {
        "local": [
            sc.TranscriptResult(
                backend="local", session="s", index=0, text="hallo wereld", latency_ms=200.0, duration_s=1.0
            ),
        ],
        "groq:whisper-large-v3-turbo": [
            sc.TranscriptResult(
                backend="groq:whisper-large-v3-turbo",
                session="s",
                index=0,
                text="hallo wereld",
                latency_ms=90.0,
                duration_s=1.0,
            ),
        ],
    }
    backend_kinds = {"local": "local", "groq:whisper-large-v3-turbo": "cloud"}

    report = sc.build_report(
        sessions_scanned=1,
        segments_collected=1,
        local_server_mode="resident whisper-server (HTTP, model stays loaded)",
        results_by_backend=results_by_backend,
        backend_kinds=backend_kinds,
        reference_backend="local",
    )

    names = {b.name: b for b in report.backends}
    assert names["local"].verdict != ""
    groq_summary = names["groq:whisper-large-v3-turbo"]
    assert groq_summary.mean_wer == pytest.approx(0.0)
    assert "faster than local" in groq_summary.verdict


def test_build_report_with_no_results_does_not_crash() -> None:
    report = sc.build_report(
        sessions_scanned=0,
        segments_collected=0,
        local_server_mode="not run",
        results_by_backend={},
        backend_kinds={},
        reference_backend=None,
    )
    assert report.backends == []
    md = sc.render_markdown(report)
    assert "STT comparison" in md


def test_render_markdown_contains_expected_sections() -> None:
    results_by_backend = {
        "local": [
            sc.TranscriptResult(backend="local", session="s", index=0, text="hallo", latency_ms=100.0, duration_s=1.0)
        ],
        "openai:whisper-1": [
            sc.TranscriptResult(backend="openai:whisper-1", session="s", index=0, error="timeout", duration_s=1.0),
        ],
    }
    report = sc.build_report(
        sessions_scanned=1,
        segments_collected=1,
        local_server_mode="resident whisper-server (HTTP, model stays loaded)",
        results_by_backend=results_by_backend,
        backend_kinds={"local": "local", "openai:whisper-1": "cloud"},
        reference_backend="local",
    )

    md = sc.render_markdown(report)

    assert "## Per-backend latency" in md
    assert "## Transcript quality" in md
    assert "## Cost estimate for this run" in md
    assert "## Verdict" in md
    assert "## Errors (sample, up to 3 per backend)" in md
    assert "timeout" in md


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


def test_report_to_json_dict_is_json_serializable() -> None:
    import json

    report = sc.build_report(
        sessions_scanned=1,
        segments_collected=1,
        local_server_mode="resident whisper-server (HTTP, model stays loaded)",
        results_by_backend={
            "local": [
                sc.TranscriptResult(backend="local", session="s", index=0, text="hallo", latency_ms=1.0, duration_s=1.0)
            ]
        },
        backend_kinds={"local": "local"},
        reference_backend="local",
    )

    payload = json.dumps(report.to_json_dict())
    assert "sessions_scanned" in payload


# ---------------------------------------------------------------------------
# CLI early-exit (no fixtures)
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_no_sessions_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = sc.main(["--sessions-dir", str(tmp_path)])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No session fixtures" in captured.err


# ---------------------------------------------------------------------------
# Real session fixtures (skipped when data/sessions/ is absent -- always true in
# CI and dispatch worktrees, see CLAUDE.md)
# ---------------------------------------------------------------------------


_SESSIONS_DIR = Path("data/sessions")


@pytest.mark.audio_fixture
def test_collect_segments_against_real_session_fixtures() -> None:
    """Collect real VAD segments from data/sessions/ without transcribing them.

    Skipped when data/sessions/ has no prospect.wav fixtures -- run locally with:
    pytest -m audio_fixture tests/test_stt_compare.py -v
    """
    if not sc.discover_sessions(_SESSIONS_DIR):
        pytest.skip(f"no session fixtures with prospect.wav under {_SESSIONS_DIR}")

    segments, sessions_scanned = asyncio.run(sc.collect_segments(_SESSIONS_DIR, top_n=3))

    assert sessions_scanned >= 1
    assert len(segments) <= 3
    for seg in segments:
        assert seg.audio.size > 0
