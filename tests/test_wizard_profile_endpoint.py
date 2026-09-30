"""Tests for the wizard's seller-profile endpoints (GET/POST /api/v1/profile, PR-D4)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sales_copilot.core import profile_docs
from sales_copilot.websocket import hub


@pytest.fixture()
def profile_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the profile store at a throwaway directory for the test."""
    monkeypatch.setattr(profile_docs, "PROFILE_ROOT", tmp_path)
    return tmp_path


def test_get_profile_empty_when_no_doc(profile_root: Path) -> None:
    response = TestClient(hub.app).get("/api/v1/profile")

    assert response.status_code == 200
    data = response.json()
    assert data["content"] == ""
    assert data["exists"] is False
    assert data["max_chars"] == profile_docs.MAX_PROFILE_CHARS


def test_get_profile_returns_existing_content(profile_root: Path) -> None:
    profile_docs.write_profile_document("Ik ken de NL CRM-markt goed.", root=profile_root)

    data = TestClient(hub.app).get("/api/v1/profile").json()

    assert data["content"] == "Ik ken de NL CRM-markt goed."
    assert data["exists"] is True


def test_set_profile_writes_and_is_readable_back(
    profile_root: Path, authed_client: TestClient
) -> None:
    response = authed_client.post("/api/v1/profile", json={"content": "Mijn methode: SPIN."})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert profile_docs.read_profile_raw(root=profile_root) == "Mijn methode: SPIN."


def test_set_profile_requires_token(profile_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-shutdown-token-32-chars-ok!")
    response = TestClient(hub.app).post("/api/v1/profile", json={"content": "x"})

    assert response.status_code == 401


def test_set_profile_rejects_oversized_content(
    profile_root: Path, authed_client: TestClient
) -> None:
    response = authed_client.post("/api/v1/profile", json={"content": "A" * 200_001})

    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_set_profile_overwrites_existing(profile_root: Path, authed_client: TestClient) -> None:
    authed_client.post("/api/v1/profile", json={"content": "first"})
    authed_client.post("/api/v1/profile", json={"content": "second"})

    assert profile_docs.read_profile_raw(root=profile_root) == "second"


def test_wizard_page_has_profile_editor_fields() -> None:
    html = (Path(__file__).parent.parent / "dashboard" / "wizard" / "index.html").read_text(
        encoding="utf-8"
    )
    for element_id in ("profile-textarea", "profile-save", "profile-message"):
        assert f'id="{element_id}"' in html, f"missing #{element_id} in wizard page"
    assert "/api/v1/profile" in html
