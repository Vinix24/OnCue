"""Unit tests for the safe .env upsert writer (core.env_writer)."""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import dotenv_values

from sales_copilot.core.env_writer import read_env_value, set_env_var

_EXAMPLE = (
    "# OnCue config template\n"
    "LANGUAGE=en\n"
    "\n"
    "# --- LLM Provider ---\n"
    "LLM_PROVIDER=gemini\n"
    "GEMINI_API_KEY=\n"
    "OPENAI_API_KEY=\n"
    "\n"
    "# --- License Gate ---\n"
    "# SALES_COPILOT_LICENSE_CHECK_URL=https://license.example\n"
    "SALES_COPILOT_LICENSE=\n"
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_update_existing_key_in_place(tmp_path: Path) -> None:
    env_path = _write(tmp_path / ".env", _EXAMPLE)
    set_env_var("GEMINI_API_KEY", "placeholder-gemini-key", env_path=env_path)

    values = dotenv_values(env_path)
    assert values["GEMINI_API_KEY"] == "placeholder-gemini-key"
    # Every other key is untouched.
    assert values["LLM_PROVIDER"] == "gemini"
    assert values["LANGUAGE"] == "en"
    assert values["OPENAI_API_KEY"] == ""
    assert values["SALES_COPILOT_LICENSE"] == ""


def test_append_new_key_when_absent(tmp_path: Path) -> None:
    env_path = _write(tmp_path / ".env", "LLM_PROVIDER=gemini\n")
    set_env_var("SALES_COPILOT_LICENSE", "SCP-newkey-1234", env_path=env_path)

    values = dotenv_values(env_path)
    assert values["LLM_PROVIDER"] == "gemini"
    assert values["SALES_COPILOT_LICENSE"] == "SCP-newkey-1234"
    # Appended, not inserted mid-file.
    assert env_path.read_text(encoding="utf-8").splitlines()[-1] == "SALES_COPILOT_LICENSE=SCP-newkey-1234"


def test_preserves_comments_blanks_and_commented_lookalike(tmp_path: Path) -> None:
    env_path = _write(tmp_path / ".env", _EXAMPLE)
    set_env_var("SALES_COPILOT_LICENSE", "SCP-mykey-5678", env_path=env_path)

    text = env_path.read_text(encoding="utf-8")
    # Comments and blank lines survive verbatim.
    assert "# OnCue config template" in text
    assert "# --- License Gate ---" in text
    # The commented CHECK_URL lookalike is NOT mistaken for the license key.
    assert "# SALES_COPILOT_LICENSE_CHECK_URL=https://license.example" in text
    assert "SALES_COPILOT_LICENSE_CHECK_URL" not in dotenv_values(env_path)
    assert dotenv_values(env_path)["SALES_COPILOT_LICENSE"] == "SCP-mykey-5678"


def test_does_not_touch_sibling_prefix_keys(tmp_path: Path) -> None:
    env_path = _write(
        tmp_path / ".env",
        "SALES_COPILOT_LICENSE=\nSALES_COPILOT_LICENSE_SECRET=legacy-secret\n",
    )
    set_env_var("SALES_COPILOT_LICENSE", "SCP-real-key-999", env_path=env_path)

    values = dotenv_values(env_path)
    assert values["SALES_COPILOT_LICENSE"] == "SCP-real-key-999"
    # The _SECRET sibling must remain exactly as it was.
    assert values["SALES_COPILOT_LICENSE_SECRET"] == "legacy-secret"


def test_creates_env_from_example_when_absent(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    example_path = _write(tmp_path / ".env.example", _EXAMPLE)
    assert not env_path.exists()

    set_env_var("GEMINI_API_KEY", "seeded-key-1", env_path=env_path, example_path=example_path)

    assert env_path.exists()
    values = dotenv_values(env_path)
    assert values["GEMINI_API_KEY"] == "seeded-key-1"
    # The rest of the template came along.
    assert values["LLM_PROVIDER"] == "gemini"
    # Secrets file is locked down on creation.
    assert (env_path.stat().st_mode & 0o777) == 0o600


def test_creates_empty_env_when_no_example(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    missing_example = tmp_path / "nope.example"
    set_env_var("OPENAI_API_KEY", "sk-abc12345", env_path=env_path, example_path=missing_example)

    assert dotenv_values(env_path)["OPENAI_API_KEY"] == "sk-abc12345"


def test_simple_value_written_unquoted(tmp_path: Path) -> None:
    env_path = _write(tmp_path / ".env", "GEMINI_API_KEY=\n")
    set_env_var("GEMINI_API_KEY", "AIzaSy_abc-1.2:3", env_path=env_path)
    # Typical keys are alnum + -._:/ and stay unquoted.
    assert "GEMINI_API_KEY=AIzaSy_abc-1.2:3" in env_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "value",
    ["my secret key", "has#hash", 'has"quote', "back\\slash", "with=equals and space"],
)
def test_special_values_are_quoted_and_roundtrip(tmp_path: Path, value: str) -> None:
    env_path = _write(tmp_path / ".env", "OPENAI_API_KEY=\n")
    set_env_var("OPENAI_API_KEY", value, env_path=env_path)
    assert dotenv_values(env_path)["OPENAI_API_KEY"] == value


def test_empty_value_clears_key(tmp_path: Path) -> None:
    env_path = _write(tmp_path / ".env", "GEMINI_API_KEY=old-value\n")
    set_env_var("GEMINI_API_KEY", "", env_path=env_path)
    assert dotenv_values(env_path)["GEMINI_API_KEY"] == ""


def test_invalid_key_rejected(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    with pytest.raises(ValueError):
        set_env_var("bad key", "x", env_path=env_path)
    with pytest.raises(ValueError):
        set_env_var("1STARTS_WITH_DIGIT", "x", env_path=env_path)


def test_read_env_value_prefers_process_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = _write(tmp_path / ".env", "FOO_TOKEN=from-file\n")
    monkeypatch.setenv("FOO_TOKEN", "from-process")
    assert read_env_value("FOO_TOKEN", env_path=env_path) == "from-process"


def test_read_env_value_falls_back_to_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = _write(tmp_path / ".env", "FOO_TOKEN=from-file\n")
    monkeypatch.delenv("FOO_TOKEN", raising=False)
    assert read_env_value("FOO_TOKEN", env_path=env_path) == "from-file"


def test_read_env_value_missing_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / ".env"  # does not exist
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    assert read_env_value("NOPE_TOKEN", env_path=env_path) is None
