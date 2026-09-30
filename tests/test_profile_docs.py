"""Tests for the persistent seller profile store (core/profile_docs.py, PR-D4)."""

from __future__ import annotations

from pathlib import Path

from sales_copilot.core import profile_docs


def test_write_and_read_profile_roundtrip(tmp_path: Path) -> None:
    profile_docs.write_profile_document("Ik ken de NL CRM-markt goed.", root=tmp_path)

    assert profile_docs.profile_exists(root=tmp_path)
    assert profile_docs.read_profile_raw(root=tmp_path) == "Ik ken de NL CRM-markt goed."

    written = tmp_path / profile_docs.PROFILE_FILENAME
    assert written.is_file()


def test_profile_exists_false_when_no_file(tmp_path: Path) -> None:
    assert profile_docs.profile_exists(root=tmp_path) is False
    assert profile_docs.read_profile_raw(root=tmp_path) == ""


def test_load_profile_context_empty_when_no_file(tmp_path: Path) -> None:
    assert profile_docs.load_profile_context(root=tmp_path) == ""


def test_load_profile_context_caps_at_max_chars(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PII_REDACTION", "off")
    profile_docs.write_profile_document("A" * 5000, root=tmp_path)

    loaded = profile_docs.load_profile_context(root=tmp_path, max_chars=4000)

    assert len(loaded) == 4000


def test_load_profile_context_strips_whitespace_only_doc(tmp_path: Path) -> None:
    profile_docs.write_profile_document("   \n\n  ", root=tmp_path)

    assert profile_docs.load_profile_context(root=tmp_path) == ""


def test_load_profile_context_redacts_pii_for_cloud_destination(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("PII_REDACTION", raising=False)
    monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
    profile_docs.write_profile_document(
        "Contact: Jan de Vries, jan@example.com", root=tmp_path
    )

    loaded = profile_docs.load_profile_context(root=tmp_path)

    assert "jan@example.com" not in loaded


def test_load_profile_context_provider_param_overrides_global_llm_provider(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
    profile_docs.write_profile_document("Contact: jan@example.com", root=tmp_path)

    unspecified = profile_docs.load_profile_context(root=tmp_path)
    assert "jan@example.com" in unspecified

    stripped = profile_docs.load_profile_context(root=tmp_path, provider="openrouter")
    assert "jan@example.com" not in stripped


def test_write_profile_creates_root_and_sets_owner_only_permissions(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "profile"
    path = profile_docs.write_profile_document("content", root=root)

    assert path.exists()
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_profile_overwrites_existing_content(tmp_path: Path) -> None:
    profile_docs.write_profile_document("first", root=tmp_path)
    profile_docs.write_profile_document("second", root=tmp_path)

    assert profile_docs.read_profile_raw(root=tmp_path) == "second"
