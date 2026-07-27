from __future__ import annotations

import asyncio
import json
import logging
import signal
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import websockets

from sales_copilot.audio.capture import (
    AudioConfig,
    AudioStream,
    DualAudioCapture,
    check_streams_liveness,
    list_audio_devices,
    make_audio_warning_broadcaster,
    resolve_prospect_source,
)
from sales_copilot.core.config import (
    TalkTimeConfig,
    TranscriberConfig,
    WebSocketConfig,
    env,
    env_float,
    env_int,
    load_env,
)
from sales_copilot.core.logging import configure_logging
from sales_copilot.modules.transcriber.audio_bufferer import AudioBufferer, TurnState
from sales_copilot.modules.transcriber.backends import create_backend
from sales_copilot.modules.transcriber.engine import swap_speaker
from sales_copilot.modules.transcriber.inference_queue import SharedInferenceQueue
from sales_copilot.modules.transcriber.inference_worker import InferenceWorker
from sales_copilot.modules.transcriber.whisper_direct import DirectWhisperEngine
from sales_copilot.websocket.hub import broadcast, get_latest_config, run_hub
from sales_copilot.websocket.hub_auth import channel_ws_url

TranscriptionEngine = Literal["direct", "whisper.cpp"]
logger = logging.getLogger(__name__)


def _hub_available(host: str, port: int, *, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _print_banner(config: TranscriberConfig, ws_config: WebSocketConfig, engine: TranscriptionEngine) -> None:
    logger.info("OnCue — Transcriber Module")
    logger.info("Transcription engine: %s | backend: %s (%s)", engine, config.backend, config.model)
    logger.info(
        "Language: %s | diarization fallback: %s",
        config.language,
        config.diarization_backend,
    )
    logger.info("Whisper WS ports: mic=%s system=%s", config.port, config.system_port)
    logger.info("WebSocket hub: ws://%s:%s", ws_config.host, ws_config.port)
    logger.info(
        "Self stream live: %s (set TRANSCRIBE_SELF_LIVE in .env to override)",
        config.transcribe_self_live,
    )


def _load_transcription_engine() -> TranscriptionEngine:
    engine = (env("TRANSCRIPTION_ENGINE", "direct") or "direct").strip().lower()
    if engine not in {"direct", "whisper.cpp"}:
        raise ValueError("TRANSCRIPTION_ENGINE must be 'direct' or 'whisper.cpp'")
    return engine


def _load_audio_config() -> AudioConfig:
    platform_default = "wasapi" if sys.platform == "win32" else "audiotee"
    capture_method = env("AUDIO_CAPTURE_METHOD", platform_default) or platform_default
    if capture_method not in {"audiotee", "mic", "blackhole", "wasapi", "replay"}:
        raise ValueError("AUDIO_CAPTURE_METHOD must be 'audiotee', 'mic', 'blackhole', 'wasapi', or 'replay'")

    prospect_source = resolve_prospect_source(
        env("PROSPECT_SOURCE", "blackhole"),
        get_latest_config(),
    )

    mic_device_raw = env("MIC_INPUT_DEVICE")
    mic_device = int(mic_device_raw) if mic_device_raw and mic_device_raw.isdigit() else mic_device_raw
    return AudioConfig(
        sample_rate=env_int("AUDIO_SAMPLE_RATE", 16000) or 16000,
        channels=env_int("AUDIO_CHANNELS", 1) or 1,
        chunk_size=env_int("AUDIO_CHUNK_SIZE", 512) or 512,
        mic_device=mic_device,
        capture_method=capture_method,
        target_process=env("TARGET_PROCESS_NAME"),
        audiotee_path=env("AUDIOTEE_BINARY_PATH", "./bin/audiotee") or "./bin/audiotee",
        prospect_source=prospect_source,
        call_process_name=env("CALL_PROCESS_NAME", "avconferenced") or "avconferenced",
        replay_session_dir=env("REPLAY_SESSION_DIR"),
        replay_speed=env_float("REPLAY_SPEED", 1.0) or 1.0,
    )


def _single_stream_default_speaker(raw_speaker: str) -> str:
    speaker = raw_speaker.strip().lower()
    if speaker not in {"self", "prospect"}:
        logger.warning(
            "Invalid SINGLE_STREAM_SPEAKER_DEFAULT=%s, falling back to 'prospect'.",
            raw_speaker,
        )
        return "prospect"
    return speaker


def _build_audio_streams(
    audio_config: AudioConfig,
    single_stream_speaker_default: str,
    *,
    on_warning: Callable[[dict[str, object]], None] | None = None,
) -> tuple[list[AudioStream], tuple[str | None, ...]]:
    mic_stream, system_stream = DualAudioCapture(audio_config).create(on_warning=on_warning)
    single_stream_fallback = _use_blackhole_single_stream_fallback(audio_config)
    fallback_speaker = _single_stream_default_speaker(single_stream_speaker_default)
    if single_stream_fallback:
        system_stream = None

    streams: list[AudioStream] = [mic_stream]
    stream_speakers: list[str | None] = [fallback_speaker if single_stream_fallback else "self"]
    if system_stream is not None:
        streams.append(system_stream)
        stream_speakers.append("prospect")
    return streams, tuple(stream_speakers)


def _use_blackhole_single_stream_fallback(audio_config: AudioConfig) -> bool:
    if audio_config.prospect_source == "audiotee_call":
        return False
    if audio_config.capture_method != "blackhole":
        return False
    try:
        input_devices = [
            device
            for device in list_audio_devices()
            if int(device.get("max_input_channels", 0) or 0) > 0
        ]
    except Exception:
        return False

    if len(input_devices) != 1:
        return False
    only_name = str(input_devices[0].get("name", "")).lower()
    return "blackhole" in only_name


def _speaker_vad_params(talk_time_config: TalkTimeConfig, *, is_self: bool) -> tuple[float, float, float]:
    """Per-speaker (vad_enter, vad_exit, min_rms), reusing the existing VAD_* env vars.

    Self and prospect streams differ in mic gain and background noise, so
    TalkTimeConfig already carries separately-tuned thresholds per speaker
    (VAD_POSITIVE/NEGATIVE_THRESHOLD_SELF/PROSPECT, VAD_MIN_RMS_SELF/PROSPECT).
    The bufferer's continuous-buffer hysteresis reuses those instead of a new
    set of transcriber-specific VAD env vars.
    """
    if is_self:
        return (
            talk_time_config.vad_positive_threshold_self,
            talk_time_config.vad_negative_threshold_self,
            talk_time_config.vad_min_rms_self,
        )
    return (
        talk_time_config.vad_positive_threshold_prospect,
        talk_time_config.vad_negative_threshold_prospect,
        talk_time_config.vad_min_rms_prospect,
    )


def _backend_config(transcription_engine: TranscriptionEngine, config: TranscriberConfig) -> dict[str, object]:
    if transcription_engine == "whisper.cpp":
        return {
            "backend": "whisper.cpp",
            "language": config.language,
            "whisper_cpp_binary": config.whisper_cpp_binary,
            "whisper_cpp_model_path": config.whisper_cpp_model_path,
            "whisper_cpp_threads": config.whisper_cpp_threads,
            "whisper_cpp_server_binary": config.whisper_cpp_server_binary,
        }
    return {
        "backend": config.backend,
        "language": config.language,
    }


async def _listen_config_events(
    ws_config: WebSocketConfig,
    on_swap: Callable[[], None],
    on_start_call: Callable[[], Awaitable[None]],
    stop_event: asyncio.Event,
) -> None:
    config_url = channel_ws_url(ws_config, "config")
    backoff = 1.0
    while not stop_event.is_set():
        try:
            async with websockets.connect(config_url) as ws:
                backoff = 1.0
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    except TimeoutError:
                        continue
                    payload = _decode_payload(raw)
                    if not isinstance(payload, dict):
                        continue
                    event_type = payload.get("type")
                    if event_type == "swap_speakers":
                        on_swap()
                        logger.info("Speaker mapping swapped.")
                    if event_type in ("start_call", "call_started"):
                        logger.info("Transcriber received %s event", event_type)
                        await on_start_call()
        except asyncio.CancelledError:
            raise
        except Exception:
            if stop_event.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _decode_payload(raw: str | bytes) -> object:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _swap_direct_speakers(engines: list[DirectWhisperEngine]) -> None:
    for engine in engines:
        engine.set_speaker(swap_speaker(engine.speaker))


async def main(
    *,
    transcriber_config: TranscriberConfig | None = None,
    stop_event: asyncio.Event | None = None,
    register_signals: bool = True,
) -> None:
    load_env()
    configure_logging()
    config = transcriber_config or TranscriberConfig.from_env()
    ws_config = WebSocketConfig.from_env()
    talk_time_config = TalkTimeConfig.from_env()
    audio_config = _load_audio_config()
    transcription_engine = _load_transcription_engine()

    _print_banner(config, ws_config, transcription_engine)

    if _hub_available(ws_config.host, ws_config.port):
        logger.info("WebSocket hub already running.")
    else:
        hub_thread = threading.Thread(
            target=run_hub,
            args=(ws_config.host, ws_config.port),
            daemon=True,
            name="ws-hub",
        )
        hub_thread.start()
        logger.info("WebSocket hub started.")
        time.sleep(0.2)

    loop = asyncio.get_running_loop()

    async def _broadcast_audio_warning(payload: dict[str, object]) -> None:
        await broadcast("coaching", payload)

    audio_warning_cb = make_audio_warning_broadcaster(loop, _broadcast_audio_warning)

    streams, stream_speakers = _build_audio_streams(
        audio_config,
        config.single_stream_speaker_default,
        on_warning=audio_warning_cb,
    )
    stop_event = stop_event or asyncio.Event()
    start_event = asyncio.Event()
    warmup_lock = asyncio.Lock()

    direct_engines: list[DirectWhisperEngine] = []
    run_tasks: list[asyncio.Task[None]] = []

    def _request_shutdown() -> None:
        if not stop_event.is_set():
            logger.info("Shutdown requested. Stopping...")
            stop_event.set()

    if register_signals:
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: _request_shutdown())

    if len(stream_speakers) == 1 and stream_speakers[0] is not None:
        logger.info(
            "BlackHole-only input detected, mapping single stream to speaker '%s'.",
            stream_speakers[0],
        )

    backend_config = _backend_config(transcription_engine, config)

    async def _emit_system_status(payload: dict[str, object]) -> None:
        try:
            await broadcast("coaching", payload)
        except Exception as exc:
            logger.warning("Failed to emit system status event: %s", exc)

    liveness_checked = {"done": False}

    def _schedule_liveness_check() -> None:
        if liveness_checked["done"]:
            return
        liveness_checked["done"] = True
        streams_and_labels = [
            (stream, label) for stream, label in zip(streams, stream_speakers, strict=False) if label
        ]
        if not streams_and_labels:
            return

        async def _broadcast_warning(payload: dict[str, object]) -> None:
            await broadcast("coaching", payload)

        asyncio.create_task(check_streams_liveness(streams_and_labels, _broadcast_warning))

    if config.shared_queue_enabled:
        shared_backend = create_backend(backend_config)
        queue = SharedInferenceQueue(max_size=config.queue_max_size)
        worker = InferenceWorker(backend=shared_backend, queue=queue, ws_config=ws_config)

        mic_priority = 1 if config.self_priority == "low" else 0
        prospect_priority = 0

        # One shared TurnState across both bufferers: they run as coroutines on this
        # same asyncio loop, so a cross-stream turn hand-off (the other speaker taking
        # the floor) can flush a segment without any thread/lock coordination.
        turn_state = TurnState()

        bufferers: list[AudioBufferer] = []
        for stream, speaker in zip(streams, stream_speakers, strict=False):
            is_self = speaker == "self"
            priority = mic_priority if is_self else prospect_priority
            vad_enter, vad_exit, min_rms = _speaker_vad_params(talk_time_config, is_self=is_self)
            bufferers.append(
                AudioBufferer(
                    audio_stream=stream,
                    queue=queue,
                    priority=priority,
                    speaker=speaker or "prospect",
                    sample_rate=audio_config.sample_rate,
                    max_buffer_seconds=config.max_buffer_seconds,
                    max_buffer_hard_seconds=config.max_buffer_hard_seconds,
                    silence_gap_seconds=config.silence_gap_seconds,
                    min_segment_seconds=config.min_segment_seconds,
                    long_silence_escape_seconds=config.long_silence_escape_seconds,
                    pre_roll_ms=config.pre_roll_ms,
                    hangover_ms=config.hangover_ms,
                    vad_enter=vad_enter,
                    vad_exit=vad_exit,
                    min_rms=min_rms,
                    turn_backchannel_guard_ms=config.turn_backchannel_guard_ms,
                    transcribe_live=config.transcribe_self_live if is_self else True,
                    turn_state=turn_state,
                )
            )

        async def _warmup_backends_on_start_call() -> None:
            if start_event.is_set():
                return
            async with warmup_lock:
                if start_event.is_set():
                    return
                await _emit_system_status(
                    {
                        "type": "system_status",
                        "state": "warming_up",
                        "message": "AI opwarmen...",
                    }
                )
                await shared_backend.warmup()
                await _emit_system_status({"type": "system_status", "state": "ready"})
                start_event.set()
                _schedule_liveness_check()

        if config.eager_warmup:
            logger.info("Eager warmup enabled, pre-loading Whisper model...")
            await _warmup_backends_on_start_call()

        logger.info("Transcriber engine ready (%s, shared-queue mode).", transcription_engine)
        run_tasks = [
            asyncio.create_task(worker.run(stop_event)),
            *[asyncio.create_task(buf.run(stop_event, start_event=start_event)) for buf in bufferers],
        ]
        config_listener_task = asyncio.create_task(
            _listen_config_events(
                ws_config,
                lambda: None,
                _warmup_backends_on_start_call,
                stop_event,
            )
        )
    else:
        for stream, speaker in zip(streams, stream_speakers, strict=False):
            direct_engines.append(
                DirectWhisperEngine(
                    audio_stream=stream,
                    ws_config=ws_config,
                    speaker=speaker,
                    backend=create_backend(backend_config),
                    sample_rate=audio_config.sample_rate,
                    max_buffer_seconds=config.max_buffer_seconds,
                    silence_gap_seconds=config.silence_gap_seconds,
                )
            )

        async def _warmup_backends_on_start_call() -> None:
            if start_event.is_set():
                return
            async with warmup_lock:
                if start_event.is_set():
                    return

                await _emit_system_status(
                    {
                        "type": "system_status",
                        "state": "warming_up",
                        "message": "AI opwarmen...",
                    }
                )

                for engine in direct_engines:
                    if engine.backend is None:
                        continue
                    await engine.backend.warmup()

                await _emit_system_status({"type": "system_status", "state": "ready"})
                start_event.set()
                _schedule_liveness_check()

        if config.eager_warmup:
            logger.info("Eager warmup enabled, pre-loading Whisper model...")
            await _warmup_backends_on_start_call()

        logger.info("Transcriber engine ready (%s).", transcription_engine)
        run_tasks = [
            asyncio.create_task(direct.run(stop_event, start_event=start_event)) for direct in direct_engines
        ]
        config_listener_task = asyncio.create_task(
            _listen_config_events(
                ws_config,
                lambda: _swap_direct_speakers(direct_engines),
                _warmup_backends_on_start_call,
                stop_event,
            )
        )

    logger.info(
        "Audio transcriber started (%s, engine=%s, streams=%s).",
        audio_config.capture_method,
        transcription_engine,
        len(run_tasks),
    )

    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
    finally:
        for task in run_tasks:
            task.cancel()
        config_listener_task.cancel()
        for task in run_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await config_listener_task
        except asyncio.CancelledError:
            pass

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
