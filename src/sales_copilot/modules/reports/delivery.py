"""Deliver a finished call report to a customer-configured trigger destination.

The customer buys OnCue as a trigger: local capture and transcription, and
when the call ends the report lands wherever they point their own automation
at -- a directory, an HTTP endpoint, or both. Both sinks are optional and
independent, and both default OFF (``ReportDeliveryConfig.directory`` /
``.endpoint`` are ``None``): a fresh install behaves exactly as before.

This is a deliberate, documented THIRD outbound class alongside the two
already described in ``docs/ARCHITECTURE_BOUNDARIES.md`` -- see "Trigger
delivery outbound class" there for the full argument. In short: unlike the
deep-insight lane, this class does not go through ``core/outbound_policy.py``,
because the destination is not an LLM/AI provider and the whole point of the
feature is handing the operator's own automation their own words verbatim so
it can act on them (extract a name, a company, a deal detail). PII redaction
for this payload is controlled by the existing ``REPORT_REDACT_PII`` flag
(the same one that governs the local ``data/reports/`` copy): whatever shape
the reports module wrote to disk (``generator.write_report``, rewritten with the
post-call enrichment by ``generator.rewrite_with_enrichment``) is exactly what is
delivered here, never a second, differently-redacted payload.

Delivery is best-effort and never risks the local copy:

  - **Directory**: an atomic write (temp file + ``os.replace`` in the same
    directory) so a watcher never sees a half-written file. A failure is
    logged loudly; the local report is untouched.
  - **HTTP endpoint**: one POST per report, with a timeout and a bounded
    number of attempts (linear backoff between them). Exhausting the retries
    logs a loud, actionable error naming the local report path so the
    operator can redeliver manually once the endpoint recovers -- never a
    silently dropped transcript. There is no queue and no persistent retry
    across process restarts; that is a deliberate scope limit (see
    ``docs/MODULE4.md``), not an oversight.

Both sinks run through :func:`deliver_report_in_background` on a daemon
thread so a slow or hung endpoint can never delay the caller -- the report
generator's own local write (the operator's guaranteed copy) has already
completed by the time this module is invoked.

Operator decision 2026-09-28: the endpoint sink is Pro/Enterprise
(``FEATURE_REPORT_DELIVERY_ENDPOINT``, see ``auth/feature_policy.py``); the
directory sink stays Free. On Free, a configured endpoint is skipped -- one
WARNING per process, never an exception, never a silent no-op -- while the
directory sink (if configured) still runs. ``validate_report_delivery_config``
is unaffected: configuring an endpoint is always allowed, only delivering to
it is gated.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from sales_copilot.auth.feature_policy import (
    FEATURE_REPORT_DELIVERY_ENDPOINT,
    FeaturePolicy,
    get_feature_policy,
)
from sales_copilot.core.config import ReportDeliveryConfig

logger = logging.getLogger(__name__)

# Operator decision 2026-09-28: endpoint delivery is Pro/Enterprise; the
# directory sink stays Free. Logged once per process (not once per call) so a
# Free operator who leaves REPORT_DELIVERY_ENDPOINT configured doesn't get a
# WARNING flood, one per finished call, for the life of the process.
_endpoint_gate_warned = False
_endpoint_gate_warned_lock = threading.Lock()


def _warn_endpoint_gated_once() -> None:
    global _endpoint_gate_warned
    with _endpoint_gate_warned_lock:
        if _endpoint_gate_warned:
            return
        _endpoint_gate_warned = True
    logger.warning(
        "REPORT_DELIVERY_ENDPOINT is configured but endpoint delivery is a Pro "
        "feature (%s); skipping the endpoint sink for this and future reports. "
        "Directory delivery, if configured, is unaffected.",
        FEATURE_REPORT_DELIVERY_ENDPOINT,
    )


class ReportDeliveryConfigError(ValueError):
    """A configured delivery destination cannot work.

    Raised at startup (see :func:`validate_report_delivery_config`) so the
    operator sees an actionable message before the first call ends, not
    after.
    """


class ReportDeliveryError(RuntimeError):
    """A single HTTP delivery attempt failed (non-2xx response)."""


def validate_report_delivery_config(config: ReportDeliveryConfig) -> None:
    """Fail fast on a delivery destination that cannot work.

    A configured directory that does not exist, is not a directory, or is not
    writable raises HERE -- with the exact path and reason -- instead of at
    the end of the first call, when the operator is in the worst position to
    notice or fix it. A configured endpoint is checked for a plausible
    http(s) URL shape only; reachability is not (and cannot usefully be)
    checked at startup.
    """
    if config.directory:
        directory = Path(config.directory)
        if not directory.exists():
            raise ReportDeliveryConfigError(
                f"REPORT_DELIVERY_DIR={directory} does not exist. Create the "
                "directory (or mount the network share) before starting the "
                "reports module."
            )
        if not directory.is_dir():
            raise ReportDeliveryConfigError(
                f"REPORT_DELIVERY_DIR={directory} exists but is not a directory."
            )
        if not os.access(directory, os.W_OK):
            raise ReportDeliveryConfigError(
                f"REPORT_DELIVERY_DIR={directory} is not writable by this process."
            )
    if config.endpoint:
        parsed = urlparse(config.endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ReportDeliveryConfigError(
                f"REPORT_DELIVERY_ENDPOINT={config.endpoint!r} is not a valid http(s) URL."
            )


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` atomically: temp file + ``os.replace``.

    The temp file is created in the SAME directory as ``path`` so the final
    rename is a single filesystem operation (same mount, including a mounted
    network share) rather than a cross-filesystem copy. A watcher polling the
    directory therefore only ever sees the old file or the fully-written new
    one, never a partial write. Permissions are widened to 0644 (rather than
    ``tempfile.mkstemp``'s default 0600) because the reader is, by design, a
    different process/user -- the operator's own automation.
    """
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def deliver_to_directory(
    payload: bytes,
    filename: str,
    directory: str,
    *,
    session_id: str,
    local_report_path: Path | None,
) -> None:
    """Atomically copy ``payload`` into ``directory`` under ``filename``.

    Best-effort: any failure (permission revoked mid-call, share unmounted)
    is logged loudly and never raised -- the local report already written by
    ``generator.write_report`` is the record of truth and is unaffected.
    """
    destination = Path(directory) / filename
    try:
        _atomic_write(destination, payload)
    except OSError:
        logger.error(
            "Report delivery to directory failed for session=%s destination=%s; "
            "local copy at %s is unaffected.",
            session_id,
            destination,
            local_report_path,
            exc_info=True,
        )
        return
    logger.info("Report delivered to directory for session=%s: %s", session_id, destination)


def _post_once(endpoint: str, payload: bytes, timeout_s: float) -> None:
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - operator-configured destination
        status = getattr(response, "status", None) or response.getcode()
        if not (200 <= status < 300):
            raise ReportDeliveryError(f"endpoint returned HTTP {status}")


def deliver_to_endpoint(
    payload: bytes,
    endpoint: str,
    *,
    session_id: str,
    local_report_path: Path | None,
    timeout_s: float,
    max_attempts: int,
    retry_backoff_s: float,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """POST ``payload`` to ``endpoint``, with a bounded number of attempts.

    Attempts are separated by a linear backoff (``retry_backoff_s * attempt``)
    so a transient blip gets a couple of quick retries without hammering a
    genuinely down endpoint. Exhausting every attempt logs one loud ERROR
    naming the local report path -- the operator's copy is never touched by
    this function, so nothing is lost, only undelivered.
    """
    attempts = max(1, max_attempts)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            _post_once(endpoint, payload, timeout_s)
        except (urllib.error.URLError, OSError, TimeoutError, ReportDeliveryError) as exc:
            last_exc = exc
            logger.warning(
                "Report delivery attempt %d/%d to %s failed for session=%s: %s",
                attempt,
                attempts,
                endpoint,
                session_id,
                exc,
            )
            if attempt < attempts:
                sleep(retry_backoff_s * attempt)
            continue
        else:
            logger.info(
                "Report delivered to endpoint for session=%s on attempt %d/%d: %s",
                session_id,
                attempt,
                attempts,
                endpoint,
            )
            return

    logger.error(
        "Report delivery to endpoint EXHAUSTED %d attempt(s) for session=%s endpoint=%s: %s. "
        "The local copy at %s is unaffected; redeliver manually once the endpoint recovers.",
        attempts,
        session_id,
        endpoint,
        last_exc,
        local_report_path,
    )


def deliver_report(
    payload: bytes,
    filename: str,
    config: ReportDeliveryConfig,
    *,
    session_id: str,
    local_report_path: Path | None,
    sleep: Callable[[float], None] = time.sleep,
    feature_policy: FeaturePolicy | None = None,
) -> None:
    """Run every configured sink for one report. Synchronous; see below for non-blocking use.

    The endpoint sink is Pro/Enterprise (``FEATURE_REPORT_DELIVERY_ENDPOINT``);
    on Free it is skipped with a one-time WARNING and the directory sink still
    runs. ``validate_report_delivery_config`` does not gate on entitlement --
    configuring an endpoint is always allowed, only delivering to it is Pro.
    """
    if config.directory:
        deliver_to_directory(
            payload, filename, config.directory, session_id=session_id, local_report_path=local_report_path
        )
    if config.endpoint:
        policy = feature_policy or get_feature_policy()
        if policy.allows(FEATURE_REPORT_DELIVERY_ENDPOINT):
            deliver_to_endpoint(
                payload,
                config.endpoint,
                session_id=session_id,
                local_report_path=local_report_path,
                timeout_s=config.endpoint_timeout_s,
                max_attempts=config.endpoint_max_attempts,
                retry_backoff_s=config.endpoint_retry_backoff_s,
                sleep=sleep,
            )
        else:
            _warn_endpoint_gated_once()


def deliver_report_in_background(
    payload: bytes,
    filename: str,
    config: ReportDeliveryConfig,
    *,
    session_id: str,
    local_report_path: Path | None,
    feature_policy: FeaturePolicy | None = None,
) -> threading.Thread | None:
    """Fire-and-forget wrapper around :func:`deliver_report` on a daemon thread.

    Returns ``None`` (no thread started) when neither sink is configured --
    the fast, zero-overhead default path. Runs on a background thread so a
    slow or hung endpoint never delays the caller (the reports module's
    post-call shutdown, which must reach the dashboard promptly regardless of
    delivery outcome).
    """
    if not (config.directory or config.endpoint):
        return None
    thread = threading.Thread(
        target=deliver_report,
        args=(payload, filename, config),
        kwargs={
            "session_id": session_id,
            "local_report_path": local_report_path,
            "feature_policy": feature_policy,
        },
        name="report-delivery",
        daemon=True,
    )
    thread.start()
    return thread
