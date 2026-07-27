"""Resume state persistence for the first-run wizard."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sales_copilot.core.paths import resolve_app_path

DEFAULT_STATE_PATH = Path(
    os.environ.get("WIZARD_STATE_PATH") or resolve_app_path("data/wizard_state.json")
)


@dataclass
class WizardState:
    """Persisted progress through the wizard.

    ``completed`` holds the step_id of every step that finished successfully.
    ``results`` keeps the latest ``StepResult`` details per step so the doctor
    step can aggregate them without re-running every detector.
    """

    completed: list[str] = field(default_factory=list)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    env_written: bool = False

    def is_completed(self, step_id: str) -> bool:
        return step_id in self.completed

    def mark_completed(self, step_id: str, result: dict[str, Any] | None = None) -> None:
        if step_id not in self.completed:
            self.completed.append(step_id)
        if result is not None:
            self.results[step_id] = {
                "ok": bool(result.get("ok", True)),
                "message": str(result.get("message", "")),
                "details": result.get("details", {}),
            }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WizardState:
        return cls(
            completed=list(data.get("completed", [])),
            results=dict(data.get("results", {})),
            env_written=bool(data.get("env_written", False)),
        )


def load_state(path: Path | None = None) -> WizardState:
    """Load wizard state from disk, or return a fresh state if absent/corrupt."""

    target = path or DEFAULT_STATE_PATH
    if not target.exists():
        return WizardState()
    try:
        with target.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return WizardState()
        return WizardState.from_dict(data)
    except (json.JSONDecodeError, OSError):
        return WizardState()


def save_state(state: WizardState, path: Path | None = None) -> None:
    """Persist wizard state to disk."""

    target = path or DEFAULT_STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(state.to_dict(), handle, indent=2)
