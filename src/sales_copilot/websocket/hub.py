"""FastAPI WebSocket pub/sub hub wrapper."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from sales_copilot.core.autostart_monitor import (
    AutostartMonitorConfig,
    RuntimeAutostartMonitor,
    run_autostart_monitor_loop,
    set_active_monitor,
)
from sales_copilot.core.config import env_bool, env_float
from sales_copilot.core.measurement_signals import start_pro_license_heartbeat
from sales_copilot.websocket import hub_api, hub_core, hub_static, hub_upload, hub_wizard

logger = logging.getLogger(__name__)

_heartbeat: Any = None
_autostart_monitor_task: asyncio.Task[None] | None = None
_autostart_stop_event: asyncio.Event | None = None

# Auto-armed calls have no user-supplied call config (no dashboard round-trip
# happened before the session started), so a fixed default preset is used. The
# "sales" preset is the same default the sample-aha replay uses.
_AUTOSTART_DEFAULT_PRESET = "sales"


def _build_autostart_call_config() -> dict[str, Any]:
    """Build the start-call config for a runtime auto-armed session.

    ``consent`` is marked ``asked=True, given=True`` here deliberately: reaching
    this point already required the user's explicit per-call confirm-consent
    tick (``RuntimeAutostartMonitor.confirm_consent()``), so this *is* the fresh
    consent signal for this call, not a silent bypass of it.
    """

    return {
        "preset_name": _AUTOSTART_DEFAULT_PRESET,
        "install_code": "",
        "consent": {"asked": True, "given": True},
    }


def _make_autostart_monitor() -> RuntimeAutostartMonitor:
    def _on_consent_required(process_name: str) -> None:
        asyncio.create_task(
            hub_core.broadcast(
                "system",
                {"type": "autostart_consent_required", "process": process_name},
            )
        )

    def _on_session_arm(process_name: str) -> None:
        logger.info("Runtime auto-arm: session-arm triggered by '%s'.", process_name)

        async def _arm() -> None:
            config = _build_autostart_call_config()
            missing = hub_core.missing_entitlements(config)
            if missing:
                # Defense in depth: the monitor already gated on FEATURE_AUTOSTART
                # before surfacing the prompt, but the start-call entitlement
                # check is re-applied here too, exactly like every other
                # apply_start_call() call site.
                logger.warning("Runtime auto-arm blocked: missing entitlements %s", missing)
                return
            async with hub_core._config_lock:
                hub_core.apply_start_call(config)
            await hub_core.broadcast("config", {"type": "start_call", "config": config})

        asyncio.create_task(_arm())

    return RuntimeAutostartMonitor(
        config=AutostartMonitorConfig.from_env(),
        on_consent_required=_on_consent_required,
        on_session_arm=_on_session_arm,
    )


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _heartbeat, _autostart_monitor_task, _autostart_stop_event
    _heartbeat = start_pro_license_heartbeat()

    monitor = _make_autostart_monitor()
    set_active_monitor(monitor)
    _autostart_stop_event = asyncio.Event()
    if env_bool("AUTOSTART_MONITOR_ENABLED", True):
        _autostart_monitor_task = asyncio.create_task(
            run_autostart_monitor_loop(
                monitor,
                _autostart_stop_event,
                poll_interval_s=env_float("AUTOSTART_POLL_INTERVAL_S", 2.0) or 2.0,
                is_call_active=lambda: hub_core._call_active,
            )
        )

    yield

    if _heartbeat is not None:
        _heartbeat.stop()
    if _autostart_stop_event is not None:
        _autostart_stop_event.set()
    if _autostart_monitor_task is not None:
        try:
            await asyncio.wait_for(_autostart_monitor_task, timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            _autostart_monitor_task.cancel()
    set_active_monitor(None)


app = FastAPI(lifespan=_lifespan)
_ALLOWED_ORIGINS = [
    "http://localhost:8760",
    "http://127.0.0.1:8760",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Sales-Copilot-Token"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "connect-src 'self' http://localhost:8760 http://127.0.0.1:8760 ws://localhost:8760 ws://127.0.0.1:8760; "
        "img-src 'self' data: https:; font-src 'self' data: https:; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


app.include_router(hub_core.router)
app.include_router(hub_api.router)
app.include_router(hub_upload.router)
app.include_router(hub_wizard.router)
hub_static.mount_static_apps(app)

_subscribers = hub_core._subscribers

UPLOAD_ROOT = hub_upload.UPLOAD_ROOT
MAX_UPLOAD_BYTES = hub_upload.MAX_UPLOAD_BYTES
ALLOWED_EXTENSIONS = hub_upload.ALLOWED_EXTENSIONS

broadcast = hub_core.broadcast
get_latest_config = hub_core.get_latest_config
reset_config_state = hub_core.reset_config_state
wait_for_start_call = hub_core.wait_for_start_call
wait_for_end_call = hub_core.wait_for_end_call
close_all_connections = hub_core.close_all_connections


def run_hub(host: str, port: int) -> None:
    import uvicorn

    if host not in {"localhost", "127.0.0.1"}:
        raise ValueError("The local WebSocket hub must bind to localhost")
    uvicorn.run(app, host=host, port=port, log_level="info")
