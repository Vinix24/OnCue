"""Tests for the W2 Pro-tier permissions wizard extensions."""

from __future__ import annotations

import io
import plistlib
from pathlib import Path
from unittest.mock import patch

import pytest

from sales_copilot.auth.license_format import FEATURE_AUTOSTART, FEATURE_CALLTAP
from sales_copilot.wizard.cli import get_steps, run_wizard
from sales_copilot.wizard.detectors import (
    detect_autostart_consent_arm,
    detect_calltap_setup,
)
from sales_copilot.wizard.state import WizardState
from sales_copilot.wizard.steps import StepResult, WizardStep


def test_get_steps_includes_pro_steps_for_pro(pro_feature_policy: object) -> None:
    steps = get_steps(pro_feature_policy)
    assert [s.step_id for s in steps] == [
        "mic",
        "route",
        "model",
        "provider",
        "autostart",
        "calltap",
        "doctor",
    ]
    assert steps[4].feature_id == FEATURE_AUTOSTART
    assert steps[5].feature_id == FEATURE_CALLTAP


def test_run_wizard_free_shows_upgrade_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    free_feature_policy: object,
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("sales_copilot.wizard.cli.save_state", lambda state, path=None: None)
    monkeypatch.setattr(
        "sales_copilot.wizard.cli.load_state", lambda path=None: WizardState()
    )

    fake_steps = [
        WizardStep(
            step_id="mic",
            title="Microfoon",
            auto_detect=lambda: StepResult(ok=True, message="mic ok"),
        ),
        WizardStep(
            step_id="doctor",
            title="Doctor",
            auto_detect=lambda: StepResult(ok=True, message="doctor ok"),
        ),
    ]

    output = io.StringIO()
    with patch("sales_copilot.wizard.detectors.Path.exists", return_value=True):
        _state, _results, all_ok = run_wizard(
            steps=fake_steps,
            state_path=state_path,
            file=output,
            feature_policy=free_feature_policy,
        )

    assert all_ok is True
    text = output.getvalue()
    assert "Pro-only" in text
    assert "upgrade to Pro" in text
    assert "Autostart & consent-arm" in text
    assert "Call-tap setup" in text


def test_run_wizard_pro_executes_pro_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pro_feature_policy: object,
) -> None:
    state_path = tmp_path / "state.json"
    saved: WizardState | None = None

    def _capture_save(state: WizardState, path: Path | None = None) -> None:
        nonlocal saved
        saved = state

    monkeypatch.setattr("sales_copilot.wizard.cli.save_state", _capture_save)
    monkeypatch.setattr(
        "sales_copilot.wizard.cli.load_state", lambda path=None: WizardState()
    )

    fake_steps = [
        WizardStep(
            step_id="mic",
            title="Microfoon",
            auto_detect=lambda: StepResult(ok=True, message="mic ok"),
        ),
        WizardStep(
            step_id="autostart",
            title="Autostart & consent-arm",
            auto_detect=lambda: StepResult(ok=True, message="autostart ok"),
            feature_id=FEATURE_AUTOSTART,
        ),
        WizardStep(
            step_id="calltap",
            title="Call-tap setup (iPhone-relay)",
            auto_detect=lambda: StepResult(ok=True, message="calltap ok"),
            feature_id=FEATURE_CALLTAP,
        ),
        WizardStep(
            step_id="doctor",
            title="Doctor",
            auto_detect=lambda: StepResult(ok=True, message="doctor ok"),
        ),
    ]

    with patch("sales_copilot.wizard.detectors.Path.exists", return_value=True):
        _state, results, all_ok = run_wizard(
            steps=fake_steps,
            state_path=state_path,
            file=io.StringIO(),
            feature_policy=pro_feature_policy,
        )

    assert all_ok is True
    assert results["autostart"].ok is True
    assert results["calltap"].ok is True
    assert saved is not None
    assert "autostart" in saved.completed
    assert "calltap" in saved.completed


def test_doctor_only_reverifies_w1_steps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pro_feature_policy: object,
) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr("sales_copilot.wizard.cli.save_state", lambda state, path=None: None)
    monkeypatch.setattr(
        "sales_copilot.wizard.cli.load_state", lambda path=None: WizardState()
    )

    calls: list[str] = []

    def _make_detect(step_id: str):
        def _detect() -> StepResult:
            calls.append(step_id)
            return StepResult(ok=True, message="ok")

        return _detect

    fake_steps = [
        WizardStep("mic", "Microfoon", _make_detect("mic")),
        WizardStep("route", "Audio-route", _make_detect("route")),
        WizardStep("model", "Whisper model", _make_detect("model")),
        WizardStep("provider", "LLM provider", _make_detect("provider")),
        WizardStep(
            "autostart",
            "Autostart & consent-arm",
            _make_detect("autostart"),
            feature_id=FEATURE_AUTOSTART,
        ),
        WizardStep(
            "calltap",
            "Call-tap setup",
            _make_detect("calltap"),
            feature_id=FEATURE_CALLTAP,
        ),
        WizardStep("doctor", "Doctor", lambda: StepResult(ok=True, message="ok")),
    ]

    with patch("sales_copilot.wizard.detectors.Path.exists", return_value=True):
        _state, _results, all_ok = run_wizard(
            steps=fake_steps,
            state_path=state_path,
            file=io.StringIO(),
            feature_policy=pro_feature_policy,
        )

    assert all_ok is True
    # W1 steps are run once during the walk-through and once during the doctor.
    assert calls.count("mic") == 2
    assert calls.count("route") == 2
    assert calls.count("model") == 2
    assert calls.count("provider") == 2
    # Pro steps are run only during the walk-through, never by the doctor.
    assert calls.count("autostart") == 1
    assert calls.count("calltap") == 1


class TestDetectAutostartConsentArm:
    def test_missing_plist_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        monkeypatch.setattr(detectors, "_AUTOSTART_PLIST_PATH", tmp_path / "missing.plist")
        result = detect_autostart_consent_arm()

        assert result.ok is False
        assert "plist_missing" in result.details["error"]

    def test_wrong_label_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        plist_path = tmp_path / "nl.vnx.sales-copilot.plist"
        with plist_path.open("wb") as handle:
            plistlib.dump({"Label": "wrong.label", "RunAtLoad": True}, handle)

        monkeypatch.setattr(detectors, "_AUTOSTART_PLIST_PATH", plist_path)
        monkeypatch.setattr(
            "sales_copilot.core.consent.consent_tracking_enabled", lambda: True
        )

        result = detect_autostart_consent_arm()

        assert result.ok is False
        assert result.details["error"] == "label_mismatch"

    def test_run_at_load_disabled_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        plist_path = tmp_path / "nl.vnx.sales-copilot.plist"
        with plist_path.open("wb") as handle:
            plistlib.dump(
                {"Label": "nl.vnx.sales-copilot", "RunAtLoad": False}, handle
            )

        monkeypatch.setattr(detectors, "_AUTOSTART_PLIST_PATH", plist_path)
        monkeypatch.setattr(
            "sales_copilot.core.consent.consent_tracking_enabled", lambda: True
        )

        result = detect_autostart_consent_arm()

        assert result.ok is False
        assert result.details["error"] == "run_at_load_disabled"

    def test_consent_tracking_disabled_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        plist_path = tmp_path / "nl.vnx.sales-copilot.plist"
        with plist_path.open("wb") as handle:
            plistlib.dump(
                {"Label": "nl.vnx.sales-copilot", "RunAtLoad": True}, handle
            )

        monkeypatch.setattr(detectors, "_AUTOSTART_PLIST_PATH", plist_path)
        monkeypatch.setattr(
            "sales_copilot.core.consent.consent_tracking_enabled", lambda: False
        )

        result = detect_autostart_consent_arm()

        assert result.ok is False
        assert result.details["error"] == "consent_tracking_disabled"

    def test_valid_plist_and_consent_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        plist_path = tmp_path / "nl.vnx.sales-copilot.plist"
        with plist_path.open("wb") as handle:
            plistlib.dump(
                {"Label": "nl.vnx.sales-copilot", "RunAtLoad": True}, handle
            )

        monkeypatch.setattr(detectors, "_AUTOSTART_PLIST_PATH", plist_path)
        monkeypatch.setattr(
            "sales_copilot.core.consent.consent_tracking_enabled", lambda: True
        )

        result = detect_autostart_consent_arm()

        assert result.ok is True
        assert "consent-arm active" in result.message

    def test_env_consent_tracking_disabled_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A .env with CONSENT_TRACKING_ENABLED=false must disarm the autostart check."""
        import sales_copilot.wizard.detectors as detectors

        plist_path = tmp_path / "nl.vnx.sales-copilot.plist"
        with plist_path.open("wb") as handle:
            plistlib.dump(
                {"Label": "nl.vnx.sales-copilot", "RunAtLoad": True}, handle
            )

        env_path = tmp_path / ".env"
        env_path.write_text("CONSENT_TRACKING_ENABLED=false\n")

        monkeypatch.setattr(detectors, "_AUTOSTART_PLIST_PATH", plist_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CONSENT_TRACKING_ENABLED", raising=False)
        monkeypatch.delenv("CONSENT_TIER", raising=False)
        monkeypatch.delenv("CONSENT_GATE_MODE", raising=False)

        result = detect_autostart_consent_arm()

        assert result.ok is False
        assert result.details["error"] == "consent_tracking_disabled"

    def test_consent_tier_off_disarms_autostart(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Autostart without a consent gate (tier=off) must not be considered armed."""
        import sales_copilot.wizard.detectors as detectors

        plist_path = tmp_path / "nl.vnx.sales-copilot.plist"
        with plist_path.open("wb") as handle:
            plistlib.dump(
                {"Label": "nl.vnx.sales-copilot", "RunAtLoad": True}, handle
            )

        env_path = tmp_path / ".env"
        env_path.write_text("CONSENT_TIER=off\nCONSENT_TRACKING_ENABLED=true\n")

        monkeypatch.setattr(detectors, "_AUTOSTART_PLIST_PATH", plist_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("CONSENT_TIER", raising=False)
        monkeypatch.delenv("CONSENT_GATE_MODE", raising=False)
        monkeypatch.delenv("CONSENT_TRACKING_ENABLED", raising=False)

        result = detect_autostart_consent_arm()

        assert result.ok is False
        assert result.details["error"] == "consent_tier_off"
        assert result.details["consent_tier"] == "off"


class TestDetectCalltapSetup:
    def test_missing_binary_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        missing_binary = tmp_path / "audiotee"

        def _env_path(key: str, default: str) -> Path:
            return missing_binary if key == "AUDIOTEE_BINARY_PATH" else Path(default)

        monkeypatch.setattr(detectors, "_env_path", _env_path)

        result = detect_calltap_setup()

        assert result.ok is False
        assert result.details["error"] == "binary_missing"

    def test_no_telephony_process_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.audio.capture as capture
        import sales_copilot.wizard.detectors as detectors

        binary = tmp_path / "audiotee"
        binary.write_text("fake")
        binary.chmod(0o755)

        def _env_path(key: str, default: str) -> Path:
            return binary if key == "AUDIOTEE_BINARY_PATH" else Path(default)

        monkeypatch.setattr(detectors, "_env_path", _env_path)
        monkeypatch.setattr(capture, "_find_pid", lambda _name: None)

        result = detect_calltap_setup()

        assert result.ok is False
        assert result.details["error"] == "no_telephony_process"

    def test_permission_proof_failure_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.audio.capture as capture
        import sales_copilot.wizard.detectors as detectors

        binary = tmp_path / "audiotee"
        binary.write_text("fake")
        binary.chmod(0o755)

        def _env_path(key: str, default: str) -> Path:
            return binary if key == "AUDIOTEE_BINARY_PATH" else Path(default)

        monkeypatch.setattr(detectors, "_env_path", _env_path)
        monkeypatch.setattr(capture, "_find_pid", lambda _name: 1234)
        monkeypatch.setattr(
            detectors,
            "_audiotee_run_proof",
            lambda _path, extra_args=None: StepResult(
                ok=False, message="permission denied"
            ),
        )

        result = detect_calltap_setup()

        assert result.ok is False
        assert result.details["error"] == "calltap_proof_failed"

    def test_valid_setup_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.audio.capture as capture
        import sales_copilot.wizard.detectors as detectors

        binary = tmp_path / "audiotee"
        binary.write_text("fake")
        binary.chmod(0o755)

        def _env_path(key: str, default: str) -> Path:
            return binary if key == "AUDIOTEE_BINARY_PATH" else Path(default)

        monkeypatch.setattr(detectors, "_env_path", _env_path)
        monkeypatch.setattr(capture, "_find_pid", lambda _name: 1234)
        monkeypatch.setattr(
            detectors,
            "_audiotee_run_proof",
            lambda _path, extra_args=None: StepResult(ok=True, message="proof ok"),
        )

        result = detect_calltap_setup()

        assert result.ok is True
        assert result.details["pid"] == 1234

    def test_calltap_uses_call_process_name_from_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """CALL_PROCESS_NAME from .env/env must override the hardcoded 'avconferenced'."""
        import sales_copilot.audio.capture as capture
        import sales_copilot.wizard.detectors as detectors

        binary = tmp_path / "audiotee"
        binary.write_text("fake")
        binary.chmod(0o755)

        def _env_path(key: str, default: str) -> Path:
            return binary if key == "AUDIOTEE_BINARY_PATH" else Path(default)

        seen_names: list[str] = []

        def _fake_find_pid(name: str) -> int:
            seen_names.append(name)
            return 1234

        monkeypatch.setattr(detectors, "_env_path", _env_path)
        monkeypatch.setattr(capture, "_find_pid", _fake_find_pid)
        monkeypatch.setattr(
            detectors,
            "_audiotee_run_proof",
            lambda _path, extra_args=None: StepResult(ok=True, message="proof ok"),
        )
        monkeypatch.setenv("CALL_PROCESS_NAME", "foo")

        result = detect_calltap_setup()

        assert result.ok is True
        assert seen_names == ["foo"]
        assert result.details["process_name"] == "foo"
        assert "'foo'" in result.message
