"""Declarative wizard step definitions.

The wizard is driven by a list of ``WizardStep`` objects. Each step declares how
to auto-detect its own OS status and, optionally, how to run a short live proof
that renders a dB meter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StepResult:
    """Outcome of a wizard step's auto-detection."""

    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WizardStep:
    """One step in the first-run wizard.

    Attributes:
        step_id: Stable identifier used for resume state.
        title: Human-readable heading shown in the CLI.
        auto_detect: Callable that returns the current OS status. Must not block
            on user input.
        needs_meter: When True the CLI may render a live dB meter during the
            proof phase (e.g. microphone test).
        proof: Optional callable that performs a short live proof and returns a
            ``StepResult``.
    """

    step_id: str
    title: str
    auto_detect: Callable[[], StepResult]
    needs_meter: bool = False
    proof: Callable[..., StepResult] | None = None
    feature_id: str | None = None

    def run(self, emitter: Any = None) -> StepResult:
        """Run auto-detection; if a proof exists and detection passed, run it.

        ``emitter`` is forwarded to proofs that render a live meter (e.g. the
        microphone level proof). Callers should always invoke this method so
        auto-detection runs before any proof.
        """

        result = self.auto_detect()
        if result.ok and self.proof is not None:
            return self.proof(emitter=emitter)
        return result
