"""Tests for scripts/e2e_latency_harness.py.

Three tiers:

- Pure unit tests (dataclass math, aggregation, CLI early-exit) that run
  unconditionally, no fixtures or network needed.
- End-to-end tests that drive the real VAD/AudioBufferer against a synthetic
  (generated, not fixture) WAV with a fake transcription backend and a fake/
  stub LLM -- no whisper.cpp, no paid API calls, but every other stage
  boundary is real. One exercises v1 (real DetectionPipeline + SlideInjector),
  one exercises v2 (real SlidingWindowBuffer + WindowClassifier + the real,
  imported detector/__main__._dispatch_window_detection).
- One @pytest.mark.audio_fixture test exercising run_harness() against real
  data/sessions/ recordings in --dry-run mode; skips when absent (CI/dispatch
  worktrees never have them -- see CLAUDE.md).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import e2e_latency_harness as harness  # noqa: E402

from sales_copilot.core.config import (  # noqa: E402
    DetectorConfig,
    SlidesConfig,
    TranscriberConfig,
    WebSocketConfig,
    with_overrides,
)
from sales_copilot.core.preset import load_preset  # noqa: E402
from sales_copilot.modules.copilot.injector import SlideInjector  # noqa: E402
from sales_copilot.modules.detector.debouncer import PainPointDebouncer  # noqa: E402
from sales_copilot.modules.detector.eval_shared import PricingTable  # noqa: E402
from sales_copilot.modules.detector.llm_confirm import LLMConfirmClient  # noqa: E402
from sales_copilot.modules.detector.pipeline import DetectionPipeline  # noqa: E402
from sales_copilot.modules.detector.router import PainPointRouter  # noqa: E402
from sales_copilot.modules.detector.window_classifier import (  # noqa: E402
    WindowAnalysis,
    WindowClassifier,
    WindowDetection,
)
from sales_copilot.modules.slides.case_db import Case, SQLiteCaseDB  # noqa: E402
from sales_copilot.modules.transcriber.audio_bufferer import AudioBufferer  # noqa: E402
from tests.conftest_replay import make_synthetic_wav  # noqa: E402

_SESSIONS_DIR = Path("data/sessions")
_KEYWORD_UTTERANCE = "het maken van offertes kost ons heel veel tijd"  # literal utterance
# from config/pain_points.yaml's "offerteproces" route -- hits
# PainPointRouter._keyword_match's substring path deterministically, so this
# test never needs to load the sentence-transformers embedding model.


class _StubLLMClient:
    """Raises if called -- proves a keyword fast-route match never escalates."""

    def confirm(self, _: str) -> None:
        raise AssertionError("Escalation-LLM must not be called for a keyword fast-route match")

    async def confirm_async(self, _: str) -> None:
        raise AssertionError("Escalation-LLM must not be called for a keyword fast-route match")


class _FakeBackend:
    """TranscriptionBackend stand-in returning a fixed transcript -- no whisper.cpp needed."""

    def __init__(self, text: str) -> None:
        self._text = text
        self.call_count = 0

    async def start(self, stop_event: asyncio.Event) -> None:  # noqa: ARG002
        pass

    async def warmup(self) -> None:
        pass

    async def transcribe(self, audio: object) -> str:  # noqa: ARG002
        self.call_count += 1
        return self._text

    async def transcribe_file(self, path: Path) -> str:  # noqa: ARG002
        return self._text

    async def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def test_discover_sessions_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert harness.discover_sessions(tmp_path / "does-not-exist") == []


def test_discover_sessions_only_returns_dirs_with_prospect_wav(tmp_path: Path) -> None:
    (tmp_path / "session-a").mkdir()
    (tmp_path / "session-a" / "prospect.wav").write_bytes(b"")
    (tmp_path / "session-b").mkdir()  # no prospect.wav -- must be excluded
    (tmp_path / "not-a-dir.txt").write_text("x", encoding="utf-8")

    found = harness.discover_sessions(tmp_path)

    assert [p.name for p in found] == ["session-a"]


# ---------------------------------------------------------------------------
# Backend config mirror
# ---------------------------------------------------------------------------


def test_build_backend_config_whisper_cpp_includes_binary_paths() -> None:
    config = TranscriberConfig(backend="whisper.cpp")
    built = harness._build_backend_config(config)

    assert built["backend"] == "whisper.cpp"
    assert built["whisper_cpp_binary"] == config.whisper_cpp_binary
    assert built["whisper_cpp_model_path"] == config.whisper_cpp_model_path


def test_build_backend_config_mlx_omits_whisper_cpp_fields() -> None:
    config = TranscriberConfig(backend="mlx-whisper")
    built = harness._build_backend_config(config)

    assert built == {"backend": "mlx-whisper", "language": config.language}


# ---------------------------------------------------------------------------
# Stage-latency math
# ---------------------------------------------------------------------------


def test_segment_result_stage_ms_fast_route() -> None:
    seg = harness.SegmentResult(
        session="s",
        index=0,
        escalated=False,
        vad_close_ts=0.0,
        whisper_done_ts=0.2,
        router_done_ts=0.25,
        emit_ts=0.3,
    )
    stages = seg.stage_ms()
    assert stages["vad_to_whisper_ms"] == pytest.approx(200.0)
    assert stages["whisper_to_router_ms"] == pytest.approx(50.0)
    assert stages["router_to_llm_ms"] is None
    assert stages["decision_to_emit_ms"] == pytest.approx(50.0)
    assert stages["total_ms"] == pytest.approx(300.0)


def test_segment_result_stage_ms_escalated_route() -> None:
    seg = harness.SegmentResult(
        session="s",
        index=0,
        escalated=True,
        vad_close_ts=0.0,
        whisper_done_ts=0.2,
        router_done_ts=0.25,
        llm_end_ts=3.0,
        emit_ts=3.05,
    )
    stages = seg.stage_ms()
    assert stages["router_to_llm_ms"] == pytest.approx(2750.0)
    assert stages["decision_to_emit_ms"] == pytest.approx(50.0)
    assert stages["total_ms"] == pytest.approx(3050.0)


def test_segment_result_stage_ms_incomplete_segment_returns_none() -> None:
    seg = harness.SegmentResult(session="s", index=0, vad_close_ts=0.0)
    stages = seg.stage_ms()
    assert stages["vad_to_whisper_ms"] is None
    assert stages["total_ms"] is None


def test_segment_result_llm_ttft_ms_defaults_null_and_flows_into_stage_ms() -> None:
    """v1 segments never set llm_ttft_ms (non-streaming path) -- stays null.
    v2 --run segments populate it from LLMClient.last_ttft_ms via WindowClassifierHooks."""
    v1_seg = harness.SegmentResult(session="s", index=0, vad_close_ts=0.0)
    assert v1_seg.llm_ttft_ms is None
    assert v1_seg.stage_ms()["llm_ttft_ms"] is None

    v2_seg = harness.SegmentResult(session="s", index=0, vad_close_ts=0.0, llm_ttft_ms=42.5)
    assert v2_seg.stage_ms()["llm_ttft_ms"] == pytest.approx(42.5)
    assert "llm_ttft_ms" in dict(harness._STAGE_KEYS)


def test_stage_stats_empty_list() -> None:
    stats = harness._stage_stats([])
    assert stats.count == 0
    assert stats.p50_ms is None


def test_stage_stats_percentiles() -> None:
    stats = harness._stage_stats([100.0, 200.0, 300.0, 400.0, 500.0])
    assert stats.count == 5
    assert stats.min_ms == 100.0
    assert stats.max_ms == 500.0
    assert stats.p50_ms == 300.0


# ---------------------------------------------------------------------------
# Report aggregation
# ---------------------------------------------------------------------------


def test_build_report_counts_over_5s_bar_and_escalation_rate() -> None:
    segments = [
        harness.SegmentResult(
            session="s", index=0, outcome="emitted", escalated=False,
            vad_close_ts=0.0, whisper_done_ts=0.1, router_done_ts=0.2, emit_ts=1.0,
        ),
        harness.SegmentResult(
            session="s", index=1, outcome="emitted", escalated=True,
            vad_close_ts=0.0, whisper_done_ts=0.1, router_done_ts=0.2, llm_end_ts=6.0, emit_ts=6.1,
        ),
        harness.SegmentResult(session="s", index=2, outcome="no_match", escalated=False, vad_close_ts=0.0),
    ]

    report = harness.build_report(
        segments,
        mode="run",
        candidate=harness._DEFAULT_CANDIDATE,
        effective_timeout_ms=7000,
        sessions_scanned=1,
        dry_run_cost_preview=None,
    )

    assert report.over_5s_total == 2
    assert report.over_5s_count == 1
    assert report.escalation_rate == pytest.approx(1 / 3)
    assert report.outcome_counts == {"emitted": 2, "no_match": 1}

    md = harness.render_markdown(report)
    assert "5s budget" in md
    assert "Escalation rate" in md
    assert "Known limitations" in md

    payload = report.to_json_dict()
    assert len(payload["segments"]) == 3
    assert payload["segments"][1]["total_ms"] == pytest.approx(6100.0)


def test_build_report_with_no_segments_does_not_crash() -> None:
    report = harness.build_report(
        [],
        mode="dry-run",
        candidate=harness._DEFAULT_CANDIDATE,
        effective_timeout_ms=7000,
        sessions_scanned=0,
        dry_run_cost_preview=None,
    )
    assert report.escalation_rate is None
    assert report.over_5s_total == 0
    md = harness.render_markdown(report)
    assert "No segments reached the emit stage" in md


# ---------------------------------------------------------------------------
# Dry-run cost preview
# ---------------------------------------------------------------------------


def test_estimate_dry_run_cost_uses_real_prompt_builder_and_empty_pricing() -> None:
    config = with_overrides(DetectorConfig(), llm_provider="none")
    llm_client = LLMConfirmClient(config)
    fake_pipeline = SimpleNamespace(llm_client=llm_client)
    segments = [
        harness.SegmentResult(
            session="s", index=0, outcome="would_escalate", text="een test fragment", vad_close_ts=0.0
        ),
        harness.SegmentResult(session="s", index=1, outcome="no_match", vad_close_ts=0.0),
    ]
    pricing = PricingTable(entries=[])

    preview = harness.estimate_dry_run_cost(segments, fake_pipeline, pricing, harness._DEFAULT_CANDIDATE)

    assert preview["would_escalate_count"] == 1
    assert preview["input_tokens"] > 0
    assert preview["output_tokens"] == harness._ESTIMATED_CONFIRM_OUTPUT_TOKENS
    assert preview["estimated_cost"] is None  # no price entry for an empty table


def test_estimate_dry_run_cost_v2_uses_real_prompt_builder_and_empty_pricing() -> None:
    config = DetectorConfig(llm_provider="openai", llm_model="test-model")
    window_classifier = WindowClassifier(config, "openai")
    segments = [
        harness.SegmentResult(
            session="s", index=0, outcome="would_classify", text="een test fragment", vad_close_ts=0.0
        ),
        harness.SegmentResult(session="s", index=1, outcome="buffer_below_min", vad_close_ts=0.0),
    ]
    pricing = PricingTable(entries=[])

    preview = harness.estimate_dry_run_cost_v2(segments, window_classifier, pricing, harness._DEFAULT_CANDIDATE)

    assert preview is not None
    assert preview["would_classify_count"] == 1
    assert preview["input_tokens"] > 0
    assert preview["output_tokens"] == harness._ESTIMATED_WINDOW_OUTPUT_TOKENS
    assert preview["estimated_cost"] is None  # no price entry for an empty table


def test_estimate_dry_run_cost_v2_returns_none_for_provider_none() -> None:
    """WindowClassifier.__init__ returns early for provider='none' and never builds
    system_prompt -- there is nothing to size, so the preview must be None, not a
    zero-cost estimate (which would misleadingly imply a real prompt was sized)."""
    config = DetectorConfig(llm_provider="none")
    window_classifier = WindowClassifier(config, "none")
    segments = [harness.SegmentResult(session="s", index=0, outcome="would_classify", text="x", vad_close_ts=0.0)]

    preview = harness.estimate_dry_run_cost_v2(
        segments, window_classifier, PricingTable(entries=[]), harness._DEFAULT_CANDIDATE
    )

    assert preview is None


def test_estimate_dry_run_cost_with_no_escalating_segments() -> None:
    config = with_overrides(DetectorConfig(), llm_provider="none")
    llm_client = LLMConfirmClient(config)
    fake_pipeline = SimpleNamespace(llm_client=llm_client)
    segments = [harness.SegmentResult(session="s", index=0, outcome="no_match", vad_close_ts=0.0)]

    preview = harness.estimate_dry_run_cost(
        segments, fake_pipeline, PricingTable(entries=[]), harness._DEFAULT_CANDIDATE
    )

    assert preview["would_escalate_count"] == 0
    assert preview["input_tokens"] == 0
    assert preview["output_tokens"] == 0


# ---------------------------------------------------------------------------
# CLI early-exit (no fixtures)
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_no_sessions_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = harness.main(["--sessions-dir", str(tmp_path), "--dry-run"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No session fixtures" in captured.err


def test_main_v2_returns_zero_when_no_sessions_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = harness.main(["--sessions-dir", str(tmp_path), "--v2", "--dry-run"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No session fixtures" in captured.err


def test_cli_report_paths_default_to_v2_filenames_only_with_v2_flag() -> None:
    parser_args_v1 = _parse_main_args(["--sessions-dir", "x", "--dry-run"])
    parser_args_v2 = _parse_main_args(["--sessions-dir", "x", "--v2", "--dry-run"])

    assert parser_args_v1.report_md == harness._DEFAULT_REPORT_MD
    assert parser_args_v2.report_md == harness._DEFAULT_REPORT_MD_V2
    assert parser_args_v1.report_json == harness._DEFAULT_REPORT_JSON
    assert parser_args_v2.report_json == harness._DEFAULT_REPORT_JSON_V2


def _parse_main_args(argv: list[str]) -> argparse.Namespace:
    """Replays main()'s own argparse setup without invoking run_harness -- there is no
    public parser-building function to call directly, so this mirrors main()'s parser
    construction (same flags) up to and including its post-parse report-path defaulting."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=str, default=None)
    parser.add_argument("--timeout-ms", type=int, default=harness._DEFAULT_TIMEOUT_MS)
    parser.add_argument("--sessions-dir", type=Path, default=harness._DEFAULT_SESSIONS_DIR)
    parser.add_argument("--top-n", type=int, default=harness._DEFAULT_TOP_N)
    parser.add_argument("--report-md", type=Path, default=None)
    parser.add_argument("--report-json", type=Path, default=None)
    parser.add_argument("--v2", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    if args.report_md is None:
        args.report_md = harness._DEFAULT_REPORT_MD_V2 if args.v2 else harness._DEFAULT_REPORT_MD
    if args.report_json is None:
        args.report_json = harness._DEFAULT_REPORT_JSON_V2 if args.v2 else harness._DEFAULT_REPORT_JSON
    return args


# ---------------------------------------------------------------------------
# render_markdown: v1 vs v2 mode branching
# ---------------------------------------------------------------------------


def test_render_markdown_v1_mode_shows_production_wiring_gap_limitation() -> None:
    report = harness.build_report(
        [], mode="dry-run", candidate=harness._DEFAULT_CANDIDATE, effective_timeout_ms=7000,
        sessions_scanned=0, dry_run_cost_preview=None,
    )
    md = harness.render_markdown(report)
    assert "Production-wiring gap" in md
    assert "TTFT not observable" in md
    assert "Escalation rate" in md


def test_render_markdown_v2_mode_shows_v2_limitations_not_wiring_gap() -> None:
    report = harness.build_report(
        [], mode="v2-run", candidate=harness._DEFAULT_CANDIDATE, effective_timeout_ms=7000,
        sessions_scanned=0, dry_run_cost_preview=None,
    )
    md = harness.render_markdown(report)
    assert "Production-wiring gap" not in md
    assert "pain_point emit path only" in md
    assert "Classify rate" in md


def test_render_markdown_v2_dry_run_cost_preview_uses_would_classify_key() -> None:
    report = harness.build_report(
        [], mode="v2-dry-run", candidate=harness._DEFAULT_CANDIDATE, effective_timeout_ms=7000,
        sessions_scanned=0,
        dry_run_cost_preview={
            "would_classify_count": 3, "input_tokens": 100, "output_tokens": 50, "estimated_cost": None,
        },
    )
    md = harness.render_markdown(report)
    assert "Segments that would classify: 3" in md


# ---------------------------------------------------------------------------
# End-to-end: real VAD + real DetectionPipeline + real SlideInjector,
# synthetic (generated) audio, fake backend, stub LLM (no whisper.cpp, no
# paid calls). This is the "dry-run path that works without paid inference"
# the dispatch asked for, at the _run_session composition layer.
# ---------------------------------------------------------------------------


async def _build_fast_route_pipeline(tmp_path: Path) -> tuple[DetectionPipeline, SlideInjector, harness._SegmentHooks]:
    detector_config = DetectorConfig(
        confidence_threshold_high=0.85,
        confidence_threshold_low=0.50,
        debounce_seconds=0,
        only_classify_prospect=True,
        preset_name="sales",
    )
    router = PainPointRouter(detector_config)
    hooks = harness._SegmentHooks()
    pipeline = DetectionPipeline(
        detector_config,
        router=router,
        llm_client=_StubLLMClient(),
        debouncer=PainPointDebouncer(0),
        hooks=hooks,
    )

    db_path = tmp_path / "cases.db"
    case_db = SQLiteCaseDB(db_path)
    await case_db.initialize()
    await case_db.upsert_cases(
        [
            Case(
                id="case-offerteproces-001",
                title="Offerteproces automatisering",
                industry=None,
                pain_point="offerteproces",
                description="Offertes kosten te veel tijd",
                slide_html="<section>Case</section>",
                metrics="70% sneller",
                priority=1,
            )
        ]
    )
    slides_config = SlidesConfig(case_db_sqlite_path=str(db_path), prospect_industry=None)
    ws_config = WebSocketConfig(host="127.0.0.1", port=0)
    injector = SlideInjector(pipeline, case_db, ws_config, slides_config)
    return pipeline, injector, hooks


async def _run_synthetic_session(tmp_path: Path) -> list[harness.SegmentResult]:
    session_dir = tmp_path / "synthetic-session"
    session_dir.mkdir()
    # 2.0s continuous tone: RMS ~0.35 is well above AudioBufferer's min_rms
    # gate, so every chunk registers as speech without needing Silero VAD;
    # AudioBufferer flushes it as one segment once post-EOF silence accumulates
    # past silence_gap_seconds (1.0s default) -- see make_synthetic_wav's docstring.
    make_synthetic_wav(session_dir / "prospect.wav", duration_s=2.0)

    pipeline, injector, hooks = await _build_fast_route_pipeline(tmp_path)
    backend = _FakeBackend(_KEYWORD_UTTERANCE)

    # Mirrors tests/conftest_replay.py's run_bufferer_until_eof: skip loading
    # Silero/torch in tests, RMS gating alone is enough for a synthetic tone.
    with unittest.mock.patch.object(AudioBufferer, "_load_vad", return_value=None):
        return await harness._run_session(
            session_dir,
            backend=backend,
            pipeline=pipeline,
            injector=injector,
            hooks=hooks,
            run_llm=True,
            config=pipeline.config,
            remaining_budget=1,
        )


def test_run_session_end_to_end_fast_route_emits(tmp_path: Path) -> None:
    results = asyncio.run(_run_synthetic_session(tmp_path))

    assert len(results) == 1
    seg = results[0]
    assert seg.outcome == "emitted"
    assert seg.category == "offerteproces"
    assert seg.escalated is False
    assert seg.text == _KEYWORD_UTTERANCE

    assert seg.whisper_done_ts is not None
    assert seg.router_done_ts is not None
    assert seg.emit_ts is not None
    assert seg.vad_close_ts <= seg.whisper_done_ts <= seg.router_done_ts <= seg.emit_ts

    stages = seg.stage_ms()
    assert stages["total_ms"] is not None
    assert stages["total_ms"] >= 0
    assert stages["router_to_llm_ms"] is None  # fast route: LLM never called


# ---------------------------------------------------------------------------
# v2 end-to-end: real VAD + real SlidingWindowBuffer + real WindowClassifier +
# real SlideInjector.handle_window_detection (via the real, imported
# detector/__main__._dispatch_window_detection), synthetic audio, fake
# backend, fake streaming LLM (no whisper.cpp, no paid calls).
# ---------------------------------------------------------------------------

_WINDOW_TTFT_MS = 37.5


def _build_window_classifier_and_injector(
    tmp_path: Path, *, min_chunks_to_classify: int = 1
) -> tuple[DetectorConfig, WindowClassifier, SlideInjector, harness._WindowHooks]:
    detector_config = DetectorConfig(
        confidence_threshold_low=0.50,
        classification_debounce_seconds=0.0,
        min_chunks_to_classify=min_chunks_to_classify,
        only_classify_prospect=True,
        preset_name="sales",
        llm_provider="openai",
        llm_model="test-model",
    )
    preset = load_preset(detector_config.preset_name)
    window_hooks = harness._WindowHooks()
    window_classifier = WindowClassifier(detector_config, "openai", preset=preset, hooks=window_hooks)

    db_path = tmp_path / "cases.db"
    case_db = SQLiteCaseDB(db_path)  # .initialize() is awaited by the caller (async context)
    slides_config = SlidesConfig(case_db_sqlite_path=str(db_path), prospect_industry=None)
    ws_config = WebSocketConfig(host="127.0.0.1", port=0)
    injector = SlideInjector(SimpleNamespace(), case_db, ws_config, slides_config)
    return detector_config, window_classifier, injector, window_hooks


def _fake_astream_yielding(analysis: WindowAnalysis):
    async def _astream(**_kwargs):
        yield analysis

    return _astream


async def _run_synthetic_session_v2(
    tmp_path: Path,
    *,
    analysis: WindowAnalysis,
    run_llm: bool = True,
    min_chunks_to_classify: int = 1,
) -> tuple[list[harness.SegmentResult], WindowClassifier]:
    session_dir = tmp_path / "synthetic-session"
    session_dir.mkdir()
    make_synthetic_wav(session_dir / "prospect.wav", duration_s=2.0)

    detector_config, window_classifier, injector, window_hooks = _build_window_classifier_and_injector(
        tmp_path, min_chunks_to_classify=min_chunks_to_classify
    )
    await injector._case_db.initialize()

    def _fake_astream(**_kwargs):
        window_classifier._llm._last_ttft_ms = _WINDOW_TTFT_MS
        return _fake_astream_yielding(analysis)(**_kwargs)

    window_classifier._llm.astream = _fake_astream
    window_classifier._llm._create = unittest.mock.MagicMock(
        side_effect=AssertionError("acreate must not be called -- v2 always streams")
    )
    backend = _FakeBackend(_KEYWORD_UTTERANCE)
    ws_config = WebSocketConfig(host="127.0.0.1", port=0)

    with unittest.mock.patch.object(AudioBufferer, "_load_vad", return_value=None):
        results = await harness._run_session_v2(
            session_dir,
            backend=backend,
            window_classifier=window_classifier,
            injector=injector,
            window_hooks=window_hooks,
            run_llm=run_llm,
            config=detector_config,
            ws_config=ws_config,
            ui_labels={},
            remaining_budget=1,
        )
    return results, window_classifier


def test_run_session_v2_end_to_end_emits_pain_point_with_ttft(tmp_path: Path) -> None:
    analysis = WindowAnalysis(
        detections=[
            WindowDetection(
                category="pain_point",
                subcategory="offerteproces",
                confidence=0.9,
                evidence_quote=_KEYWORD_UTTERANCE,
                reasoning="duidelijk pijnpunt",
            )
        ]
    )

    results, _ = asyncio.run(_run_synthetic_session_v2(tmp_path, analysis=analysis))

    assert len(results) == 1
    seg = results[0]
    assert seg.outcome == "emitted"
    assert seg.category == "offerteproces"
    assert seg.escalated is True  # v2: True iff the gate let this segment reach classify()
    assert seg.text == _KEYWORD_UTTERANCE

    assert seg.whisper_done_ts is not None
    assert seg.router_done_ts is not None
    assert seg.llm_end_ts is not None
    assert seg.emit_ts is not None
    assert seg.vad_close_ts <= seg.whisper_done_ts <= seg.router_done_ts <= seg.llm_end_ts <= seg.emit_ts
    assert seg.llm_ttft_ms == pytest.approx(_WINDOW_TTFT_MS)

    stages = seg.stage_ms()
    assert stages["total_ms"] is not None and stages["total_ms"] >= 0
    assert stages["router_to_llm_ms"] is not None and stages["router_to_llm_ms"] >= 0
    assert stages["llm_ttft_ms"] == pytest.approx(_WINDOW_TTFT_MS)


def test_run_session_v2_no_detections_does_not_emit(tmp_path: Path) -> None:
    results, _ = asyncio.run(_run_synthetic_session_v2(tmp_path, analysis=WindowAnalysis(detections=[])))

    assert len(results) == 1
    seg = results[0]
    assert seg.outcome == "classified_no_detection"
    assert seg.escalated is True
    assert seg.emit_ts is None
    assert seg.llm_ttft_ms == pytest.approx(_WINDOW_TTFT_MS)


def test_run_session_v2_dry_run_never_calls_llm(tmp_path: Path) -> None:
    analysis = WindowAnalysis(
        detections=[
            WindowDetection(
                category="pain_point",
                subcategory="offerteproces",
                confidence=0.9,
                evidence_quote=_KEYWORD_UTTERANCE,
                reasoning="duidelijk pijnpunt",
            )
        ]
    )

    results, window_classifier = asyncio.run(_run_synthetic_session_v2(tmp_path, analysis=analysis, run_llm=False))

    assert len(results) == 1
    seg = results[0]
    assert seg.outcome == "would_classify"
    assert seg.escalated is False  # no LLM call was made in dry-run
    assert seg.llm_ttft_ms is None
    assert seg.emit_ts is None
    window_classifier._llm._create.assert_not_called()


def test_run_session_v2_buffer_below_min_skips_classify(tmp_path: Path) -> None:
    """A single synthetic segment against min_chunks_to_classify=3 never reaches the
    gate -- mirrors detector/__main__.py's own min_chunks_to_classify check."""
    results, window_classifier = asyncio.run(
        _run_synthetic_session_v2(
            tmp_path, analysis=WindowAnalysis(detections=[]), min_chunks_to_classify=3
        )
    )

    assert len(results) == 1
    seg = results[0]
    assert seg.outcome == "buffer_below_min"
    assert seg.escalated is False
    assert seg.emit_ts is None


# ---------------------------------------------------------------------------
# Real session fixtures (skipped when data/sessions/ is absent -- always true
# in CI and dispatch worktrees, see CLAUDE.md)
# ---------------------------------------------------------------------------


@pytest.mark.audio_fixture
def test_dry_run_against_real_session_fixtures(tmp_path: Path) -> None:
    """Run the full harness (real create_backend()) in --dry-run against real
    session recordings. Skipped when data/sessions/ has no prospect.wav
    fixtures -- run locally with: pytest -m audio_fixture tests/test_e2e_latency_harness.py -v
    """
    if not harness.discover_sessions(_SESSIONS_DIR):
        pytest.skip(f"no session fixtures with prospect.wav under {_SESSIONS_DIR}")

    args = argparse.Namespace(
        candidate=None,
        timeout_ms=harness._DEFAULT_TIMEOUT_MS,
        sessions_dir=_SESSIONS_DIR,
        top_n=3,
        run=False,
        v2=False,
    )
    report = asyncio.run(harness.run_harness(args))

    assert report.mode == "dry-run"
    assert report.sessions_scanned >= 1
    _write_and_assert_reports(report, tmp_path)


@pytest.mark.audio_fixture
def test_v2_dry_run_against_real_session_fixtures(tmp_path: Path) -> None:
    """v2 counterpart of test_dry_run_against_real_session_fixtures: same real-fixture
    run, but through the live SlidingWindowBuffer + WindowClassifier path. Skipped when
    data/sessions/ has no prospect.wav fixtures (always true in CI/dispatch worktrees)."""
    if not harness.discover_sessions(_SESSIONS_DIR):
        pytest.skip(f"no session fixtures with prospect.wav under {_SESSIONS_DIR}")

    args = argparse.Namespace(
        candidate=None,
        timeout_ms=harness._DEFAULT_TIMEOUT_MS,
        sessions_dir=_SESSIONS_DIR,
        top_n=3,
        run=False,
        v2=True,
    )
    report = asyncio.run(harness.run_harness(args))

    assert report.mode == "v2-dry-run"
    assert report.sessions_scanned >= 1
    _write_and_assert_reports(report, tmp_path)


def _write_and_assert_reports(report: harness.Report, tmp_path: Path) -> None:
    report_md = tmp_path / "report.md"
    report_json = tmp_path / "report.json"
    harness._write_reports(report, report_md, report_json)
    assert report_md.exists()
    assert report_json.exists()
