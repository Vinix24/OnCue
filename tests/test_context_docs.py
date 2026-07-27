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

    report = generator.generate_report(session)

    assert report.prospect_name == "Sam"
    assert report.context_docs == ["intro.md", "notes.txt"]
