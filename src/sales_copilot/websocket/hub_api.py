"""HTTP API routes for the WebSocket hub."""

from __future__ import annotations

import asyncio
import logging
import os
import signal as _signal_module
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from sales_copilot.auth.email_capture import (
    LeadRequest,
    LeadResponse,
    capture_lead,
    check_lead_rate_limit,
)
from sales_copilot.auth.feature_policy import get_feature_policy
from sales_copilot.auth.license_format import FEATURE_IDS, FREE_FEATURES, TIER_FEATURES
from sales_copilot.auth.startup_check import check_license_at_startup
from sales_copilot.core import i18n
from sales_copilot.core.autostart_monitor import get_active_monitor
from sales_copilot.core.config import WebSocketConfig, load_env, load_yaml
from sales_copilot.core.consent import consent_tier, consent_tracking_enabled
from sales_copilot.core.feedback_store import HintFeedbackRecord, append_hint_feedback
from sales_copilot.core.measurement_signals import record_detection_correction
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.sample_aha import SampleAhaPlayer, default_sample_path, default_speed
from sales_copilot.websocket import hub_core
from sales_copilot.websocket.hub_auth import get_hub_token, require_token

_logger = logging.getLogger(__name__)

router = APIRouter()


class PhaseRequest(BaseModel):
    phase: str


class SampleAhaRequest(BaseModel):
    speed: float | None = None


_sample_player: SampleAhaPlayer | None = None


@router.get("/api/status")
async def get_status() -> dict[str, str]:
    return {"state": hub_core.status_state()}


@router.get("/api/presets")
async def get_presets() -> JSONResponse:
    data = load_yaml(resolve_app_path("config/presets.yaml"))
    presets = data.get("presets", data)
    if isinstance(presets, list):
        result = [item for item in presets if isinstance(item, dict)]
        return JSONResponse(content=result)
    if isinstance(presets, dict):
        result = []
        for name, preset in presets.items():
            if not isinstance(preset, dict):
                continue
            result.append({"name": name, **preset})
        return JSONResponse(content=result)
    raise HTTPException(status_code=500, detail="Invalid presets format")


@router.get("/api/llm-models")
async def get_llm_models() -> JSONResponse:
    data = load_yaml(resolve_app_path("config/llm_models.yaml"))
    providers = data.get("providers")
    if not isinstance(providers, dict):
        raise HTTPException(status_code=500, detail="Invalid llm models format")
    result: dict[str, dict[str, Any]] = {}
    for provider_key, provider_config in providers.items():
        if not isinstance(provider_key, str) or not isinstance(provider_config, dict):
            continue
        label = provider_config.get("label", provider_key)
        models = provider_config.get("models", [])
        if not isinstance(models, list):
            models = []
        filtered_models = [model for model in models if isinstance(model, str) and model.strip()]
        result[provider_key] = {"label": str(label), "models": filtered_models}
    return JSONResponse(content={"providers": result})


@router.get("/api/config")
async def get_config() -> dict[str, Any]:
    load_env()
    ws_config = WebSocketConfig.from_env()
    return {
        "ws_host": ws_config.host,
        "ws_port": ws_config.port,
        "consent_tier": consent_tier(),
        "consent_tracking_enabled": consent_tracking_enabled(),
        # Single source of truth for the dashboard i18n mechanism (js/i18n.js):
        # mirrors the same LANGUAGE env var the backend catalog (core.i18n) reads.
        "language": i18n.configured_language(),
    }


@router.get("/api/v1/auth/session-token")
async def get_session_token(request: Request) -> JSONResponse:
    """Return the hub token to the same-origin localhost dashboard."""

    host = request.url.hostname or ""
    origin = request.headers.get("origin", "")
    if host not in {"localhost", "127.0.0.1"}:
        raise HTTPException(status_code=403, detail="Forbidden")
    if origin and origin not in hub_core._ALLOWED_WS_ORIGINS:
        raise HTTPException(status_code=403, detail="Forbidden")
    return JSONResponse({"token": get_hub_token()})


@router.post("/api/start-call", dependencies=[Depends(require_token)])
async def start_call_api(request: Request) -> Any:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    try:
        config = hub_core.extract_start_call_config(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    missing = hub_core.missing_entitlements(config)
    if missing:
        # Canonical entitlement gate: reject a Pro-only config before any capture
        # or module starts. The audio factory degrades to Free as a second layer.
        return JSONResponse(
            status_code=403,
            content={"error": "feature_required", "feature": missing[0]},
        )
    async with hub_core._config_lock:
        hub_core.apply_start_call(config)
    await hub_core.broadcast("config", {"type": "start_call", "config": config})
    return {"status": "ok"}


@router.post("/api/sample-aha/start", dependencies=[Depends(require_token)])
async def sample_aha_start_api(body: SampleAhaRequest | None = None) -> JSONResponse:
    """Start the curated sample-aha replay on top of the existing hub.

    Disables the real audio/transcription path and runs the detector + reports
    modules against a replayed transcript, so a first-time user sees pain points,
    objections and suggestions without configuring a microphone or LLM key.
    """
    global _sample_player
    if hub_core._call_active or (_sample_player is not None and _sample_player.is_alive):
        raise HTTPException(status_code=409, detail="Sample already playing or call active")

    sample_path = Path(default_sample_path())
    if not sample_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Sample not found: {sample_path}",
        )

    speed = body.speed if body is not None and body.speed is not None else default_speed()
    if speed <= 0:
        raise HTTPException(status_code=400, detail="speed must be positive")

    config = {
        "preset_name": "sales",
        "screen_mode": "single",
        "modules": {
            "talk_time": False,
            "transcript": False,
            "pain_points": True,
            "presentation": False,
            "post_call_report": True,
        },
        "llm": {"provider": "none", "model": ""},
        "transcript": {"backend": "whisper.cpp", "transcribe_self_live": False},
        "prospect": {"company": "Voorbeeld B.V.", "industry": "services"},
        "prospect_source": "blackhole",
    }

    try:
        validated = hub_core.extract_start_call_config({"config": config})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    missing = hub_core.missing_entitlements(validated)
    if missing:
        return JSONResponse(
            status_code=403,
            content={"error": "feature_required", "feature": missing[0]},
        )

    async with hub_core._config_lock:
        hub_core.apply_start_call(validated)
    await hub_core.broadcast("config", {"type": "start_call", "config": validated})

    ws_config = WebSocketConfig.from_env()
    _sample_player = SampleAhaPlayer(
        sample_path=sample_path,
        host=ws_config.host,
        port=ws_config.port,
        token=get_hub_token(),
        speed=speed,
    )
    _sample_player.start()

    return JSONResponse(
        {
            "status": "ok",
            "sample": sample_path.name,
            "speed": speed,
        }
    )


@router.post("/api/sample-aha/stop", dependencies=[Depends(require_token)])
async def sample_aha_stop_api() -> dict[str, str]:
    """Stop a running sample-aha replay and end the call."""
    global _sample_player
    if _sample_player is not None:
        _sample_player.stop()
        _sample_player = None
    async with hub_core._config_lock:
        hub_core.apply_end_call()
    await hub_core.broadcast("config", {"type": "end_call"})
    monitor = get_active_monitor()
    if monitor is not None:
        monitor.notify_call_ended()
    return {"status": "ok"}


@router.post("/api/end-call", dependencies=[Depends(require_token)])
async def end_call_api() -> dict[str, str]:
    async with hub_core._config_lock:
        hub_core.apply_end_call()
    await hub_core.broadcast("config", {"type": "end_call"})
    monitor = get_active_monitor()
    if monitor is not None:
        monitor.notify_call_ended()
    return {"status": "ok"}


@router.post("/api/autostart/confirm", dependencies=[Depends(require_token)])
async def autostart_confirm_api() -> dict[str, str]:
    """Explicit per-call consent tick for a pending runtime auto-arm prompt.

    This is the only path that turns a detected meeting-app into an actually
    armed (recording) session -- detection alone never starts a call.
    """
    monitor = get_active_monitor()
    if monitor is None or not monitor.confirm_consent():
        raise HTTPException(status_code=409, detail="No pending autostart consent prompt")
    return {"status": "ok"}


@router.post("/api/autostart/decline", dependencies=[Depends(require_token)])
async def autostart_decline_api() -> dict[str, str]:
    """Decline a pending runtime auto-arm prompt for this detected call."""
    monitor = get_active_monitor()
    if monitor is not None:
        monitor.decline_consent()
    return {"status": "ok"}


@router.post("/api/phase", dependencies=[Depends(require_token)])
async def set_phase_api(body: PhaseRequest) -> dict[str, str]:
    if body.phase not in {"discovery", "pitch", "closing"}:
        raise HTTPException(status_code=400, detail="Invalid phase")
    await hub_core.broadcast("phase", {"type": "phase_change", "phase": body.phase})
    return {"status": "ok"}


class DetectionFeedbackRequest(BaseModel):
    detection_type: str
    predicted_category: str
    corrected_category: str | None = None


class HintFeedbackRequest(BaseModel):
    hint: str
    feedback: str  # "up" or "down"
    context_utterance: str = ""
    context_utterances: list[str] = []
    timestamp_ms: int | None = None
    session_id: str = ""
    phase: str | None = None

    @field_validator("feedback")
    @classmethod
    def _validate_feedback(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"up", "down"}:
            raise ValueError("feedback must be 'up' or 'down'")
        return normalized


@router.post("/api/detection-feedback", dependencies=[Depends(require_token)])
async def detection_feedback_api(body: DetectionFeedbackRequest) -> dict[str, Any]:
    """Record a user correction on a pain-point or objection detection.

    The endpoint accepts the predicted (and optionally corrected) category but
    never a transcript or trigger phrase. When measurement opt-in is enabled the
    aggregated, PII-stripped signal is forwarded to the configured endpoint.
    """
    payload = record_detection_correction(
        detection_type=body.detection_type,
        predicted_category=body.predicted_category,
        corrected_category=body.corrected_category,
    )
    return {"status": "ok", "forwarded": payload is not None}


@router.post("/api/hint-feedback", dependencies=[Depends(require_token)])
async def hint_feedback_api(body: HintFeedbackRequest) -> dict[str, Any]:
    """Record a thumbs-up/down vote on a single coaching hint.

    The record is written to ``data/feedback/hints.ndjson`` and can be used as
    eval-set input for hint-quality, objection or near-miss analysis.
    """
    record = HintFeedbackRecord(
        hint=body.hint.strip(),
        feedback=body.feedback,
        context_utterance=body.context_utterance.strip(),
        context_utterances=[u.strip() for u in body.context_utterances if isinstance(u, str) and u.strip()],
        timestamp_ms=body.timestamp_ms,
        session_id=body.session_id.strip() or None,
        phase=body.phase,
        source="tester_dashboard",
    )
    path = append_hint_feedback(record)
    return {"status": "ok", "path": str(path)}


@router.post("/api/swap-speakers", dependencies=[Depends(require_token)])
async def swap_speakers_api() -> dict[str, str]:
    await hub_core.broadcast("config", {"type": "swap_speakers"})
    return {"status": "ok"}


@router.post("/api/v1/license/request", response_model=LeadResponse)
async def request_license(body: LeadRequest, request: Request) -> LeadResponse:
    """Email capture — issue a free license key and record lead in leads.ndjson.

    Unauthenticated by design (it issues free keys), so it is rate-limited per
    IP and the email length is capped in ``LeadRequest`` before any storage.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not check_lead_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many requests")
    return await capture_lead(body)


@router.get("/api/v1/license/status")
async def license_status() -> dict[str, str]:
    """Return current license status (grace / valid / expired / missing)."""
    status, message = check_license_at_startup()
    return {"status": status.value, "message": message}


@router.get("/api/v1/license/features")
async def license_features() -> dict[str, Any]:
    """Return the active tier and the feature-IDs it unlocks (read-only).

    No secret or key material is exposed — only "what may this install do".
    The dashboard uses this to badge and disable Pro-only controls so a Free
    user sees the feature exists but cannot activate a non-entitled mode.
    """
    policy = get_feature_policy()
    tier = policy.current_tier()
    return {
        "tier": tier,
        "features": sorted(TIER_FEATURES.get(tier, FREE_FEATURES)),
        "all_features": sorted(FEATURE_IDS),
    }


@router.post("/api/shutdown", dependencies=[Depends(require_token)])
async def shutdown_server() -> dict[str, str]:
    """Graceful shutdown: broadcast server_shutdown to all WS clients, then SIGTERM."""
    _logger.warning("Shutdown request received via /api/shutdown")

    try:
        await hub_core.broadcast(
            "system",
            {"type": "server_shutdown", "reason": "manual_stop_from_dashboard"},
        )
    except Exception:
        pass

    async def _delayed_exit() -> None:
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), _signal_module.SIGTERM)

    asyncio.create_task(_delayed_exit())
    return {"status": "shutdown_initiated"}
