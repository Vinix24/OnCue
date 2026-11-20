"""Library API and pages of scripts/record_server.py: status derivation, report link, local-only."""

# ruff: noqa: F811  (the rs fixture is imported from test_record_server and used as a test argument)
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.test_record_server import BASE, EVIL_ORIGIN, GOOD_ORIGIN, _load_module, rs  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent
UUID = "3f2b8c1e-9a44-4d1b-8e0a-5c6d7e8f9a0b"


@pytest.fixture
def client(rs):
    with TestClient(rs.app, base_url=BASE, client=("127.0.0.1", 50000)) as c:
        yield c


def _session(rs, name: str, *files: str, metadata: dict | None = None) -> Path:
    path = Path(rs.OUT_DIR) / name
    path.mkdir()
    for f in files:
        (path / f).write_text("x" if f.endswith(".wav") else "[00:01] Me: hello\n[00:03] Other: hi there\n")
    if metadata is not None:
        (path / "metadata.json").write_text(json.dumps(metadata))
    return path


def _report(rs, session_id: str, stamp: str = "2026-11-10T09-00-00", **fields) -> Path:
    reports = Path(rs.OUT_DIR).parent / "reports"
    reports.mkdir(exist_ok=True)
    path = reports / f"{stamp}_{session_id}_report.json"
    data = {"session_id": session_id, "call_duration_ms": 61000, "call_started_at": "2026-11-10T09:00:00+00:00"}
    path.write_text(json.dumps({**data, **fields}))
    return path


def _rows(client) -> dict[str, dict]:
    response = client.get("/api/sessions", headers=GOOD_ORIGIN)
    assert response.status_code == 200
    return {row["id"]: row for row in response.json()["sessions"]}


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ((".recording", "self.wav"), "recording"),
        ((".recording", ".transcribe-pending", "transcript.md"), "recording"),
        ((".transcribe-pending", "self.wav"), "transcribing"),
        (("transcription-failed.txt", "self.wav"), "failed"),
        (("recovery-failed.txt",), "failed"),
        (("transcript.md", "self.wav"), "ready"),
        (("transcript.md", "transcription-failed.txt"), "failed"),
        (("self.wav", "prospect.wav"), "incomplete"),
    ],
)
def test_status_is_derived_from_markers_and_files(rs, client, files, expected) -> None:
    _session(rs, "20261110-090000", *files)
    assert _rows(client)["20261110-090000"]["status"] == expected


def test_directory_without_transcript_report_or_audio_is_skipped(rs, client) -> None:
    _session(rs, "20261110-090000", "metadata.json")
    _session(rs, "20261110-100000")
    (Path(rs.OUT_DIR) / "stray.txt").write_text("x")
    assert _rows(client) == {}


def test_sessions_are_listed_newest_first_with_title_date_and_duration(rs, client) -> None:
    _session(rs, "20261108-140300", "transcript.md", metadata={"duration_ms": 754000})
    _session(rs, "20261110-090000", "transcript.md", metadata={"title": "Planning call", "duration_ms": 1000})
    response = client.get("/api/sessions").json()["sessions"]
    assert [r["id"] for r in response] == ["20261110-090000", "20261108-140300"]
    assert response[0]["title"] == "Planning call"
    assert response[1]["title"] == "2026-11-08 14:03"
    assert response[1]["date"] == "2026-11-08"
    assert response[1]["duration_s"] == 754
    assert response[1]["has_summary"] is False


def test_copilot_directory_with_report_is_listed_with_summary_and_report_duration(rs, client) -> None:
    _session(rs, UUID)
    _report(
        rs,
        UUID,
        short_summary="Agreed on next steps.",
        full_transcript=[{"speaker": "Me", "text": "hi", "start_ms": 2000, "end_ms": 3000}],
    )
    row = _rows(client)[UUID]
    assert (row["status"], row["has_summary"], row["duration_s"]) == ("ready", True, 61)
    detail = client.get(f"/api/sessions/{UUID}").json()
    assert detail["summary"]["short_summary"] == "Agreed on next steps."
    assert detail["transcript"] is None and detail["speakers"] == []


def test_report_is_linked_by_exact_session_id_and_agreeing_json(rs, client) -> None:
    _session(rs, UUID, "transcript.md")
    _report(rs, UUID + "-other", short_summary="Wrong session.")
    _report(rs, UUID, stamp="2026-11-10T08-00-00", short_summary="Old.")
    _report(rs, UUID, stamp="2026-11-10T09-30-00", short_summary="Newest.")
    assert client.get(f"/api/sessions/{UUID}").json()["summary"]["short_summary"] == "Newest."
    mismatch = _report(rs, UUID, stamp="2026-11-11T09-30-00", short_summary="Names another session.")
    mismatch.write_text(json.dumps({"session_id": "someone-else", "short_summary": "x"}))
    detail = client.get(f"/api/sessions/{UUID}").json()
    assert detail["summary"] is None and detail["status"] == "ready"


def test_corrupt_report_shows_the_transcript_without_summary_and_logs(rs, client, caplog) -> None:
    _session(rs, UUID, "transcript.md")
    _report(rs, UUID).write_text("{not json")
    detail = client.get(f"/api/sessions/{UUID}").json()
    assert detail["summary"] is None and "hello" in detail["transcript"]
    assert "Unreadable" in caplog.text


def test_detail_has_header_transcript_speakers_and_no_summary(rs, client) -> None:
    _session(rs, "20261108-140300", "transcript.md", metadata={"duration_ms": 90000})
    detail = client.get("/api/sessions/20261108-140300").json()
    assert detail["speakers"] == ["Me", "Other"]
    assert detail["duration_s"] == 90 and detail["summary"] is None
    assert "[00:03] Other: hi there" in detail["transcript"]


@pytest.mark.parametrize("session", ["..", "../x", "%2e%2e", "a/b", "-x", "nope"])
def test_detail_refuses_invalid_ids_and_unknown_sessions(rs, client, session) -> None:
    assert client.get(f"/api/sessions/{session}").status_code in (400, 404)


def test_detail_refuses_a_symlink_out_of_the_sessions_dir(rs, client, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "transcript.md").write_text("secret")
    os.symlink(outside, Path(rs.OUT_DIR) / "link")
    assert client.get("/api/sessions/link").status_code == 404
    assert "link" not in _rows(client)


def test_detail_of_an_empty_directory_is_404(rs, client) -> None:
    _session(rs, "20261110-090000")
    assert client.get("/api/sessions/20261110-090000").status_code == 404


def test_library_api_is_local_only(rs) -> None:
    remote = TestClient(rs.app, base_url=BASE, client=("203.0.113.9", 50000))
    assert remote.get("/api/sessions").status_code == 403
    assert remote.get("/api/sessions/20261110-090000").status_code == 403


def test_library_api_refuses_a_foreign_origin(rs, client) -> None:
    assert client.get("/api/sessions", headers=EVIL_ORIGIN).status_code == 403
    assert client.get("/api/sessions/20261110-090000", headers=EVIL_ORIGIN).status_code == 403


def test_root_serves_the_library_and_record_serves_the_recorder(rs, client) -> None:
    assert 'id="list-view"' in client.get("/").text
    record = client.get("/record")
    assert record.status_code == 200 and 'id="rec"' in record.text and 'id="list-view"' not in record.text
    assert client.get("/bar").status_code == 200
    mark = client.get("/assets/oncue-mark.svg")
    assert mark.status_code == 200
    assert mark.headers["content-type"].startswith("image/svg+xml")
    assert 'viewBox="0 0 64 64"' in mark.text and 'aria-label="OnCue"' in mark.text
    loader = client.get("/css/oncue-loader.css")
    assert loader.status_code == 200
    assert loader.headers["content-type"].startswith("text/css")
    assert "oncue-loader-baton" in loader.text


@pytest.mark.parametrize("language", ["en", "nl"])
def test_catalog_endpoint_carries_the_library_section(rs, client, language) -> None:
    data = client.get(f"/i18n/{language}.json").json()
    assert data["library"]["heading"] and data["recorder"]["heading"]


def test_library_texts_exist_in_both_languages_and_the_page_uses_only_known_keys() -> None:
    en, nl = (
        yaml.safe_load((REPO_ROOT / f"config/i18n/{lang}.yaml").read_text(encoding="utf-8"))["library"]
        for lang in ("en", "nl")
    )
    assert set(en) == set(nl) and all(en.values()) and all(nl.values())
    html = (REPO_ROOT / "dashboard" / "library.html").read_text(encoding="utf-8")
    used = set(re.findall(r"library\.([a-z_]+)", html)) | set(re.findall(r'tr\("([a-z_]+)"', html))
    used.discard("status_")
    used |= {f"status_{s}" for s in ("recording", "transcribing", "ready", "failed", "incomplete")}
    assert used <= set(en)
