"""Configuration loading helpers and dataclasses."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from sales_copilot.core import i18n, lang_router
from sales_copilot.core.paths import is_frozen_app, resolve_app_path, resolve_app_resource

_ALLOWED_PRESETS = frozenset({"sales", "coach", "recruitment", "acquisitie"})

# Local Ollama models run cold in up to ~70s and warm in 15-50s (vs. hosted providers that
# reply in 1-3s). The hosted-provider default LLM_TIMEOUT_MS (7000ms) silently times out
# every local summary before the model can finish, which looks like "the model is broken"
# when it is actually just slower hardware. Enforced as a hard floor in
# ``DetectorConfig.__post_init__`` so it applies no matter how the config was built
# (``from_env``, direct construction, or ``dataclasses.replace`` via ``with_overrides``).
_OLLAMA_TIMEOUT_FLOOR_MS = 90_000


def load_env(path: str | Path = "") -> bool:
    """Load environment variables from a .env file if it exists.

    In a frozen app the location resolves inside the writable app-support
    directory so the bundle works regardless of the current working directory.
    In dev the legacy behaviour is preserved: a relative path is interpreted
    relative to the current working directory (matches python-dotenv's default).
    """
    if not path:
        dotenv_path = resolve_app_path(".env") if is_frozen_app() else Path(".env")
    else:
        dotenv_path = Path(path)
        if not dotenv_path.is_absolute() and is_frozen_app():
            dotenv_path = resolve_app_path(dotenv_path)
    return load_dotenv(dotenv_path=dotenv_path, override=False)


def env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def env_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Invalid int for {name}: {value}") from exc


def env_float(name: str, default: float | None = None) -> float | None:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Invalid float for {name}: {value}") from exc


def env_bool(name: str, default: bool | None = None) -> bool | None:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid bool for {name}: {value}")


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict."""
    yaml_path = Path(path)
    with yaml_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {yaml_path}")
    return data


@dataclass(frozen=True)
class TalkTimeConfig:
    rolling_window_seconds: int = 120
    monologue_warning_seconds: int = 76
    discovery_target_self: float = 0.35
    pitch_target_self: float = 0.65
    closing_target_self: float = 0.45
    ratio_amber_threshold: float = 0.05
    ratio_red_threshold: float = 0.10
    coaching_update_interval_ms: int = 5000
    percentage_update_interval_ms: int = 15000
    talk_time_heartbeat_ms: int = 1000
    vad_positive_threshold_self: float = 0.5
    vad_negative_threshold_self: float = 0.35
    vad_positive_threshold_prospect: float = 0.62
    vad_negative_threshold_prospect: float = 0.45
    vad_min_rms_self: float = 0.004
    vad_min_rms_prospect: float = 0.008
    vad_debounce_ms: int = 300
    single_stream_speaker_default: str = "prospect"

    @classmethod
    def from_env(cls) -> TalkTimeConfig:
        return cls(
            rolling_window_seconds=env_int("ROLLING_WINDOW_SECONDS", 120) or 120,
            monologue_warning_seconds=env_int("MONOLOGUE_WARNING_SECONDS", 76) or 76,
            discovery_target_self=env_float("DISCOVERY_TARGET_SELF", 0.35) or 0.35,
            pitch_target_self=env_float("PITCH_TARGET_SELF", 0.65) or 0.65,
            closing_target_self=env_float("CLOSING_TARGET_SELF", 0.45) or 0.45,
            ratio_amber_threshold=env_float("RATIO_AMBER_THRESHOLD", 0.05) or 0.05,
            ratio_red_threshold=env_float("RATIO_RED_THRESHOLD", 0.10) or 0.10,
            coaching_update_interval_ms=env_int("COACHING_UPDATE_INTERVAL_MS", 5000) or 5000,
            percentage_update_interval_ms=env_int("PERCENTAGE_UPDATE_INTERVAL_MS", 15000) or 15000,
            talk_time_heartbeat_ms=env_int("TALK_TIME_HEARTBEAT_MS", 1000) or 1000,
            vad_positive_threshold_self=env_float("VAD_POSITIVE_THRESHOLD_SELF", 0.5) or 0.5,
            vad_negative_threshold_self=env_float("VAD_NEGATIVE_THRESHOLD_SELF", 0.35) or 0.35,
            vad_positive_threshold_prospect=env_float("VAD_POSITIVE_THRESHOLD_PROSPECT", 0.62) or 0.62,
            vad_negative_threshold_prospect=env_float("VAD_NEGATIVE_THRESHOLD_PROSPECT", 0.45) or 0.45,
            vad_min_rms_self=env_float("VAD_MIN_RMS_SELF", 0.004) or 0.004,
            vad_min_rms_prospect=env_float("VAD_MIN_RMS_PROSPECT", 0.008) or 0.008,
            vad_debounce_ms=env_int("VAD_DEBOUNCE_MS", 300) or 300,
            single_stream_speaker_default=(
                env("SINGLE_STREAM_SPEAKER_DEFAULT", "prospect") or "prospect"
            ).strip().lower(),
        )


@dataclass(frozen=True)
class TranscriberConfig:
    backend: str = "whisper.cpp"
    model: str = "large-v3-turbo"
    language: str = "nl"
    diarization_backend: str = "sortformer"
    min_speakers: int = 2
    max_speakers: int = 2
    port: int = 8761
    system_port: int = 8761
    whisper_cpp_binary: str = str(resolve_app_resource("vendor/whisper.cpp/build/bin/whisper-cli"))
    whisper_cpp_model_path: str = str(
        resolve_app_resource("vendor/whisper.cpp/models/ggml-large-v3-turbo.bin")
    )
    whisper_cpp_chunk_ms: int = 3000
    whisper_cpp_threads: int = 4
    whisper_cpp_timeout_s: float = 30.0
    whisper_cpp_server_enabled: bool = True
    whisper_cpp_server_binary: str = str(
        resolve_app_resource("vendor/whisper.cpp/build/bin/whisper-server")
    )
    whisper_cpp_server_host: str = "127.0.0.1"
    whisper_cpp_server_port: int = 0
    whisper_cpp_server_startup_timeout_s: float = 60.0
    max_buffer_seconds: float = 4.0
    max_buffer_hard_seconds: float = 7.0
    silence_gap_seconds: float = 1.0
    min_segment_seconds: float = 1.2
    long_silence_escape_seconds: float = 2.5
    pre_roll_ms: int = 500
    hangover_ms: int = 250
    turn_backchannel_guard_ms: int = 700
    eager_warmup: bool = False
    single_stream_speaker_default: str = "prospect"
    shared_queue_enabled: bool = True
    queue_max_size: int = 32
    self_priority: str = "low"
    transcribe_self_live: bool = False
    vocabulary_config: str = "config/transcription_vocabulary.yaml"
    vocabulary_enabled: bool = True
    vocabulary_initial_prompt: str | None = None

    @classmethod
    def from_env(cls) -> TranscriberConfig:
        # Transcription-vocabulary/biasing config is routed by the configured
        # LANGUAGE via lang_router (default nl -> config/transcription_vocabulary.yaml).
        default_vocabulary_config = lang_router.route_vocabulary_config()
        mic_port = env_int("WHISPER_PORT_MIC", env_int("WHISPER_PORT", 8761)) or 8761
        system_port = (
            env_int(
                "WHISPER_PORT_SYSTEM",
                env_int("WHISPER_PORT_MIC", env_int("WHISPER_PORT", 8761)),
            )
            or 8761
        )
        return cls(
            backend=env("WHISPER_BACKEND", "whisper.cpp") or "whisper.cpp",
            model=env("WHISPER_MODEL", "large-v3-turbo") or "large-v3-turbo",
            language=(env("WHISPER_LANGUAGE", env("CALL_LANGUAGE", "nl")) or "nl").strip().lower(),
            diarization_backend=env("DIARIZATION_BACKEND", "sortformer") or "sortformer",
            min_speakers=env_int("MIN_SPEAKERS", 2) or 2,
            max_speakers=env_int("MAX_SPEAKERS", 2) or 2,
            port=mic_port,
            system_port=system_port,
            whisper_cpp_binary=env(
                "WHISPER_CPP_BINARY",
                str(resolve_app_resource("vendor/whisper.cpp/build/bin/whisper-cli")),
            )
            or str(resolve_app_resource("vendor/whisper.cpp/build/bin/whisper-cli")),
            whisper_cpp_model_path=env(
                "WHISPER_CPP_MODEL_PATH",
                str(resolve_app_resource("vendor/whisper.cpp/models/ggml-large-v3-turbo.bin")),
            )
            or str(resolve_app_resource("vendor/whisper.cpp/models/ggml-large-v3-turbo.bin")),
            whisper_cpp_chunk_ms=env_int("WHISPER_CPP_CHUNK_MS", 3000) or 3000,
            whisper_cpp_threads=env_int("WHISPER_CPP_THREADS", 4) or 4,
            whisper_cpp_timeout_s=env_float("WHISPER_CPP_TIMEOUT_S", 30.0) or 30.0,
            whisper_cpp_server_enabled=bool(env_bool("WHISPER_CPP_SERVER_ENABLED", True)),
            whisper_cpp_server_binary=env(
                "WHISPER_CPP_SERVER_BINARY",
                str(resolve_app_resource("vendor/whisper.cpp/build/bin/whisper-server")),
            )
            or str(resolve_app_resource("vendor/whisper.cpp/build/bin/whisper-server")),
            whisper_cpp_server_host=env("WHISPER_CPP_SERVER_HOST", "127.0.0.1") or "127.0.0.1",
            whisper_cpp_server_port=env_int("WHISPER_CPP_SERVER_PORT", 0) or 0,
            whisper_cpp_server_startup_timeout_s=env_float(
                "WHISPER_CPP_SERVER_STARTUP_TIMEOUT_S", 60.0
            )
            or 60.0,
            max_buffer_seconds=env_float("WHISPER_MAX_BUFFER_SECONDS", 4.0) or 4.0,
            max_buffer_hard_seconds=env_float("WHISPER_MAX_BUFFER_HARD_SECONDS", 7.0) or 7.0,
            silence_gap_seconds=env_float("WHISPER_SILENCE_GAP_SECONDS", 1.0) or 1.0,
            min_segment_seconds=env_float("WHISPER_MIN_SEGMENT_SECONDS", 1.2) or 1.2,
            long_silence_escape_seconds=env_float("WHISPER_LONG_SILENCE_ESCAPE_SECONDS", 2.5) or 2.5,
            pre_roll_ms=env_int("WHISPER_PRE_ROLL_MS", 500) or 500,
            hangover_ms=env_int("WHISPER_HANGOVER_MS", 250) or 250,
            turn_backchannel_guard_ms=env_int("WHISPER_TURN_BACKCHANNEL_GUARD_MS", 700) or 700,
            eager_warmup=bool(env_bool("WHISPER_EAGER_WARMUP", False)),
            single_stream_speaker_default=(
                env("SINGLE_STREAM_SPEAKER_DEFAULT", "prospect") or "prospect"
            ).strip().lower(),
            shared_queue_enabled=bool(env_bool("TRANSCRIBER_SHARED_QUEUE", True)),
            queue_max_size=env_int("TRANSCRIBER_QUEUE_MAX_SIZE", 32) or 32,
            self_priority=(env("TRANSCRIBER_SELF_PRIORITY", "low") or "low").strip().lower(),
            transcribe_self_live=bool(env_bool("TRANSCRIBE_SELF_LIVE", False)),
            vocabulary_config=env("WHISPER_VOCABULARY_CONFIG", default_vocabulary_config)
            or default_vocabulary_config,
            vocabulary_enabled=bool(env_bool("WHISPER_VOCABULARY_ENABLED", True)),
            vocabulary_initial_prompt=env("WHISPER_VOCABULARY_INITIAL_PROMPT"),
        )


@dataclass(frozen=True)
class DetectorConfig:
    llm_provider: str = "openrouter"
    llm_model: str = "anthropic/claude-haiku-4.5"
    llm_temperature: float = 0.1
    llm_timeout_ms: int = 7000
    llm_streaming: bool = True
    llm_prompt_cache: bool = True
    embedding_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    confidence_threshold_high: float = 0.85
    confidence_threshold_low: float = 0.50
    debounce_seconds: int = 45
    auto_phase_detection: bool = False
    pain_points_config: str = str(resolve_app_path("config/pain_points.yaml"))
    objections_config: str = str(resolve_app_path("config/objections.yaml"))
    opportunities_config: str = str(resolve_app_path("config/opportunities.yaml"))
    negatives_config: str = str(resolve_app_path("config/negatives.yaml"))
    objection_responses_config: str = str(resolve_app_path("config/objection_responses.yaml"))
    opportunity_responses_config: str = str(resolve_app_path("config/opportunity_responses.yaml"))
    include_opportunities: bool = True
    include_negatives: bool = True
    enable_objection_detection: bool = True
    enable_suggestions: bool = True
    enable_summary: bool = True
    enable_script_tracking: bool = True
    script_tracking_config: str = "config/scripts/default.yaml"
    only_classify_prospect: bool = True
    call_language: str = "nl"
    sliding_window_size: int = 5
    min_chunks_to_classify: int = 3
    classification_debounce_seconds: float = 5.0
    preset_name: str = "sales"

    def __post_init__(self) -> None:
        if (
            self.llm_provider.strip().lower() == "ollama"
            and self.llm_timeout_ms < _OLLAMA_TIMEOUT_FLOOR_MS
        ):
            object.__setattr__(self, "llm_timeout_ms", _OLLAMA_TIMEOUT_FLOOR_MS)

    @classmethod
    def from_env(cls) -> DetectorConfig:
        only_classify = env_bool("ONLY_CLASSIFY_PROSPECT", True)
        if only_classify is None:
            only_classify = True
        auto_phase_detection = env_bool("AUTO_PHASE_DETECTION", False)
        if auto_phase_detection is None:
            auto_phase_detection = False
        enable_objection_detection = env_bool("ENABLE_OBJECTION_DETECTION", True)
        if enable_objection_detection is None:
            enable_objection_detection = True
        enable_suggestions = env_bool("ENABLE_SUGGESTIONS", True)
        if enable_suggestions is None:
            enable_suggestions = True
        enable_summary = env_bool("ENABLE_SUMMARY", True)
        if enable_summary is None:
            enable_summary = True
        enable_script_tracking = env_bool("ENABLE_SCRIPT_TRACKING", True)
        if enable_script_tracking is None:
            enable_script_tracking = True
        llm_streaming = env_bool("LLM_STREAMING", True)
        if llm_streaming is None:
            llm_streaming = True
        return cls(
            llm_provider=env("LLM_PROVIDER", "openrouter") or "openrouter",
            llm_model=env("LLM_MODEL", "anthropic/claude-haiku-4.5") or "anthropic/claude-haiku-4.5",
            llm_temperature=env_float("LLM_TEMPERATURE", 0.1) or 0.1,
            llm_timeout_ms=env_int("LLM_TIMEOUT_MS", 7000) or 7000,
            llm_streaming=llm_streaming,
            llm_prompt_cache=bool(env_bool("LLM_PROMPT_CACHE", True)),
            embedding_model=env("EMBEDDING_MODEL", "paraphrase-multilingual-MiniLM-L12-v2")
            or "paraphrase-multilingual-MiniLM-L12-v2",
            confidence_threshold_high=(
                env_float("CONFIDENCE_THRESHOLD_HIGH", 0.85)
                if env_float("CONFIDENCE_THRESHOLD_HIGH", 0.85) is not None
                else 0.85
            ),
            confidence_threshold_low=(
                env_float("CONFIDENCE_THRESHOLD_LOW", 0.50)
                if env_float("CONFIDENCE_THRESHOLD_LOW", 0.50) is not None
                else 0.50
            ),
            debounce_seconds=env_int("DEBOUNCE_SECONDS", 45) or 45,
            auto_phase_detection=auto_phase_detection,
            pain_points_config=env(
                "PAIN_POINTS_CONFIG", str(resolve_app_path("config/pain_points.yaml"))
            )
            or str(resolve_app_path("config/pain_points.yaml")),
            objections_config=env(
                "OBJECTIONS_CONFIG", str(resolve_app_path("config/objections.yaml"))
            )
            or str(resolve_app_path("config/objections.yaml")),
            opportunities_config=env(
                "OPPORTUNITIES_CONFIG", str(resolve_app_path("config/opportunities.yaml"))
            )
            or str(resolve_app_path("config/opportunities.yaml")),
            negatives_config=env(
                "NEGATIVES_CONFIG", str(resolve_app_path("config/negatives.yaml"))
            )
            or str(resolve_app_path("config/negatives.yaml")),
            objection_responses_config=env(
                "OBJECTION_RESPONSES_CONFIG", str(resolve_app_path("config/objection_responses.yaml"))
            )
            or str(resolve_app_path("config/objection_responses.yaml")),
            opportunity_responses_config=env(
                "OPPORTUNITY_RESPONSES_CONFIG", str(resolve_app_path("config/opportunity_responses.yaml"))
            )
            or str(resolve_app_path("config/opportunity_responses.yaml")),
            include_opportunities=env_bool("INCLUDE_OPPORTUNITIES", True) or False,
            include_negatives=env_bool("INCLUDE_NEGATIVES", True) or False,
            enable_objection_detection=enable_objection_detection,
            enable_suggestions=enable_suggestions,
            enable_summary=enable_summary,
            enable_script_tracking=enable_script_tracking,
            script_tracking_config=env("SCRIPT_TRACKING_CONFIG", "config/scripts/default.yaml")
            or "config/scripts/default.yaml",
            only_classify_prospect=only_classify,
            call_language=(env("CALL_LANGUAGE", "nl") or "nl").strip().lower(),
            sliding_window_size=env_int("DETECTOR_WINDOW_SIZE", 5) or 5,
            min_chunks_to_classify=env_int("DETECTOR_MIN_CHUNKS", 3) or 3,
            classification_debounce_seconds=env_float("DETECTOR_DEBOUNCE_S", 5.0) or 5.0,
            preset_name=(env("PRESET", "sales") or "sales").strip().lower(),
        )


@dataclass(frozen=True)
class SlidesConfig:
    case_db_type: str = "sqlite"
    case_db_sqlite_path: str = str(resolve_app_path("data/cases.db"))
    case_db_url: str | None = None
    case_db_key: str | None = None
    prospect_industry: str | None = None
    reveal_ws_port: int = 8765
    reveal_transition: str = "slide"
    presentation_path: str = str(resolve_app_resource("presentation/index.html"))
    dynamic_slides: bool = True

    @classmethod
    def from_env(cls) -> SlidesConfig:
        return cls(
            case_db_type=env("CASE_DB_TYPE", "sqlite") or "sqlite",
            case_db_sqlite_path=env(
                "CASE_DB_SQLITE_PATH", str(resolve_app_path("data/cases.db"))
            )
            or str(resolve_app_path("data/cases.db")),
            case_db_url=env("CASE_DB_URL"),
            case_db_key=env("CASE_DB_KEY"),
            prospect_industry=env("PROSPECT_INDUSTRY"),
            reveal_ws_port=env_int("REVEAL_WS_PORT", 8765) or 8765,
            reveal_transition=env("REVEAL_TRANSITION", "slide") or "slide",
            presentation_path=env(
                "PRESENTATION_PATH", str(resolve_app_resource("presentation/index.html"))
            )
            or str(resolve_app_resource("presentation/index.html")),
            dynamic_slides=env_bool("DYNAMIC_SLIDES", True) or False,
        )


@dataclass(frozen=True)
class WebSocketConfig:
    host: str = "127.0.0.1"
    port: int = 8760

    @classmethod
    def from_env(cls) -> WebSocketConfig:
        return cls(
            host=env("WS_HUB_HOST", "127.0.0.1") or "127.0.0.1",
            port=env_int("WS_HUB_PORT", 8760) or 8760,
        )


@dataclass(frozen=True)
class CallConfig:
    screens: int = 2
    enable_talk_time: bool = True
    enable_transcriber: bool = True
    enable_detector: bool = True
    enable_presentation: bool = True
    enable_reports: bool = True
    transcript_backend: str | None = None
    prospect_name: str | None = None
    prospect_company: str | None = None
    prospect_industry: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    preset_name: str | None = None
    context_docs: list[str] = field(default_factory=list)
    call_language: str = "nl"
    transcribe_self_live: bool | None = None

    def __post_init__(self) -> None:
        if self.screens not in {1, 2}:
            raise ValueError("screens must be 1 or 2")
        if self.screens == 1 and self.enable_presentation:
            object.__setattr__(self, "enable_presentation", False)
        if self.preset_name is not None:
            preset_name = self.preset_name.strip().lower()
            if preset_name not in _ALLOWED_PRESETS:
                raise ValueError(f"preset_name must be one of: {sorted(_ALLOWED_PRESETS)}")
            object.__setattr__(self, "preset_name", preset_name)


@dataclass(frozen=True)
class I18nConfig:
    """Configured product/UI language for the i18n message layer.

    ``language`` selects the user-facing message catalog (``config/i18n/<lang>.yaml``)
    and the coaching-prompt / transcription-vocabulary routing in
    ``sales_copilot.core.lang_router``. Distinct from ``call_language`` /
    ``CALL_LANGUAGE``, which is the spoken language passed to the transcriber and
    detector routes. Missing catalogs and keys fall back to the default language
    (``sales_copilot.core.i18n.DEFAULT_LANGUAGE`` -- the single source of truth,
    never hardcoded here).
    """

    language: str = i18n.DEFAULT_LANGUAGE

    @classmethod
    def from_env(cls) -> I18nConfig:
        return cls(language=(env("LANGUAGE", i18n.DEFAULT_LANGUAGE) or i18n.DEFAULT_LANGUAGE).strip().lower())


@dataclass(frozen=True)
class MeasurementConfig:
    """Privacy-safe launch-funnel measurement toggles.

    Every signal defaults to OFF and every destination is configurable. See
    ``sales_copilot.core.measurement_signals`` for the implementation.
    """

    correction_opt_in: bool = False
    correction_endpoint: str | None = None
    heartbeat_enabled: bool = False
    heartbeat_endpoint: str | None = None
    heartbeat_interval_s: int = 3600
    install_code: str = ""

    @classmethod
    def from_env(cls) -> MeasurementConfig:
        return cls(
            correction_opt_in=bool(env_bool("MEASUREMENT_CORRECTION_OPT_IN", False)),
            correction_endpoint=env("MEASUREMENT_CORRECTION_ENDPOINT"),
            heartbeat_enabled=bool(env_bool("MEASUREMENT_HEARTBEAT_ENABLED", False)),
            heartbeat_endpoint=env("MEASUREMENT_HEARTBEAT_ENDPOINT"),
            heartbeat_interval_s=env_int("MEASUREMENT_HEARTBEAT_INTERVAL_S", 3600) or 3600,
            install_code=env("INSTALL_CODE", "") or "",
        )


def with_overrides(config: Any, **overrides: Any) -> Any:
    filtered = {key: value for key, value in overrides.items() if value is not None}
    return replace(config, **filtered)


def build_module_configs(call_config: CallConfig) -> dict[str, Any]:
    talk_time = TalkTimeConfig.from_env()
    transcriber = TranscriberConfig.from_env()
    detector = DetectorConfig.from_env()
    slides = SlidesConfig.from_env()
    websocket = WebSocketConfig.from_env()
    i18n_config = I18nConfig.from_env()

    transcriber = with_overrides(transcriber, backend=call_config.transcript_backend)
    detector = with_overrides(
        detector,
        llm_provider=call_config.llm_provider,
        llm_model=call_config.llm_model,
        call_language=call_config.call_language,
        preset_name=call_config.preset_name,
    )
    transcriber = with_overrides(transcriber, language=call_config.call_language)
    transcriber = with_overrides(transcriber, transcribe_self_live=call_config.transcribe_self_live)
    slides = with_overrides(slides, prospect_industry=call_config.prospect_industry)

    return {
        "talk_time": talk_time,
        "transcriber": transcriber,
        "detector": detector,
        "slides": slides,
        "websocket": websocket,
        "i18n": i18n_config,
    }
