"""Tests for scripts/eval_suggestion.py -- the fase-B B3 rebuttal-shootout evaluator."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import eval_suggestion  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent

_SALES_PREP_NL = """# Sales-prep — Testklant (Jane Doe, CFO)

**Bron:** test fixture.

## 1. Bedrijfscontext
Testklant-bedrijfscontext-marker, mag NIET in de prompt lekken.

## 2. Prospect & DMU
Jane Doe.

## 3. Expliciete pijnpunten
Pijnpunt-marker, mag NIET in de prompt lekken.

## 4. Waarschijnlijke bezwaren + beste antwoord-hoek
| Bezwaar | Beste hoek |
|---|---|
| "Te duur" | ROI-argument-fixture-marker |

## 5. Relevante differentiators
- Differentiator-fixture-marker.

## 6. Trigger (waar het hem raakt)
Trigger-fixture-marker.

## 7. Concurrenten / alternatieven in beeld
Concurrent-marker, mag NIET in de prompt lekken.

## 8. Gespreksdoel / next-step
Next-step-marker, mag NIET in de prompt lekken.
"""

_SALES_PREP_EN = """# Sales-prep — English Testclient (John Doe, CTO)

**Taal:** Engels. **Eval-scope:** B3/B4.

## 1. Bedrijfscontext
English context marker.

## 4. Waarschijnlijke bezwaren + beste antwoord-hoek
English-objections-marker.

## 5. Relevante differentiators
English-differentiators-marker.

## 6. Trigger (waar het hem raakt)
English-trigger-marker.
"""


def _row(row_id: str, text: str, source: str, category: str | None, confidence: float) -> dict:
    return {
        "id": row_id,
        "text": text,
        "source": source,
        "predicted_category": category,
        "predicted_confidence": confidence,
        "label": "review",
    }


def _write_mined_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _build_fixture_dirs(tmp_path: Path) -> tuple[Path, Path]:
    fireflies_dir = tmp_path / "fireflies"
    objection_dir = tmp_path / "objection_eval"

    client_dir = fireflies_dir / "testklant"
    client_dir.mkdir(parents=True)
    (client_dir / "sales-prep.md").write_text(_SALES_PREP_NL, encoding="utf-8")

    en_client_dir = fireflies_dir / "english-testclient"
    en_client_dir.mkdir(parents=True)
    (en_client_dir / "sales-prep.md").write_text(_SALES_PREP_EN, encoding="utf-8")

    # Client with a sales-prep but no mined objection file -> must be skipped.
    no_mined_dir = fireflies_dir / "no-mined-client"
    no_mined_dir.mkdir(parents=True)
    (no_mined_dir / "sales-prep.md").write_text(_SALES_PREP_NL, encoding="utf-8")

    # Client with mined rows but no sales-prep -> must be skipped (mirrors a real skip case).
    (fireflies_dir / "no-prep-client").mkdir(parents=True)
    _write_mined_jsonl(
        objection_dir / "mined_no-prep-client__call-1.jsonl",
        [_row("x1", "Bezwaar zonder prep.", "call-1", "prijs", 0.9)],
    )

    rows = [
        _row("1", "Dat is te duur voor ons.", "call-1", "prijs", 0.9),
        _row("2", "We moeten dit nog intern bespreken.", "call-1", "autoriteit-proces", 0.6),
        _row("3", "Geen categorie hier, moet uitgesloten worden.", "call-1", None, 0.0),
        _row("4", "Nog een bezwaar over timing.", "call-1", "timing", 0.8),
        _row("5", "Vijfde bezwaar.", "call-1", "scope", 0.7),
        _row("6", "Zesde bezwaar.", "call-1", "concurrent", 0.5),
        _row("7", "Zevende bezwaar, moet gecapt worden op top 6.", "call-1", "gap-behoefte", 0.95),
    ]
    _write_mined_jsonl(objection_dir / "mined_testklant__call-1.jsonl", rows)

    en_rows = [_row("e1", "This is too expensive for us.", "call-en", "prijs", 0.9)]
    _write_mined_jsonl(objection_dir / "mined_english-testclient__call-en.jsonl", en_rows)

    return fireflies_dir, objection_dir


# ---------------------------------------------------------------------------
# Sales-prep parsing (pure)
# ---------------------------------------------------------------------------


def test_parse_sales_prep_sections_extracts_only_target_sections() -> None:
    sections = eval_suggestion.parse_sales_prep_sections(_SALES_PREP_NL)

    assert "ROI-argument-fixture-marker" in sections["objections"]
    assert "Differentiator-fixture-marker" in sections["differentiators"]
    assert "Trigger-fixture-marker" in sections["trigger"]

    leaked = " ".join(sections.values())
    assert "Testklant-bedrijfscontext-marker" not in leaked
    assert "Pijnpunt-marker" not in leaked
    assert "Concurrent-marker" not in leaked
    assert "Next-step-marker" not in leaked


def test_detect_language_uses_explicit_marker() -> None:
    assert eval_suggestion.detect_language(_SALES_PREP_EN, "This is fine") == "en"
    assert eval_suggestion.detect_language(_SALES_PREP_NL, "dit is een test") == "nl"


def test_detect_language_falls_back_to_utterance_heuristic_without_marker() -> None:
    no_marker_prep = "# Sales-prep — geen marker\n\n## 1. Bedrijfscontext\nGeen taal-marker hier.\n"
    assert eval_suggestion.detect_language(no_marker_prep, "dat is te duur voor ons, wij hebben budget") == "nl"
    assert eval_suggestion.detect_language(no_marker_prep, "that is too expensive for us and our budget") == "en"


# ---------------------------------------------------------------------------
# Hard-moment selection / discovery (pure)
# ---------------------------------------------------------------------------


def test_select_hard_moments_caps_and_orders_by_confidence() -> None:
    rows = [
        {"predicted_category": "prijs", "predicted_confidence": 0.5},
        {"predicted_category": None, "predicted_confidence": 0.99},
        {"predicted_category": "timing", "predicted_confidence": 0.9},
        {"predicted_category": "scope", "predicted_confidence": 0.95},
    ]
    selected = eval_suggestion.select_hard_moments(rows, limit=2)
    assert [r["predicted_confidence"] for r in selected] == [0.95, 0.9]


def test_select_hard_moments_falls_back_to_file_order_when_confidence_missing() -> None:
    rows = [
        {"predicted_category": "prijs"},
        {"predicted_category": "timing"},
        {"predicted_category": "scope"},
    ]
    selected = eval_suggestion.select_hard_moments(rows, limit=2)
    assert [r["predicted_category"] for r in selected] == ["prijs", "timing"]


def test_discover_hard_moments_skips_missing_prep_and_missing_mined(tmp_path: Path) -> None:
    fireflies_dir, objection_dir = _build_fixture_dirs(tmp_path)

    discovery = eval_suggestion.discover_hard_moments(fireflies_dir, objection_dir, limit=6)

    assert set(discovery.hard_moments_by_client) == {"testklant", "english-testclient"}
    assert len(discovery.hard_moments_by_client["testklant"]) == 6
    assert all(m.predicted_category is not None for m in discovery.hard_moments_by_client["testklant"])

    assert discovery.skipped_clients["no-prep-client"] == "geen sales-prep.md"
    assert "gemijnde objection-rows" in discovery.skipped_clients["no-mined-client"]

    assert discovery.hard_moments_by_client["english-testclient"][0].language == "en"
    assert discovery.hard_moments_by_client["testklant"][0].language == "nl"


def test_discover_hard_moments_missing_root_returns_empty(tmp_path: Path) -> None:
    discovery = eval_suggestion.discover_hard_moments(tmp_path / "does-not-exist", tmp_path / "objection_eval", limit=6)
    assert discovery.hard_moments_by_client == {}
    assert discovery.skipped_clients == {}


# ---------------------------------------------------------------------------
# Prompt builder: must inject sections 4/5/6 only
# ---------------------------------------------------------------------------


def test_build_user_prompt_injects_prep_sections_and_utterance_only() -> None:
    sections = eval_suggestion.parse_sales_prep_sections(_SALES_PREP_NL)
    prompt = eval_suggestion.build_user_prompt(sections, "Dat is te duur.", "prijs")

    assert "ROI-argument-fixture-marker" in prompt
    assert "Differentiator-fixture-marker" in prompt
    assert "Trigger-fixture-marker" in prompt
    assert "Dat is te duur." in prompt
    assert "prijs" in prompt

    assert "Testklant-bedrijfscontext-marker" not in prompt
    assert "Pijnpunt-marker" not in prompt
    assert "Concurrent-marker" not in prompt
    assert "Next-step-marker" not in prompt


def test_build_user_prompt_handles_missing_sections_gracefully() -> None:
    prompt = eval_suggestion.build_user_prompt({}, "Een bezwaar.", None)
    assert "geen sales-prep-context beschikbaar" in prompt
    assert "Een bezwaar." in prompt


# ---------------------------------------------------------------------------
# --dry-run: must print a cost estimate and must call NO provider whatsoever
# ---------------------------------------------------------------------------


def test_dry_run_prints_cost_estimate_and_calls_no_provider(tmp_path: Path, monkeypatch, capsys) -> None:
    fireflies_dir, objection_dir = _build_fixture_dirs(tmp_path)

    class _ExplodingLLMClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("LLMClient must not be instantiated during --dry-run")

    monkeypatch.setattr(eval_suggestion, "LLMClient", _ExplodingLLMClient)

    exit_code = eval_suggestion.main(
        [
            "--dry-run",
            "--fireflies-dir",
            str(fireflies_dir),
            "--objection-dir",
            str(objection_dir),
            "--prices",
            str(REPO_ROOT / "config" / "eval_model_prices.yaml"),
            "--models-yaml",
            str(REPO_ROOT / "config" / "eval_model_register.yaml"),
        ]
    )
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "Fase-B B3 cost dry-run" in out
    assert "GEEN provider aangeroepen" in out
    assert "Geschatte kosten" in out
    assert "Geschatte totaalkosten" in out
    assert "Hard moments" in out
    assert "no-prep-client" in out
    assert "no-mined-client" in out
    assert "DRY-RUN" in out


def test_default_mode_without_flags_is_also_a_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    fireflies_dir, objection_dir = _build_fixture_dirs(tmp_path)

    class _ExplodingLLMClient:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("LLMClient must not be instantiated by default")

    monkeypatch.setattr(eval_suggestion, "LLMClient", _ExplodingLLMClient)

    exit_code = eval_suggestion.main(
        [
            "--fireflies-dir",
            str(fireflies_dir),
            "--objection-dir",
            str(objection_dir),
            "--prices",
            str(REPO_ROOT / "config" / "eval_model_prices.yaml"),
        ]
    )
    assert exit_code == 0
    assert "Fase-B B3 cost dry-run" in capsys.readouterr().out


def test_dry_run_and_run_flags_are_mutually_exclusive(tmp_path: Path) -> None:
    fireflies_dir, objection_dir = _build_fixture_dirs(tmp_path)
    try:
        eval_suggestion.main(
            [
                "--fireflies-dir",
                str(fireflies_dir),
                "--objection-dir",
                str(objection_dir),
                "--dry-run",
                "--run",
            ]
        )
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("--dry-run en --run hadden een SystemExit moeten geven")


def test_missing_hard_moments_is_fatal(tmp_path: Path) -> None:
    empty_fireflies = tmp_path / "fireflies"
    empty_fireflies.mkdir()
    exit_code = eval_suggestion.main(
        [
            "--fireflies-dir",
            str(empty_fireflies),
            "--objection-dir",
            str(tmp_path / "objection_eval"),
        ]
    )
    assert exit_code == 1


# ---------------------------------------------------------------------------
# --run with a fully mocked provider layer: no real inference, verifies wiring
# ---------------------------------------------------------------------------


def test_run_mode_uses_mocked_provider_and_writes_reports(tmp_path: Path, monkeypatch) -> None:
    fireflies_dir, objection_dir = _build_fixture_dirs(tmp_path)

    class _StubResponse:
        def __init__(self, rebuttal: str) -> None:
            self.rebuttal = rebuttal

    calls: list[dict] = []

    class _StubLLMClient:
        def __init__(self, provider: str, *, timeout_ms: int) -> None:
            self.provider = provider
            self.last_usage = {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}

        def create(
            self, *, model: str, system_prompt: str, user_text: str, response_model, temperature, allow_local,
            max_tokens=None,
        ):
            calls.append({"model": model, "max_tokens": max_tokens})
            if "qwen" in model:
                return _StubResponse("Qwen-stub-rebuttal met differentiator-referentie")
            return _StubResponse("Sonnet-stub-rebuttal")

    monkeypatch.setattr(eval_suggestion, "LLMClient", _StubLLMClient)
    monkeypatch.setattr(eval_suggestion, "pace", lambda *a, **k: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    report_md = tmp_path / "report.md"
    report_json = tmp_path / "report.json"

    exit_code = eval_suggestion.main(
        [
            "--run",
            "--fireflies-dir",
            str(fireflies_dir),
            "--objection-dir",
            str(objection_dir),
            "--prices",
            str(REPO_ROOT / "config" / "eval_model_prices.yaml"),
            "--models-yaml",
            str(REPO_ROOT / "config" / "eval_model_register.yaml"),
            "--report-md",
            str(report_md),
            "--report-json",
            str(report_json),
        ]
    )
    assert exit_code == 0

    md_text = report_md.read_text(encoding="utf-8")
    assert "Qwen-stub-rebuttal" in md_text
    assert "Sonnet-stub-rebuttal" in md_text
    assert "testklant" in md_text
    assert "no-prep-client" in md_text  # skipped-clients section

    payload = json.loads(report_json.read_text(encoding="utf-8"))
    assert payload["candidate_model"] == "openrouter:qwen/qwen3.6-35b-a3b"
    assert payload["reference_model"] == "openrouter:anthropic/claude-sonnet-5"
    assert len(payload["results"]["testklant"]) == 6
    first = payload["results"]["testklant"][0]
    assert first["candidate_rebuttal"] == "Qwen-stub-rebuttal met differentiator-referentie"
    assert first["reference_rebuttal"] == "Sonnet-stub-rebuttal"
    assert first["candidate_error"] is None
    assert first["reference_error"] is None

    # The thinking-capable candidate gets a generous explicit max_tokens floor (avoids
    # finish_reason='length' truncating its reasoning trace); the non-thinking reference
    # gets none -- its provider default was never the problem, and capping it adds risk.
    candidate_calls = [c for c in calls if "qwen" in c["model"]]
    reference_calls = [c for c in calls if "qwen" not in c["model"]]
    assert candidate_calls and all(
        c["max_tokens"] == eval_suggestion._ESTIMATED_OUTPUT_TOKENS_CANDIDATE for c in candidate_calls
    )
    assert reference_calls and all(c["max_tokens"] is None for c in reference_calls)

    assert "Effective LLM_TIMEOUT_MS" in md_text
    assert "Pace tussen calls" in md_text


# ---------------------------------------------------------------------------
# --timeout-ms: must win over a live-copilot .env's LLM_TIMEOUT_MS
# (dispatch D-6ad4091a item 1)
# ---------------------------------------------------------------------------


def _make_moment() -> eval_suggestion.HardMoment:
    return eval_suggestion.HardMoment(
        client="testklant",
        source="call-1",
        id="1",
        text="Dat is te duur voor ons.",
        predicted_category="prijs",
        predicted_confidence=0.9,
        language="nl",
    )


def test_generate_rebuttal_timeout_ms_ignores_env_llm_timeout_ms(monkeypatch) -> None:
    """``timeout_ms`` goes straight to ``LLMClient``, never through ``config.llm_timeout_ms`` --
    so a live-copilot ``.env`` (``LLM_TIMEOUT_MS=3000``) can never leak into an eval run."""
    from sales_copilot.core.config import DetectorConfig

    monkeypatch.setenv("LLM_TIMEOUT_MS", "3000")
    captured_timeout_ms: list[int] = []

    class _StubResponse:
        rebuttal = "ok"

    class _StubLLMClient:
        def __init__(self, provider: str, *, timeout_ms: int) -> None:
            captured_timeout_ms.append(timeout_ms)
            self.last_usage: dict[str, int] = {}

        def create(self, **kwargs):
            return _StubResponse()

    monkeypatch.setattr(eval_suggestion, "LLMClient", _StubLLMClient)
    monkeypatch.setattr(eval_suggestion, "pace", lambda *a, **k: None)

    config = DetectorConfig.from_env()
    spec = eval_suggestion.ModelSpec(provider="openrouter", model="qwen/qwen3.6-35b-a3b")

    eval_suggestion.generate_rebuttal(spec, _make_moment(), {}, config, timeout_ms=45000)

    assert captured_timeout_ms == [45000]


# ---------------------------------------------------------------------------
# EVAL-ONLY retry-with-backoff (dispatch D-6ad4091a item 2)
# ---------------------------------------------------------------------------


def test_generate_rebuttal_retries_on_rate_limit_and_recovers(monkeypatch) -> None:
    from sales_copilot.core.config import DetectorConfig

    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    attempts = {"n": 0}

    class _StubResponse:
        rebuttal = "recovered"

    class _StubLLMClient:
        def __init__(self, provider: str, *, timeout_ms: int) -> None:
            self.last_usage: dict[str, int] = {}

        def create(self, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return _StubResponse()

    monkeypatch.setattr(eval_suggestion, "LLMClient", _StubLLMClient)

    config = DetectorConfig.from_env()
    spec = eval_suggestion.ModelSpec(provider="openrouter", model="qwen/qwen3.6-35b-a3b")

    text, meta = eval_suggestion.generate_rebuttal(
        spec, _make_moment(), {}, config, timeout_ms=30000, pace_ms=0, max_retries=3, base_delay_s=0.0
    )

    assert text == "recovered"
    assert meta["error"] is None
    assert attempts["n"] == 3
    assert len(sleeps) == 2
