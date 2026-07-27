"""Tests for the first-run front-door decision (wizard vs. dashboard)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from sales_copilot.wizard import front_door


def _reader(values: dict[str, str]) -> Callable[..., str | None]:
    return lambda name, **_kwargs: values.get(name)


def test_wizard_when_nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(front_door, "read_env_value", _reader({}))
    assert front_door.is_configured() is False
    assert front_door.first_run_path() == front_door.WIZARD_PATH


def test_dashboard_when_provider_key_and_license_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        front_door,
        "read_env_value",
        _reader(
            {
                "LLM_PROVIDER": "gemini",
                "GEMINI_API_KEY": "placeholder-gemini-key",
                "SALES_COPILOT_LICENSE": "SCP-key-1234",
            }
        ),
    )
    assert front_door.provider_key_present() is True
    assert front_door.license_present() is True
    assert front_door.is_configured() is True
    assert front_door.first_run_path() == front_door.DASHBOARD_PATH


def test_wizard_when_provider_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        front_door,
        "read_env_value",
        _reader({"LLM_PROVIDER": "openai", "SALES_COPILOT_LICENSE": "SCP-key-1234"}),
    )
    assert front_door.provider_key_present() is False
    assert front_door.first_run_path() == front_door.WIZARD_PATH


def test_wizard_when_license_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        front_door,
        "read_env_value",
        _reader({"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "placeholder-gemini-key"}),
    )
    assert front_door.license_present() is False
    assert front_door.first_run_path() == front_door.WIZARD_PATH


def test_provider_agnostic_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider half resolves against the CONFIGURED provider, not Gemini."""
    monkeypatch.setattr(
        front_door,
        "read_env_value",
        _reader(
            {
                "LLM_PROVIDER": "openai",
                "OPENAI_API_KEY": "sk-openai-123",
                "SALES_COPILOT_LICENSE": "SCP-key-1234",
            }
        ),
    )
    assert front_door.provider_key_present() is True
    assert front_door.is_configured() is True


def test_keyless_provider_satisfies_provider_half(monkeypatch: pytest.MonkeyPatch) -> None:
    """ollama/vertex need no API key, so only the license gates them."""
    monkeypatch.setattr(
        front_door,
        "read_env_value",
        _reader({"LLM_PROVIDER": "ollama", "SALES_COPILOT_LICENSE": "SCP-key-1234"}),
    )
    assert front_door.provider_key_present() is True
    assert front_door.is_configured() is True


def test_unset_provider_is_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        front_door,
        "read_env_value",
        _reader({"GEMINI_API_KEY": "placeholder-gemini-key", "SALES_COPILOT_LICENSE": "SCP-key-1234"}),
    )
    assert front_door.provider_key_present() is False
    assert front_door.first_run_path() == front_door.WIZARD_PATH
