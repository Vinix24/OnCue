"""HTTP API for the browser-first permissions wizard.

This module exposes the W1/W2 steps-as-data over HTTP so the browser wizard can
render, run, and resume the same checklist as the CLI without duplicating step
definitions.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from sales_copilot.auth.feature_policy import get_feature_policy
from sales_copilot.core.config import load_env
from sales_copilot.core.env_writer import read_env_value, set_env_var
from sales_copilot.core.profile_docs import (
    MAX_PROFILE_CHARS,
    read_profile_raw,
    write_profile_document,
)
from sales_copilot.websocket.hub_auth import require_token
from sales_copilot.wizard.cli import _W1_STEP_IDS, get_steps
from sales_copilot.wizard.detectors import provider_key_env
from sales_copilot.wizard.meter import websocket_emitter
from sales_copilot.wizard.serialize import get_step_descriptors
from sales_copilot.wizard.state import WizardState, load_state, save_state
from sales_copilot.wizard.steps import StepResult

router = APIRouter()

_LICENSE_ENV = "SALES_COPILOT_LICENSE"
# Below this a pasted value is almost certainly a paste error, not a real key.
_MIN_PROVIDER_KEY_LEN = 8
_MIN_LICENSE_LEN = 8
# Defensive ceiling on a saved profile doc -- generous for a hand-written
# "what I already know / what I sell / my method" doc; INSIGHT_PROFILE_MAX_CHARS
# (default 4000) is the separate, much smaller cap applied when the profile is
# loaded into a deep-lane prompt (core/profile_docs.py::load_profile_context).
_MAX_PROFILE_INPUT_CHARS = 200_000


def _result_to_dict(result: StepResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "message": result.message,
        "details": result.details,
    }


def _state_to_dict(state: WizardState) -> dict[str, Any]:
    return {
        "completed": state.completed,
        "results": state.results,
        "env_written": state.env_written,
    }


@router.get("/api/v1/wizard/steps")
async def get_wizard_steps() -> dict[str, Any]:
    """Return the ordered step list with license-gating metadata."""

    policy = get_feature_policy()
    return {
        "tier": policy.current_tier(),
        "steps": get_step_descriptors(policy),
    }


@router.get("/api/v1/wizard/state")
async def get_wizard_state() -> dict[str, Any]:
    """Return the persisted wizard resume state."""

    return _state_to_dict(load_state())


@router.post("/api/v1/wizard/restart", dependencies=[Depends(require_token)])
async def restart_wizard() -> dict[str, Any]:
    """Clear persisted wizard state and return a fresh state."""

    state = WizardState()
    save_state(state)
    return _state_to_dict(state)


def _find_step(step_id: str) -> Any:
    """Look up a step object by id from the active step list."""

    for step in get_steps():
        if step.step_id == step_id:
            return step
    return None


def _run_step_sync(step_id: str, loop: asyncio.AbstractEventLoop | None = None) -> StepResult:
    """Run a single wizard step synchronously, persisting the outcome.

    This runs on a worker thread so the hub event loop stays responsive.
    ``loop`` is the hub event loop; it is forwarded to the WebSocket meter
    emitter so dBFS samples can be broadcast without blocking the worker.
    """

    step = _find_step(step_id)
    if step is None:
        return StepResult(
            ok=False,
            message=f"Onbekende wizard stap '{step_id}'.",
            details={"error": "unknown_step"},
        )

    state = load_state()

    if step_id == "doctor":
        step_list = get_steps()
        w1_steps = [s.step_id for s in step_list if s.step_id in _W1_STEP_IDS]
        detectors = {s.step_id: s.auto_detect for s in step_list if s.step_id in _W1_STEP_IDS}
        prior_results = {
            sid: StepResult(
                ok=res.get("ok", False),
                message=res.get("message", ""),
                details=res.get("details", {}),
            )
            for sid, res in state.results.items()
        }
        result = step.auto_detect(prior_results, steps=w1_steps, detectors=detectors)
    else:
        emitter = None
        if loop is not None and step.needs_meter and step.proof is not None:
            emitter = websocket_emitter(loop)
        result = step.run(emitter=emitter)

    state.mark_completed(step_id, _result_to_dict(result))
    save_state(state)
    return result


@router.post("/api/v1/wizard/run/{step_id}", dependencies=[Depends(require_token)])
async def run_wizard_step(step_id: str, request: Request) -> JSONResponse:
    """Run one wizard step and return its result.

    For the microphone step the proof streams live dBFS samples over the
    ``/ws/wizard`` channel; the browser should subscribe before calling this
    endpoint.
    """

    # FastAPI path parameters are decoded strings; keep them URL-safe.
    if not step_id or "/" in step_id:
        raise HTTPException(status_code=400, detail="Invalid step_id")

    loop = asyncio.get_running_loop()
    result = await asyncio.to_thread(_run_step_sync, step_id, loop)
    return JSONResponse(content={"step_id": step_id, **_result_to_dict(result)})


# ---------------------------------------------------------------------------
# .env key entry — let a non-technical user paste their provider key + license
# key in the browser instead of hand-editing .env. Both write via the safe
# upsert writer (core.env_writer) and take effect in this process immediately.
# ---------------------------------------------------------------------------


def _configured_provider() -> str | None:
    """Resolve the active LLM provider from env/.env (lower-cased), or None."""

    load_env()
    provider = read_env_value("LLM_PROVIDER")
    return provider.strip().lower() if provider and provider.strip() else None


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 — any malformed body is just "no fields"
        return {}
    return data if isinstance(data, dict) else {}


def _persist_env_var(key: str, value: str) -> None:
    """Write ``key`` to .env and make it live for the running process.

    Updating ``os.environ`` here means the just-pasted key is effective for the
    current server without a restart (the detector/health-check read
    ``os.getenv`` at start_call). The value is never logged.
    """

    set_env_var(key, value)
    os.environ[key] = value


@router.get("/api/v1/wizard/config")
async def get_wizard_config() -> dict[str, Any]:
    """Report which .env keys the first-run wizard still needs.

    Returns only presence booleans and env-var names — never a secret value —
    so the browser can label the provider-key field and hide it for providers
    that need no key.
    """

    provider = _configured_provider()
    key_env = provider_key_env(provider) if provider else None
    provider_needs_key = provider is not None and key_env is not None
    key_present = bool(read_env_value(key_env)) if key_env else False
    return {
        "provider": provider,
        "provider_key_env": key_env,
        "provider_needs_key": provider_needs_key,
        "provider_key_present": key_present if provider_needs_key else True,
        "license_present": bool(read_env_value(_LICENSE_ENV)),
    }


@router.post("/api/v1/wizard/set-provider-key", dependencies=[Depends(require_token)])
async def set_provider_key(request: Request) -> JSONResponse:
    """Write the API key for the configured LLM provider to .env.

    Provider-agnostic: the target env var (``GEMINI_API_KEY`` /
    ``OPENAI_API_KEY`` / …) is derived from ``LLM_PROVIDER``, never hardcoded.
    """

    body = await _read_json(request)
    value = str(body.get("value", "")).strip()

    provider = _configured_provider()
    if not provider:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": "LLM_PROVIDER is niet ingesteld in .env."},
        )

    key_env = provider_key_env(provider)
    if key_env is None:
        return JSONResponse(
            content={
                "ok": True,
                "message": f"Provider '{provider}' vereist geen API-sleutel in .env.",
                "provider": provider,
                "key_env": None,
            }
        )

    if not value:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": "Geef een API-sleutel op."},
        )
    if len(value) < _MIN_PROVIDER_KEY_LEN:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": f"{key_env} lijkt te kort; controleer je API-sleutel.",
            },
        )

    await asyncio.to_thread(_persist_env_var, key_env, value)
    return JSONResponse(
        content={
            "ok": True,
            "message": f"{key_env} opgeslagen in .env.",
            "provider": provider,
            "key_env": key_env,
        }
    )


@router.post("/api/v1/wizard/set-license", dependencies=[Depends(require_token)])
async def set_license(request: Request) -> JSONResponse:
    """Write the Pro license key (``SALES_COPILOT_LICENSE``) to .env."""

    body = await _read_json(request)
    value = str(body.get("value", "")).strip()

    if not value:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": "Geef een licentiesleutel op."},
        )
    if len(value) < _MIN_LICENSE_LEN:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": "Licentiesleutel lijkt te kort."},
        )

    await asyncio.to_thread(_persist_env_var, _LICENSE_ENV, value)
    return JSONResponse(
        content={
            "ok": True,
            "message": "Licentiesleutel opgeslagen in .env.",
            "key_env": _LICENSE_ENV,
        }
    )


# ---------------------------------------------------------------------------
# Verkopersprofiel — the persistent "what I already know, what I sell, my
# method" doc every deep-insight run loads (PR-D4). GET is unauthenticated,
# matching the other read-only wizard endpoints above (steps/state/config):
# the hub is localhost-only (invariant 4) and this is the operator's own
# profile text, not a secret like a provider or license key. POST requires a
# token like every other mutating wizard route.
# ---------------------------------------------------------------------------


@router.get("/api/v1/profile")
async def get_profile() -> dict[str, Any]:
    """Return the seller profile doc's current content for the wizard editor."""

    content = read_profile_raw()
    return {
        "content": content,
        "exists": bool(content.strip()),
        "max_chars": MAX_PROFILE_CHARS,
    }


@router.post("/api/v1/profile", dependencies=[Depends(require_token)])
async def set_profile(request: Request) -> JSONResponse:
    """Create or overwrite the seller profile doc."""

    body = await _read_json(request)
    content = str(body.get("content", ""))
    if len(content) > _MAX_PROFILE_INPUT_CHARS:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": f"Profiel is te lang (max {_MAX_PROFILE_INPUT_CHARS} tekens).",
            },
        )
    await asyncio.to_thread(write_profile_document, content)
    return JSONResponse(content={"ok": True, "message": "Verkopersprofiel opgeslagen."})
