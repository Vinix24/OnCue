"""Three fixes from the README measurement of the notetaker route (#292).

- An empty model pick in the start-call must not overwrite ``LLM_PROVIDER`` from ``.env``.
- The report prompt is neutral for the notetaker preset and for a call without a sales
  preset, uses the preset's ``ui_labels`` and names the call language; the sales prompt is
  unchanged for the sales presets.
- The vocabulary and normalization lists load from the app's own resolution, not the cwd.

No model is called: the prompt build, the config resolution and the path resolution run for
real; the one LLM call is replaced by a recorder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sales_copilot.__main__ import _parse_call_config
from sales_copilot.core import paths
from sales_copilot.core.config import DetectorConfig, build_module_configs
from sales_copilot.modules.reports import enrichment as enrichment_mod
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.__main__ import _report_roles
from sales_copilot.modules.reports.enrichment import (
    NeutralReportEnrichment,
    ReportEnrichment,
    SpeakerRoles,
    enrich_report,
)
from sales_copilot.modules.reports.term_corrections import TERM_CORRECTION_PROMPT
from sales_copilot.modules.transcriber.normalize import default_normalization_lists
from sales_copilot.modules.transcriber.vocabulary import default_vocabulary_config
from sales_copilot.websocket import hub_core

# ---------------------------------------------------------------------------
# 1. An empty model pick keeps .env
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_model_pick_keeps_env_provider(monkeypatch: pytest.MonkeyPatch, blank: str | None) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_MODEL", "gemma4:e4b")
    call_config = _parse_call_config({"llm": {"provider": blank, "model": blank}})

    assert call_config.llm_provider is None
    assert call_config.llm_model is None
    detector = build_module_configs(call_config)["detector"]
    assert detector.llm_provider == "ollama"
    assert detector.llm_model == "gemma4:e4b"


def test_flat_blank_model_pick_keeps_env_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    detector = build_module_configs(_parse_call_config({"llm_provider": " ", "llm_model": ""}))["detector"]

    assert detector.llm_provider == "ollama"


def test_chosen_model_still_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    call_config = _parse_call_config({"llm": {"provider": " groq ", "model": "llama-3.3"}})

    detector = build_module_configs(call_config)["detector"]
    assert detector.llm_provider == "groq"
    assert detector.llm_model == "llama-3.3"


def test_privacy_gate_mirror_treats_blank_pick_as_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    config = {"llm": {"provider": "", "model": ""}}

    assert hub_core._resolve_llm_provider(config) == "ollama"
    detector = hub_core._conversation_detector_config(config, privacy=None, client_slug=None)
    assert detector.llm_provider == "ollama"


# ---------------------------------------------------------------------------
# 2. A neutral report prompt outside the sales presets
# ---------------------------------------------------------------------------

_SALES_WORDS = ("Seller", "Prospect", "salesperson")


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call(self, part: Any, route: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        return None


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(enrichment_mod, "_resolve_part", lambda *args, **kwargs: object())
    monkeypatch.setattr(enrichment_mod, "_call", rec.call)
    return rec


def _session() -> generator.SessionData:
    segments = (("self", "Ik denk dat we dinsdag verder kunnen."), ("prospect", "Prima, dinsdag past."))
    return generator.SessionData(
        session_id="session-notes",
        call_start_ms=0,
        call_end_ms=60000,
        prospect_name=None,
        prospect_company=None,
        context_docs=[],
        speech_events=[],
        phase_events=[],
        pain_points=[],
        monologues=[],
        transcript=[
            generator.TranscriptSegment(speaker, text, index * 1000, index * 1000 + 900)
            for index, (speaker, text) in enumerate(segments)
        ],
    )


def _config(**kwargs: Any) -> DetectorConfig:
    return DetectorConfig(llm_provider="ollama", llm_model="m", **kwargs)


async def test_notetaker_prompt_is_neutral_and_uses_the_preset_labels(recorder: _Recorder) -> None:
    roles = _report_roles({}, _config(preset_name="sales", call_language="nl"))
    assert roles is not None

    await enrich_report(_session(), _config(call_language="nl"), roles=roles)

    [call] = recorder.calls
    prompt = call["system_prompt"]
    assert not any(word in prompt for word in _SALES_WORDS)
    assert "Ik is the person using the assistant, Gesprekspartner is the other party" in prompt
    assert call["user_text"] == "Ik: Ik denk dat we dinsdag verder kunnen.\nGesprekspartner: Prima, dinsdag past."
    assert call["response_model"] is NeutralReportEnrichment
    assert "prospect" not in NeutralReportEnrichment.model_fields["overview"].description


async def test_preset_ui_labels_name_the_speakers(recorder: _Recorder) -> None:
    roles = _report_roles({"self": "Coach", "prospect": "Client"}, _config(preset_name="coach"))
    assert roles == SpeakerRoles(own="Coach", other="Client")

    await enrich_report(_session(), _config(), roles=roles)

    [call] = recorder.calls
    assert "Coach is the person using the assistant, Client is the other party" in call["system_prompt"]
    assert call["user_text"].startswith("Coach: ")


@pytest.mark.parametrize(("language", "name"), [("nl", "Dutch"), ("en", "English"), ("de", "German")])
async def test_summary_language_follows_the_call_language(recorder: _Recorder, language: str, name: str) -> None:
    roles = SpeakerRoles(own="Ik", other="Gesprekspartner")

    await enrich_report(_session(), _config(call_language=language), roles=roles)

    [call] = recorder.calls
    assert f"Write every text field in {name}, the language the conversation was held in" in call["system_prompt"]


async def test_sales_prompt_is_unchanged_for_the_sales_presets(recorder: _Recorder) -> None:
    assert _report_roles(None, _config(preset_name="sales")) is None

    await enrich_report(_session(), _config(preset_name="sales"), roles=None)

    [call] = recorder.calls
    assert call["system_prompt"] == enrichment_mod._SYSTEM_PROMPT
    assert "Seller" in call["system_prompt"]
    assert call["user_text"].startswith("Seller: ")
    assert call["response_model"] is ReportEnrichment


def test_call_without_a_preset_gets_neutral_roles() -> None:
    roles = _report_roles(None, None)

    assert roles is not None
    assert roles.own == "Ik"
    assert roles.other == "Gesprekspartner"


def test_neutral_prompts_for_terms_and_combined_call_carry_no_sales_words() -> None:
    prompts = enrichment_mod._Prompts.for_call(SpeakerRoles(own="Ik", other="Gesprekspartner"), "nl")

    for text in (prompts.report, prompts.terms, prompts.combined):
        assert not any(word in text for word in _SALES_WORDS)
    assert "segment N (Ik): text" in prompts.combined
    sales = enrichment_mod._Prompts.for_call(None, "nl")
    assert sales.combined == enrichment_mod._SYSTEM_PROMPT + TERM_CORRECTION_PROMPT
    assert sales.terms == enrichment_mod._TERMS_SYSTEM_PROMPT


async def test_label_the_numbered_transcript_cannot_carry_falls_back_to_neutral(recorder: _Recorder) -> None:
    await enrich_report(_session(), _config(call_language="en"), roles=SpeakerRoles(own="Sales rep", other="Klant 1"))

    [call] = recorder.calls
    assert call["user_text"].startswith("Me: ")
    assert "Me is the person using the assistant, Other is the other party" in call["system_prompt"]


# ---------------------------------------------------------------------------
# 3. Vocabulary and normalization resolve like the rest of the app
# ---------------------------------------------------------------------------


def test_lists_load_from_another_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    vocabulary = default_vocabulary_config()
    normalization = default_normalization_lists()

    assert vocabulary.enabled is True
    assert vocabulary.terms
    assert normalization.enabled is True
    assert normalization.terms or normalization.variants


def test_lists_load_from_the_bundled_resources_of_an_installed_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resources = tmp_path / "_resources"
    (resources / "config").mkdir(parents=True)
    (resources / "config" / "transcription_vocabulary.yaml").write_text(
        "enabled: true\nterms:\n  - OnCue\n", encoding="utf-8"
    )
    (resources / "config" / "transcript_normalization.yaml").write_text(
        "enabled: true\nterms:\n  - OnCue\nvariants:\n  Oncue: OnCue\n", encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(paths, "_find_repo_root", lambda: None)
    monkeypatch.setattr(paths, "is_frozen_app", lambda: False)
    monkeypatch.setattr(paths, "_installed_package_resources_dir", lambda: resources)
    monkeypatch.setattr(paths, "_installed_package_data_dir", lambda: tmp_path / "support")

    assert default_vocabulary_config().terms == ("OnCue",)
    assert default_normalization_lists().variants == {"Oncue": "OnCue"}


def test_a_users_own_copy_in_app_support_wins_over_the_bundled_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resources = tmp_path / "_resources"
    support = tmp_path / "support"
    for base, term in ((resources, "Bundled"), (support, "Own")):
        (base / "config").mkdir(parents=True)
        (base / "config" / "transcription_vocabulary.yaml").write_text(
            f"enabled: true\nterms:\n  - {term}\n", encoding="utf-8"
        )
    monkeypatch.setattr(paths, "_find_repo_root", lambda: None)
    monkeypatch.setattr(paths, "is_frozen_app", lambda: False)
    monkeypatch.setattr(paths, "_installed_package_resources_dir", lambda: resources)
    monkeypatch.setattr(paths, "_installed_package_data_dir", lambda: support)

    assert default_vocabulary_config().terms == ("Own",)


def test_a_missing_list_stays_disabled_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "_find_repo_root", lambda: None)
    monkeypatch.setattr(paths, "is_frozen_app", lambda: False)
    monkeypatch.setattr(paths, "_installed_package_resources_dir", lambda: tmp_path / "none")
    monkeypatch.setattr(paths, "_installed_package_data_dir", lambda: tmp_path / "none-support")

    with caplog.at_level("WARNING"):
        assert default_vocabulary_config().enabled is False
        assert default_normalization_lists().enabled is False

    assert "not found" in caplog.text


def test_resolve_config_file_keeps_absolute_and_cwd_relative_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = tmp_path / "own.yaml"
    own.write_text("enabled: true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert paths.resolve_config_file(own) == own
    assert paths.resolve_config_file("own.yaml") == Path("own.yaml")
