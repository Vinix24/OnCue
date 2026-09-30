import pytest

from sales_copilot.__main__ import _parse_call_config
from sales_copilot.core.config import CallConfig, build_module_configs


def test_call_config_disables_presentation_on_single_screen() -> None:
    config = CallConfig(screens=1, enable_presentation=True)

    assert config.enable_presentation is False


def test_call_config_rejects_invalid_screen_count() -> None:
    with pytest.raises(ValueError):
        CallConfig(screens=3)


def test_build_module_configs_applies_overrides() -> None:
    call_config = CallConfig(
        screens=2,
        transcript_backend="whisper.cpp",
        call_language="de",
        llm_provider="openai",
        llm_model="gpt-4.1",
        preset_name="recruitment",
        prospect_industry="manufacturing",
        prospect_company="Acme",
    )

    configs = build_module_configs(call_config)

    assert configs["transcriber"].backend == "whisper.cpp"
    assert configs["transcriber"].language == "de"
    assert configs["detector"].llm_provider == "openai"
    assert configs["detector"].llm_model == "gpt-4.1"
    assert configs["detector"].call_language == "de"
    assert configs["detector"].preset_name == "recruitment"
    assert configs["slides"].prospect_industry == "manufacturing"


def test_call_config_rejects_unknown_preset() -> None:
    with pytest.raises(ValueError, match="preset_name"):
        CallConfig(preset_name="../private")


def test_parse_call_config_accepts_preset_alias() -> None:
    parsed = _parse_call_config({"preset": "COACH"})

    assert parsed.preset_name == "coach"


def test_parse_call_config_accepts_language_field() -> None:
    parsed = _parse_call_config(
        {
            "screen_mode": "single",
            "language": "EN",
            "transcript": {"backend": "mlx-whisper"},
        }
    )

    assert parsed.call_language == "en"


def test_parse_call_config_prefers_transcript_language() -> None:
    parsed = _parse_call_config(
        {
            "language": "nl",
            "transcript": {"language": "de"},
        }
    )

    assert parsed.call_language == "de"


# --- client_slug (PR-D4 dossier opt-in) --------------------------------------


def test_parse_call_config_defaults_client_slug_to_none() -> None:
    parsed = _parse_call_config({})

    assert parsed.client_slug is None


def test_parse_call_config_reads_top_level_client_slug() -> None:
    parsed = _parse_call_config({"client_slug": "Acme Corp"})

    assert parsed.client_slug == "acme-corp"


def test_parse_call_config_reads_nested_dossier_client_slug() -> None:
    parsed = _parse_call_config({"dossier": {"client_slug": "Acme Corp"}})

    assert parsed.client_slug == "acme-corp"


def test_parse_call_config_blank_client_slug_is_none() -> None:
    parsed = _parse_call_config({"client_slug": "   "})

    assert parsed.client_slug is None


def test_parse_call_config_top_level_client_slug_wins_over_dossier() -> None:
    parsed = _parse_call_config(
        {"client_slug": "top-level", "dossier": {"client_slug": "nested"}}
    )

    assert parsed.client_slug == "top-level"


# --- aflevering (klantmap-als-eenheid D2, server-derived from klant.yaml) ----


def test_parse_call_config_defaults_aflevering_to_none() -> None:
    parsed = _parse_call_config({})

    assert parsed.aflevering is None


def test_parse_call_config_reads_aflevering() -> None:
    parsed = _parse_call_config({"aflevering": "lokaal"})

    assert parsed.aflevering == "lokaal"


def test_parse_call_config_ignores_non_string_aflevering() -> None:
    parsed = _parse_call_config({"aflevering": 123})

    assert parsed.aflevering is None
