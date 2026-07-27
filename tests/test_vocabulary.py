"""Tests for sales_copilot.modules.transcriber.vocabulary."""

from __future__ import annotations

from pathlib import Path

from sales_copilot.modules.transcriber.vocabulary import (
    VocabularyConfig,
    load_vocabulary_config,
)


def test_load_vocabulary_config_returns_expected_shape(tmp_path: Path) -> None:
    yaml_path = tmp_path / "vocab.yaml"
    yaml_path.write_text(
        "enabled: true\n"
        "language: nl\n"
        "description: test vocab\n"
        "prompt_prefix: Termen\n"
        "terms:\n"
        "  - offerte\n"
        "  - propositie\n",
        encoding="utf-8",
    )

    vocab = load_vocabulary_config(yaml_path)

    assert vocab.enabled is True
    assert vocab.language == "nl"
    assert vocab.description == "test vocab"
    assert vocab.prompt_prefix == "Termen"
    assert vocab.terms == ("offerte", "propositie")
    assert vocab.initial_prompt == "Termen offerte, propositie."


def test_disabled_vocabulary_has_empty_initial_prompt(tmp_path: Path) -> None:
    yaml_path = tmp_path / "vocab.yaml"
    yaml_path.write_text(
        "enabled: false\n"
        "terms:\n"
        "  - offerte\n",
        encoding="utf-8",
    )

    vocab = load_vocabulary_config(yaml_path)

    assert vocab.enabled is False
    assert vocab.initial_prompt == ""


def test_missing_vocabulary_config_is_disabled() -> None:
    vocab = load_vocabulary_config(Path("/nonexistent/path/vocab.yaml"))

    assert vocab.enabled is False
    assert vocab.terms == ()
    assert vocab.initial_prompt == ""


def test_empty_terms_yields_empty_prompt(tmp_path: Path) -> None:
    yaml_path = tmp_path / "vocab.yaml"
    yaml_path.write_text("enabled: true\nterms: []\n", encoding="utf-8")

    vocab = load_vocabulary_config(yaml_path)

    assert vocab.initial_prompt == ""


def test_initial_prompt_without_prefix() -> None:
    vocab = VocabularyConfig(
        enabled=True,
        language="nl",
        description="",
        prompt_prefix="",
        terms=("offerte", "propositie"),
    )

    assert vocab.initial_prompt == "offerte, propositie."


def test_vocabulary_config_defaults_are_sensible() -> None:
    """The shipped default config must parse and contain known NL-B2B terms."""
    from sales_copilot.modules.transcriber.vocabulary import DEFAULT_VOCABULARY_PATH

    vocab = load_vocabulary_config(DEFAULT_VOCABULARY_PATH)

    assert vocab.enabled is True
    assert vocab.language == "nl"
    assert "offerte" in vocab.terms
    assert "CRM" in vocab.terms
    assert vocab.initial_prompt
    # The prompt should be a manageable length; 200 terms would be suspicious.
    assert len(vocab.terms) < 100
