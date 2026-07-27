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
