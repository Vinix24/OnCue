"""The public LLM tier is OpenRouter only in the user-facing docs and the setup wizard."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

# Docs that scripts/export_public.sh keeps out of the public export (its SUB_EXCLUDES).
# They are absent by design in an export tree and must exist in this (private) repo.
EXPORT_EXCLUDED_DOCS = {"docs/PRD.md", "docs/TTD.md", "docs/OPERATOR_TEST_PLAN.md"}

# The export leaves scripts/export_public.sh itself out, so its presence marks the private
# checkout: there every listed doc must exist, also the export-excluded ones.
PRIVATE_REPO = (ROOT / "scripts" / "export_public.sh").is_file()

USER_FACING_DOCS = [
    "README.md",
    "INSTALL.md",
    "docs/FEATURES.md",
    "docs/FAQ.md",
    "docs/CONFIG.md",
    "docs/PRIVACY.md",
    "docs/ARCHITECTURE_BOUNDARIES.md",
    "docs/INTEGRATIONS.md",
    "docs/BETA_TESTER_GUIDE.md",
    "docs/PRD.md",
    "docs/TTD.md",
    "docs/ARCHITECTURE.md",
    "docs/OPERATOR_TEST_PLAN.md",
    ".env.example",
]

# Provider names that must not be offered as a public LLM choice.
PUBLIC_OFFER = re.compile(r"\b(openai|groq|gemini)\b", re.IGNORECASE)
# Mentions that are allowed: the own-tenant name, the `openai` pip extra (the OpenRouter
# client library), model names served via OpenRouter/Vertex, and the OpenAI-compatible API shape.
ALLOWED = re.compile(
    r"Azure OpenAI|AZURE_OPENAI\w*|openai\.azure\.com|OpenAI models|OpenAI-compatible"
    r"|\.\[[\w,]*openai[\w,]*\]|google/gemini[\w.\-]*|gemini-[\w.\-]*|Gemini models?"
    r"|a Gemini model|gemini/vertex|api\.groq\.com/openai|Direct imports of [^|]*",
    re.IGNORECASE,
)
# The audio route is transcription, not the LLM tier: a Groq mention on such a line is fine.
AUDIO_LINE = re.compile(r"WHISPER_BACKEND|whisper|audio|transcri", re.IGNORECASE)


# The older openai/groq/gemini provider values still work for existing .env files; a line may
# name them only when it says they are a developer option or still accepted for existing configs.
LEGACY_NOTE = re.compile(r"developer option|still (accepted|work)", re.IGNORECASE)


def _offending_lines(rel: str, root: Path = ROOT) -> list[str]:
    hits: list[str] = []
    for number, line in enumerate((root / rel).read_text().splitlines(), start=1):
        if LEGACY_NOTE.search(line):
            continue
        remainder = ALLOWED.sub("", line)
        if AUDIO_LINE.search(line):
            remainder = re.sub(r"\bgroq\b", "", remainder, flags=re.IGNORECASE)
        if PUBLIC_OFFER.search(remainder):
            hits.append(f"{rel}:{number}: {line.strip()[:140]}")
    return hits


def _missing_docs(root: Path = ROOT) -> list[str]:
    """Listed docs that must exist here: in an export the export-excluded ones may be absent."""

    return [
        rel
        for rel in USER_FACING_DOCS
        if not (root / rel).is_file() and (PRIVATE_REPO or rel not in EXPORT_EXCLUDED_DOCS)
    ]


def test_user_facing_docs_offer_no_public_openai_groq_or_gemini() -> None:
    missing = _missing_docs()
    assert missing == [], f"user-facing docs missing: {missing}"
    scanned = [rel for rel in USER_FACING_DOCS if (ROOT / rel).is_file()]
    hits = [hit for rel in scanned for hit in _offending_lines(rel)]
    assert hits == []


def test_docs_that_must_exist_are_never_silently_skipped(tmp_path: Path) -> None:
    # tmp_path holds no docs at all: exactly the docs that must exist here are reported,
    # so a doc removed by accident is caught while the export-excluded ones may be absent.
    expected = [rel for rel in USER_FACING_DOCS if PRIVATE_REPO or rel not in EXPORT_EXCLUDED_DOCS]
    assert _missing_docs(tmp_path) == expected


def test_export_excluded_docs_match_the_export_script() -> None:
    script = ROOT / "scripts" / "export_public.sh"
    if not script.is_file():
        pytest.skip("public tree: the export script is not shipped")
    block = script.read_text().split("SUB_EXCLUDES=(", 1)[1].split("\n)", 1)[0]
    excluded_docs = set(re.findall(r'"(docs/[^"]+)"', block))
    assert EXPORT_EXCLUDED_DOCS == excluded_docs & set(USER_FACING_DOCS)


def test_doc_scan_catches_a_public_offer(tmp_path: Path) -> None:
    sample = tmp_path / "sample.md"
    sample.write_text("Use LLM_PROVIDER=gemini or a public API (OpenAI, Groq).\n")
    assert len(_offending_lines("sample.md", tmp_path)) == 1


def test_doc_scan_allows_legacy_values_only_with_a_note(tmp_path: Path) -> None:
    sample = tmp_path / "sample.md"
    sample.write_text(
        "Older `openai` values are still accepted (developer option).\n"
        "LLM_PROVIDER=none|gemini|groq|openai\n"
    )
    assert len(_offending_lines("sample.md", tmp_path)) == 1


def test_doc_scan_allows_audio_route_and_own_tenant(tmp_path: Path) -> None:
    sample = tmp_path / "sample.md"
    sample.write_text(
        "`WHISPER_BACKEND=groq` uploads audio to Groq.\n"
        "Your own Azure OpenAI subscription.\n"
    )
    assert _offending_lines("sample.md", tmp_path) == []


def test_dashboard_setup_choices_are_the_three_tiers() -> None:
    data = yaml.safe_load((ROOT / "config/llm_models.yaml").read_text())
    providers = set(data["providers"])
    tiers = {
        "local": {"ollama"},
        "own tenant": {"azure", "vertex"},
        "public": {"openrouter"},
    }
    assert providers == set().union(*tiers.values())


def _run_tier_prompt(tmp_path: Path, answers: str) -> str:
    (tmp_path / ".env").write_text("LLM_PROVIDER=none\nOPENROUTER_API_KEY=\n")
    script = ROOT / "scripts" / "first-run.sh"
    cmd = (
        f'source "{script}"; ROOT_DIR="{tmp_path}"; '
        "choose_llm_tier >/dev/null"
    )
    subprocess.run(
        ["bash", "-c", cmd], input=answers, text=True, check=True, cwd=tmp_path
    )
    return (tmp_path / ".env").read_text()


def test_first_run_public_choice_writes_openrouter_and_its_key(tmp_path: Path) -> None:
    env = _run_tier_prompt(tmp_path, "3\nsk-or-test\n")
    assert "LLM_PROVIDER=openrouter\n" in env
    assert "OPENROUTER_API_KEY=sk-or-test\n" in env
    assert "GEMINI_API_KEY" not in env
    assert "GROQ_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_first_run_local_and_tenant_choices(tmp_path: Path) -> None:
    assert "LLM_PROVIDER=ollama\n" in _run_tier_prompt(tmp_path, "1\n\n")
    assert "LLM_PROVIDER=vertex\n" in _run_tier_prompt(tmp_path, "2\nvertex\n\n")
    assert "LLM_PROVIDER=azure\n" in _run_tier_prompt(tmp_path, "2\nazure\ndep\n")


def test_first_run_rejects_unknown_choices_and_leaves_env_unchanged(tmp_path: Path) -> None:
    for answers in ("9\n", "2\ngemini\n", "\n"):
        env = _run_tier_prompt(tmp_path, answers)
        assert "LLM_PROVIDER=none\n" in env


def test_first_run_prompt_lists_exactly_three_tiers() -> None:
    text = (ROOT / "scripts" / "first-run.sh").read_text()
    menu = re.findall(r'^\s*echo "\s+(\d)\) ', text, re.MULTILINE)
    assert menu == ["1", "2", "3"]
    assert "GEMINI_API_KEY" not in text


def _set_env_value(tmp_path: Path, key: str, value: str) -> subprocess.CompletedProcess:
    script = ROOT / "scripts" / "first-run.sh"
    cmd = (
        f'source "{script}"; ROOT_DIR="{tmp_path}"; set_env_value "{key}" "{value}"'
    )
    return subprocess.run(
        ["bash", "-c", cmd], text=True, capture_output=True, cwd=tmp_path
    )


def test_set_env_value_keeps_other_lines_and_mode(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\nLLM_PROVIDER=none\nSECRET=x\n")
    env.chmod(0o600)
    result = _set_env_value(tmp_path, "LLM_PROVIDER", "ollama")
    assert result.returncode == 0
    assert env.read_text() == "A=1\nSECRET=x\nLLM_PROVIDER=ollama\n"
    assert (env.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_set_env_value_without_match_appends(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    assert _set_env_value(tmp_path, "NEW", "v").returncode == 0
    assert env.read_text() == "A=1\nNEW=v\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_set_env_value_unreadable_env_is_left_untouched(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    original = "A=1\nSECRET=x\n"
    env.write_text(original)
    env.chmod(0o000)
    try:
        result = _set_env_value(tmp_path, "LLM_PROVIDER", "ollama")
    finally:
        env.chmod(0o600)
    assert result.returncode != 0
    assert "left unchanged" in result.stdout + result.stderr
    assert env.read_bytes() == original.encode()
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def _model_of(env: str) -> str:
    return re.search(r"^LLM_MODEL=(.+)$", env, re.MULTILINE).group(1)


def test_each_tier_writes_a_model_it_can_serve(tmp_path: Path) -> None:
    assert _model_of(_run_tier_prompt(tmp_path, "1\n\n")) == "qwen2.5:7b"
    assert _model_of(_run_tier_prompt(tmp_path, "1\nllama3.2:3b\n")) == "llama3.2:3b"
    assert _model_of(_run_tier_prompt(tmp_path, "2\nvertex\n\n")) == "gemini-2.5-flash"
    assert _model_of(_run_tier_prompt(tmp_path, "2\nazure\nmy-deploy\n")) == "my-deploy"
    assert (
        _model_of(_run_tier_prompt(tmp_path, "3\nsk-or-test\n"))
        == "anthropic/claude-haiku-4.5"
    )


def test_azure_without_deployment_name_changes_nothing(tmp_path: Path) -> None:
    env = _run_tier_prompt(tmp_path, "2\nazure\n\n")
    assert "LLM_PROVIDER=none\n" in env
    assert "LLM_MODEL" not in env


def test_faq_search_index_links_the_renamed_faq_entry() -> None:
    index = (ROOT / "docs" / "FAQ_SEARCH_INDEX.md").read_text()
    faq = (ROOT / "docs" / "FAQ.md").read_text()
    anchors = set(re.findall(r'<a id="([^"]+)"></a>', faq))
    for anchor in re.findall(r"\]\(FAQ\.md#([^)]+)\)", index):
        assert anchor in anchors, anchor
    assert "Gemini API call fails" not in index
