"""Phase 3 -- Free post-call scorecard + Q3 conversion counter.

Design doc claudedocs/2026-07-22-salesprep-pro-design.md: section 9 Phase 3
(scorecard = the count and the gap, read from the PERSISTED checkpoint
snapshot), section 3 (Free/Pro seam: deterministic, local, no LLM), open
question 5 (Q3 conversion instrumentation: local-only counts, no nag).
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.conversion import (
    METRIC_GAP_SHOWN,
    METRIC_TIER_OBSERVED,
    METRIC_UPGRADE,
    ConversionCounterStore,
)
from sales_copilot.modules.coaching.script_tracker import (
    ScriptPoint,
    ScriptTracker,
    build_coverage_summary,
    load_script_points,
)
from sales_copilot.modules.detector.router import RouteMatch
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.__main__ import _build_scorecard, _report_payload
from sales_copilot.modules.reports.session import (
    ScriptCoverageCheckpointStore,
    SessionData,
    SessionTracker,
    _count_responded_objections,
)

SCRIPT_CONFIG = "config/scripts/default.yaml"


class _StaticRouter:
    def __init__(self, matches: dict[str, str]) -> None:
        self._matches = matches

    async def classify_async(self, text: str) -> RouteMatch | None:
        category = self._matches.get(text)
        if category is None:
            return None
        return RouteMatch(category=category, confidence=0.99, tier="high", source="keyword")


@pytest.fixture
def detector_config() -> DetectorConfig:
    return DetectorConfig(
        llm_provider="none",
        confidence_threshold_high=0.85,
        confidence_threshold_low=0.50,
    )


def _minimal_session(**overrides: object) -> SessionData:
    base: dict[str, object] = {
        "session_id": "session-1",
        "started_at": "2026-07-23T00:00:00+00:00",
        "ended_at": "2026-07-23T00:01:30+00:00",
        "prospect_name": None,
        "prospect_company": None,
        "context_docs": [],
        "transcript": [],
        "pain_points": [],
        "talk_time_snapshots": [],
        "phase_transitions": [],
        "coaching_alerts": [],
        "summaries": [],
    }
    base.update(overrides)
    return SessionData(**base)  # type: ignore[arg-type]


def _coverage_row(point_id: str, title: str, status: str) -> dict[str, object]:
    return {
        "point_id": point_id,
        "title": title,
        "phase": "discovery",
        "required": True,
        "status": status,
        "confidence": 0.9,
        "hint": "",
        "timestamp_ms": 1000,
    }


# --- (a) SessionData carries script_coverage + counts and round-trips -------


def test_session_data_scorecard_fields_default() -> None:
    session = _minimal_session()

    assert session.script_coverage is None
    assert session.objection_count == 0
    assert session.objection_responded_count == 0
    assert session.opportunity_count == 0


async def test_session_data_carries_scorecard_and_persists(tmp_path: Path) -> None:
    db_path = tmp_path / "cases.db"
    store = ScriptCoverageCheckpointStore(db_path)
    await store.save(
        "session-1",
        {"budget": _coverage_row("budget", "Budget / investeringsruimte", "discussed")},
    )

    tracker = SessionTracker(
        db_path=db_path,
        session_id="session-1",
        now_iso=lambda: "2026-07-23T00:00:00+00:00",
        now_ms=lambda: 0,
    )
    tracker._start_state()
    await tracker._handle_transcript(
        {
            "type": "transcript",
            "text": "Begrijpelijk, laat me uitleggen.",
            "speaker": "self",
            "start_ms": 5000,
            "end_ms": 7000,
            "is_final": True,
        }
    )
    await tracker._handle_objection(
        {
            "type": "objection",
            "category": "prijs",
            "confidence": 0.9,
            "trigger_phrase": "het is te duur",
            "timestamp_ms": 4000,
        }
    )
    await tracker._handle_objection(
        {
            "type": "objection",
            "category": "timing",
            "confidence": 0.8,
            "trigger_phrase": "niet nu",
            "timestamp_ms": 600000,
        }
    )
    await tracker._handle_buying_signal(
        {
            "type": "buying_signal",
            "category": "gap-behoefte",
            "confidence": 0.9,
            "trigger_phrase": "dat kost ons veel tijd",
            "timestamp_ms": 9000,
        }
    )

    session = await tracker.get_session_data()

    assert session.objection_count == 2
    # Only the "prijs" objection (ts=4000) has self speech inside the window.
    assert session.objection_responded_count == 1
    assert session.opportunity_count == 1
    assert session.script_coverage is not None
    assert session.script_coverage["covered"] == 1
    assert session.script_coverage["total"] == 9
    missing = session.script_coverage["missing_required"]
    assert "Budget / investeringsruimte" not in missing
    assert len(missing) == 8

    # Round-trip: the scorecard data persists with the session row, even if
    # the report UI section is hidden (the phase's rollback invariant).
    await tracker._persist_session(session)
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT talk_time_data FROM call_sessions WHERE id = ?", ("session-1",)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    bundle = json.loads(row[0])
    assert bundle["script_coverage"]["covered"] == 1
    assert bundle["objection_count"] == 2
    assert bundle["objection_responded_count"] == 1
    assert bundle["opportunity_count"] == 1


# --- (b) The scorecard reads from the PERSISTED snapshot --------------------


async def test_scorecard_reads_persisted_snapshot_not_live_memory(
    tmp_path: Path, detector_config: DetectorConfig
) -> None:
    db_path = tmp_path / "cases.db"
    store = ScriptCoverageCheckpointStore(db_path)

    # Detector side: a live ScriptTracker covers one point and checkpoints.
    tracker_a = ScriptTracker(
        detector_config,
        router=_StaticRouter({"laten we over budget praten": "budget"}),
        session_id="session-1",
        checkpoint_store=store,
    )
    await tracker_a.process_utterance("laten we over budget praten", "self", 1000)
    await tracker_a.checkpoint()

    # Reports side: a different object with no access to tracker_a's memory --
    # the SQLite checkpoint file is the only possible source.
    tracker = SessionTracker(db_path=db_path, session_id="session-1")
    tracker._start_state()
    session = await tracker.get_session_data()

    persisted = await store.load("session-1")
    expected = build_coverage_summary(load_script_points(SCRIPT_CONFIG), persisted)
    assert session.script_coverage == expected
    assert session.script_coverage is not None
    assert session.script_coverage["covered"] == 1
    assert session.script_coverage["total"] == 9


async def test_scorecard_is_none_without_checkpoint(tmp_path: Path) -> None:
    tracker = SessionTracker(db_path=tmp_path / "cases.db", session_id="no-checkpoint")
    tracker._start_state()

    session = await tracker.get_session_data()

    assert session.script_coverage is None


# --- (c) The missing-required list is correct --------------------------------


def test_missing_required_list_matches_status_rules() -> None:
    points = load_script_points(SCRIPT_CONFIG)
    stored = {
        "budget": {"status": "discussed"},
        "tijdlijn": {"status": "partial"},
        "beslisser": {"status": "confirmed"},
    }

    summary = build_coverage_summary(points, stored)

    assert summary is not None
    assert summary["covered"] == 2
    assert summary["total"] == 9
    missing = summary["missing_required"]
    assert "Budget / investeringsruimte" not in missing
    assert "Beslissers / betrokkenen" not in missing
    # "partial" counts as a gap, exactly like missing_required_points().
    assert "Tijdlijn / beslisftermijn" in missing
    assert len(missing) == 7


def test_coverage_summary_skips_non_required_unknown_and_invalid() -> None:
    points = [
        ScriptPoint(id="a", title="A", phase="discovery", required=True),
        ScriptPoint(id="b", title="B", phase="pitch", required=False),
    ]
    stored = {
        "a": {"status": "bogus"},  # invalid -> treated as missing
        "b": {"status": "missing"},  # not required -> never a gap
        "ghost": {"status": "discussed"},  # unknown point -> ignored
    }

    summary = build_coverage_summary(points, stored)

    assert summary == {"covered": 0, "total": 2, "missing_required": ["A"]}


def test_coverage_summary_none_without_snapshot() -> None:
    assert build_coverage_summary(load_script_points(SCRIPT_CONFIG), None) is None


def test_responded_counts_self_speech_within_window() -> None:
    objections = [{"timestamp_ms": 10_000}, {"timestamp_ms": 50_000}]
    transcript = [
        {"speaker": "self", "start_ms": 12_000},
        {"speaker": "prospect", "start_ms": 13_000},
        {"speaker": "self", "start_ms": 55_000},
    ]

    assert _count_responded_objections(objections, transcript, window_ms=60_000) == 2


def test_responded_ignores_late_or_missing_replies() -> None:
    objections = [{"timestamp_ms": 10_000}, {"timestamp_ms": 500_000}, {"category": "prijs"}]
    transcript = [{"speaker": "self", "start_ms": 100_000}]

    # 100s > 10s + 60s window; nothing after 500s; no timestamp -> skipped.
    assert _count_responded_objections(objections, transcript, window_ms=60_000) == 0


# --- Scorecard assembly + report payload -------------------------------------


def test_build_scorecard_from_session_data() -> None:
    session = _minimal_session(
        script_coverage={"covered": 6, "total": 9, "missing_required": ["Budget / investeringsruimte"]},
        objection_count=8,
        objection_responded_count=3,
        opportunity_count=2,
    )

    scorecard = _build_scorecard(session)

    assert scorecard == {
        "script_covered": 6,
        "script_total": 9,
        "missing_required": ["Budget / investeringsruimte"],
        "objection_count": 8,
        "objection_responded_count": 3,
        "opportunity_count": 2,
    }


def test_build_scorecard_without_coverage_omits_script_numbers() -> None:
    scorecard = _build_scorecard(_minimal_session(objection_count=1))

    assert scorecard["script_covered"] is None
    assert scorecard["script_total"] is None
    assert scorecard["missing_required"] == []
    assert scorecard["objection_count"] == 1


def test_report_payload_carries_scorecard() -> None:
    scorecard = {
        "script_covered": 6,
        "script_total": 9,
        "missing_required": ["Budget / investeringsruimte"],
        "objection_count": 8,
        "objection_responded_count": 3,
        "opportunity_count": 2,
    }
    report = dataclasses.replace(
        generator.CallReport(
            session_id="session-1",
            call_duration_ms=90000,
            prospect_name=None,
            prospect_company=None,
            context_docs=[],
            phase_timeline=[],
            per_minute_talk_time=[],
            pain_points_detected=[],
            conversation_summary=None,
            key_moments=[],
            monologue_count=0,
            total_self_pct=0.4,
            total_prospect_pct=0.6,
            full_transcript=[],
        ),
        scorecard=scorecard,
    )

    payload = _report_payload(report, None)

    assert payload["scorecard"] == scorecard


# --- (d) Q3 conversion counter: local-only, increments on both events -------


def test_conversion_counter_counts_gap_shown(tmp_path: Path) -> None:
    store = ConversionCounterStore(tmp_path / "sessions.db")

    store.record_gap_shown("session-1")
    store.record_gap_shown("session-2")

    assert store.count(METRIC_GAP_SHOWN) == 2
    assert store.counts()[METRIC_GAP_SHOWN] == 2


def test_conversion_counter_upgrade_on_free_to_pro_transition(tmp_path: Path) -> None:
    store = ConversionCounterStore(tmp_path / "sessions.db")

    assert store.note_tier("free") is False
    assert store.note_tier("free") is False
    assert store.note_tier("pro") is True
    assert store.count(METRIC_UPGRADE) == 1
    # Staying on pro is not a new upgrade.
    assert store.note_tier("pro") is False
    assert store.note_tier("enterprise") is False
    assert store.count(METRIC_UPGRADE) == 1
    assert store.counts()[METRIC_TIER_OBSERVED] == 5


def test_conversion_counter_first_pro_observation_is_not_an_upgrade(tmp_path: Path) -> None:
    store = ConversionCounterStore(tmp_path / "sessions.db")

    assert store.note_tier("pro") is False
    assert store.count(METRIC_UPGRADE) == 0


def test_conversion_counter_free_to_enterprise_counts(tmp_path: Path) -> None:
    store = ConversionCounterStore(tmp_path / "sessions.db")

    assert store.note_tier("free") is False
    assert store.note_tier("enterprise") is True
    assert store.count(METRIC_UPGRADE) == 1


def test_conversion_counter_stays_local(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = ConversionCounterStore(db_path)
    store.record_gap_shown("session-1")

    # Queryable directly via sqlite3 -- no service, no network involved.
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT metric, detail FROM conversion_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(METRIC_GAP_SHOWN, "session-1")]
