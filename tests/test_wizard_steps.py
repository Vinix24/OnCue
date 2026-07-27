import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sales_copilot.wizard.detectors import (
    _audiotee_run_proof,
    detect_doctor,
    detect_mic,
    detect_model,
    detect_provider,
    detect_route,
    run_mic_proof,
)
from sales_copilot.wizard.steps import StepResult, WizardStep


class TestDetectMic:
    def test_no_default_input_device_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sales_copilot.audio.capture as cap

        monkeypatch.setattr(cap, "get_default_input_device", lambda: None)
        result = detect_mic()

        assert result.ok is False
        assert "No default microphone" in result.message

    def test_default_input_device_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sales_copilot.audio.capture as cap

        monkeypatch.setattr(
            cap,
            "get_default_input_device",
            lambda: {"name": "MacBook Pro Microphone", "index": 0},
        )
        result = detect_mic()

        assert result.ok is True
        assert "MacBook Pro Microphone" in result.message

    def test_import_failure_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sales_copilot.audio.capture as cap

        def _broken_get_default() -> None:
            raise ImportError("no audio")

        monkeypatch.setattr(cap, "get_default_input_device", _broken_get_default)

        result = detect_mic()

        assert result.ok is False
        assert "audio library" in result.message


class TestRunMicProof:
    def test_high_levels_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sales_copilot.wizard.meter as meter

        monkeypatch.setattr(meter, "meter_session", lambda **kwargs: (-10.0, -25.0))
        result = run_mic_proof()

        assert result.ok is True
        assert result.details["peak_db"] == -10.0

    def test_low_levels_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sales_copilot.wizard.meter as meter

        monkeypatch.setattr(meter, "meter_session", lambda **kwargs: (-80.0, -90.0))
        result = run_mic_proof()

        assert result.ok is False
        assert "very low" in result.message

    def test_exception_is_caught(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sales_copilot.wizard.meter as meter

        monkeypatch.setattr(meter, "meter_session", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
        result = run_mic_proof()

        assert result.ok is False
        assert "failed" in result.message


class TestWizardStepRun:
    def test_run_calls_auto_detect_before_proof(self) -> None:
        calls: list[str] = []

        def _detect() -> StepResult:
            calls.append("detect")
            return StepResult(ok=True, message="detected")

        def _proof(*, emitter: object = None) -> StepResult:
            calls.append("proof")
            return StepResult(ok=True, message="proved")

        step = WizardStep(step_id="x", title="X", auto_detect=_detect, proof=_proof)
        result = step.run()

        assert calls == ["detect", "proof"]
        assert result.ok is True

    def test_run_skips_proof_when_detection_fails(self) -> None:
        def _detect() -> StepResult:
            return StepResult(ok=False, message="no device")

        def _proof(*, emitter: object = None) -> StepResult:
            raise AssertionError("proof should not run")

        step = WizardStep(step_id="x", title="X", auto_detect=_detect, proof=_proof)
        result = step.run()

        assert result.ok is False
        assert "no device" in result.message

    def test_run_forwards_emitter_to_proof(self) -> None:
        received: list[object] = []

        def _detect() -> StepResult:
            return StepResult(ok=True, message="detected")

        def _proof(*, emitter: object = None) -> StepResult:
            received.append(emitter)
            return StepResult(ok=True, message="proved")

        step = WizardStep(step_id="x", title="X", auto_detect=_detect, proof=_proof)
        fake_emitter = object()
        step.run(emitter=fake_emitter)

        assert received == [fake_emitter]


class TestDetectRoute:
    def test_audiotee_binary_with_proof_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        binary = tmp_path / "audiotee"
        binary.write_text("fake")
        binary.chmod(0o755)

        def _env_path(key: str, default: str) -> Path:
            return binary if key == "AUDIOTEE_BINARY_PATH" else Path(default)

        monkeypatch.setattr(detectors, "_env_path", _env_path)
        monkeypatch.setattr(detectors, "_blackhole_device_present", lambda: False)
        monkeypatch.setattr(
            detectors,
            "_audiotee_run_proof",
            lambda path: StepResult(ok=True, message="proof ok"),
        )

        result = detect_route()

        assert result.ok is True
        assert result.details["route"] == "audiotee"
        assert result.details["audiotee_proof"]["ok"] is True

    def test_audiotee_proof_falls_back_to_blackhole(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        binary = tmp_path / "audiotee"
        binary.write_text("fake")
        binary.chmod(0o755)

        def _env_path(key: str, default: str) -> Path:
            return binary if key == "AUDIOTEE_BINARY_PATH" else Path(default)

        monkeypatch.setattr(detectors, "_env_path", _env_path)
        monkeypatch.setattr(detectors, "_blackhole_device_present", lambda: True)
        monkeypatch.setattr(
            detectors,
            "_audiotee_run_proof",
            lambda path: StepResult(ok=False, message="permission denied"),
        )
        monkeypatch.setattr(
            detectors,
            "_blackhole_default_output_configured",
            lambda: (True, {"name": "OnCue Output"}),
        )
        monkeypatch.setattr(detectors, "_blackhole_has_audio", lambda: (True, 0.05))

        result = detect_route()

        assert result.ok is True
        assert result.details["route"] == "blackhole"

    def test_blackhole_without_routing_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        monkeypatch.setattr(detectors, "_env_path", lambda key, default: Path(default))
        monkeypatch.setattr(detectors, "_audiotee_binary_exists", lambda: False)
        monkeypatch.setattr(detectors, "_blackhole_device_present", lambda: True)
        monkeypatch.setattr(
            detectors,
            "_blackhole_default_output_configured",
            lambda: (False, {"name": "MacBook Pro Speakers"}),
        )
        monkeypatch.setattr(detectors, "_blackhole_has_audio", lambda: (False, 0.0))

        result = detect_route()

        assert result.ok is False
        assert result.details["route"] == "blackhole"
        assert result.details["error"] == "not_routed"

    def test_blackhole_routed_without_audio_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        monkeypatch.setattr(detectors, "_env_path", lambda key, default: Path(default))
        monkeypatch.setattr(detectors, "_audiotee_binary_exists", lambda: False)
        monkeypatch.setattr(detectors, "_blackhole_device_present", lambda: True)
        monkeypatch.setattr(
            detectors,
            "_blackhole_default_output_configured",
            lambda: (True, {"name": "OnCue Output"}),
        )
        monkeypatch.setattr(detectors, "_blackhole_has_audio", lambda: (False, 0.0))

        result = detect_route()

        assert result.ok is False
        assert result.details["route"] == "blackhole"
        assert result.details["error"] == "no_audio_flow"

    def test_blackhole_fallback(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        monkeypatch.setattr(detectors, "_env_path", lambda key, default: Path(default))
        monkeypatch.setattr(detectors, "_audiotee_binary_exists", lambda: False)
        monkeypatch.setattr(detectors, "_blackhole_device_present", lambda: True)
        monkeypatch.setattr(
            detectors,
            "_blackhole_default_output_configured",
            lambda: (True, {"name": "OnCue Output"}),
        )
        monkeypatch.setattr(detectors, "_blackhole_has_audio", lambda: (True, 0.05))

        result = detect_route()

        assert result.ok is True
        assert result.details["route"] == "blackhole"

    def test_nothing_available_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        monkeypatch.setattr(detectors, "_env_path", lambda key, default: Path(default))
        monkeypatch.setattr(detectors, "_audiotee_binary_exists", lambda: False)
        monkeypatch.setattr(detectors, "_blackhole_device_present", lambda: False)

        result = detect_route()

        assert result.ok is False
        assert result.details["route"] is None


class TestDetectModel:
    def test_whisper_cpp_model_present(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:

        model_file = tmp_path / "ggml-large-v3-turbo.bin"
        model_file.write_text("fake model")
        monkeypatch.setenv("WHISPER_BACKEND", "whisper.cpp")
        monkeypatch.setenv("WHISPER_CPP_MODEL_PATH", str(model_file))
        monkeypatch.setenv("WHISPER_CPP_BINARY", str(tmp_path / "whisper-cli"))

        result = detect_model()

        assert result.ok is True
        assert "found" in result.message

    def test_whisper_cpp_model_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:

        monkeypatch.setenv("WHISPER_BACKEND", "whisper.cpp")
        monkeypatch.setenv("WHISPER_CPP_MODEL_PATH", str(tmp_path / "missing.bin"))
        monkeypatch.setenv("WHISPER_CPP_BINARY", str(tmp_path / "whisper-cli"))

        result = detect_model()

        assert result.ok is False
        assert "not found" in result.message

    def test_unknown_backend_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHISPER_BACKEND", "unknown")

        result = detect_model()

        assert result.ok is False
        assert "Unknown Whisper backend" in result.message


class TestDetectProvider:
    def test_missing_provider_fails_without_silent_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sales_copilot.wizard.detectors as detectors

        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        # Scope the wizard's .env lookup so this test is independent of any .env file in CI.
        monkeypatch.setattr(detectors, "dotenv_values", lambda _path: {})

        result = detect_provider()

        assert result.ok is False
        assert "LLM_PROVIDER is not set" in result.message
        assert result.details["error"] == "provider_not_set"

    def test_openrouter_missing_key_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sales_copilot.wizard.detectors as detectors

        monkeypatch.setenv("LLM_PROVIDER", "openrouter")
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.setattr(detectors, "dotenv_values", lambda _path: {})

        result = detect_provider()

        assert result.ok is False
        assert "OPENROUTER_API_KEY" in result.message

    def test_openrouter_key_present_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "openrouter")
        monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key-long-enough")

        result = detect_provider()

        assert result.ok is True
        assert "openrouter" in result.message

    def test_gemini_missing_key_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sales_copilot.wizard.detectors as detectors

        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.setattr(detectors, "dotenv_values", lambda _path: {})

        result = detect_provider()

        assert result.ok is False
        assert "GEMINI_API_KEY" in result.message

    def test_gemini_key_present_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-long-enough")

        result = detect_provider()

        assert result.ok is True
        assert "gemini" in result.message

    def test_ollama_no_key_needed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")

        result = detect_provider()

        assert result.ok is True
        assert "ollama" in result.message

    def test_unknown_provider_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "skynet")

        result = detect_provider()

        assert result.ok is False
        assert "Unknown LLM provider" in result.message


class TestDetectDoctor:
    def test_all_prior_pass_and_env_exists_passes(self, tmp_path: Path) -> None:
        prior = {
            "mic": StepResult(ok=True, message="ok"),
            "route": StepResult(ok=True, message="ok"),
            "model": StepResult(ok=True, message="ok"),
            "provider": StepResult(ok=True, message="ok"),
        }
        detectors = {
            "mic": lambda: StepResult(ok=True, message="live ok"),
            "route": lambda: StepResult(ok=True, message="live ok"),
            "model": lambda: StepResult(ok=True, message="live ok"),
            "provider": lambda: StepResult(ok=True, message="live ok"),
        }

        with patch("sales_copilot.wizard.detectors.Path.exists", return_value=True):
            result = detect_doctor(prior, steps=["mic", "route", "model", "provider"], detectors=detectors)

        assert result.ok is True
        assert "All checks OK" in result.message
        assert result.details["reverify_results"]["mic"]["ok"] is True

    def test_missing_step_fails(self) -> None:
        prior = {
            "mic": StepResult(ok=True, message="ok"),
            "route": StepResult(ok=False, message="bad"),
        }

        result = detect_doctor(prior, steps=["mic", "route"])

        assert result.ok is False
        assert "route" in result.details["failed_steps"]

    def test_live_reverify_failure_fails(self) -> None:
        prior = {
            "mic": StepResult(ok=True, message="ok"),
            "route": StepResult(ok=True, message="ok"),
        }
        detectors = {
            "mic": lambda: StepResult(ok=True, message="live ok"),
            "route": lambda: StepResult(ok=False, message="live failed"),
        }

        with patch("sales_copilot.wizard.detectors.Path.exists", return_value=True):
            result = detect_doctor(prior, steps=["mic", "route"], detectors=detectors)

        assert result.ok is False
        assert "route" in result.details["reverify_failures"]
        assert "failed live" in result.message


class TestAudioteeRunProof:
    def test_permission_denied_in_stderr_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        binary = tmp_path / "audiotee"
        binary.write_text("# fake audiotee that prints permission error")
        binary.chmod(0o755)

        def _fake_popen(*args, **kwargs):  # noqa: ANN002, ANN003
            class FakeProc:
                returncode = 1

                def communicate(self, timeout=None):  # noqa: ANN001, ANN201
                    return b"", b"error: audio recording permission not allowed"

                def terminate(self) -> None:
                    pass

                def kill(self) -> None:
                    pass

                def wait(self, timeout=None):  # noqa: ANN001, ANN201
                    return self.returncode

            return FakeProc()

        monkeypatch.setattr(detectors.subprocess, "Popen", _fake_popen)

        result = _audiotee_run_proof(binary)

        assert result.ok is False
        assert "permission" in result.message.lower()
        assert result.details["error"] == "tap_permission_denied"

    def test_successful_start_passes(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        import sales_copilot.wizard.detectors as detectors

        binary = tmp_path / "audiotee"
        binary.write_text("# fake audiotee")
        binary.chmod(0o755)

        def _fake_popen(*args, **kwargs):  # noqa: ANN002, ANN003
            class FakeProc:
                returncode = -15

                def communicate(self, timeout=None):  # noqa: ANN001, ANN201
                    # Simulates a running tap that we terminate after timeout.
                    raise subprocess.TimeoutExpired(cmd=str(binary), timeout=1.5)

                def terminate(self) -> None:
                    self.returncode = -15

                def kill(self) -> None:
                    self.returncode = -9

                def wait(self, timeout=None):  # noqa: ANN001, ANN201
                    return self.returncode

            return FakeProc()

        monkeypatch.setattr(detectors.subprocess, "Popen", _fake_popen)

        result = _audiotee_run_proof(binary)

        assert result.ok is True
        assert result.details["path"] == str(binary)
