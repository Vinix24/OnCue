import io
from pathlib import Path
from unittest.mock import patch

import pytest

from sales_copilot.wizard.cli import get_steps, main, parse_args, run_wizard
from sales_copilot.wizard.state import WizardState
from sales_copilot.wizard.steps import StepResult, WizardStep


def test_get_steps_returns_ordered_five_steps_for_free(free_feature_policy: object) -> None:
    steps = get_steps(free_feature_policy)
    assert [s.step_id for s in steps] == ["mic", "route", "model", "provider", "doctor"]
    assert all(isinstance(s, WizardStep) for s in steps)
    assert all(s.feature_id is None for s in steps)


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.restart is False
    assert args.state_path is None


def test_parse_args_restart_and_state_path(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    args = parse_args(["--restart", "--state-path", str(state_path)])
    assert args.restart is True
    assert args.state_path == state_path


def test_run_wizard_skips_completed_steps_and_runs_doctor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    save_count: list[int] = []

    def _counting_save(state, path=None):  # noqa: ANN001, ANN001
        save_count.append(1)

    monkeypatch.setattr("sales_copilot.wizard.cli.save_state", _counting_save)

    state = WizardState(
        completed=["mic"],
        results={"mic": {"ok": True, "message": "mic ok", "details": {}}},
    )
    monkeypatch.setattr("sales_copilot.wizard.cli.load_state", lambda path=None: state)

    fake_steps = [
        WizardStep(
            step_id="mic",
            title="Microfoon",
            auto_detect=lambda: StepResult(ok=True, message="mic ok"),
            needs_meter=True,
        ),
        WizardStep(
            step_id="route",
            title="Route",
            auto_detect=lambda: StepResult(ok=True, message="route ok"),
        ),
        WizardStep(
            step_id="doctor",
            title="Doctor",
            auto_detect=lambda: StepResult(ok=True, message="doctor ok"),
        ),
    ]

    output = io.StringIO()
    with patch("sales_copilot.wizard.detectors.Path.exists", return_value=True):
        _state, results, all_ok = run_wizard(steps=fake_steps, state_path=state_path, file=output)

    assert all_ok is True
    assert results["mic"].ok is True  # reconstructed from stored result
    assert results["route"].ok is True
    assert results["doctor"].ok is True
    assert "already completed" in output.getvalue()


def test_run_wizard_resume_without_result_is_incomplete(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("sales_copilot.wizard.cli.save_state", lambda state, path=None: None)

    state = WizardState(completed=["mic"], results={})
    monkeypatch.setattr("sales_copilot.wizard.cli.load_state", lambda path=None: state)

    fake_steps = [
        WizardStep(
            step_id="mic",
            title="Microfoon",
            auto_detect=lambda: StepResult(ok=True, message="mic ok"),
            needs_meter=True,
        ),
        WizardStep(
            step_id="route",
            title="Route",
            auto_detect=lambda: StepResult(ok=True, message="route ok"),
        ),
    ]

    output = io.StringIO()
    _state, results, all_ok = run_wizard(steps=fake_steps, state_path=state_path, file=output)

    assert all_ok is False
    assert results["mic"].ok is False
    assert "unknown" in results["mic"].message
    assert "route" not in results
    assert "Wizard stopped" in output.getvalue()


def test_run_wizard_stops_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("sales_copilot.wizard.cli.save_state", lambda state, path=None: None)

    fake_steps = [
        WizardStep(
            step_id="mic",
            title="Microfoon",
            auto_detect=lambda: StepResult(ok=False, message="no mic"),
            needs_meter=True,
        ),
        WizardStep(
            step_id="route",
            title="Route",
            auto_detect=lambda: StepResult(ok=True, message="route ok"),
        ),
    ]

    output = io.StringIO()
    _state, results, all_ok = run_wizard(steps=fake_steps, state_path=state_path, file=output)

    assert all_ok is False
    assert results["mic"].ok is False
    assert "route" not in results
    assert "Wizard stopped" in output.getvalue()


def test_run_wizard_meter_proof_uses_injected_emitter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("sales_copilot.wizard.cli.save_state", lambda state, path=None: None)

    emitted: list[float] = []

    def _fake_emitter():
        def _emit(dbfs: float) -> None:
            emitted.append(dbfs)
        return _emit

    fake_steps = [
        WizardStep(
            step_id="mic",
            title="Microfoon",
            auto_detect=lambda: StepResult(ok=True, message="mic detected"),
            needs_meter=True,
            proof=lambda **kwargs: StepResult(ok=True, message="proof ran", details={"emitter_used": True}),
        ),
    ]

    output = io.StringIO()
    _state, results, all_ok = run_wizard(
        steps=fake_steps,
        state_path=state_path,
        file=output,
        meter_emitter_factory=_fake_emitter,
    )

    assert all_ok is True
    assert results["mic"].details.get("emitter_used") is True


def test_main_exits_zero_when_all_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("sales_copilot.wizard.cli.save_state", lambda state, path=None: None)

    fake_steps = [
        WizardStep(
            step_id="mic",
            title="Microfoon",
            auto_detect=lambda: StepResult(ok=True, message="mic ok"),
        ),
        WizardStep(
            step_id="doctor",
            title="Doctor",
            auto_detect=lambda: StepResult(ok=True, message="ok"),
        ),
    ]

    monkeypatch.setattr("sales_copilot.wizard.cli.get_steps", lambda _=None: fake_steps)

    with patch("sales_copilot.wizard.detectors.Path.exists", return_value=True):
        rc = main(["--state-path", str(state_path)])
    assert rc == 0


def test_main_exits_one_when_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("sales_copilot.wizard.cli.save_state", lambda state, path=None: None)

    fake_steps = [
        WizardStep(
            step_id="mic",
            title="Microfoon",
            auto_detect=lambda: StepResult(ok=False, message="no mic"),
        ),
    ]

    monkeypatch.setattr("sales_copilot.wizard.cli.get_steps", lambda _=None: fake_steps)

    rc = main(["--state-path", str(state_path)])
    assert rc == 1
