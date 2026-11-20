from __future__ import annotations

from fastapi.testclient import TestClient

from sales_copilot.websocket import hub


def test_llm_models_endpoint_returns_provider_model_mapping() -> None:
    client = TestClient(hub.app)

    response = client.get("/api/llm-models")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, dict)
    providers = payload.get("providers")
    assert isinstance(providers, dict)
    assert set(providers) == {"ollama", "azure", "vertex", "openrouter"}
    assert providers["openrouter"]["label"] == "OpenRouter (public, default)"
    assert "anthropic/claude-haiku-4.5" in providers["openrouter"]["models"]
    assert providers["ollama"]["label"] == "Ollama (local)"
    assert "qwen2.5:7b" in providers["ollama"]["models"]
