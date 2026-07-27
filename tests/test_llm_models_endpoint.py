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
    assert providers["openrouter"]["label"] == "OpenRouter (default)"
    assert "anthropic/claude-haiku-4.5" in providers["openrouter"]["models"]
    assert providers["gemini"]["label"] == "Gemini"
    assert "gemini-2.5-flash" in providers["gemini"]["models"]
    assert providers["openai"]["label"] == "OpenAI"
    assert "gpt-4o-mini" in providers["openai"]["models"]
    assert providers["groq"]["label"] == "Groq"
    assert "llama-3.3-70b-versatile" in providers["groq"]["models"]
    assert providers["ollama"]["label"] == "Ollama (local)"
    assert "qwen2.5:7b" in providers["ollama"]["models"]
