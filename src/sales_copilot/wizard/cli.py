"""CLI runner for the first-run permissions wizard."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from sales_copilot.auth.feature_policy import FeaturePolicy, get_feature_policy
from sales_copilot.auth.license_format import FEATURE_AUTOSTART, FEATURE_CALLTAP
from sales_copilot.core.config import load_env
from sales_copilot.wizard.detectors import (
    detect_autostart_consent_arm,
    detect_calltap_setup,
    detect_doctor,
    detect_mic,
    detect_model,
    detect_provider,
    detect_route,
    run_mic_proof,
)
from sales_copilot.wizard.meter import terminal_emitter
from sales_copilot.wizard.state import WizardState, load_state, save_state
from sales_copilot.wizard.steps import StepResult, WizardStep

_CORE_STEP_DEFINITIONS: list[WizardStep] = [
    WizardStep(
        step_id="mic",
        title="Microphone",
        auto_detect=detect_mic,
        needs_meter=True,
        proof=run_mic_proof,
    ),
    WizardStep(
        step_id="route",
        title="Audio route (AudioTee / BlackHole)",
        auto_detect=detect_route,
        needs_meter=False,
    ),
    WizardStep(
        step_id="model",
        title="Whisper model",
        auto_detect=detect_model,
        needs_meter=False,
    ),
    WizardStep(
        step_id="provider",
        title="LLM provider",
        auto_detect=detect_provider,
        needs_meter=False,
    ),
]

_PRO_STEP_DEFINITIONS: list[WizardStep] = [
    WizardStep(
        step_id="autostart",
        title="Autostart & consent-arm",
        auto_detect=detect_autostart_consent_arm,
        feature_id=FEATURE_AUTOSTART,
    ),
    WizardStep(
        step_id="calltap",
        title="Call-tap setup (iPhone-relay)",
        auto_detect=detect_calltap_setup,
        feature_id=FEATURE_CALLTAP,
    ),
]

_DOCTOR_STEP_DEFINITION: WizardStep = WizardStep(
    step_id="doctor",
    title="Doctor",
    auto_detect=detect_doctor,
    needs_meter=False,
)

_W1_STEP_IDS = frozenset({"mic", "route", "model", "provider"})


def get_steps(feature_policy: FeaturePolicy | None = None) -> list[WizardStep]:
    """Return the ordered list of wizard steps for the active tier."""

    policy = feature_policy or get_feature_policy()

    steps: list[WizardStep] = list(_CORE_STEP_DEFINITIONS)

    for pro_step in _PRO_STEP_DEFINITIONS:
        if policy.allows(pro_step.feature_id):
            steps.append(pro_step)

    steps.append(_DOCTOR_STEP_DEFINITION)

    return steps



def _print_banner() -> None:
    print("=" * 56)
    print("  OnCue — first-run wizard")
    print("  Automatic checks (no manual checkboxes)")
    print("=" * 56)
    print()


def _print_pro_hints(feature_policy: FeaturePolicy, file: Any) -> None:
    """Print a clear upgrade hint for every Pro-only step locked in this tier."""

    locked = [
        step for step in _PRO_STEP_DEFINITIONS if not feature_policy.allows(step.feature_id)
    ]
    if not locked:
        return

    print("The following Pro-only features are not visible in the free tier:", file=file)
    for step in locked:
        print(
            f"  • {step.title}: upgrade to Pro to enable this step.",
            file=file,
        )
    print(file=file)


def _print_step(step: WizardStep, index: int, total: int) -> None:
    print(f"Step {index + 1}/{total}: {step.title}")
    print("-" * 40)


def _print_result(result: StepResult) -> None:
    icon = "✓" if result.ok else "✗"
    print(f"{icon} {result.message}")
    print()


def run_wizard(
    *,
    restart: bool = False,
    state_path: Any = None,
    steps: list[WizardStep] | None = None,
    file: Any = sys.stdout,
    meter_emitter_factory: Any = None,
    feature_policy: FeaturePolicy | None = None,
) -> tuple[WizardState, dict[str, StepResult], bool]:
    """Run the wizard, resuming from persisted state unless ``restart`` is True.

    Returns ``(state, results, all_ok)``. ``meter_emitter_factory`` is injected
    for tests; the default renders a live terminal bar.
    """

    policy = feature_policy or get_feature_policy()
    step_list = steps or get_steps(policy)
    state = WizardState() if restart else load_state(state_path)
    if restart:
        save_state(state, state_path)

    results: dict[str, StepResult] = {}
    all_ok = True

    def _print(text: str = "") -> None:
        print(text, file=file)

    _print_pro_hints(policy, file)

    for index, step in enumerate(step_list):
        if step.step_id == "doctor":
            # Doctor is the final health-check: it re-verifies every W1 step live,
            # regardless of whether Pro-only steps are also present in this run.
            w1_steps = [s.step_id for s in step_list if s.step_id in _W1_STEP_IDS]
            detectors = {s.step_id: s.auto_detect for s in step_list if s.step_id in _W1_STEP_IDS}
            result = detect_doctor(
                results,
                steps=w1_steps,
                detectors=detectors,
            )
            results[step.step_id] = result
            _print(f"✓ {result.message}" if result.ok else f"✗ {result.message}")
            _print()
            all_ok = all_ok and result.ok
            state.mark_completed(step.step_id, {"ok": result.ok, "message": result.message})
            save_state(state, state_path)
            continue

        if state.is_completed(step.step_id):
            prior = state.results.get(step.step_id, {})
            if not prior:
                # A half-finished run may mark a step completed without storing
                # the result. Do not treat that as a success; force re-run.
                results[step.step_id] = StepResult(
                    ok=False,
                    message=f"{step.title}: status unknown (incomplete); check again.",
                    details={"error": "incomplete_resume"},
                )
                _print(f"  (!) {results[step.step_id].message}")
                all_ok = False
                _print("Wizard stopped at this step. Restart later to continue.")
                _print()
                break

            results[step.step_id] = StepResult(
                ok=prior.get("ok", False),
                message=prior.get("message", f"already completed: {step.title}"),
                details=prior.get("details", {}),
            )
            _print(f"  (already completed: {step.title})")
            continue

        _print(f"Step {index + 1}/{len(step_list)}: {step.title}")
        _print("-" * 40)

        emitter = None
        if step.needs_meter and step.proof is not None:
            emitter = meter_emitter_factory() if meter_emitter_factory else terminal_emitter()
        result = step.run(emitter=emitter)

        results[step.step_id] = result
        _print(f"✓ {result.message}" if result.ok else f"✗ {result.message}")
        _print()

        if not result.ok:
            all_ok = False
            _print("Wizard stopped at this step. Restart later to continue.")
            _print()
            break

        state.mark_completed(
            step.step_id,
            {"ok": result.ok, "message": result.message, "details": result.details},
        )
        save_state(state, state_path)

    return state, results, all_ok


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OnCue first-run permissions wizard (CLI).",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Start over and clear previously saved progress.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=None,
        help="Path to the resume-state JSON (default: data/wizard_state.json).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_env()

    args = parse_args(argv)
    state_path = args.state_path if args.state_path else None

    _print_banner()
    _state, _results, all_ok = run_wizard(restart=args.restart, state_path=state_path)

    if all_ok:
        print("Wizard completed successfully.")
        return 0
    print("Wizard incomplete. Fix the failing step and start over.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
