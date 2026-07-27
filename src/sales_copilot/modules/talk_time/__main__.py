from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable

import websockets

from sales_copilot.audio.capture import (
    AudioConfig,
    DualAudioCapture,
    list_audio_devices,
    make_audio_warning_broadcaster,
    resolve_prospect_source,
)
from sales_copilot.core.config import TalkTimeConfig, WebSocketConfig, env, env_float, env_int, load_env
from sales_copilot.core.logging import configure_logging
from sales_copilot.modules.talk_time.publisher import TalkTimePublisher
from sales_copilot.modules.talk_time.tracker import TalkTimeTracker
from sales_copilot.modules.talk_time.vad import VADProcessor, load_silero_model
from sales_copilot.websocket.hub import broadcast, get_latest_config, run_hub
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)


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


def _print_banner(
    talk_time_config: TalkTimeConfig,
    ws_config: WebSocketConfig,
    audio_config: AudioConfig,
) -> None:
    logger.info("OnCue — Talk Time Module")
    logger.info("Rolling window: %ss", talk_time_config.rolling_window_seconds)
    logger.info("Monologue warning: %ss", talk_time_config.monologue_warning_seconds)
    logger.info(
        "Targets (self): discovery=%s pitch=%s closing=%s",
        f"{talk_time_config.discovery_target_self:.0%}",
        f"{talk_time_config.pitch_target_self:.0%}",
        f"{talk_time_config.closing_target_self:.0%}",
    )
    logger.info("WebSocket hub: ws://%s:%s", ws_config.host, ws_config.port)
    logger.info(
        "Audio: method=%s sample_rate=%s channels=%s target_process=%s",
        audio_config.capture_method,
        audio_config.sample_rate,
        audio_config.channels,
        audio_config.target_process or "(none)",
    )
    logger.info("Talk-time heartbeat interval: %sms", talk_time_config.talk_time_heartbeat_ms)
    logger.info(
        "Single-stream default speaker: %s",
        talk_time_config.single_stream_speaker_default,
    )


def _decode_payload(raw: str | bytes) -> object:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _single_stream_default_speaker(raw_speaker: str) -> str:
    speaker = raw_speaker.strip().lower()
    if speaker not in {"self", "prospect"}:
        logger.warning(
            "Invalid SINGLE_STREAM_SPEAKER_DEFAULT=%s, falling back to 'prospect'.",
            raw_speaker,
        )
        return "prospect"
    return speaker


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


async def _listen_config_events(
    ws_config: WebSocketConfig,
    on_start_call,
    on_end_call,
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
                    if event_type in {"start_call", "call_started"}:
                        logger.info("Received config event '%s'; activating talk-time session.", event_type)
                        on_start_call()
                    elif event_type in {"end_call", "call_ended"}:
                        logger.info(
                            "Received config event '%s'; deactivating talk-time session.",
                            event_type,
                        )
                        on_end_call()
        except asyncio.CancelledError:
            raise
        except Exception:
            if stop_event.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class _HeartbeatController:
    def __init__(
        self,
        tracker: TalkTimeTracker,
        publisher: TalkTimePublisher,
        *,
        now_ms: Callable[[], int],
        heartbeat_ms: int,
    ) -> None:
        self._tracker = tracker
        self._publisher = publisher
        self._now_ms = now_ms
        self._heartbeat_s = max(1, int(heartbeat_ms)) / 1000.0
        self._call_active = False

    def handle_start_call(self) -> None:
        self._call_active = True
        self._tracker.reset()
        self._tracker.mark_call_started(self._now_ms())
        logger.info("Talk-time heartbeat started.")

    def handle_end_call(self) -> None:
        self._call_active = False
        self._tracker.mark_call_ended()
        logger.info("Talk-time heartbeat stopped.")

    @property
    def call_active(self) -> bool:
        return self._call_active

    async def emit_snapshot(self) -> None:
        if not self._call_active:
            return
        now = self._now_ms()
        state = self._tracker.get_state(now)
        if state.call_duration_ms <= 0:
            return
        logger.info(
            "talk_time_snapshot call_duration_ms=%d rolling_self_pct=%.4f rolling_prospect_pct=%.4f",
            state.call_duration_ms,
            state.rolling_self_pct,
            state.rolling_prospect_pct,
        )
        logger.debug(
            "Heartbeat emit: call_duration_ms=%d rolling_self_pct=%.4f rolling_prospect_pct=%.4f",
            state.call_duration_ms,
            state.rolling_self_pct,
            state.rolling_prospect_pct,
        )
        await self._publisher.publish_snapshot(now)
        await self._publisher.publish_alert_if_needed(now)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.emit_snapshot()
            await asyncio.sleep(self._heartbeat_s)


async def _run_audio_loop(
    tracker: TalkTimeTracker,
    stop_event: asyncio.Event,
    *,
    primary_stream,
    system_stream,
    primary_vad: VADProcessor,
    vad_prospect: VADProcessor | None,
    on_speech_event: Callable[[], Awaitable[None]],
    should_publish: Callable[[], bool],
    poll_interval: float = 0.01,
) -> None:
    speech_event_count = 0
    while not stop_event.is_set():
        idle = True
        primary_chunk = primary_stream.read()
        if primary_chunk is not None:
            idle = False
            event = primary_vad.process_chunk(primary_chunk)
            if event is not None:
                duration = max(0, event.end_ms - event.start_ms)
                speech_event_count += 1
                if speech_event_count <= 5:
                    logger.info("Speech event: speaker=%s duration=%dms", event.speaker, duration)
                else:
                    logger.debug("Speech event: speaker=%s duration=%dms", event.speaker, duration)
                tracker.record_speech(event.speaker, event.start_ms, event.end_ms)
                if should_publish():
                    await on_speech_event()

        if system_stream is not None and vad_prospect is not None:
            system_chunk = system_stream.read()
            if system_chunk is not None:
                idle = False
                event = vad_prospect.process_chunk(system_chunk)
                if event is not None:
                    duration = max(0, event.end_ms - event.start_ms)
                    speech_event_count += 1
                    if speech_event_count <= 5:
                        logger.info("Speech event: speaker=%s duration=%dms", event.speaker, duration)
                    else:
                        logger.debug("Speech event: speaker=%s duration=%dms", event.speaker, duration)
                    tracker.record_speech(event.speaker, event.start_ms, event.end_ms)
                    if should_publish():
                        await on_speech_event()

        if idle:
            await asyncio.sleep(poll_interval)


async def main(
    *,
    talk_time_config: TalkTimeConfig | None = None,
    stop_event: asyncio.Event | None = None,
    register_signals: bool = True,
) -> None:
    load_env()
    configure_logging()
    talk_time_config = talk_time_config or TalkTimeConfig.from_env()
    ws_config = WebSocketConfig.from_env()
    audio_config = _load_audio_config()

    _print_banner(talk_time_config, ws_config, audio_config)

    import socket as _socket
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        _probe.connect((ws_config.host, ws_config.port))
        _probe.close()
        logger.info("WebSocket hub already running on %s:%s", ws_config.host, ws_config.port)
    except OSError:
        _probe.close()
        hub_thread = threading.Thread(
            target=run_hub,
            args=(ws_config.host, ws_config.port),
            daemon=True,
            name="ws-hub",
        )
        hub_thread.start()
        logger.info("WebSocket hub started.")

    loop = asyncio.get_running_loop()

    async def _broadcast_audio_warning(payload: dict[str, object]) -> None:
        await broadcast("coaching", payload)

    audio_warning_cb = make_audio_warning_broadcaster(loop, _broadcast_audio_warning)

    capture = DualAudioCapture(audio_config)
    mic_stream, system_stream = capture.create(on_warning=audio_warning_cb)
    single_stream_fallback = _use_blackhole_single_stream_fallback(audio_config)
    single_stream_speaker_default = _single_stream_default_speaker(
        talk_time_config.single_stream_speaker_default
    )
    if single_stream_fallback:
        system_stream = None
        logger.info(
            "BlackHole-only input detected, mapping single stream to speaker '%s'.",
            single_stream_speaker_default,
        )

    logger.info("Loading Silero VAD model...")
    model = load_silero_model()
    primary_speaker = "self" if not single_stream_fallback else single_stream_speaker_default
    primary_positive = (
        talk_time_config.vad_positive_threshold_self
        if primary_speaker == "self"
        else talk_time_config.vad_positive_threshold_prospect
    )
    primary_negative = (
        talk_time_config.vad_negative_threshold_self
        if primary_speaker == "self"
        else talk_time_config.vad_negative_threshold_prospect
    )
    primary_vad = VADProcessor(
        model,
        primary_speaker,
        sample_rate=audio_config.sample_rate,
        positive_threshold=primary_positive,
        negative_threshold=primary_negative,
        debounce_ms=talk_time_config.vad_debounce_ms,
    )
    vad_prospect = None
    if system_stream is not None:
        vad_prospect = VADProcessor(
            model,
            "prospect",
            sample_rate=audio_config.sample_rate,
            positive_threshold=talk_time_config.vad_positive_threshold_prospect,
            negative_threshold=talk_time_config.vad_negative_threshold_prospect,
            debounce_ms=talk_time_config.vad_debounce_ms,
        )

    tracker = TalkTimeTracker(talk_time_config)
    module_start = time.monotonic()

    def now_ms() -> int:
        return int((time.monotonic() - module_start) * 1000)

    stop_event = stop_event or asyncio.Event()

    def _request_shutdown() -> None:
        if not stop_event.is_set():
            logger.info("Shutdown requested. Stopping...")
            stop_event.set()

    if register_signals:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: _request_shutdown())

    mic_stream.start()
    if system_stream is not None:
        system_stream.start()

    async with TalkTimePublisher(
        tracker,
        config=talk_time_config,
        ws_config=ws_config,
        now_ms=now_ms,
    ) as publisher:
        logger.info("Talk-time publisher connected.")
        heartbeat = _HeartbeatController(
            tracker,
            publisher,
            now_ms=now_ms,
            heartbeat_ms=talk_time_config.talk_time_heartbeat_ms,
        )
        config_listener_task = asyncio.create_task(
            _listen_config_events(
                ws_config,
                heartbeat.handle_start_call,
                heartbeat.handle_end_call,
                stop_event,
            )
        )
        heartbeat_task = asyncio.create_task(heartbeat.run(stop_event))
        try:
            await _run_audio_loop(
                tracker,
                stop_event,
                primary_stream=mic_stream,
                system_stream=system_stream,
                primary_vad=primary_vad,
                vad_prospect=vad_prospect,
                on_speech_event=heartbeat.emit_snapshot,
                should_publish=lambda: heartbeat.call_active,
            )
        finally:
            heartbeat_task.cancel()
            config_listener_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            try:
                await config_listener_task
            except asyncio.CancelledError:
                pass

    mic_stream.stop()
    if system_stream is not None:
        system_stream.stop()

    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
