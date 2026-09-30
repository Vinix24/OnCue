import asyncio
import json
import socket
import threading
import time
from collections.abc import AsyncIterator

import aiosqlite
import pytest
import uvicorn
import websockets

from sales_copilot.core.config import WebSocketConfig
from sales_copilot.core.session_store import SessionStore
from sales_copilot.modules.reports.session import SessionTracker
from sales_copilot.websocket import hub, hub_core
from tests.ws_helpers import ws_url


@pytest.mark.asyncio
async def test_session_tracker_accumulates_payloads(tmp_path) -> None:
    tracker = SessionTracker(
        db_path=tmp_path / "cases.db",
        prospect_name="Alicia",
        prospect_company="Umbrella",
        context_docs=["/tmp/context.md"],
        now_iso=lambda: "2026-04-13T00:00:00+00:00",
        now_ms=lambda: 123,
    )
    tracker._start_state()

    await tracker._handle_transcript(
        {
            "type": "transcript",
            "text": "Hallo",
            "speaker": "prospect",
            "start_ms": 100,
            "end_ms": 200,
            "is_final": True,
        }
    )
    await tracker._handle_pain_points(
        {
            "type": "pain_point",
            "category": "offerteproces",
            "confidence": 0.9,
        }
    )
    await tracker._handle_talk_time(
        {"type": "talk_time", "phase": "discovery", "call_duration_ms": 5000}
    )
    await tracker._handle_talk_time(
        {"type": "talk_time", "phase": "pitch", "call_duration_ms": 9000}
    )
    await tracker._handle_coaching(
        {"type": "coaching_alert", "alert_type": "ratio_warning"}
    )
    await tracker._handle_summary(
        {
            "type": "summary",
            "text": "Korte samenvatting.",
            "timestamp_ms": 10000,
            "key_moments": [],
        }
    )
    await tracker._handle_insight(
        {
            "type": "insight",
            "insight_type": "doorvraag",
            "text": "Wat weet de klant al over Lime CRM?",
            "grounding": "prospect noemde Lime CRM",
            "speculation": "hoog",
            "ttl_s": 120,
            "timestamp_ms": 12000,
        }
    )

    data = await tracker.get_session_data()

    assert len(data.transcript) == 1
    assert data.prospect_name == "Alicia"
    assert data.prospect_company == "Umbrella"
    assert data.context_docs == ["/tmp/context.md"]
    assert len(data.pain_points) == 1
    assert len(data.talk_time_snapshots) == 2
    assert len(data.phase_transitions) == 2
    assert data.phase_transitions[0]["phase"] == "discovery"
    assert data.phase_transitions[1]["phase"] == "pitch"
    assert len(data.coaching_alerts) == 1
    assert len(data.summaries) == 1
    assert len(data.insights) == 1
    assert data.insights[0]["insight_type"] == "doorvraag"


@pytest.mark.asyncio
async def test_session_tracker_insight_handler_filters_non_insight_payloads(tmp_path) -> None:
    """Only `insight` payloads land in the report -- `ask` (input) and
    `insight_budget_exhausted` (a management notice) are excluded."""
    tracker = SessionTracker(
        db_path=tmp_path / "cases.db",
        now_iso=lambda: "2026-08-04T00:00:00+00:00",
        now_ms=lambda: 1,
    )
    tracker._start_state()

    await tracker._handle_insight({"type": "ask", "text": "zwaktes van Lime CRM?"})
    await tracker._handle_insight(
        {"type": "insight_budget_exhausted", "calls_used": 30, "max_calls": 30}
    )
    await tracker._handle_insight(
        {
            "type": "insight",
            "insight_type": "antwoord",
            "text": "Lime CRM mist een native koppeling.",
            "grounding": "vraag: zwaktes van Lime CRM?",
            "speculation": "laag",
            "ttl_s": 60,
            "question": "zwaktes van Lime CRM?",
            "timestamp_ms": 5000,
        }
    )
    await tracker._handle_insight("not-a-dict")

    data = await tracker.get_session_data()

    assert len(data.insights) == 1
    assert data.insights[0]["insight_type"] == "antwoord"
    assert data.insights[0]["question"] == "zwaktes van Lime CRM?"


@pytest.mark.asyncio
async def test_session_tracker_persists_session(tmp_path) -> None:
    tracker = SessionTracker(
        db_path=tmp_path / "cases.db",
        prospect_name="Dana",
        prospect_company="Globex",
        context_docs=[],
        now_iso=lambda: "2026-04-13T00:00:00+00:00",
        now_ms=lambda: 456,
    )
    tracker._start_state()
    await tracker._handle_transcript(
        {"type": "transcript", "text": "Test", "speaker": "self", "start_ms": 10}
    )

    session = await tracker.get_session_data()
    await tracker._persist_session(session)

    async with aiosqlite.connect(tmp_path / "cases.db") as conn:
        cursor = await conn.execute("SELECT transcript FROM call_sessions")
        row = await cursor.fetchone()

    assert row is not None
    transcript = json.loads(row[0])
    assert transcript[0]["text"] == "Test"


@pytest.mark.asyncio
async def test_session_tracker_checkpoint_loop_persists_periodically(tmp_path) -> None:
    tracker = SessionTracker(
        db_path=tmp_path / "cases.db",
        checkpoint_enabled=True,
        checkpoint_interval_seconds=1,
    )
    tracker._start_state()  # noqa: SLF001
    tracker._stop_event = asyncio.Event()  # noqa: SLF001
    await tracker._handle_transcript(  # noqa: SLF001
        {"type": "transcript", "text": "Checkpoint", "speaker": "self"}
    )

    # Count periodic checkpoints and release on the first one instead of
    # sleeping a fixed interval — this pins the checkpoint count and the
    # prompt loop-halt on stop rather than only the persisted row.
    checkpoints = 0
    first_checkpoint = asyncio.Event()
    original_persist = tracker._persist_session  # noqa: SLF001

    async def _counting_persist(session) -> None:
        nonlocal checkpoints
        checkpoints += 1
        await original_persist(session)
        first_checkpoint.set()

    tracker._persist_session = _counting_persist  # type: ignore[assignment]  # noqa: SLF001

    checkpoint_task = asyncio.create_task(tracker._checkpoint_loop())  # noqa: SLF001
    await asyncio.wait_for(first_checkpoint.wait(), timeout=5.0)

    # Stop must halt the loop promptly: it waits on stop_event with a per-cycle
    # timeout, so the wait returns immediately when set.
    tracker._stop_event.set()  # noqa: SLF001
    await asyncio.wait_for(checkpoint_task, timeout=1.0)

    # Exactly one periodic checkpoint fired in this window — the loop must not
    # double-persist per interval, and it must stop after the halt signal.
    assert checkpoints == 1

    async with aiosqlite.connect(tmp_path / "cases.db") as conn:
        cursor = await conn.execute(
            "SELECT transcript FROM call_sessions WHERE id = ?",
            (tracker._session_id,),  # noqa: SLF001
        )
        row = await cursor.fetchone()

    assert row is not None
    transcript = json.loads(row[0])
    assert transcript[0]["text"] == "Checkpoint"


@pytest.mark.asyncio
async def test_session_tracker_accepts_transcript_with_null_speaker(tmp_path) -> None:
    tracker = SessionTracker(db_path=tmp_path / "cases.db")
    tracker._start_state()  # noqa: SLF001
    await tracker._handle_transcript(  # noqa: SLF001
        {"type": "transcript", "text": "Hello", "speaker": None, "start_ms": 0, "end_ms": 100}
    )
    data = await tracker.get_session_data()
    assert len(data.transcript) == 1
    assert data.transcript[0]["speaker"] == "unknown"
    assert data.transcript[0]["text"] == "Hello"


@pytest.mark.asyncio
async def test_session_tracker_records_audio_loss_marker(tmp_path) -> None:
    """A saved transcript must say the audio stopped, not imply the speaker did."""

    tracker = SessionTracker(db_path=tmp_path / "cases.db")
    tracker._start_state()  # noqa: SLF001
    await tracker._handle_transcript(  # noqa: SLF001
        {
            "type": "transcript",
            "text": "Hoeveel offertes maakt u per week?",
            "speaker": "self",
            "start_ms": 0,
            "end_ms": 900,
        }
    )
    await tracker._handle_transcript(  # noqa: SLF001
        {
            "type": "transcript_marker",
            "marker": "audio_signal_lost",
            "stream": "prospect",
            "speaker": "system",
            "text": "[audio ontbreekt vanaf hier: de prospect-tap levert geen signaal meer.]",
            "start_ms": 960000,
            "end_ms": 960000,
        }
    )

    data = await tracker.get_session_data()
    assert len(data.transcript) == 2
    marker = data.transcript[1]
    assert marker["speaker"] == "system"
    assert marker["start_ms"] == 960000
    assert marker["end_ms"] == 960000
    assert "audio ontbreekt" in marker["text"]


@pytest.mark.asyncio
async def test_session_tracker_drops_empty_marker(tmp_path) -> None:
    tracker = SessionTracker(db_path=tmp_path / "cases.db")
    tracker._start_state()  # noqa: SLF001
    await tracker._handle_transcript(  # noqa: SLF001
        {"type": "transcript_marker", "text": "   ", "start_ms": 10}
    )
    data = await tracker.get_session_data()
    assert data.transcript == []


@pytest.mark.asyncio
async def test_session_tracker_drops_empty_text(tmp_path) -> None:
    tracker = SessionTracker(db_path=tmp_path / "cases.db")
    tracker._start_state()  # noqa: SLF001
    await tracker._handle_transcript(  # noqa: SLF001
        {"type": "transcript", "text": "   ", "speaker": "self"}
    )
    data = await tracker.get_session_data()
    assert data.transcript == []


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 3.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("Timed out waiting for WebSocket hub")


@pytest.fixture()
async def running_hub() -> AsyncIterator[int]:
    port = _free_port()
    config = uvicorn.Config(
        hub.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _wait_for_port("127.0.0.1", port)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=2)
        hub._subscribers.clear()
        hub_core.reset_config_state()


@pytest.mark.asyncio
async def test_late_session_tracker_replays_buffered_transcripts(
    running_hub: int, tmp_path
) -> None:
    """Reproduces the PR-96.3/PR-98 bug surface: orchestrator fires
    start_call, transcriber publishes events, and the session-tracker
    subscribes after the first events have already been broadcast. The
    sticky replay buffer + the relaxed transcript filter together must
    capture every event in the post-call session data."""

    hub_core.reset_config_state()
    async with hub_core._config_lock:
        hub_core.apply_start_call({"preset_name": "demo"})

    transcript_uri = ws_url("127.0.0.1", running_hub, "transcript")
    async with websockets.connect(transcript_uri) as publisher:
        events = [
            {"type": "transcript", "text": "Eerste zin.", "speaker": "self",
             "start_ms": 0, "end_ms": 1000, "is_final": True},
            {"type": "transcript", "text": "Tweede zin.", "speaker": "prospect",
             "start_ms": 1000, "end_ms": 2000, "is_final": True},
            {"type": "transcript", "text": "Derde zin.", "speaker": None,
             "start_ms": 2000, "end_ms": 3000, "is_final": True},
        ]
        for event in events:
            await publisher.send(json.dumps(event))

        for _ in range(40):
            if len(hub_core._transcript_buffer) >= 3:
                break
            await asyncio.sleep(0.05)
        assert len(hub_core._transcript_buffer) == 3

        ws_config = WebSocketConfig(host="127.0.0.1", port=running_hub)
        tracker = SessionTracker(
            ws_config=ws_config,
            db_path=tmp_path / "cases.db",
            checkpoint_enabled=False,
        )
        await tracker.start_session()

        for _ in range(40):
            data = await tracker.get_session_data()
            if len(data.transcript) >= 3:
                break
            await asyncio.sleep(0.05)

        session = await tracker.end_session()

    assert len(session.transcript) == 3
    assert session.transcript[0]["text"] == "Eerste zin."
    assert session.transcript[0]["speaker"] == "self"
    assert session.transcript[1]["speaker"] == "prospect"
    assert session.transcript[2]["speaker"] == "unknown"


@pytest.mark.asyncio
async def test_session_tracker_with_store_writes_to_sqlite(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    tracker = SessionTracker(
        db_path=tmp_path / "cases.db",
        now_iso=lambda: "2026-05-16T00:00:00+00:00",
        now_ms=lambda: 100,
        session_store=store,
    )
    tracker._start_state()

    await tracker._handle_pain_points(
        {"type": "pain_point", "category": "capaciteit", "confidence": 0.9}
    )
    # Allow to_thread to complete
    await asyncio.sleep(0.05)

    session_id = tracker._session_id
    assert session_id is not None

    unfinished = store.find_unfinished_sessions()
    assert any(s.id == session_id for s in unfinished)

    detections = store.get_session_detections(session_id)
    assert len(detections) == 1
    assert detections[0].type == "pain_point"
    assert detections[0].subcat == "capaciteit"


@pytest.mark.asyncio
async def test_session_tracker_without_store_backward_compat(tmp_path) -> None:
    tracker = SessionTracker(
        db_path=tmp_path / "cases.db",
        now_iso=lambda: "2026-05-16T00:00:00+00:00",
        now_ms=lambda: 100,
    )
    tracker._start_state()

    await tracker._handle_pain_points(
        {"type": "pain_point", "category": "kosten", "confidence": 0.8}
    )
    data = await tracker.get_session_data()
    assert len(data.pain_points) == 1
    # No store attached - must not raise
    assert tracker._session_store is None


@pytest.mark.asyncio
async def test_session_tracker_resume_scenario_store_has_correct_detections(tmp_path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    tracker = SessionTracker(
        db_path=tmp_path / "cases.db",
        now_iso=lambda: "2026-05-16T00:00:00+00:00",
        now_ms=lambda: 100,
        session_store=store,
    )
    tracker._start_state()
    session_id = tracker._session_id
    assert session_id is not None

    await tracker._handle_pain_points(
        {"type": "pain_point", "category": "integratie", "confidence": 0.95}
    )
    await tracker._handle_pain_points(
        {"type": "pain_point", "category": "prijs", "confidence": 0.75}
    )
    await asyncio.sleep(0.05)

    # Simulate crash: store still has open session
    unfinished = store.find_unfinished_sessions()
    ids = [s.id for s in unfinished]
    assert session_id in ids

    detections = store.get_session_detections(session_id)
    assert len(detections) == 2
    subcats = {d.subcat for d in detections}
    assert "integratie" in subcats
    assert "prijs" in subcats
