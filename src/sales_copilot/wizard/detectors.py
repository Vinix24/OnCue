"""Auto-detectors for each wizard step.

Every detector returns a ``StepResult`` describing the current OS status. They
must not require interactive input and should be safe to call repeatedly (idempotent
reads/probes only).
"""

from __future__ import annotations

import importlib.util
import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from sales_copilot.core.config import TranscriberConfig, env, load_env
from sales_copilot.wizard.steps import StepResult


def _ensure_wizard_env_loaded() -> None:
    """Load ``.env`` before a detector reads configuration.

    The runtime entrypoints call ``load_env()`` in their ``main()`` before any
    config is read. Wizard detectors may also be imported and called directly
    (tests, scripts), so each detector that relies on ``os.environ``-based
    config reloads ``.env`` at call time. ``load_dotenv(override=False)`` is
    idempotent and will not overwrite values already present in the process
    environment.
    """
    load_env()


_MIC_LEVEL_THRESHOLD_DB = -50.0
_BLACKHOLE_DEVICE_NAME = "BlackHole 2ch"
_AUDIOTEE_TEST_TIMEOUT_S = 1.5


def _env_path(key: str, default: str) -> Path:
    return Path(env(key) or default)


def _wizard_env(name: str) -> str | None:
    """Resolve an env var for wizard checks without mutating ``os.environ``.

    The wizard reads ``.env`` only as a fallback for variables that are not
    already present in the process environment. This matches the behaviour of
    ``load_dotenv(override=False)`` but without the global side-effect of
    modifying ``os.environ``, which leaked into other tests/modules (e.g.
    ``LLM_TIMEOUT_MS`` affecting ``LLMConfirmClient``).
    """
    value = os.getenv(name)
    if value:
        return value
    return dotenv_values(".env").get(name)


def detect_mic() -> StepResult:
    """Detect default input device and permission status."""

    try:
        from sales_copilot.audio.capture import get_default_input_device

        device = get_default_input_device()
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            ok=False,
            message=f"Could not load audio library: {exc}",
            details={"error": "import_failed"},
        )

    if device is None:
        return StepResult(
            ok=False,
            message="No default microphone found. Connect a microphone or grant permission.",
            details={"error": "no_default_input"},
        )

    return StepResult(
        ok=True,
        message=f"Microphone found: {device.get('name', 'unknown')}.",
        details={"device": device},
    )


def run_mic_proof(emitter: Any = None) -> StepResult:
    """Record a few seconds from the mic and report levels.

    ``emitter`` is optional; when omitted a terminal dB meter is rendered.
    """

    from sales_copilot.wizard.meter import meter_session

    try:
        peak_db, mean_db = meter_session(emitter=emitter, duration_seconds=3.0)
    except Exception as exc:  # noqa: BLE001
        return StepResult(
            ok=False,
            message=f"Microphone test failed (usually a missing macOS permission): {exc}",
            details={"error": "proof_failed", "exception": str(exc)},
        )

    details = {"peak_db": peak_db, "mean_db": mean_db, "threshold_db": _MIC_LEVEL_THRESHOLD_DB}
    if peak_db < _MIC_LEVEL_THRESHOLD_DB:
        return StepResult(
            ok=False,
            message=(
                f"Microphone works, but the level is very low ({peak_db:.1f} dBFS). "
                "Check whether your microphone is muted or too far from your mouth."
            ),
            details=details,
        )

    return StepResult(
        ok=True,
        message=f"Microphone test OK (peak {peak_db:.1f} dBFS, mean {mean_db:.1f} dBFS).",
        details=details,
    )


def _audiotee_binary_exists() -> bool:
    path = _env_path("AUDIOTEE_BINARY_PATH", "./bin/audiotee")
    return path.exists() and os.access(path, os.X_OK)


def _audiotee_run_proof(path: Path, extra_args: list[str] | None = None) -> StepResult:
    """Start AudioTee briefly to verify the binary actually runs and can tap.

    A binary that merely exists on disk is not enough: macOS Gatekeeper or a
    missing Core Audio process-tap permission can still block it. We run a
    short system-wide tap and inspect stderr for authorization/permission
    failures.
    """

    args = [str(path), "--sample-rate", "16000", "--chunk-duration", "0.1"]
    if extra_args:
        args.extend(extra_args)

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        return StepResult(
            ok=False,
            message=f"AudioTee binary could not start: {exc}",
            details={"error": "start_failed", "path": str(path)},
        )

    try:
        _stdout_data, stderr_data = proc.communicate(timeout=_AUDIOTEE_TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        stderr_data = b""
    except Exception as exc:  # noqa: BLE001
        proc.kill()
        proc.wait()
        return StepResult(
            ok=False,
            message=f"AudioTee proof unexpected error: {exc}",
            details={"error": "proof_exception", "path": str(path)},
        )

    stderr_text = stderr_data.decode("utf-8", "replace").lower()
    permission_denied = any(
        phrase in stderr_text
        for phrase in [
            "permission",
            "not authorized",
            "not allowed",
            "audio recording",
            "denied",
            "kext",
            "could not enable",
        ]
    )
    other_error = "error:" in stderr_text or "failed" in stderr_text

    if permission_denied or (other_error and proc.returncode not in (0, -15)):
        return StepResult(
            ok=False,
            message=(
                "AudioTee did not start (the macOS process-tap permission is probably missing). "
                "Check System Settings > Privacy & Security > Audio Recording."
            ),
            details={
                "error": "tap_permission_denied",
                "path": str(path),
                "stderr": stderr_data.decode("utf-8", "replace").strip()[:500],
            },
        )

    return StepResult(
        ok=True,
        message="AudioTee proof OK.",
        details={"path": str(path)},
    )


def _blackhole_device_present() -> bool:
    try:
        from sales_copilot.audio.capture import find_input_device

        return find_input_device(_BLACKHOLE_DEVICE_NAME) is not None
    except Exception:  # noqa: BLE001
        return False


def _blackhole_default_output_configured() -> tuple[bool, dict[str, Any] | None]:
    """Check whether BlackHole is part of the macOS output routing.

    Returns ``(configured, device_info)``. We consider the routing OK when the
    default output device is BlackHole itself or an aggregate/multi-output
    device (heuristic: name contains 'multi'/'aggregate' or >2 output channels).
    """

    try:
        from sales_copilot.audio.capture import get_default_output_device

        device = get_default_output_device()
    except Exception:  # noqa: BLE001
        return False, None

    if device is None:
        return False, None

    name = str(device.get("name", "")).lower()
    channels = int(device.get("max_output_channels", 0) or 0)
    configured = any(
        keyword in name for keyword in ("blackhole", "multi", "aggregate", "copilot output")
    ) or (channels > 2)

    return configured, device


def _blackhole_has_audio() -> tuple[bool, float]:
    """Record a short sample from BlackHole to confirm actual audio flow."""

    try:
        from sales_copilot.audio.capture import check_device_health

        return check_device_health(_BLACKHOLE_DEVICE_NAME)
    except Exception:  # noqa: BLE001
        return False, 0.0


def detect_route() -> StepResult:
    """Detect available prospect-audio route: AudioTee (preferred) or BlackHole."""

    audiotee_path = _env_path("AUDIOTEE_BINARY_PATH", "./bin/audiotee")
    has_audiotee_binary = _audiotee_binary_exists()
    has_blackhole_device = _blackhole_device_present()
    audiotee_proof: StepResult | None = None

    details: dict[str, Any] = {
        "route": None,
        "audiotee_binary": str(audiotee_path),
        "audiotee_present": has_audiotee_binary,
        "blackhole_present": has_blackhole_device,
    }

    if has_audiotee_binary:
        audiotee_proof = _audiotee_run_proof(audiotee_path)
        details["audiotee_proof"] = {
            "ok": audiotee_proof.ok,
            "message": audiotee_proof.message,
            "details": audiotee_proof.details,
        }
        if audiotee_proof.ok:
            return StepResult(
                ok=True,
                message="AudioTee route available (auto-detection of meeting apps).",
                details={**details, "route": "audiotee"},
            )

    if has_blackhole_device:
        routed, output_device = _blackhole_default_output_configured()
        has_audio, level = _blackhole_has_audio()
        details["blackhole_routed"] = routed
        details["blackhole_output_device"] = output_device
        details["blackhole_has_audio"] = has_audio
        details["blackhole_audio_level"] = level

        if not routed:
            return StepResult(
                ok=False,
                message=(
                    "BlackHole 2ch is installed, but the macOS output is not set to a "
                    "Multi-Output Device. Open Audio MIDI Setup and choose 'OnCue Output' "
                    "as the default output, or switch back to AudioTee."
                ),
                details={**details, "route": "blackhole", "error": "not_routed"},
            )

        if not has_audio:
            return StepResult(
                ok=False,
                message=(
                    "BlackHole is routed correctly, but no audio is audible right now. "
                    "Start a video call or play some sound to test the route."
                ),
                details={**details, "route": "blackhole", "error": "no_audio_flow"},
            )

        return StepResult(
            ok=True,
            message=(
                "BlackHole 2ch fallback available and active "
                f"(output: {output_device.get('name', 'unknown')})."
            ),
            details={**details, "route": "blackhole"},
        )

    if audiotee_proof is not None and not audiotee_proof.ok:
        return StepResult(
            ok=False,
            message=audiotee_proof.message,
            details={**details, "error": "audiotee_proof_failed"},
        )

    return StepResult(
        ok=False,
        message=(
            "No audio routing found. Install BlackHole 2ch (brew install --cask blackhole-2ch) "
            "or place the AudioTee binary at ./bin/audiotee."
        ),
        details=details,
    )


def _whisper_cpp_model_present(config: TranscriberConfig) -> bool:
    path = Path(config.whisper_cpp_model_path)
    return path.exists() and path.stat().st_size > 0


def _load_install_whisper_model() -> Any:
    """Load scripts/install_whisper_model.py as a module so we can reuse MODEL_MAP."""

    script_path = Path("scripts/install_whisper_model.py")
    if not script_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("install_whisper_model", script_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mlx_model_present(model_alias: str) -> bool:
    """Check whether an MLX Whisper model snapshot exists in the HF cache."""

    module = _load_install_whisper_model()
    if module is None:
        return False
    model_map = getattr(module, "MODEL_MAP", None)
    if model_map is None:
        return False
    spec = model_map.get(model_alias)
    if spec is None:
        return False

    try:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo_id=spec.repo_id,
            local_files_only=True,
            resume_download=True,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def detect_model() -> StepResult:
    """Detect whether the configured Whisper model is already present locally."""

    config = TranscriberConfig.from_env()
    backend = config.backend.strip().lower()
    model_alias = config.model

    details: dict[str, Any] = {
        "backend": backend,
        "model": model_alias,
        "whisper_cpp_model_path": config.whisper_cpp_model_path,
    }

    if backend == "whisper.cpp":
        if _whisper_cpp_model_present(config):
            return StepResult(
                ok=True,
                message=f"Whisper.cpp model found: {config.whisper_cpp_model_path}.",
                details=details,
            )
        return StepResult(
            ok=False,
            message=(
                f"Whisper.cpp model not found: {config.whisper_cpp_model_path}. "
                f"Download with: python scripts/install_whisper_model.py --model {model_alias}"
            ),
            details={**details, "error": "model_missing"},
        )

    if backend == "mlx-whisper":
        present = _mlx_model_present(model_alias)
        details["huggingface_cache"] = present
        if present:
            return StepResult(
                ok=True,
                message=f"MLX Whisper model '{model_alias}' present in Hugging Face cache.",
                details=details,
            )
        return StepResult(
            ok=False,
            message=(
                f"MLX Whisper model '{model_alias}' not found in cache. "
                f"Download with: python scripts/install_whisper_model.py --model {model_alias}"
            ),
            details={**details, "error": "model_missing"},
        )

    return StepResult(
        ok=False,
        message=f"Unknown Whisper backend '{backend}'.",
        details={**details, "error": "unknown_backend"},
    )


def provider_key_env(provider: str) -> str | None:
    """Return the env var holding the API key for ``provider``.

    ``None`` means the provider needs no API key in ``.env`` (``ollama`` uses a
    local base URL, ``vertex`` uses Application Default Credentials) or the
    provider is unknown. This is the single source of truth shared by the
    detector and the wizard's ``.env`` writer, so the two never disagree about
    which key a provider requires.
    """

    mapping = {
        "openrouter": "OPENROUTER_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
        "openai": "OPENAI_API_KEY",
        "azure": "AZURE_OPENAI_API_KEY",
        "ollama": None,
        "vertex": None,
    }
    return mapping.get(provider.strip().lower())


def detect_provider() -> StepResult:
    """Detect whether the configured LLM provider has credentials."""

    provider = _wizard_env("LLM_PROVIDER")

    if not provider:
        return StepResult(
            ok=False,
            message="LLM_PROVIDER is not set. Copy .env.example to .env and choose a provider.",
            details={"error": "provider_not_set"},
        )

    provider = provider.strip().lower()
    key_env = provider_key_env(provider)

    details: dict[str, Any] = {"provider": provider, "key_env": key_env}

    if provider not in {"openrouter", "gemini", "vertex", "groq", "openai", "azure", "ollama"}:
        return StepResult(
            ok=False,
            message=f"Unknown LLM provider '{provider}'. Set LLM_PROVIDER in .env.",
            details={**details, "error": "unknown_provider"},
        )

    if provider == "ollama":
        base_url = _wizard_env("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
        details["base_url"] = base_url
        return StepResult(
            ok=True,
            message=f"Provider 'ollama' selected; make sure Ollama is running at {base_url}.",
            details=details,
        )

    if provider == "vertex":
        return StepResult(
            ok=True,
            message=(
                "Provider 'vertex' uses Application Default Credentials; "
                "check that gcloud is configured."
            ),
            details=details,
        )

    key = _wizard_env(key_env) if key_env else None
    if not key:
        return StepResult(
            ok=False,
            message=f"Provider '{provider}' requires {key_env} in .env.",
            details={**details, "error": "missing_key"},
        )

    if len(key) < 8:
        return StepResult(
            ok=False,
            message=f"{key_env} looks too short; check your API key.",
            details={**details, "error": "key_too_short"},
        )

    return StepResult(
        ok=True,
        message=f"Provider '{provider}' is configured with {key_env}.",
        details=details,
    )


_AUTOSTART_PLIST_LABEL = "nl.vnx.sales-copilot"
_AUTOSTART_PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{_AUTOSTART_PLIST_LABEL}.plist"


def detect_autostart_consent_arm() -> StepResult:
    """Detect whether the optional macOS login autostart agent is installed.

    The agent is considered armed when the launchd plist exists, is enabled for
    ``RunAtLoad``, and consent-tracking is turned on so every auto-started
    session is auditable.
    """

    _ensure_wizard_env_loaded()

    from sales_copilot.core.consent import consent_tier, consent_tracking_enabled

    active_tier = consent_tier()
    if active_tier == "off":
        return StepResult(
            ok=False,
            message=(
                "Autostart agent found, but CONSENT_TIER=off. "
                "Autostart without a consent gate is not allowed."
            ),
            details={
                "error": "consent_tier_off",
                "consent_tier": active_tier,
                "path": str(_AUTOSTART_PLIST_PATH),
            },
        )

    if not _AUTOSTART_PLIST_PATH.exists():
        return StepResult(
            ok=False,
            message=(
                "No launchd autostart agent found. "
                "Install it via bash scripts/first-run.sh or add it manually."
            ),
            details={
                "error": "plist_missing",
                "path": str(_AUTOSTART_PLIST_PATH),
            },
        )

    try:
        with _AUTOSTART_PLIST_PATH.open("rb") as handle:
            plist = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        return StepResult(
            ok=False,
            message=f"Autostart agent plist is unreadable: {exc}",
            details={
                "error": "plist_unreadable",
                "path": str(_AUTOSTART_PLIST_PATH),
            },
        )

    label = plist.get("Label")
    run_at_load = bool(plist.get("RunAtLoad", False))
    details: dict[str, Any] = {
        "path": str(_AUTOSTART_PLIST_PATH),
        "label": label,
        "run_at_load": run_at_load,
    }

    if label != _AUTOSTART_PLIST_LABEL:
        return StepResult(
            ok=False,
            message=f"LaunchAgent label does not match (found: {label}).",
            details={**details, "error": "label_mismatch"},
        )

    if not run_at_load:
        return StepResult(
            ok=False,
            message="Autostart agent does not have RunAtLoad enabled.",
            details={**details, "error": "run_at_load_disabled"},
        )

    consent_armed = consent_tracking_enabled()
    details["consent_tracking_enabled"] = consent_armed
    details["consent_tier"] = active_tier
    if not consent_armed:
        return StepResult(
            ok=False,
            message=(
                "Autostart agent found, but consent tracking is off. "
                "Set CONSENT_TRACKING_ENABLED=true in .env."
            ),
            details={**details, "error": "consent_tracking_disabled"},
        )

    return StepResult(
        ok=True,
        message="Autostart agent installed and consent-arm active.",
        details=details,
    )


def detect_calltap_setup() -> StepResult:
    """Detect whether the telephony call-tap can be opened.

    Phone-call audio on macOS flows through the process configured by
    ``CALL_PROCESS_NAME`` (default ``avconferenced``). This detector checks that
    the AudioTee binary is present and that the telephony process is currently
    running, then performs a short permission-proof tap against it.
    """

    _ensure_wizard_env_loaded()

    from sales_copilot.audio.capture import _find_pid

    audiotee_path = _env_path("AUDIOTEE_BINARY_PATH", "./bin/audiotee")
    if not audiotee_path.exists() or not os.access(audiotee_path, os.X_OK):
        return StepResult(
            ok=False,
            message=(
                f"AudioTee binary not found at {audiotee_path}. "
                "Call-tap requires a built AudioTee binary."
            ),
            details={
                "error": "binary_missing",
                "path": str(audiotee_path),
            },
        )

    process_name = env("CALL_PROCESS_NAME", "avconferenced") or "avconferenced"
    pid = _find_pid(process_name)
    if pid is None:
        return StepResult(
            ok=False,
            message=(
                f"Telephony process '{process_name}' not found. "
                "Start an iPhone-relay or FaceTime call to test the call-tap."
            ),
            details={"error": "no_telephony_process", "process_name": process_name},
        )

    proof = _audiotee_run_proof(
        audiotee_path, extra_args=["--include-processes", str(pid)]
    )
    if not proof.ok:
        return StepResult(
            ok=False,
            message=f"Call-tap permission check failed: {proof.message}",
            details={
                "error": "calltap_proof_failed",
                "pid": pid,
                "process_name": process_name,
                "proof": {
                    "ok": proof.ok,
                    "message": proof.message,
                    "details": proof.details,
                },
            },
        )

    return StepResult(
        ok=True,
        message=f"Call-tap setup OK (AudioTee can tap '{process_name}', pid {pid}).",
        details={"pid": pid, "process_name": process_name, "audiotee_path": str(audiotee_path)},
    )


def detect_doctor(
    results: dict[str, StepResult] | None = None,
    steps: list[str] | None = None,
    detectors: dict[str, Any] | None = None,
) -> StepResult:
    """Final health check that re-verifies every required step.

    The doctor does not blindly trust cached results: it re-runs each detector
    and fails if the live state no longer matches the cached pass. This catches
    permission revocations, unplugged microphones, or half-finished runs.
    """

    prior = results or {}
    required = steps or ["mic", "route", "model", "provider"]
    detectors = detectors or {}

    failed = [sid for sid in required if not prior.get(sid, StepResult(ok=False, message="not run")).ok]

    reverify_failures: list[str] = []
    reverify_results: dict[str, Any] = {}
    for sid in required:
        detector = detectors.get(sid)
        if detector is None:
            reverify_results[sid] = {"ran": False, "reason": "no_detector"}
            continue
        try:
            live = detector()
        except Exception as exc:  # noqa: BLE001
            live = StepResult(ok=False, message=f"Live check failed: {exc}", details={"error": "exception"})
        reverify_results[sid] = {"ok": live.ok, "message": live.message}
        if not live.ok:
            reverify_failures.append(sid)

    env_exists = Path(".env").exists()
    details = {
        "required_steps": required,
        "failed_steps": failed,
        "reverify_failures": reverify_failures,
        "reverify_results": reverify_results,
        "env_exists": env_exists,
    }

    if failed:
        return StepResult(
            ok=False,
            message=f"Wizard not complete. Restart with: python -m sales_copilot.wizard ({', '.join(failed)})",
            details=details,
        )

    if reverify_failures:
        return StepResult(
            ok=False,
            message=(
                "One or more checks failed live: "
                f"{', '.join(reverify_failures)}. Restart the wizard to check again."
            ),
            details=details,
        )

    if not env_exists:
        return StepResult(
            ok=False,
            message=".env does not exist yet. Copy .env.example to .env and fill in your API keys.",
            details=details,
        )

    return StepResult(
        ok=True,
        message="All checks OK. Start OnCue with: python -m sales_copilot",
        details=details,
    )
