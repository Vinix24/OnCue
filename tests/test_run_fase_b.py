"""Tests for scripts/run_fase_b.py — the fase-B grote-N eval runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import run_fase_b  # noqa: E402

# ---------------------------------------------------------------------------
# Fireflies discovery / loading (pure, no provider involved)
# ---------------------------------------------------------------------------


def _make_fireflies_tree(root: Path) -> None:
    client_a = root / "client-a"
    client_a.mkdir(parents=True)
    (client_a / "fireflies-aaa11111.md").write_text(
        "Prospect: we hebben interesse in een demo.\n", encoding="utf-8"
    )
    (client_a / "fireflies-aaa11111.action_items.json").write_text(
        json.dumps([{"description": "Stuur een demo-uitnodiging"}]), encoding="utf-8"
    )

    client_b_sub = root / "client-b" / "sub"
    client_b_sub.mkdir(parents=True)
    (client_b_sub / "fireflies-bbb22222.md").write_text(
        "Prospect: kunt u een offerte sturen?\n", encoding="utf-8"
    )

    excluded_client_demo = root / "excluded-client" / "demo"
    excluded_client_demo.mkdir(parents=True)
    (excluded_client_demo / "fireflies-zzz99999.md").write_text(
        "Prospect: interested in a demo.\n", encoding="utf-8"
    )


def test_discover_fireflies_transcripts_excludes_configured_client_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_fireflies_tree(tmp_path)
    monkeypatch.setenv("FASE_B_EXCLUDED_CLIENT_DIRS", "excluded-client")

    found = run_fase_b.discover_fireflies_transcripts(tmp_path)

    assert [p.name for p in found] == ["fireflies-aaa11111.md", "fireflies-bbb22222.md"]
    assert all("excluded-client" not in p.parts for p in found)


def test_discover_fireflies_transcripts_without_config_excludes_nothing(tmp_path: Path) -> None:
    """No FASE_B_EXCLUDED_CLIENT_DIRS set -> every transcript is discovered (safe default)."""
    _make_fireflies_tree(tmp_path)

    found = run_fase_b.discover_fireflies_transcripts(tmp_path)

    assert [p.name for p in found] == [
        "fireflies-aaa11111.md",
        "fireflies-bbb22222.md",
        "fireflies-zzz99999.md",
    ]


def test_discover_fireflies_transcripts_missing_root_returns_empty(tmp_path: Path) -> None:
    assert run_fase_b.discover_fireflies_transcripts(tmp_path / "does-not-exist") == []


def test_count_excluded_transcripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_fireflies_tree(tmp_path)
    monkeypatch.setenv("FASE_B_EXCLUDED_CLIENT_DIRS", "excluded-client")

    assert run_fase_b.count_excluded_transcripts(tmp_path) == 1


def test_count_excluded_transcripts_without_config_is_zero(tmp_path: Path) -> None:
    _make_fireflies_tree(tmp_path)

    assert run_fase_b.count_excluded_transcripts(tmp_path) == 0


def test_count_excluded_transcripts_missing_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FASE_B_EXCLUDED_CLIENT_DIRS", "excluded-client")

    assert run_fase_b.count_excluded_transcripts(tmp_path) == 0


def test_load_fireflies_records_relabels_source_and_loads_ground_truth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_fireflies_tree(tmp_path)
    monkeypatch.setenv("FASE_B_EXCLUDED_CLIENT_DIRS", "excluded-client")
    paths = run_fase_b.discover_fireflies_transcripts(tmp_path)

    records = run_fase_b.load_fireflies_records(paths, tmp_path)

    by_source = {rec["source"]: rec for rec in records}
    assert set(by_source) == {"client-a__fireflies-aaa11111", "client-b__fireflies-bbb22222"}

    scored = by_source["client-a__fireflies-aaa11111"]
    assert scored["reference"] == [{"description": "Stuur een demo-uitnodiging"}]
    assert "demo" in scored["transcript"]

    unscored = by_source["client-b__fireflies-bbb22222"]
    assert unscored["reference"] == []


# ---------------------------------------------------------------------------
# Qualitative-only report formatting (pure)
# ---------------------------------------------------------------------------


def test_format_qualitative_section_marks_unavailable_error_and_predicted() -> None:
    qualitative_by_source = {
        "client-a__fireflies-aaa11111": {
            "openai:gpt-4o-mini": {
                "available": True,
                "error": None,
                "predicted": [{"description": "Stuur de offerte"}],
                "item_count": 1,
            },
            "openrouter:qwen/qwen3-30b-a3b-instruct-2507": {
                "available": False,
                "error": None,
                "predicted": None,
                "item_count": 0,
            },
            "openai:gpt-4o": {
                "available": True,
                "error": "timeout",
                "predicted": None,
                "item_count": 0,
            },
        }
    }
    model_order = ["openai:gpt-4o-mini", "openrouter:qwen/qwen3-30b-a3b-instruct-2507", "openai:gpt-4o"]

    section = run_fase_b._format_qualitative_section(qualitative_by_source, model_order)

    assert "geen ground truth" in section
    assert "| openai:gpt-4o-mini | 1 | Stuur de offerte |" in section
    assert "| openrouter:qwen/qwen3-30b-a3b-instruct-2507 | n/a | provider niet beschikbaar |" in section
    assert "| openai:gpt-4o | n/a | fout: timeout |" in section


def test_format_qualitative_section_empty_input_returns_empty_string() -> None:
    assert run_fase_b._format_qualitative_section({}, []) == ""


# ---------------------------------------------------------------------------
# --dry-run: must print a cost estimate and must call NO provider whatsoever
# ---------------------------------------------------------------------------


def _write_cascade_fixture(path: Path) -> None:
    rows = [
        {"text": "Dit is een testzin over de prijs van de licentie", "label": "review", "source": "call-1"},
        {"text": "Nog een testzin over de implementatietijd", "label": "review", "source": "call-1"},
        {"text": "Derde utterance uit een heel ander gesprek", "label": "review", "source": "call-2"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _write_register_fixture(path: Path) -> None:
    path.write_text(
        "models:\n"
        "  - provider: openai\n"
        "    model: gpt-4o-mini\n"
        "    description: test model met prijs-entry\n"
        "  - provider: none\n"
        "    model: none\n"
        "    description: geen prijs-entry, oefent de onbekend-kosten-tak uit\n",
        encoding="utf-8",
    )


def _write_prices_fixture(path: Path) -> None:
    path.write_text(
        "prices:\n"
        "  - provider: openai\n"
        "    model: gpt-4o-mini\n"
        "    input_per_1m: 0.15\n"
        "    output_per_1m: 0.60\n",
        encoding="utf-8",
    )


def test_dry_run_prints_cost_estimate_and_calls_no_provider(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    mined_path = tmp_path / "mined.jsonl"
    _write_cascade_fixture(mined_path)

    fireflies_root = tmp_path / "fireflies"
    _make_fireflies_tree(fireflies_root)
    monkeypatch.setenv("FASE_B_EXCLUDED_CLIENT_DIRS", "excluded-client")

    register_path = tmp_path / "register.yaml"
    _write_register_fixture(register_path)
    prices_path = tmp_path / "prices.yaml"
    _write_prices_fixture(prices_path)

    with (
        patch("sales_copilot.modules.detector.llm_confirm.LLMConfirmClient") as mock_confirm_client,
        patch("sales_copilot.core.llm_client.LLMClient") as mock_llm_client,
        patch("httpx.get") as mock_httpx_get,
    ):
        exit_code = run_fase_b.main(
            [
                "--models-yaml",
                str(register_path),
                "--eval-set",
                str(mined_path),
                "--fireflies-dir",
                str(fireflies_root),
                "--prices",
                str(prices_path),
                "--dry-run",
            ]
        )

        assert mock_confirm_client.called is False
        assert mock_llm_client.called is False
        assert mock_httpx_get.called is False

    assert exit_code == 0

    out = capsys.readouterr().out
    assert "Fase-B cost dry-run" in out
    assert "GEEN provider aangeroepen" in out
    assert "openai:gpt-4o-mini" in out
    assert "3 utterances over 2 bronnen" in out
    assert "2 NL-transcripties" in out
    assert "1 uitgesloten via FASE_B_EXCLUDED_CLIENT_DIRS" in out
    assert "Geschatte kosten" in out
    assert "onbekend (geen prijs-entry)" in out
    assert "Geschatte totaalkosten" in out
    assert "DRY-RUN" in out


def test_default_mode_without_flags_is_also_a_dry_run(tmp_path: Path, capsys) -> None:
    """No flags at all must behave exactly like --dry-run (never spend by accident)."""
    mined_path = tmp_path / "mined.jsonl"
    _write_cascade_fixture(mined_path)

    fireflies_root = tmp_path / "fireflies"
    _make_fireflies_tree(fireflies_root)

    register_path = tmp_path / "register.yaml"
    _write_register_fixture(register_path)
    prices_path = tmp_path / "prices.yaml"
    _write_prices_fixture(prices_path)

    with (
        patch("sales_copilot.modules.detector.llm_confirm.LLMConfirmClient") as mock_confirm_client,
        patch("sales_copilot.core.llm_client.LLMClient") as mock_llm_client,
    ):
        exit_code = run_fase_b.main(
            [
                "--models-yaml",
                str(register_path),
                "--eval-set",
                str(mined_path),
                "--fireflies-dir",
                str(fireflies_root),
                "--prices",
                str(prices_path),
            ]
        )

        assert mock_confirm_client.called is False
        assert mock_llm_client.called is False

    assert exit_code == 0
    assert "Fase-B cost dry-run" in capsys.readouterr().out


def test_dry_run_and_run_flags_are_mutually_exclusive(tmp_path: Path) -> None:
    mined_path = tmp_path / "mined.jsonl"
    _write_cascade_fixture(mined_path)
    register_path = tmp_path / "register.yaml"
    _write_register_fixture(register_path)

    try:
        run_fase_b.main(
            [
                "--models-yaml",
                str(register_path),
                "--eval-set",
                str(mined_path),
                "--dry-run",
                "--run",
            ]
        )
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("--dry-run en --run hadden een SystemExit moeten geven")


def test_missing_cascade_eval_set_is_fatal(tmp_path: Path) -> None:
    register_path = tmp_path / "register.yaml"
    _write_register_fixture(register_path)

    exit_code = run_fase_b.main(
        [
            "--models-yaml",
            str(register_path),
            "--eval-set",
            str(tmp_path / "does-not-exist.jsonl"),
        ]
    )

    assert exit_code == 1


# ---------------------------------------------------------------------------
# --profiles --dry-run: thinking-policy sweep cost preview, must call NO provider
# ---------------------------------------------------------------------------


def _write_register_fixture_with_thinking_provider(path: Path) -> None:
    """One thinking-capable provider (openrouter) and one non-thinking one (openai)."""
    path.write_text(
        "models:\n"
        "  - provider: openrouter\n"
        '    model: "qwen/qwen3.6-35b-a3b"\n'
        "    description: thinking-capable model\n"
        "  - provider: openai\n"
        "    model: gpt-4o-mini\n"
        "    description: niet-thinking model (GPT-4o-familie)\n",
        encoding="utf-8",
    )


def _write_prices_fixture_with_thinking_provider(path: Path) -> None:
    path.write_text(
        "prices:\n"
        "  - provider: openrouter\n"
        '    model: "qwen/qwen3.6-35b-a3b"\n'
        "    input_per_1m: 0.10\n"
        "    output_per_1m: 0.30\n"
        "  - provider: openai\n"
        "    model: gpt-4o-mini\n"
        "    input_per_1m: 0.15\n"
        "    output_per_1m: 0.60\n",
        encoding="utf-8",
    )


def test_profile_sweep_dry_run_prints_per_model_profile_cost_and_calls_no_provider(
    tmp_path: Path, capsys
) -> None:
    mined_path = tmp_path / "mined.jsonl"
    _write_cascade_fixture(mined_path)

    register_path = tmp_path / "register.yaml"
    _write_register_fixture_with_thinking_provider(register_path)
    prices_path = tmp_path / "prices.yaml"
    _write_prices_fixture_with_thinking_provider(prices_path)

    with (
        patch("sales_copilot.modules.detector.llm_confirm.LLMConfirmClient") as mock_confirm_client,
        patch("sales_copilot.core.llm_client.LLMClient") as mock_llm_client,
        patch("httpx.get") as mock_httpx_get,
    ):
        exit_code = run_fase_b.main(
            [
                "--models-yaml",
                str(register_path),
                "--eval-set",
                str(mined_path),
                "--prices",
                str(prices_path),
                "--profiles",
                "no-think,think-512",
                "--dry-run",
            ]
        )

        assert mock_confirm_client.called is False
        assert mock_llm_client.called is False
        assert mock_httpx_get.called is False

    assert exit_code == 0

    out = capsys.readouterr().out
    assert "Fase-B thinking-sweep cost dry-run" in out
    assert "GEEN provider aangeroepen" in out
    assert "Profielen:** no-think, think-512" in out

    # Thinking-capable provider gets one row per requested profile.
    assert "| openrouter:qwen/qwen3.6-35b-a3b | no-think |" in out
    assert "| openrouter:qwen/qwen3.6-35b-a3b | think-512 |" in out

    # Non-thinking provider collapses to exactly one "n/a" row, not one per profile.
    assert out.count("| openai:gpt-4o-mini |") == 1
    assert "| openai:gpt-4o-mini | n/a (geen thinking-model) |" in out

    assert "Geschatte totaalkosten over de volledige sweep" in out
    assert "DRY-RUN" in out


def test_profile_sweep_dry_run_is_default_mode_without_run_flag(tmp_path: Path, capsys) -> None:
    """No --dry-run/--run at all, only --profiles, must still never call a provider."""
    mined_path = tmp_path / "mined.jsonl"
    _write_cascade_fixture(mined_path)

    register_path = tmp_path / "register.yaml"
    _write_register_fixture_with_thinking_provider(register_path)
    prices_path = tmp_path / "prices.yaml"
    _write_prices_fixture_with_thinking_provider(prices_path)

    with (
        patch("sales_copilot.modules.detector.llm_confirm.LLMConfirmClient") as mock_confirm_client,
        patch("sales_copilot.core.llm_client.LLMClient") as mock_llm_client,
    ):
        exit_code = run_fase_b.main(
            [
                "--models-yaml",
                str(register_path),
                "--eval-set",
                str(mined_path),
                "--prices",
                str(prices_path),
                "--profiles",
                "think-8192",
            ]
        )

        assert mock_confirm_client.called is False
        assert mock_llm_client.called is False

    assert exit_code == 0
    assert "Fase-B thinking-sweep cost dry-run" in capsys.readouterr().out


def test_profile_sweep_extra_output_tokens_scale_with_reasoning_budget(tmp_path: Path, capsys) -> None:
    """think-8192 must estimate strictly more output tokens (and cost) than no-think."""
    mined_path = tmp_path / "mined.jsonl"
    _write_cascade_fixture(mined_path)

    register_path = tmp_path / "register.yaml"
    _write_register_fixture_with_thinking_provider(register_path)
    prices_path = tmp_path / "prices.yaml"
    _write_prices_fixture_with_thinking_provider(prices_path)

    exit_code = run_fase_b.main(
        [
            "--models-yaml",
            str(register_path),
            "--eval-set",
            str(mined_path),
            "--prices",
            str(prices_path),
            "--profiles",
            "no-think,think-8192",
            "--dry-run",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    no_think_row = next(
        line for line in out.splitlines() if line.startswith("| openrouter:qwen/qwen3.6-35b-a3b | no-think |")
    )
    think_row = next(
        line for line in out.splitlines() if line.startswith("| openrouter:qwen/qwen3.6-35b-a3b | think-8192 |")
    )
    no_think_output_tokens = int(no_think_row.split("|")[4].strip())
    think_output_tokens = int(think_row.split("|")[4].strip())
    num_cascade_records = 3  # matches _write_cascade_fixture's fixture rows
    assert think_output_tokens - no_think_output_tokens == 8192 * num_cascade_records


# ---------------------------------------------------------------------------
# --timeout-ms: must override config.llm_timeout_ms and win over .env
# (dispatch D-6ad4091a item 1 -- load_dotenv(override=True) otherwise always wins)
# ---------------------------------------------------------------------------


def test_timeout_ms_override_wins_over_env_llm_timeout_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_MS", "3000")

    config = run_fase_b._resolve_detector_config(30000)

    assert config.llm_timeout_ms == 30000


def test_timeout_ms_omitted_keeps_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_TIMEOUT_MS", "3000")

    config = run_fase_b._resolve_detector_config(None)

    assert config.llm_timeout_ms == 3000


def test_unknown_profile_name_is_fatal(tmp_path: Path) -> None:
    mined_path = tmp_path / "mined.jsonl"
    _write_cascade_fixture(mined_path)
    register_path = tmp_path / "register.yaml"
    _write_register_fixture_with_thinking_provider(register_path)

    exit_code = run_fase_b.main(
        [
            "--models-yaml",
            str(register_path),
            "--eval-set",
            str(mined_path),
            "--profiles",
            "no-think,turbo-think",
            "--dry-run",
        ]
    )

    assert exit_code == 1
