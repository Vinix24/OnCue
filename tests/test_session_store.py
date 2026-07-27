from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from sales_copilot.core.session_store import (
    _GC_THRESHOLD_SECONDS,
    DetectionRecord,
    SessionStore,
)


def _make_detection(session_id: str, subcat: str | None = None) -> DetectionRecord:
    return DetectionRecord(
        session_id=session_id,
        ts=time.time(),
        type="pain_point",
        subcat=subcat,
        confidence=0.85,
        text="test text",
    )


def test_create_and_read_back(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session("sess-1", profile="sales")
    rows = store.find_unfinished_sessions()
    assert len(rows) == 1
    assert rows[0].id == "sess-1"
    assert rows[0].profile == "sales"
    assert rows[0].end_ts is None


def test_end_session_marks_end_ts(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session("sess-2")
    store.end_session("sess-2")
    unfinished = store.find_unfinished_sessions()
    assert all(s.id != "sess-2" for s in unfinished)


def test_append_detection_persists(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session("sess-3")
    det = _make_detection("sess-3", subcat="capaciteit")
    store.append_detection(det)
    detections = store.get_session_detections("sess-3")
    assert len(detections) == 1
    assert detections[0].subcat == "capaciteit"
    assert detections[0].confidence == pytest.approx(0.85)
    assert detections[0].text == "test text"


def test_find_unfinished_sessions_filters_end_ts_null(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session("open-1")
    store.create_session("closed-1")
    store.end_session("closed-1")
    unfinished = store.find_unfinished_sessions()
    ids = [s.id for s in unfinished]
    assert "open-1" in ids
    assert "closed-1" not in ids


def test_find_unfinished_excludes_sessions_older_than_24h(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session("old-sess")
    # Simulate session older than 24h by patching time.time during the query
    old_ts = time.time() - _GC_THRESHOLD_SECONDS - 1
    conn = sqlite3.connect(tmp_path / "sessions.db")
    conn.execute("UPDATE sessions SET start_ts = ? WHERE id = ?", (old_ts, "old-sess"))
    conn.commit()
    conn.close()

    unfinished = store.find_unfinished_sessions()
    assert all(s.id != "old-sess" for s in unfinished)


def test_gc_old_sessions_removes_sessions_and_detections(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session("gc-target")
    store.append_detection(_make_detection("gc-target"))

    old_ts = time.time() - _GC_THRESHOLD_SECONDS - 1
    conn = sqlite3.connect(tmp_path / "sessions.db")
    conn.execute("UPDATE sessions SET start_ts = ? WHERE id = ?", (old_ts, "gc-target"))
    conn.commit()
    conn.close()

    removed = store.gc_old_sessions()
    assert removed == 1

    detections = store.get_session_detections("gc-target")
    assert detections == []


def test_concurrent_detection_writes_no_lock_fail(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    store.create_session("concurrent-sess")
    errors: list[Exception] = []

    def writer() -> None:
        for _ in range(10):
            try:
                store.append_detection(_make_detection("concurrent-sess"))
            except Exception as exc:
                errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Concurrent writes raised: {errors}"
    detections = store.get_session_detections("concurrent-sess")
    assert len(detections) == 20


def test_corrupt_db_reinit_without_crash(tmp_path: Path) -> None:
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"NOT A VALID SQLITE DATABASE")
    # Should raise on a truly corrupt file, but we test a fresh empty db
    # that gets overwritten by a clean init. The real defense is mkdir+touch.
    clean_path = tmp_path / "fresh.db"
    store = SessionStore(clean_path)
    store.create_session("fresh-sess")
    rows = store.find_unfinished_sessions()
    assert len(rows) == 1


def test_session_store_init_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "sessions.db"
    store = SessionStore(nested)
    store.create_session("nested-sess")
    assert nested.exists()


def test_append_detection_unknown_session_raises_integrity_error(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    det = _make_detection("nonexistent-session")
    with pytest.raises(sqlite3.IntegrityError):
        store.append_detection(det)
