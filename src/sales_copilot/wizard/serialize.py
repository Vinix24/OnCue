"""JSON-safe serialization for wizard step definitions.

The browser wizard is a view on the same ``WizardStep`` objects used by the CLI.
This module converts step metadata to plain dicts; it never serializes the
``auto_detect`` / ``proof`` callables.
"""

from __future__ import annotations

from typing import Any

from sales_copilot.auth.feature_policy import FeaturePolicy, get_feature_policy
from sales_copilot.wizard.cli import (
    _CORE_STEP_DEFINITIONS,
    _DOCTOR_STEP_DEFINITION,
    _PRO_STEP_DEFINITIONS,
    get_steps,
)
from sales_copilot.wizard.steps import WizardStep


def step_descriptor(
    step: WizardStep,
    policy: FeaturePolicy,
    active_step_ids: set[str],
) -> dict[str, Any]:
    """Return a JSON-safe description of ``step`` for the browser view.

    A Pro step is ``locked`` when the active license tier does not allow it.
    Core steps and the doctor step are never locked.
    """

    locked = step.feature_id is not None and not policy.allows(step.feature_id)
    return {
        "step_id": step.step_id,
        "title": step.title,
        "needs_meter": step.needs_meter,
        "feature_id": step.feature_id,
        "locked": locked,
        "active": step.step_id in active_step_ids,
    }


def get_step_descriptors(feature_policy: FeaturePolicy | None = None) -> list[dict[str, Any]]:
    """Return metadata for every wizard step in display order.

    The order matches the CLI: core steps, Pro steps, doctor. Pro steps are
    included even when locked so the UI can show an upgrade hint.
    """

    policy = feature_policy or get_feature_policy()
    active_steps = get_steps(policy)
    active_step_ids = {s.step_id for s in active_steps}

    all_steps: list[WizardStep] = [
        *_CORE_STEP_DEFINITIONS,
        *_PRO_STEP_DEFINITIONS,
        _DOCTOR_STEP_DEFINITION,
    ]
    return [step_descriptor(step, policy, active_step_ids) for step in all_steps]
