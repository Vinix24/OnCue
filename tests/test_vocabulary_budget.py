"""Token budget for the transcription vocabulary: render script, startup warning, candidates."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

from sales_copilot.modules.transcriber.vocabulary import (
    PROMPT_TOKEN_BUDGET,
    check_budget,
    load_vocabulary_config,
    warn_if_over_budget,
)

ROOT = Path(__file__).resolve().parents[1]
pytest.importorskip("mlx_whisper.tokenizer")


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _over_budget_file(tmp_path: Path) -> Path:
    terms = "\n".join(f"  - Kwartaalrapportagebureau{i}" for i in range(40))
    path = tmp_path / "big.yaml"
    path.write_text(f"enabled: true\nprompt_prefix: Termen\nterms:\n{terms}\n", encoding="utf-8")
    return path


def test_shipped_default_is_under_budget() -> None:
    config = load_vocabulary_config(ROOT / "config" / "transcription_vocabulary.yaml")
    report = check_budget(config)
    assert not report.over_budget
    assert report.margin >= 0
    assert "SalesMinds" not in config.terms
    assert "OnCue" in config.terms


def test_render_script_ok_for_default(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _script("render_vocabulary").main([str(ROOT / "config" / "transcription_vocabulary.yaml")])
    assert rc == 0
    assert f"/ {PROMPT_TOKEN_BUDGET}" in capsys.readouterr().out


def test_render_script_over_budget_names_trailing_terms(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = _script("render_vocabulary").main([str(_over_budget_file(tmp_path))])
    err = capsys.readouterr().err
    assert rc == 1
    assert "over budget" in err
    assert "cut the trailing terms" in err
    cut, lost = err.split("cut the trailing terms:")[1].split("lost:")
    assert "Kwartaalrapportagebureau39" in cut
    assert "Kwartaalrapportagebureau0," not in cut
    assert "(framing sentence)" in lost
    assert "Kwartaalrapportagebureau0" in lost
    assert "Kwartaalrapportagebureau39" not in lost


def test_render_script_missing_file_is_an_error(tmp_path: Path) -> None:
    assert _script("render_vocabulary").main([str(tmp_path / "nope.yaml")]) == 2


def test_render_script_disabled_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "off.yaml"
    path.write_text("enabled: false\nterms: [a]\n", encoding="utf-8")
    assert _script("render_vocabulary").main([str(path)]) == 2


def test_warn_if_over_budget_logs_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = load_vocabulary_config(_over_budget_file(tmp_path))
    with caplog.at_level(logging.WARNING):
        report = warn_if_over_budget(config, "big.yaml")
    assert report is not None and report.over_budget
    assert any("over the Whisper prompt budget" in r.getMessage() for r in caplog.records)
    assert "Kwartaalrapportagebureau39" in caplog.text


def test_warn_if_over_budget_silent_when_within_budget(caplog: pytest.LogCaptureFixture) -> None:
    config = load_vocabulary_config(ROOT / "config" / "transcription_vocabulary.yaml")
    with caplog.at_level(logging.WARNING):
        warn_if_over_budget(config)
    assert not caplog.records


def test_backend_startup_warns_when_over_budget(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from sales_copilot.core.config import TranscriberConfig
    from sales_copilot.modules.transcriber.backends import _effective_initial_prompt

    config = TranscriberConfig(vocabulary_config=str(_over_budget_file(tmp_path)), vocabulary_enabled=True)
    with caplog.at_level(logging.WARNING):
        _effective_initial_prompt(config)
    assert any("over the Whisper prompt budget" in r.getMessage() for r in caplog.records)


def test_candidates_ignore_case_only_changes() -> None:
    mod = _script("vocabulary_candidates")
    assert mod.candidates("we gebruiken afas en crm", "We gebruiken AFAS en CRM") == []


def test_candidates_correct_terms_in_order() -> None:
    mod = _script("vocabulary_candidates")
    rows = mod.candidates("praat met simplicaat en hub spot", "praat met Simplicate en HubSpot")
    assert [(t, w) for t, w, _ in rows] == [("Simplicate", "simplicaat"), ("HubSpot", "hub spot")]


def test_candidates_counts_and_ranks() -> None:
    mod = _script("vocabulary_candidates")
    rows = mod.candidates("een fisma en twin veld en fisma", "een Visma en Twinfield en Visma")
    assert [(t, c) for t, _, c in rows] == [("Visma", 2), ("Twinfield", 1)]


def test_candidates_cli_missing_file(tmp_path: Path) -> None:
    mod = _script("vocabulary_candidates")
    assert mod.main([str(tmp_path / "a"), str(tmp_path / "b")]) == 2


def test_report_names_cut_terms_and_lost_head(tmp_path: Path) -> None:
    report = check_budget(load_vocabulary_config(_over_budget_file(tmp_path)))
    assert report.over_terms and report.over_terms[-1] == "Kwartaalrapportagebureau39"
    assert report.framing_lost
    assert report.lost_terms[0] == "Kwartaalrapportagebureau0"
    assert "Kwartaalrapportagebureau39" not in report.lost_terms


def test_both_backend_paths_log_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from sales_copilot.core.config import TranscriberConfig
    from sales_copilot.modules.transcriber import vocabulary
    from sales_copilot.modules.transcriber.backends import _effective_initial_prompt

    vocabulary._logged.clear()
    config = TranscriberConfig(vocabulary_config=str(_over_budget_file(tmp_path)), vocabulary_enabled=True)
    with caplog.at_level(logging.INFO):
        _effective_initial_prompt(config)
        _effective_initial_prompt(config)
        warn_if_over_budget(load_vocabulary_config(config.vocabulary_config), str(config.vocabulary_config))
    assert sum("over the Whisper prompt budget" in r.getMessage() for r in caplog.records) == 1
