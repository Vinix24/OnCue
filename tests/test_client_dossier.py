"""Tests for the client-dossier helpers added to core/context_docs.py (PR-D4)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from sales_copilot.core import context_docs

# --- slugify_client_name -----------------------------------------------------


def test_slugify_client_name_lowercases_and_collapses_separators() -> None:
    assert context_docs.slugify_client_name("Acme Corp!!") == "acme-corp"


def test_slugify_client_name_empty_defaults_to_default() -> None:
    assert context_docs.slugify_client_name("") == "default"
    assert context_docs.slugify_client_name("   ") == "default"


def test_slugify_client_name_strips_traversal_characters() -> None:
    assert context_docs.slugify_client_name("../../etc/passwd") == "etc-passwd"


# --- resolve_client_dir -------------------------------------------------------


def test_resolve_client_dir_none_when_missing_and_not_create(tmp_path: Path) -> None:
    assert context_docs.resolve_client_dir("acme", upload_root=tmp_path, create=False) is None


def test_resolve_client_dir_creates_when_requested(tmp_path: Path) -> None:
    resolved = context_docs.resolve_client_dir("Acme Corp", upload_root=tmp_path, create=True)

    assert resolved is not None
    assert resolved.is_dir()
    assert resolved == (tmp_path / "acme-corp").resolve()


def test_resolve_client_dir_stays_under_root_for_hostile_slug(tmp_path: Path) -> None:
    resolved = context_docs.resolve_client_dir("../../../etc", upload_root=tmp_path, create=True)

    assert resolved is not None
    resolved.relative_to(tmp_path.resolve())


# --- list_client_slugs --------------------------------------------------------


def test_list_client_slugs_empty_when_root_missing(tmp_path: Path) -> None:
    assert context_docs.list_client_slugs(upload_root=tmp_path / "does-not-exist") == []


def test_list_client_slugs_returns_sorted_directory_names(tmp_path: Path) -> None:
    (tmp_path / "zeta").mkdir()
    (tmp_path / "acme").mkdir()
    (tmp_path / "not-a-dir.txt").write_text("x", encoding="utf-8")

    assert context_docs.list_client_slugs(upload_root=tmp_path) == ["acme", "zeta"]


# --- load_client_dossier -------------------------------------------------------


def _write_with_mtime(path: Path, content: str, *, age_seconds: float) -> None:
    path.write_text(content, encoding="utf-8")
    now = time.time()
    os.utime(path, (now - age_seconds, now - age_seconds))


def test_load_client_dossier_empty_when_folder_missing(tmp_path: Path) -> None:
    assert context_docs.load_client_dossier("acme", upload_root=tmp_path) == ""


def test_load_client_dossier_orders_most_recent_first_and_drops_oldest_over_cap(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PII_REDACTION", "off")
    client_dir = tmp_path / "acme"
    client_dir.mkdir()
    _write_with_mtime(client_dir / "oldest.md", "A" * 50, age_seconds=300)
    _write_with_mtime(client_dir / "middle.md", "B" * 50, age_seconds=200)
    _write_with_mtime(client_dir / "newest.md", "C" * 50, age_seconds=100)

    # Cap large enough for two chunks (each ~65 chars incl. header) but not three.
    loaded = context_docs.load_client_dossier("acme", upload_root=tmp_path, max_chars=140)

    assert "newest.md" in loaded
    assert "middle.md" in loaded
    assert "oldest.md" not in loaded
    # Chronological within what's kept: middle before newest.
    assert loaded.index("middle.md") < loaded.index("newest.md")


def test_load_client_dossier_excludes_paths_already_used_as_context_docs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PII_REDACTION", "off")
    client_dir = tmp_path / "acme"
    client_dir.mkdir()
    prep_doc = client_dir / "prep.md"
    prep_doc.write_text("prep doc content", encoding="utf-8")
    (client_dir / "history.md").write_text("history content", encoding="utf-8")

    loaded = context_docs.load_client_dossier(
        "acme", upload_root=tmp_path, exclude={str(prep_doc)}
    )

    assert "prep.md" not in loaded
    assert "history.md" in loaded


def test_load_client_dossier_ignores_disallowed_extensions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PII_REDACTION", "off")
    client_dir = tmp_path / "acme"
    client_dir.mkdir()
    (client_dir / "notes.md").write_text("kept", encoding="utf-8")
    (client_dir / "audio.wav").write_bytes(b"\x00\x01")

    loaded = context_docs.load_client_dossier("acme", upload_root=tmp_path)

    assert "notes.md" in loaded
    assert "audio.wav" not in loaded


def test_load_client_dossier_redacts_pii_for_cloud_destination(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("PII_REDACTION", raising=False)
    monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
    client_dir = tmp_path / "acme"
    client_dir.mkdir()
    (client_dir / "call.md").write_text("Contact: jan@example.com", encoding="utf-8")

    loaded = context_docs.load_client_dossier("acme", upload_root=tmp_path)

    assert "jan@example.com" not in loaded


def test_load_client_dossier_provider_param_overrides_global_llm_provider(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
    client_dir = tmp_path / "acme"
    client_dir.mkdir()
    (client_dir / "call.md").write_text("Contact: jan@example.com", encoding="utf-8")

    unspecified = context_docs.load_client_dossier("acme", upload_root=tmp_path)
    assert "jan@example.com" in unspecified

    stripped = context_docs.load_client_dossier("acme", upload_root=tmp_path, provider="openrouter")
    assert "jan@example.com" not in stripped


# --- write_dossier_transcript --------------------------------------------------


def test_write_dossier_transcript_creates_dir_and_file(tmp_path: Path) -> None:
    path = context_docs.write_dossier_transcript(
        "Acme Corp", "2026-08-05T10-00-00_session-1_transcript", "self: hallo", upload_root=tmp_path
    )

    assert path.is_file()
    assert path.parent == (tmp_path / "acme-corp" / "dossier").resolve()
    assert path.read_text(encoding="utf-8") == "self: hallo"
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_dossier_transcript_is_readable_by_load_client_dossier(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PII_REDACTION", "off")
    context_docs.write_dossier_transcript(
        "acme", "2026-08-05T10-00-00_session-1_transcript", "prospect: budget is krap", upload_root=tmp_path
    )

    loaded = context_docs.load_client_dossier("acme", upload_root=tmp_path)

    assert "budget is krap" in loaded


def test_load_client_dossier_reads_root_and_dossier_but_never_gesprekken(
    tmp_path: Path, monkeypatch
) -> None:
    """klantmap-als-eenheid D3: a pre-D3 folder keeps its loose root files as
    dossier material; a new save lands in dossier/; the gesprekken/ call
    archive (also D3) is never read as dossier, even though it sits right
    next to dossier/ in the same client folder."""
    monkeypatch.setenv("PII_REDACTION", "off")
    client_dir = tmp_path / "acme"
    (client_dir / "dossier").mkdir(parents=True)
    (client_dir / "gesprekken" / "2026-09-28-session-9").mkdir(parents=True)

    (client_dir / "legacy-root-note.md").write_text("legacy root content", encoding="utf-8")
    (client_dir / "dossier" / "new-save.md").write_text("new dossier content", encoding="utf-8")
    (client_dir / "gesprekken" / "2026-09-28-session-9" / "transcript.md").write_text(
        "archived call content", encoding="utf-8"
    )

    loaded = context_docs.load_client_dossier("acme", upload_root=tmp_path)

    assert "legacy root content" in loaded
    assert "new dossier content" in loaded
    assert "archived call content" not in loaded
