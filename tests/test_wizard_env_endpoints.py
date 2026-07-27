"""Tests for the wizard .env-writer endpoints (config + set-provider-key + set-license)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from sales_copilot.websocket import hub

_EXAMPLE = "LLM_PROVIDER=gemini\nGEMINI_API_KEY=\nOPENAI_API_KEY=\nSALES_COPILOT_LICENSE=\n"
_TEST_TOKEN = "test-shutdown-token-32-chars-ok!"


@pytest.fixture()
def env_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the .env writer/reader at a throwaway file for the test."""
    env_path = tmp_path / ".env"
    example_path = tmp_path / ".env.example"
    example_path.write_text(_EXAMPLE, encoding="utf-8")
    monkeypatch.setattr("sales_copilot.core.env_writer._default_env_path", lambda: env_path)
    monkeypatch.setattr("sales_copilot.core.env_writer._default_example_path", lambda: example_path)
    return env_path


# --- GET /api/v1/wizard/config ---------------------------------------------


def test_config_reports_needed_keys(monkeypatch: pytest.MonkeyPatch, env_files: Path) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "")

    response = TestClient(hub.app).get("/api/v1/wizard/config")
    assert response.status_code == 200
    data = response.json()
    assert data["provider"] == "gemini"
    assert data["provider_key_env"] == "GEMINI_API_KEY"
    assert data["provider_needs_key"] is True
    assert data["provider_key_present"] is False
    assert data["license_present"] is False


def test_config_provider_agnostic_for_openai(monkeypatch: pytest.MonkeyPatch, env_files: Path) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-openai-key")

    data = TestClient(hub.app).get("/api/v1/wizard/config").json()
    assert data["provider_key_env"] == "OPENAI_API_KEY"
    assert data["provider_key_present"] is True


def test_config_keyless_provider_needs_no_key(monkeypatch: pytest.MonkeyPatch, env_files: Path) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    data = TestClient(hub.app).get("/api/v1/wizard/config").json()
    assert data["provider_needs_key"] is False
    assert data["provider_key_env"] is None
    assert data["provider_key_present"] is True


# --- POST /api/v1/wizard/set-provider-key ----------------------------------


def test_set_provider_key_writes_configured_provider(
    monkeypatch: pytest.MonkeyPatch, env_files: Path, authed_client: TestClient
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "")

    response = authed_client.post(
        "/api/v1/wizard/set-provider-key", json={"value": "sk-openai-abcd1234"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["key_env"] == "OPENAI_API_KEY"

    # Written to .env for the CONFIGURED provider, not hardcoded Gemini.
    values = dotenv_values(env_files)
    assert values["OPENAI_API_KEY"] == "sk-openai-abcd1234"
    assert values["GEMINI_API_KEY"] == ""
    # Effective in-process without a restart.
    assert os.environ["OPENAI_API_KEY"] == "sk-openai-abcd1234"


def test_set_provider_key_secret_not_logged(
    monkeypatch: pytest.MonkeyPatch,
    env_files: Path,
    authed_client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    secret = "AIzaSy-super-secret-value-999"
    with caplog.at_level("DEBUG"):
        authed_client.post("/api/v1/wizard/set-provider-key", json={"value": secret})
    assert secret not in caplog.text


def test_set_provider_key_rejects_short(
    monkeypatch: pytest.MonkeyPatch, env_files: Path, authed_client: TestClient
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    response = authed_client.post("/api/v1/wizard/set-provider-key", json={"value": "abc"})
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_set_provider_key_keyless_provider_is_noop_ok(
    monkeypatch: pytest.MonkeyPatch, env_files: Path, authed_client: TestClient
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    response = authed_client.post("/api/v1/wizard/set-provider-key", json={"value": ""})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert data["key_env"] is None


def test_set_provider_key_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TEST_TOKEN)
    response = TestClient(hub.app).post(
        "/api/v1/wizard/set-provider-key", json={"value": "whatever-12345"}
    )
    assert response.status_code == 401


# --- POST /api/v1/wizard/set-license ---------------------------------------


def test_set_license_writes_env(
    monkeypatch: pytest.MonkeyPatch, env_files: Path, authed_client: TestClient
) -> None:
    monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
    response = authed_client.post(
        "/api/v1/wizard/set-license", json={"value": "SCP-license-abcd1234"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True

    assert dotenv_values(env_files)["SALES_COPILOT_LICENSE"] == "SCP-license-abcd1234"
    assert os.environ["SALES_COPILOT_LICENSE"] == "SCP-license-abcd1234"


def test_set_license_rejects_empty(env_files: Path, authed_client: TestClient) -> None:
    response = authed_client.post("/api/v1/wizard/set-license", json={"value": ""})
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_set_license_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TEST_TOKEN)
    response = TestClient(hub.app).post(
        "/api/v1/wizard/set-license", json={"value": "SCP-license-abcd1234"}
    )
    assert response.status_code == 401


# --- Browser page wiring ----------------------------------------------------


def test_wizard_page_has_key_entry_fields() -> None:
    """The wizard page exposes paste-and-submit fields for both keys."""
    html = (Path(__file__).parent.parent / "dashboard" / "wizard" / "index.html").read_text(
        encoding="utf-8"
    )
    for element_id in (
        "wizard-config",
        "provider-key-input",
        "provider-key-save",
        "license-input",
        "license-save",
    ):
        assert f'id="{element_id}"' in html, f"missing #{element_id} in wizard page"
    # Secrets use password inputs and hit the new endpoints.
    assert 'type="password"' in html
    assert "/api/v1/wizard/set-provider-key" in html
    assert "/api/v1/wizard/set-license" in html
    # Still self-contained vanilla — no external script.
    assert "wizard.js" not in html
