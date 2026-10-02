from pathlib import Path

from sales_copilot.core import context_docs
from sales_copilot.modules.detector.llm_confirm import LLMConfirmClient
from sales_copilot.modules.reports import generator


def test_llm_confirm_appends_context_docs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    doc = tmp_path / "context.txt"
    doc.write_text("A" * 5000, encoding="utf-8")

    client = LLMConfirmClient(context_docs=[str(doc)])

    assert "context.txt" in client.system_prompt
    assert len(client._context_block) <= 4000


def test_load_context_documents_provider_param_overrides_global_llm_provider(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: a caller must be able to name its OWN destination provider
    (e.g. INSIGHT_PROVIDER) instead of relying on the global LLM_PROVIDER env
    var, which may point somewhere else entirely (e.g. the local detector)."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    doc = tmp_path / "context.txt"
    doc.write_text("Contact: jan@example.com", encoding="utf-8")

    # No provider given: falls back to global LLM_PROVIDER=ollama (local, no strip).
    unspecified = context_docs.load_context_documents([str(doc)], upload_root=tmp_path)
    assert "jan@example.com" in unspecified

    # Explicit provider="openrouter" (public cloud): must strip regardless of
    # what LLM_PROVIDER is set to.
    stripped = context_docs.load_context_documents(
        [str(doc)], upload_root=tmp_path, provider="openrouter"
    )
    assert "jan@example.com" not in stripped


def test_report_includes_context_doc_names() -> None:
    session = generator.SessionData(
        session_id="session-1",
        call_start_ms=0,
        call_end_ms=1000,
        prospect_name="Sam",
        prospect_company="Acme",
        context_docs=["/tmp/intro.md", "/tmp/notes.txt"],
        speech_events=[],
        phase_events=[],
        pain_points=[],
        conversation_summary=None,
        key_moments=[],
        monologues=[],
        transcript=[],
    )

    report = generator.write_report(session).report

    assert report.prospect_name == "Sam"
    assert report.context_docs == ["intro.md", "notes.txt"]
