"""Entry point: python -m sales_copilot"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import socket
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import websockets

from sales_copilot import __version__
from sales_copilot.audio.recorder import (
    start_session_recording,
    stop_session_recording,
)
from sales_copilot.auth.startup_check import LicenseStatus, check_license_at_startup
from sales_copilot.core.audit_ledger import get_audit_writer
from sales_copilot.core.config import (
    CallConfig,
    DetectorConfig,
    InsightConfig,
    TranscriberConfig,
    WebSocketConfig,
    build_module_configs,
    env_bool,
    env_float,
    env_int,
    load_env,
)
from sales_copilot.core.consent import (
    consent_tier,
    must_block_start,
    record_consent,
    should_soft_nudge,
)
from sales_copilot.core.context_docs import (
    parse_client_slug_from_payload,
    resolve_context_doc_ids,
    write_session_context_manifest,
)
from sales_copilot.core.logging import configure_logging
from sales_copilot.core.paths import (
    ensure_app_support_tree,
    resolve_app_path,
    seed_app_support_defaults,
)
from sales_copilot.core.retention import run_retention_sweep
from sales_copilot.core.session_store import SessionStore
from sales_copilot.modules.talk_time.__main__ import main as talk_time_main
from sales_copilot.modules.transcriber.__main__ import main as transcriber_main
from sales_copilot.modules.transcriber.backends import create_backend
from sales_copilot.websocket import hub
from sales_copilot.websocket.hub import run_hub
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)


def _start_or_signal_recording(
    record_audio: bool,
    session_id: str,
    record_dir: Path,
    *,
    sample_rate: int,
    flush_seconds: float,
) -> str | None:
    """Start call-audio recording when enabled, or say plainly that it is off.

    RECORD_AUDIO defaults to false (privacy-first: this product records sales
    calls, so recording is opt-in, not silently on). An operator who never
    sets it must still get an explicit signal either way -- previously only
    the "recording started" path logged anything, so an operator relying on
    the default got no confirmation their calls were never being saved.
    Returns the session id to pass to ``stop_session_recording()`` at call end,
    or ``None`` when recording did not start.
    """
    if not record_audio:
        logger.info(
            "RECORD_AUDIO=false: this call's audio is not being saved to disk "
            "(live transcription/pain-point detection are unaffected). Set "
            "RECORD_AUDIO=true in .env to record calls for later review."
        )
        return None

    try:
        start_session_recording(
            session_id,
            record_dir,
            sample_rate=sample_rate,
            flush_seconds=flush_seconds,
        )
        logger.info(
            "RECORD_AUDIO=true: recording this call's audio to %s.",
            record_dir / session_id,
        )
        return session_id
    except Exception as exc:
        logger.warning("AudioRecorder start failed: %s", exc)
        return None


async def _eager_warmup_at_startup() -> None:
    """Pre-load the Whisper backend at orchestrator startup.

    PR-96.2 added WHISPER_EAGER_WARMUP, but the transcriber module is only
    spawned at start_call. With this task the model is loaded as soon as
    the backend boots, so by the time the operator clicks Start Call the
    in-process Whisper cache is hot. The transcriber module's own backend
    instances are created later but reuse mlx_whisper's process-level
    model cache, so their warmup() returns near-instantly.
    """

    try:
        cfg = TranscriberConfig.from_env()
        logger.info(
            "Eager warmup: pre-loading Whisper model (%s) at backend startup...",
            cfg.backend,
        )
        backend = create_backend({
            "backend": cfg.backend,
            "language": cfg.language,
            "model_repo": f"mlx-community/whisper-{cfg.model}",
            "whisper_cpp_binary": cfg.whisper_cpp_binary,
            "whisper_cpp_model_path": cfg.whisper_cpp_model_path,
            "whisper_cpp_threads": cfg.whisper_cpp_threads,
        })
        await backend.warmup()
        await backend.stop()
    except Exception:
        logger.exception("Eager warmup failed; first call will pay the cost.")


def _start_hub(ws_config: WebSocketConfig) -> threading.Thread:
    hub_thread = threading.Thread(
        target=run_hub,
        args=(ws_config.host, ws_config.port),
        daemon=True,
        name="ws-hub",
    )
    hub_thread.start()
    return hub_thread


def _wait_port_ready(port: int, *, host: str = "127.0.0.1", timeout_s: float = 30.0) -> bool:
    """Block until the hub accepts TCP connections on ``port``, or timeout."""

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _browser_front_door_url(port: int) -> str:
    """Launch URL: the first-run wizard until the app is configured, else dashboard."""

    from sales_copilot.wizard.front_door import first_run_path

    return f"http://localhost:{port}{first_run_path()}"


def open_front_door(
    port: int,
    *,
    opener: Callable[[str], Any] = webbrowser.open,
    wait_ready: Callable[[int], bool] = _wait_port_ready,
) -> str | None:
    """Open the front door in the default browser once the hub is up.

    Sends first-run users to ``/dashboard/wizard/`` (no provider key and/or no
    license yet) and everyone else to ``/dashboard`` — one rule, resolved from
    ``.env``. Guarded by ``SALES_COPILOT_NO_BROWSER`` (truthy = do not open) so
    headless runs, CI, tests, and the launcher (which manages its own tabs) opt
    out. Default is to open. Returns the opened URL, or ``None`` when skipped.
    """

    if env_bool("SALES_COPILOT_NO_BROWSER", False):
        return None
    if not wait_ready(port):
        logger.warning("Hub not ready on port %s; skipping browser open.", port)
        return None
    url = _browser_front_door_url(port)
    try:
        opener(url)
    except Exception:
        logger.debug("Could not open browser at %s", url, exc_info=True)
        return None
    logger.info("Opened %s in the default browser.", url)
    return url


_MODULE_SOURCE_SPECS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("talk_time", ("talk_time",), "enable_talk_time"),
    ("transcriber", ("transcriber", "transcript"), "enable_transcriber"),
    ("detector", ("pain_points",), "enable_detector"),
    ("reports", ("post_call_report",), "enable_reports"),
)


def _module_source(
    payload: dict[str, Any],
    modules: dict[str, Any],
    modules_keys: tuple[str, ...],
    legacy_key: str,
) -> str:
    """Describe where a module's enable/disable flag came from.

    A start_call payload can set a module explicitly via ``modules.<key>``
    (current dashboard shape) or the legacy top-level ``enable_<module>``
    flag; anything else falls through to the hardcoded default in
    ``_parse_call_config``. Used so a disabled module's origin is visible in
    the log instead of a silent name-drop from the "Enabled modules" line --
    on 2026-09-05 a detector-off call left zero trace anywhere.
    """
    for key in modules_keys:
        if key in modules:
            return f"modules.{key}={modules[key]!r}, explicit in start_call payload"
    if legacy_key in payload:
        return f"payload.{legacy_key}={payload[legacy_key]!r}, explicit in start_call payload"
    return "defaulted (no explicit flag in start_call payload)"


def _parse_call_config(payload: dict[str, Any]) -> CallConfig:
    document_ids = payload.get("context_doc_ids")
    if not isinstance(document_ids, list):
        uploads = payload.get("uploads")
        document_ids = [
            upload.get("id")
            for upload in uploads
            if isinstance(upload, dict) and isinstance(upload.get("id"), str)
        ] if isinstance(uploads, list) else []
    context_docs = resolve_context_doc_ids(document_ids)
    screen_mode = payload.get("screen_mode")
    if screen_mode == "single":
        screens = 1
    elif screen_mode == "dual":
        screens = 2
    else:
        screens = payload.get("screens", 2)
    modules = payload.get("modules")
    if not isinstance(modules, dict):
        modules = {}
    llm_payload = payload.get("llm")
    if not isinstance(llm_payload, dict):
        llm_payload = {}
    transcript_payload = payload.get("transcript")
    if not isinstance(transcript_payload, dict):
        transcript_payload = {}
    prospect_payload = payload.get("prospect")
    if not isinstance(prospect_payload, dict):
        prospect_payload = {}
    enable_detector = modules.get("pain_points", payload.get("enable_detector", True))
    enable_transcriber = modules.get(
        "transcriber",
        modules.get("transcript", payload.get("enable_transcriber", True)),
    )
    if enable_detector and not enable_transcriber:
        logger.warning(
            "Detector is enabled but transcriber is disabled; detector will not receive "
            "transcript input unless another source injects via the transcript channel."
        )
    if not enable_detector:
        logger.warning(
            "Detector is disabled for this call; no pain-point detection will run (%s).",
            _module_source(payload, modules, ("pain_points",), "enable_detector"),
        )
    data = {
        "screens": screens,
        "enable_talk_time": modules.get("talk_time", payload.get("enable_talk_time", True)),
        "enable_transcriber": enable_transcriber,
        "enable_detector": enable_detector,
        "enable_presentation": modules.get(
            "presentation", payload.get("enable_presentation", True)
        ),
        "enable_reports": modules.get(
            "post_call_report", payload.get("enable_reports", True)
        ),
        "transcript_backend": transcript_payload.get(
            "backend", payload.get("transcript_backend")
        ),
        "call_language": transcript_payload.get("language", payload.get("language", "nl")),
        "prospect_name": prospect_payload.get("name", payload.get("prospect_name")),
        "prospect_company": prospect_payload.get(
            "company", payload.get("prospect_company")
        ),
        "prospect_industry": prospect_payload.get(
            "industry", payload.get("prospect_industry")
        ),
        "llm_provider": llm_payload.get("provider", payload.get("llm_provider")),
        "llm_model": llm_payload.get("model", payload.get("llm_model")),
        "preset_name": payload.get("preset_name", payload.get("preset")),
        "context_docs": context_docs,
        "client_slug": parse_client_slug_from_payload(payload),
        # klantmap-als-eenheid D2: set server-side by hub_core.extract_start_call_config
        # from the selected client's klant.yaml (aflevering: lokaal). None (no client, or
        # no aflevering set) means the existing global report-delivery-sinks behaviour.
        "aflevering": payload.get("aflevering") if isinstance(payload.get("aflevering"), str) else None,
        # llm-routering-per-taak: the conversation's privacy ceiling, set (and validated)
        # server-side by hub_core.extract_start_call_config. CallConfig refuses an unknown
        # value; build_module_configs carries it onto the DetectorConfig every task
        # resolves its provider from.
        "privacy": payload.get("privacy"),
    }
    transcribe_self_live_raw = transcript_payload.get("transcribe_self_live")
    if isinstance(transcribe_self_live_raw, bool):
        data["transcribe_self_live"] = transcribe_self_live_raw
    if isinstance(data["call_language"], str):
        data["call_language"] = data["call_language"].strip().lower() or "nl"
    else:
        data["call_language"] = "nl"
    return CallConfig(**data)


def _extract_consent(payload: dict[str, Any]) -> tuple[bool, bool] | None:
    """Read the per-session consent signal from the start-call payload.

    Returns ``(asked, given)`` when the payload carries a consent signal — either
    a nested ``{"consent": {"asked": ..., "given": ...}}`` block or flat
    ``consent_asked``/``consent_given`` keys — else ``None`` (no signal present,
    so there is nothing to record and nothing changes for default installs).
    """
    block = payload.get("consent")
    if isinstance(block, dict):
        given = bool(block.get("given", False))
        asked = bool(block.get("asked", given))
        return asked, given
    if "consent_given" in payload or "consent_asked" in payload:
        given = bool(payload.get("consent_given", False))
        asked = bool(payload.get("consent_asked", given))
        return asked, given
    return None


def _resolve_start_call_consent(
    config_payload: dict[str, Any],
    preset_name: str | None,
    session_id: str,
) -> tuple[str, tuple[bool, bool] | None, bool, bool]:
    """Resolve consent state for a start-call payload.

    Returns ``(tier, consent_signal, consent_given, should_block)``. This is the
    canonical decision point used by the runtime start-call path; tests can call
    it directly to assert that a strict recruitment preset blocks without consent.
    """
    call_tier = consent_tier(preset_name)
    consent_signal = _extract_consent(config_payload)
    consent_given = bool(consent_signal and consent_signal[1])
    blocked = must_block_start(consent_given, tier=call_tier)
    return call_tier, consent_signal, consent_given, blocked


def _write_audit_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """Append a non-blocking audit event via the tiered writer.

    Local write is authoritative; a Pro entitlement may additionally mirror a
    tamper-evident hash to the central endpoint. Failures are logged but never
    raise, so audit emission cannot break a call or startup.
    """
    try:
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event_type": event_type,
        }
        if payload:
            record.update(payload)
        get_audit_writer().write(record)
    except Exception:
        logger.exception("audit write failed (non-blocking): %s", event_type)


async def _retention_sweep_at_startup() -> None:
    """Enforce ``DATA_RETENTION_DAYS`` once at startup, off the event loop.

    Runs in a worker thread so the (synchronous) purge never delays the operator
    clicking Start Call. ``run_retention_sweep`` never raises; this wrapper only
    guards against an unexpected scheduling error.
    """
    try:
        await asyncio.to_thread(run_retention_sweep)
    except Exception:
        logger.exception("Retention sweep at startup failed (non-blocking).")


def _enabled_modules(call_config: CallConfig) -> list[str]:
    modules = []
    if call_config.enable_talk_time:
        modules.append("talk_time")
    if call_config.enable_transcriber:
        modules.append("transcriber")
    if call_config.enable_detector:
        modules.append("detector")
    if call_config.enable_reports:
        modules.append("reports")
    return modules


def _disabled_modules(config_payload: dict[str, Any], call_config: CallConfig) -> list[str]:
    """Complement of ``_enabled_modules``: every known module that is off, with why.

    A name missing from "Enabled modules" is easy to miss -- this always logs
    the complement, "none" included, so a disabled module never again looks
    identical to one that was simply never started.
    """
    modules = config_payload.get("modules")
    if not isinstance(modules, dict):
        modules = {}
    enabled_flags = {
        "talk_time": call_config.enable_talk_time,
        "transcriber": call_config.enable_transcriber,
        "detector": call_config.enable_detector,
        "reports": call_config.enable_reports,
    }
    disabled = []
    for name, modules_keys, legacy_key in _MODULE_SOURCE_SPECS:
        if enabled_flags[name]:
            continue
        source = _module_source(config_payload, modules, modules_keys, legacy_key)
        disabled.append(f"{name} ({source})")
    return disabled


async def _emit_config_event(
    ws_config: WebSocketConfig,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    url = channel_ws_url(ws_config, "config")
    message = {"type": event_type}
    if payload:
        message.update(payload)
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(message))
    except Exception:
        pass


def _build_provider_client(provider: str) -> None:
    """Attempt to construct the LLM provider client. Raises if init fails.

    Delegates to the central seam so the health-check validates construction the same way the
    live detector/copilot modules build their clients (timeout included, all six providers).
    """
    from sales_copilot.core.llm_client import build_client

    build_client(provider, timeout_ms=env_int("LLM_TIMEOUT_MS", 7000) or 7000)


async def _health_check_providers(
    call_config: CallConfig,
    detector_config: DetectorConfig,
    insight_config: InsightConfig,
    ws_config: WebSocketConfig,
) -> None:
    """Try to instantiate the configured LLM provider client(s) at start_call time.

    Publishes a module_warning coaching event immediately on failure so the
    operator knows something is wrong before the detector/insight engine
    crashes silently. Checks every distinct provider the per-task resolver
    (``core.llm_routing``) hands out for this conversation -- a task can have its
    own ``<TAAK>_LLM_PROVIDER`` and the deep lane its ``INSIGHT_PROVIDER`` -- once
    per provider. The live tasks, the insight engine included, only ever run
    nested inside the detector module (see ``modules/detector/__main__.py``), so
    they are gated on ``enable_detector``; the post-call ``report`` and
    ``report_terms`` tasks run in the reports module and are gated on
    ``enable_reports``.
    """
    from sales_copilot.core.llm_routing import active_tasks, resolve_llm

    checked: set[str] = set()
    tasks = active_tasks(
        detector_config,
        insight_active=insight_config.enabled,
        report_active=call_config.enable_reports,
    )
    for task in tasks:
        if task in ("report", "report_terms"):
            module = "reports"
        elif not call_config.enable_detector:
            continue
        else:
            module = "insight" if task == "insight" else "detector"
        try:
            provider = resolve_llm(task, detector_config, insight_config=insight_config).provider
        except ValueError as exc:
            logger.error(
                "LLM routing refused task at start_call: task=%s exc_type=%s msg=%s",
                task,
                type(exc).__name__,
                str(exc)[:300],
            )
            await _publish_module_warning(module, f"LLM task '{task}' refused: {str(exc)[:200]}", ws_config)
            continue
        if provider and provider not in checked:
            checked.add(provider)
            await _health_check_one_provider(provider, module, ws_config)


async def _health_check_one_provider(provider: str, module: str, ws_config: WebSocketConfig) -> None:
    try:
        await asyncio.to_thread(_build_provider_client, provider)
    except Exception as exc:
        logger.error(
            "Provider health-check failed at start_call: module=%s provider=%s exc_type=%s msg=%s",
            module,
            provider,
            type(exc).__name__,
            str(exc)[:300],
        )
        await _publish_module_warning(
            module, f"LLM provider '{provider}' init failed: {str(exc)[:200]}", ws_config
        )


async def _publish_module_warning(module: str, warning: str, ws_config: WebSocketConfig) -> None:
    payload: dict[str, Any] = {
        "type": "module_warning",
        "module": module,
        "warning": warning,
        "at_ms": int(time.time() * 1000),
    }
    url = channel_ws_url(ws_config, "coaching")
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(payload))
    except Exception:
        logger.warning("Could not publish module_warning for provider health-check (module=%s)", module)


async def _guarded_task(
    coro: Any,
    module_name: str,
    crashed_modules: set[str],
    ws_config: WebSocketConfig,
) -> None:
    """Wrap a module coroutine so crashes are logged and published immediately."""
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        crashed_modules.add(module_name)
        logger.error(
            "Module '%s' crashed during call: exc_type=%s msg=%s",
            module_name,
            type(exc).__name__,
            str(exc)[:300],
            exc_info=True,
        )
        payload: dict[str, Any] = {
            "type": "module_failure",
            "module": module_name,
            "error": str(exc)[:300],
            "at_ms": int(time.time() * 1000),
        }
        url = channel_ws_url(ws_config, "coaching")
        try:
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps(payload))
        except Exception:
            logger.warning("Could not publish module_failure for module=%s", module_name)


async def _run_call(
    stop_event: asyncio.Event,
    call_config: CallConfig,
    module_configs: dict[str, Any],
    ws_config: WebSocketConfig | None = None,
    session_id: str | None = None,
) -> None:
    if ws_config is None:
        ws_config = WebSocketConfig.from_env()
    tasks: list[asyncio.Task[None]] = []
    crashed_modules: set[str] = set()
    if call_config.enable_talk_time:
        tasks.append(
            asyncio.create_task(
                _guarded_task(
                    talk_time_main(
                        stop_event=stop_event,
                        register_signals=False,
                        talk_time_config=module_configs["talk_time"],
                    ),
                    "talk_time",
                    crashed_modules,
                    ws_config,
                ),
                name="talk-time",
            )
        )
    if call_config.enable_transcriber:
        tasks.append(
            asyncio.create_task(
                _guarded_task(
                    transcriber_main(
                        stop_event=stop_event,
                        register_signals=False,
                        transcriber_config=module_configs["transcriber"],
                    ),
                    "transcriber",
                    crashed_modules,
                    ws_config,
                ),
                name="transcriber",
            )
        )
    if call_config.enable_detector:
        from sales_copilot.modules.detector.__main__ import main as detector_main

        tasks.append(
            asyncio.create_task(
                _guarded_task(
                    detector_main(
                        stop_event=stop_event,
                        register_signals=False,
                        detector_config=module_configs["detector"],
                        insight_config=module_configs["insight"],
                        slides_config=module_configs["slides"],
                        context_docs=call_config.context_docs,
                        session_id=session_id,
                        client_slug=call_config.client_slug,
                    ),
                    "detector",
                    crashed_modules,
                    ws_config,
                ),
                name="detector",
            )
        )
    if call_config.enable_reports:
        from sales_copilot.modules.reports.__main__ import main as reports_main

        tasks.append(
            asyncio.create_task(
                _guarded_task(
                    reports_main(
                        stop_event=stop_event,
                        register_signals=False,
                        prospect_name=call_config.prospect_name,
                        prospect_company=call_config.prospect_company,
                        context_docs=call_config.context_docs,
                        session_id=session_id,
                        client_slug=call_config.client_slug,
                        aflevering=call_config.aflevering,
                        detector_config=module_configs["detector"],
                    ),
                    "reports",
                    crashed_modules,
                    ws_config,
                ),
                name="reports",
            )
        )

    call_stop = asyncio.Event()

    try:
        while not stop_event.is_set() and not call_stop.is_set():
            ended = await hub.wait_for_end_call(timeout=0.25)
            if ended:
                call_stop.set()
                break
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.exception("Module task '%s' exited with error: %s", task.get_name(), exc)


async def _emit_system_event(
    ws_config: WebSocketConfig,
    payload: dict[str, Any],
) -> None:
    """Publish a payload to the hub's ``system`` channel via an authed WS client.

    The hub subscribers live on the uvicorn thread's event loop, so a direct
    ``hub_core.broadcast`` from the orchestrator loop cannot reach them. This
    re-uses the cross-thread publish pattern ``_emit_config_event`` relies on:
    connect as an authenticated client and let the hub re-broadcast on its own
    loop. Best-effort — the polled ``/api/v1/license/status`` is authoritative.
    """
    url = channel_ws_url(ws_config, "system")
    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(payload))
    except Exception:
        logger.debug("Could not publish system event %s", payload.get("event"))


async def _broadcast_license_status(ws_config: WebSocketConfig) -> None:
    """Check the license inside the running loop and push status to the dashboard.

    The old synchronous gate ran before ``asyncio.run(_orchestrate())`` and
    guarded every emit with ``loop.is_running()`` — always False there — so the
    ``license_grace``/``license_expired`` events were never broadcast. Running it
    here, on the live orchestrator loop, lets the emit actually fire. The status
    itself is now authoritative because ``check_license_at_startup`` shares the
    Ed25519 verifier with ``feature_policy``.
    """
    status, message = check_license_at_startup()
    _write_audit_event(
        "license_verify",
        {"license_status": status.value, "license_message": message},
    )
    if status == LicenseStatus.VALID:
        logger.info("License: %s", message)
        return

    logger.warning("License: %s", message)
    if status == LicenseStatus.GRACE:
        days_remaining = int(message.split("day")[0].split()[-1]) if "day" in message else 0
        await _emit_system_event(
            ws_config,
            {
                "event": "license_grace",
                "days_remaining": days_remaining,
                "message": message,
            },
        )
    elif status in (LicenseStatus.EXPIRED, LicenseStatus.MISSING):
        await _emit_system_event(
            ws_config,
            {"event": "license_expired", "message": message},
        )


def main() -> None:
    # In a frozen .app the writable tree may be empty on first launch; create it
    # and seed the default config/data before anything else touches the paths.
    ensure_app_support_tree()
    seed_app_support_defaults()
    load_env()
    configure_logging()
    ws_config = WebSocketConfig.from_env()
    logger.info("OnCue v%s", __version__)
    active_consent_tier = consent_tier()
    logger.info("Consent tier: %s", active_consent_tier)
    if active_consent_tier == "off":
        logger.warning(
            "CONSENT_TIER=off: no consent gate is active. This is available but "
            "not recommended; the deploying organisation remains responsible for "
            "obtaining a valid legal basis for recording."
        )
    logger.info("Starting components...")
    logger.info("Modules: talk_time, transcriber, detector, reports")
    logger.info("WebSocket hub: ws://%s:%s", ws_config.host, ws_config.port)

    _start_hub(ws_config)

    # Open the front door (first-run wizard until configured, else dashboard) in
    # the default browser once the hub is up. Runs off-thread so it never blocks
    # the orchestrator; opt out with SALES_COPILOT_NO_BROWSER=1.
    threading.Thread(
        target=open_front_door,
        args=(ws_config.port,),
        daemon=True,
        name="front-door-open",
    ).start()

    stop_event = asyncio.Event()

    def _shutdown(*_args: object) -> None:
        logger.info("Shutdown requested. Stopping modules...")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    async def _orchestrate() -> None:
        record_audio = env_bool("RECORD_AUDIO", False)
        record_dir = resolve_app_path("data/sessions")
        record_sample_rate = env_int("AUDIO_SAMPLE_RATE", 16000) or 16000
        record_flush = env_float("AUDIO_RECORDER_FLUSH_SECONDS", 5.0) or 5.0

        await _broadcast_license_status(ws_config)

        session_store = SessionStore()
        unfinished = session_store.find_unfinished_sessions()
        if unfinished:
            logger.info(
                "Crash-recovery: %d unfinished session(s) found: %s",
                len(unfinished),
                [s.id for s in unfinished],
            )
            await _emit_config_event(
                ws_config,
                "session_resume_available",
                {"session_ids": [s.id for s in unfinished]},
            )

        warmup_task: asyncio.Task[None] | None = None
        if env_bool("WHISPER_EAGER_WARMUP", False):
            warmup_task = asyncio.create_task(
                _eager_warmup_at_startup(), name="eager-warmup"
            )

        # Enforce data retention at the app level (GDPR Art. 5(1)(e)) instead of
        # relying only on the cron purge. Non-blocking: it sweeps old sessions in
        # a worker thread and never delays the first call.
        retention_task: asyncio.Task[None] = asyncio.create_task(
            _retention_sweep_at_startup(), name="retention-sweep"
        )

        while not stop_event.is_set():
            logger.info("Waiting for start_call config...")
            config_payload = None
            while config_payload is None and not stop_event.is_set():
                config_payload = await hub.wait_for_start_call(timeout=0.25)
            if stop_event.is_set():
                break
            call_config = _parse_call_config(config_payload)
            configs = build_module_configs(call_config)
            enabled = _enabled_modules(call_config)
            disabled = _disabled_modules(config_payload, call_config)
            logger.info("Call preset: %s", call_config.preset_name or "custom")
            logger.info("Enabled modules: %s", ", ".join(enabled) if enabled else "none")
            logger.info("Disabled modules: %s", ", ".join(disabled) if disabled else "none")
            logger.info("LLM provider: %s", configs["detector"].llm_provider)
            session_id = str(uuid.uuid4())

            # Consent gate (#12): configurable tier ladder. ``strict`` blocks
            # call start until explicit consent is recorded; ``soft`` emits a
            # non-blocking nudge; ``audit`` records the signal when present but
            # never blocks; ``off`` skips the gate (not recommended).
            call_tier, consent_signal, consent_given, blocked = _resolve_start_call_consent(
                config_payload, call_config.preset_name, session_id
            )
            if consent_signal is not None:
                record_consent(
                    session_id,
                    asked=consent_signal[0],
                    given=consent_signal[1],
                )
            if blocked:
                logger.warning(
                    "CONSENT_TIER=strict: consent not given for this call; "
                    "refusing to start capture (session_id=%s).",
                    session_id,
                )
                await _emit_config_event(
                    ws_config,
                    "call_blocked",
                    {"reason": "consent_required", "session_id": session_id},
                )
                hub.reset_config_state()
                continue
            if should_soft_nudge(consent_given, tier=call_tier):
                logger.warning(
                    "CONSENT_TIER=soft: consent not recorded for this call; "
                    "showing a non-blocking nudge on the dashboard."
                )
                await _emit_config_event(
                    ws_config,
                    "consent_nudge",
                    {
                        "reason": "consent_not_recorded",
                        "session_id": session_id,
                        "message": (
                            "Let op: toestemming is nog niet vastgelegd. "
                            "De sessie start wel door."
                        ),
                    },
                )

            write_session_context_manifest(session_id, call_config.context_docs)
            recorder_session_id = _start_or_signal_recording(
                record_audio,
                session_id,
                record_dir,
                sample_rate=record_sample_rate,
                flush_seconds=record_flush,
            )
            await _emit_config_event(ws_config, "call_started")
            _write_audit_event(
                "session_start",
                {"session_id": session_id, "preset": call_config.preset_name},
            )
            await _health_check_providers(call_config, configs["detector"], configs["insight"], ws_config)
            try:
                await _run_call(
                    stop_event,
                    call_config,
                    configs,
                    ws_config,
                    session_id=session_id,
                )
            finally:
                if recorder_session_id is not None:
                    try:
                        path = stop_session_recording()
                        if path is not None:
                            logger.info("Session audio recorded to %s", path)
                    except Exception as exc:
                        logger.warning("AudioRecorder finalize failed: %s", exc)
            _write_audit_event(
                "session_end",
                {"session_id": session_id, "preset": call_config.preset_name},
            )
            await _emit_config_event(ws_config, "call_ended")
            hub.reset_config_state()

        # A report whose post-call enrichment is still running (LLM step -> rewrite ->
        # delivery) finishes before the process exits. Each one is bounded by its own
        # REPORT_LLM_TIMEOUT_MS, and its local copy was already on disk before it started.
        from sales_copilot.modules.reports.enrichment import wait_for_pending_reports

        await wait_for_pending_reports()

        for background_task in (warmup_task, retention_task):
            if background_task is not None and not background_task.done():
                background_task.cancel()
                try:
                    await background_task
                except (asyncio.CancelledError, Exception):
                    pass

    try:
        asyncio.run(_orchestrate())
    except KeyboardInterrupt:
        _shutdown()
        time.sleep(0.1)
    except RuntimeError as exc:
        logger.error("%s", exc)
        _shutdown()
        time.sleep(0.1)
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
