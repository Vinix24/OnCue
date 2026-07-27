"""First-run permissions wizard (CLI slice W1).

A steps-as-data wizard that auto-detects OS-level readiness for microphone,
audio routing, the Whisper model, and the LLM provider. Resumable and aimed at
non-technical users.
"""

from __future__ import annotations

from sales_copilot.wizard.cli import get_steps, main
from sales_copilot.wizard.state import WizardState
from sales_copilot.wizard.steps import StepResult, WizardStep

__all__ = ["WizardState", "WizardStep", "StepResult", "get_steps", "main"]
