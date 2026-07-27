"""Core WebSocket pub/sub behavior for the hub."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from sales_copilot.auth.feature_policy import (
    FEATURE_CALLTAP,
    FEATURE_LIVE_COACHING,
    FEATURE_SCRIPT_TRACKING,
    FeaturePolicy,
    get_feature_policy,
)
from sales_copilot.core.measurement_signals import record_install_attribution
from sales_copilot.websocket.hub_auth import is_valid_token, websocket_token

logger = logging.getLogger(__name__)
router = APIRouter()

_logger = logging.getLogger(__name__)
_feature_policy: FeaturePolicy | None = None


def _get_feature_policy() -> FeaturePolicy:
    global _feature_policy
    if _feature_policy is None:
        _feature_policy = get_feature_policy()
    return _feature_policy


def _coaching_enabled() -> bool:
    return _get_feature_policy().allows(FEATURE_LIVE_COACHING)


def _script_tracking_enabled() -> bool:
    return _get_feature_policy().allows(FEATURE_SCRIPT_TRACKING)


_subscribers: dict[str, set[WebSocket]] = defaultdict(set)
_subscribers_lock = asyncio.Lock()
_config_lock = asyncio.Lock()

_config_ready = threading.Event()
_end_call_event = threading.Event()
_latest_config: dict[str, Any] | None = None
_call_active = False

# Bounded rolling buffer of transcript events seen during the active call.
# Lets late subscribers (e.g. session-tracker) catch up on transcripts that
# arrived before they joined /ws/transcript. Reset on call boundaries.
_TRANSCRIPT_BUFFER_MAX = 1000
_transcript_buffer: list[Any] = []
_ALLOWED_PRESETS = frozenset({"sales", "coach", "recruitment"})

# Per-message send bound. A back-pressured/wedged subscriber stalls on send
# without ever raising, which the _safe_send try/except cannot catch on its own.
# Wrapping the send in wait_for converts that stall into a TimeoutError so the
# slow subscriber is discarded by the existing _safe_send path instead of
# blocking the whole broadcast.
_SEND_TIMEOUT_S = 5.0

# Per-close bound for close_all_connections. A wedged subscriber socket that
# never completes its close handshake would otherwise stall shutdown; the bound
# converts that into a TimeoutError isolated by return_exceptions=True.
_CLOSE_TIMEOUT_S = 2.0


async def _send(ws: WebSocket, data: Any) -> None:
    if isinstance(data, str):
        await asyncio.wait_for(ws.send_text(data), timeout=_SEND_TIMEOUT_S)
        return
    await asyncio.wait_for(ws.send_json(data), timeout=_SEND_TIMEOUT_S)


async def _broadcast_to_channel(channel: str, data: Any, *, skip: WebSocket | None = None) -> None:
    if channel == "coaching" and not _coaching_enabled():
        _logger.info("Suppressing coaching broadcast: live coaching is not entitled.")
        return
    if channel == "script-tracking" and not _script_tracking_enabled():
        _logger.info("Suppressing script-tracking broadcast: not entitled.")
        return
    if channel == "transcript" and _call_active and _is_transcript_event(data):
        _transcript_buffer.append(data)
        if len(_transcript_buffer) > _TRANSCRIPT_BUFFER_MAX:
            del _transcript_buffer[: len(_transcript_buffer) - _TRANSCRIPT_BUFFER_MAX]

    async with _subscribers_lock:
        recipients = list(_subscribers.get(channel, set()))

    async def _safe_send(recipient: WebSocket) -> None:
        if skip is not None and recipient is skip:
            return
        try:
            await _send(recipient, data)
        except Exception:
            async with _subscribers_lock:
                _subscribers[channel].discard(recipient)
                if not _subscribers[channel]:
                    _subscribers.pop(channel, None)

    await asyncio.gather(*(_safe_send(recipient) for recipient in recipients))


async def broadcast(channel: str, data: Any) -> None:
    """Programmatically publish `data` to all subscribers of `channel`."""

    await _broadcast_to_channel(channel, data)


async def close_all_connections() -> None:
    """Close all active websocket subscribers and clear channels."""

    async with _subscribers_lock:
        recipients = [ws for channel in _subscribers.values() for ws in channel]
        _subscribers.clear()

    async def _close(ws: WebSocket) -> None:
        await asyncio.wait_for(ws.close(), timeout=_CLOSE_TIMEOUT_S)

    await asyncio.gather(*(_close(ws) for ws in recipients), return_exceptions=True)


def _decode_incoming(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def status_state() -> str:
    if _call_active:
        return "call_active"
    if _config_ready.is_set():
        return "idle"
    return "waiting_for_config"


def apply_start_call(config: dict[str, Any] | None) -> None:
    global _latest_config, _call_active
    _latest_config = config or {}
    _call_active = True
    _transcript_buffer.clear()
    _config_ready.set()
    _end_call_event.clear()
    # Capture install/attribution code in the local audit trail (only when set).
    record_install_attribution(source="intake")


def apply_end_call() -> None:
    global _call_active, _latest_config
    _call_active = False
    _latest_config = None
    _transcript_buffer.clear()
    _config_ready.clear()
    _end_call_event.set()


def extract_start_call_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept both `{config: {...}}` and flat config payloads.

    The ``install_code`` field is required in the intake but may be empty; an
    empty value is accepted with a warning so attribution remains optional while
    the field is always present in the schema.
    """

    config = payload.get("config")
    if config is None:
        config = {key: value for key, value in payload.items() if key != "type"}
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    preset_name = config.get("preset_name", config.get("preset"))
    if preset_name is not None and (
        not isinstance(preset_name, str) or preset_name.strip().lower() not in _ALLOWED_PRESETS
    ):
        raise ValueError(f"preset must be one of: {sorted(_ALLOWED_PRESETS)}")

    install_code = config.get("install_code", "")
    if install_code is None:
        install_code = ""
        logger.warning("start-call intake missing install_code; defaulting to empty string")
    if not isinstance(install_code, str):
        raise ValueError("install_code must be a string")
    config["install_code"] = install_code
    if install_code.strip() == "":
        logger.warning("start-call intake received an empty install_code")

    return config


def required_features_for_config(config: dict[str, Any]) -> set[str]:
    """Map a start-call config to the Pro feature-IDs it needs.

    Currently only telephony capture (``prospect_source == "audiotee_call"``)
    is Pro-gated. Extend this mapping as new paid capture/module options land.
    """
    required: set[str] = set()
    if str(config.get("prospect_source", "")).strip().lower() == "audiotee_call":
        required.add(FEATURE_CALLTAP)
    return required


def missing_entitlements(
    config: dict[str, Any], *, policy: FeaturePolicy | None = None
) -> list[str]:
    """Return the Pro feature-IDs this config needs but the license lacks.

    This is the canonical entitlement gate for start-call. The HTTP start-call
    handler rejects with ``feature_required`` when this is non-empty, before any
    capture or module starts. The audio factory still degrades to a Free stream
    as a second layer if this gate is ever bypassed (defense-in-depth).
    """
    policy = policy or get_feature_policy()
    return sorted(
        feature
        for feature in required_features_for_config(config)
        if not policy.allows(feature)
    )


def get_latest_config() -> dict[str, Any] | None:
    return _latest_config


def reset_config_state() -> None:
    global _latest_config, _call_active
    _latest_config = None
    _call_active = False
    _transcript_buffer.clear()
    _config_ready.clear()
    _end_call_event.clear()


def _is_transcript_event(data: Any) -> bool:
    if isinstance(data, dict):
        return data.get("type") == "transcript"
    return False


async def wait_for_start_call(timeout: float | None = None) -> dict[str, Any] | None:
    loop = asyncio.get_event_loop()
    ready = await loop.run_in_executor(None, _config_ready.wait, timeout)
    if not ready:
        return None
    return _latest_config or {}


async def wait_for_end_call(timeout: float | None = None) -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _end_call_event.wait, timeout)


_ALLOWED_WS_ORIGINS = frozenset(
    {
        "http://localhost:8760",
        "http://127.0.0.1:8760",
    }
)
_ALLOWED_CHANNELS = frozenset(
    {
        "buying-signals",
        "coaching",
        "config",
        "objections",
        "pain-points",
        "phase",
        "script-tracking",
        "slide-control",
        "suggestions",
        "summary",
        "system",
        "talk-time",
        "transcript",
        "wizard",
    }
)


@router.websocket("/ws/{channel}")
async def ws_channel(websocket: WebSocket, channel: str) -> None:
    origin = websocket.headers.get("origin", "")
    token = websocket_token(websocket.headers.get("authorization", ""))
    token_valid = is_valid_token(token)
    browser_subscriber = origin in _ALLOWED_WS_ORIGINS
    if channel not in _ALLOWED_CHANNELS or not (token_valid or browser_subscriber):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    async with _subscribers_lock:
        _subscribers[channel].add(websocket)

    # Sticky replay: a module that connects to /ws/config after the
    # operator already clicked Start Call would otherwise miss the
    # broadcast and never warm up. Re-deliver the current call state so
    # late subscribers (transcriber, talk-time, detector) can activate.
    if channel == "config" and _call_active:
        try:
            await _send(
                websocket,
                {"type": "call_started", "config": _latest_config or {}},
            )
        except Exception:
            async with _subscribers_lock:
                _subscribers[channel].discard(websocket)
                if not _subscribers[channel]:
                    _subscribers.pop(channel, None)
            return

    # Sticky replay for transcripts: the post-call session tracker
    # subscribes to /ws/transcript after start_call has fired and would
    # otherwise miss every transcript emitted before it joined. Replay the
    # rolling buffer so late subscribers reconstruct the full transcript.
    if channel == "transcript" and _call_active and _transcript_buffer:
        try:
            for buffered in list(_transcript_buffer):
                await _send(websocket, buffered)
        except Exception:
            async with _subscribers_lock:
                _subscribers[channel].discard(websocket)
                if not _subscribers[channel]:
                    _subscribers.pop(channel, None)
            return

    try:
        while True:
            raw = await websocket.receive_text()
            if not token_valid:
                await websocket.close(code=1008, reason="Publishing is not allowed")
                return
            payload = _decode_incoming(raw)
            if channel == "config" and (
                not isinstance(payload, dict)
                or payload.get("type") not in {"call_started", "call_ended", "swap_speakers"}
            ):
                await websocket.close(code=1008, reason="Config state is HTTP-only")
                return
            await _broadcast_to_channel(channel, payload, skip=websocket)
    except WebSocketDisconnect:
        pass
    finally:
        async with _subscribers_lock:
            _subscribers[channel].discard(websocket)
            if not _subscribers[channel]:
                _subscribers.pop(channel, None)
