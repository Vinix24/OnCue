from pathlib import Path

import pytest

from sales_copilot.core import i18n
from sales_copilot.core.config import (
    DetectorConfig,
    I18nConfig,
    SlidesConfig,
    TalkTimeConfig,
    TranscriberConfig,
    env,
    env_bool,
    env_float,
    env_int,
    load_yaml,
)


def test_env_helpers_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOME_VAL", raising=False)
    assert env("SOME_VAL") is None
    assert env_int("SOME_INT", 7) == 7
    assert env_float("SOME_FLOAT", 1.5) == 1.5
    assert env_bool("SOME_BOOL", True) is True

    monkeypatch.setenv("SOME_VAL", "hello")
    monkeypatch.setenv("SOME_INT", "10")
    monkeypatch.setenv("SOME_FLOAT", "2.25")
    monkeypatch.setenv("SOME_BOOL", "no")

    assert env("SOME_VAL") == "hello"
    assert env_int("SOME_INT") == 10
    assert env_float("SOME_FLOAT") == 2.25
    assert env_bool("SOME_BOOL") is False


@pytest.mark.parametrize("value", ["nope", "truthy"])
def test_env_bool_invalid_raises(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BAD_BOOL", value)
    with pytest.raises(ValueError):
        env_bool("BAD_BOOL")


def test_i18n_config_from_env_defaults_to_i18n_default_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I18nConfig must never hardcode its own default -- it defers to
    sales_copilot.core.i18n.DEFAULT_LANGUAGE as the single source of truth
    (nl is default for the Dutch-language pilot)."""
    monkeypatch.delenv("LANGUAGE", raising=False)
    assert I18nConfig.from_env().language == i18n.DEFAULT_LANGUAGE == "nl"
    assert I18nConfig().language == i18n.DEFAULT_LANGUAGE


def test_i18n_config_from_env_respects_nl_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGUAGE", "nl")
    assert I18nConfig.from_env().language == "nl"


def test_talk_time_config_from_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROLLING_WINDOW_SECONDS", "90")
    monkeypatch.setenv("MONOLOGUE_WARNING_SECONDS", "60")
    monkeypatch.setenv("DISCOVERY_TARGET_SELF", "0.4")
    monkeypatch.setenv("RATIO_AMBER_THRESHOLD", "0.07")
    monkeypatch.setenv("RATIO_RED_THRESHOLD", "0.12")
    monkeypatch.setenv("VAD_MIN_RMS_PROSPECT", "0.01")
    monkeypatch.setenv("TALK_TIME_HEARTBEAT_MS", "1200")

    config = TalkTimeConfig.from_env()

    assert config.rolling_window_seconds == 90
    assert config.monologue_warning_seconds == 60
    assert config.discovery_target_self == 0.4
    assert config.ratio_amber_threshold == 0.07
    assert config.ratio_red_threshold == 0.12
    assert config.vad_min_rms_prospect == 0.01
    assert config.talk_time_heartbeat_ms == 1200


def test_detector_config_auto_phase_detection_defaults_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AUTO_PHASE_DETECTION", raising=False)
    assert DetectorConfig.from_env().auto_phase_detection is False

    monkeypatch.setenv("AUTO_PHASE_DETECTION", "true")
    assert DetectorConfig.from_env().auto_phase_detection is True


def test_transcriber_config_reads_dual_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_PORT_MIC", "9101")
    monkeypatch.setenv("WHISPER_PORT_SYSTEM", "9102")

    config = TranscriberConfig.from_env()

    assert config.port == 9101
    assert config.system_port == 9102


def test_transcriber_config_uses_call_language_when_whisper_language_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CALL_LANGUAGE", "de")
    monkeypatch.delenv("WHISPER_LANGUAGE", raising=False)

    config = TranscriberConfig.from_env()

    assert config.language == "de"


def test_transcriber_config_defaults_system_port_to_mic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_PORT_MIC", "9101")
    monkeypatch.delenv("WHISPER_PORT_SYSTEM", raising=False)

    config = TranscriberConfig.from_env()

    assert config.port == 9101
    assert config.system_port == 9101


def test_transcriber_config_reads_whisper_cpp_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_CPP_BINARY", "/tmp/whisper-cli")
    monkeypatch.setenv("WHISPER_CPP_MODEL_PATH", "/tmp/ggml-large-v3.bin")
    monkeypatch.setenv("WHISPER_CPP_CHUNK_MS", "4200")
    monkeypatch.setenv("WHISPER_CPP_THREADS", "8")

    config = TranscriberConfig.from_env()

    assert config.whisper_cpp_binary == "/tmp/whisper-cli"
    assert config.whisper_cpp_model_path == "/tmp/ggml-large-v3.bin"
    assert config.whisper_cpp_chunk_ms == 4200
    assert config.whisper_cpp_threads == 8


def test_transcriber_config_reads_whisper_cpp_server_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_CPP_SERVER_HOST", "10.0.0.5")

    config = TranscriberConfig.from_env()

    assert config.whisper_cpp_server_host == "10.0.0.5"


def test_transcriber_config_whisper_cpp_server_host_defaults_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WHISPER_CPP_SERVER_HOST", raising=False)

    config = TranscriberConfig.from_env()

    assert config.whisper_cpp_server_host == "127.0.0.1"


def test_transcriber_config_vocabulary_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WHISPER_VOCABULARY_CONFIG", raising=False)
    monkeypatch.delenv("WHISPER_VOCABULARY_ENABLED", raising=False)
    monkeypatch.delenv("WHISPER_VOCABULARY_INITIAL_PROMPT", raising=False)

    config = TranscriberConfig.from_env()

    assert config.vocabulary_config == "config/transcription_vocabulary.yaml"
    assert config.vocabulary_enabled is True
    assert config.vocabulary_initial_prompt is None


def test_transcriber_config_vocabulary_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_VOCABULARY_CONFIG", "config/custom_vocab.yaml")
    monkeypatch.setenv("WHISPER_VOCABULARY_ENABLED", "false")
    monkeypatch.setenv("WHISPER_VOCABULARY_INITIAL_PROMPT", "custom prompt")

    config = TranscriberConfig.from_env()

    assert config.vocabulary_config == "config/custom_vocab.yaml"
    assert config.vocabulary_enabled is False
    assert config.vocabulary_initial_prompt == "custom prompt"


def test_detector_config_reads_objection_detection_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBJECTIONS_CONFIG", "config/custom-objections.yaml")
    monkeypatch.setenv("OBJECTION_RESPONSES_CONFIG", "config/custom-objection-responses.yaml")
    monkeypatch.setenv("ENABLE_OBJECTION_DETECTION", "true")

    config = DetectorConfig.from_env()

    assert config.objections_config == "config/custom-objections.yaml"
    assert config.objection_responses_config == "config/custom-objection-responses.yaml"
    assert config.enable_objection_detection is True


def test_detector_config_objection_detection_toggle_defaults_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENABLE_OBJECTION_DETECTION", raising=False)
    assert DetectorConfig.from_env().enable_objection_detection is True

    monkeypatch.setenv("ENABLE_OBJECTION_DETECTION", "false")
    assert DetectorConfig.from_env().enable_objection_detection is False


def test_detector_config_ollama_timeout_floor_applies_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the hosted-provider default (7000ms) silently timed out every local
    Ollama summary. Constructing with the provider default timeout must be floored."""
    monkeypatch.delenv("LLM_TIMEOUT_MS", raising=False)

    config = DetectorConfig(llm_provider="ollama")

    assert config.llm_timeout_ms == 90_000


def test_detector_config_ollama_timeout_floor_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """.env.example ships an explicit LLM_TIMEOUT_MS=7000 — the floor must win even when
    the value came from an explicit (but too-low-for-local-models) env var."""
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_TIMEOUT_MS", "7000")

    config = DetectorConfig.from_env()

    assert config.llm_timeout_ms == 90_000


def test_detector_config_ollama_timeout_floor_does_not_lower_explicit_higher_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_TIMEOUT_MS", "120000")

    config = DetectorConfig.from_env()

    assert config.llm_timeout_ms == 120_000


def test_detector_config_hosted_providers_keep_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ollama floor must not leak into hosted providers (gemini/vertex/openai/azure/groq)."""
    monkeypatch.delenv("LLM_TIMEOUT_MS", raising=False)

    config = DetectorConfig(llm_provider="gemini")

    assert config.llm_timeout_ms == 7000


def test_detector_config_ollama_timeout_floor_applies_after_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_module_configs() overrides llm_provider via dataclasses.replace() *after*
    DetectorConfig.from_env() already ran with the hosted default. __post_init__ must
    re-apply the floor on replace, not only at first construction."""
    from dataclasses import replace

    monkeypatch.delenv("LLM_TIMEOUT_MS", raising=False)
    config = DetectorConfig.from_env()
    assert config.llm_timeout_ms == 7000  # sanity: hosted-provider default before override

    switched = replace(config, llm_provider="ollama")

    assert switched.llm_timeout_ms == 90_000


def test_detector_config_reads_call_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CALL_LANGUAGE", "EN")

    config = DetectorConfig.from_env()

    assert config.call_language == "en"


def test_detector_config_suggestions_toggle_defaults_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENABLE_SUGGESTIONS", raising=False)
    assert DetectorConfig.from_env().enable_suggestions is True

    monkeypatch.setenv("ENABLE_SUGGESTIONS", "false")
    assert DetectorConfig.from_env().enable_suggestions is False


def test_detector_config_summary_toggle_defaults_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENABLE_SUMMARY", raising=False)
    assert DetectorConfig.from_env().enable_summary is True

    monkeypatch.setenv("ENABLE_SUMMARY", "false")
    assert DetectorConfig.from_env().enable_summary is False


def test_detector_config_llm_streaming_toggle_defaults_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_STREAMING", raising=False)
    assert DetectorConfig.from_env().llm_streaming is True
    assert DetectorConfig().llm_streaming is True

    monkeypatch.setenv("LLM_STREAMING", "false")
    assert DetectorConfig.from_env().llm_streaming is False


def test_detector_config_llm_prompt_cache_toggle_defaults_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LLM_PROMPT_CACHE", raising=False)
    assert DetectorConfig.from_env().llm_prompt_cache is True
    assert DetectorConfig().llm_prompt_cache is True

    monkeypatch.setenv("LLM_PROMPT_CACHE", "false")
    assert DetectorConfig.from_env().llm_prompt_cache is False


def test_slides_config_dynamic_slides_defaults_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DYNAMIC_SLIDES", raising=False)
    assert SlidesConfig.from_env().dynamic_slides is True

    monkeypatch.setenv("DYNAMIC_SLIDES", "false")
    assert SlidesConfig.from_env().dynamic_slides is False


def test_load_yaml_returns_mapping(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("key: value\n", encoding="utf-8")

    data = load_yaml(yaml_path)

    assert data == {"key": "value"}


def test_load_yaml_rejects_non_mapping(tmp_path: Path) -> None:
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("- item\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_yaml(yaml_path)



def test_detector_config_reads_opportunity_responses_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPPORTUNITY_RESPONSES_CONFIG", "config/custom-opportunity-responses.yaml")

    config = DetectorConfig.from_env()

    assert config.opportunity_responses_config == "config/custom-opportunity-responses.yaml"
