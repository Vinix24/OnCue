"""Objective telephony-tap gate for the recorder engine.

Phone-call audio on macOS is routed through the ``avconferenced`` daemon. A
free-tier user must not be able to target that process, so this module provides
a PID-based guard that the ``RecorderEngine`` consults before opening a
targeted process tap.

Also home to ``resolve_telephony_call_target``, the ``avconferenced``-active
detector consumed by the runtime autostart monitor
(``sales_copilot.core.autostart_monitor``) as its telephony trigger source --
kept alongside the guard rather than duplicated since both need the same
process-presence resolution for the same daemon.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sales_copilot.audio.capture import _find_pid
from sales_copilot.auth.feature_policy import FEATURE_CALLTAP, FeaturePolicy, get_feature_policy

logger = logging.getLogger(__name__)

# Default telephony processes whose targeted tap requires FEATURE_CALLTAP.
DEFAULT_TELEPHONY_PROCESSES: tuple[str, ...] = ("avconferenced",)


def resolve_telephony_call_target(
    candidates: Sequence[str] = DEFAULT_TELEPHONY_PROCESSES,
) -> tuple[str | None, int | None, list[str]]:
    """Detect an active macOS phone/FaceTime call via the ``avconferenced`` daemon.

    Mirrors ``resolve_meeting_app_target``'s ambiguity-safe shape (same
    ``(process_name, pid, running_candidates)`` return contract) so it can be
    dropped into ``RuntimeAutostartMonitor`` as a second, independent
    ``DetectFn``. ``process_name``/``pid`` are only set when exactly one
    candidate resolves to a running PID -- with the single-entry default this
    just means "avconferenced is running", but the ambiguity guard is kept for
    parity with the video-path detector and in case a caller supplies more than
    one telephony process name.

    macOS only places the underlying call (Continuity/iPhone-relay); this
    function only observes that ``avconferenced`` is already active for one.
    """

    running: list[tuple[str, int]] = []
    for name in candidates:
        pid = _find_pid(name)
        if pid is not None:
            running.append((name, pid))
    if len(running) == 1:
        name, pid = running[0]
        return name, pid, [name]
    return None, None, [name for name, _ in running]


@dataclass(frozen=True)
class ProcessWatcher:
    """Lightweight PID watcher for a single named process."""

    process_name: str

    def pid(self) -> int | None:
        """Return the PID of the watched process, or None if not running."""

        return _find_pid(self.process_name)

    def is_running(self) -> bool:
        return self.pid() is not None


@dataclass(frozen=True)
class TelephonyTapGuard:
    """Decide whether a targeted process tap must be excluded.

    A telephony process is excluded from a targeted tap when the current tier
    does not allow ``FEATURE_CALLTAP``. The gate no longer depends on a
    point-in-time PID check: ``avconferenced`` may start between the guard
    decision and the moment AudioTee actually opens the tap, so the exclusion
    must be enforced at tap-start (and continuously while the tap runs). For
    the same reason the guard uses the same fuzzy ``pgrep -f`` resolution that
    AudioTee uses, so a fuzzy process name cannot bypass the gate while still
    resolving to a telephony PID.
    """

    feature_policy: FeaturePolicy
    telephony_processes: tuple[str, ...] = DEFAULT_TELEPHONY_PROCESSES

    def __post_init__(self) -> None:
        # Normalize once so ``is_telephony_process`` is case-insensitive.
        object.__setattr__(
            self,
            "_telephony_names",
            frozenset(p.lower() for p in self.telephony_processes),
        )

    def _telephony_pids(self) -> set[int]:
        """PIDs currently resolved for the known telephony process names."""

        pids: set[int] = set()
        for name in self._telephony_names:  # type: ignore[attr-defined]
            pid = _find_pid(name)
            if pid is not None:
                pids.add(pid)
        return pids

    def is_telephony_process(self, process_name: str | None) -> bool:
        if not process_name:
            return False
        lower = process_name.lower()
        if lower in self._telephony_names:  # type: ignore[attr-defined]
            return True
        # Fuzzy/pgrep match: the supplied name may be a substring such as
        # "avconference" but still resolve, via ``pgrep -f``, to the same PID
        # as one of the canonical telephony processes. Close that name-gap by
        # using the same resolution AudioTee will use later.
        pid = _find_pid(process_name)
        if pid is None:
            return False
        return pid in self._telephony_pids()

    def is_target_excluded(
        self,
        process_name: str | None,
        watcher: ProcessWatcher | None = None,
    ) -> bool:
        if self.feature_policy.allows(FEATURE_CALLTAP):
            return False
        if not self.is_telephony_process(process_name):
            return False
        logger.warning(
            "Telephony process '%s' is blocked in the free tier; "
            "FEATURE_CALLTAP is not entitled.",
            process_name,
        )
        return True

    def excluded_telephony_processes(self) -> tuple[str, ...]:
        """Return the telephony process names that must be excluded from a tap.

        In the free tier these are the processes that must never be captured,
        even via a whole-system ``tap_all`` fallback.

        On the owner's own dev build this is always an empty tuple: dev builds
        default ``FeaturePolicy.current_tier()`` to unconditional
        ``"enterprise"`` (see ``feature_policy.FeaturePolicy.current_tier``),
        which has ``FEATURE_CALLTAP``, so ``tap_all`` naturally picks up the
        telephony daemon (``avconferenced``) without a separate dev-build
        check here -- a second, independent gate on the same feature would
        risk drifting out of sync with the tier logic above.
        """

        if self.feature_policy.allows(FEATURE_CALLTAP):
            return ()
        return self.telephony_processes


def get_telephony_tap_guard() -> TelephonyTapGuard:
    return TelephonyTapGuard(feature_policy=get_feature_policy())
