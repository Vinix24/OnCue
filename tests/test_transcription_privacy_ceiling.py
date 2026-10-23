"""OI-002: the per-conversation privacy ceiling also covers the transcription backend.

``outbound_policy.transcription_tier_of`` classifies the backend, ``privacy_gate``
refuses it above the ceiling, and ``hub_core.extract_start_call_config`` enforces it before
any capture starts. The start-call tests run the real function; nothing is mocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sales_copilot.core import context_docs
from sales_copilot.core.outbound_policy import is_loopback_host, transcription_tier_of
from sales_copilot.core.privacy_gate import (
    PrivacyGateError,
    enforce_transcription_privacy,
)
from sales_copilot.modules.transcriber.backends import create_backend
from sales_copilot.websocket import hub_core

_LOCAL_LLM = {"provider": "ollama", "model": "llama3"}


@pytest.fixture(autouse=True)
def _clean_transcription_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "WHISPER_BACKEND",
        "WHISPER_CPP_SERVER_HOST",
        "TRANSCRIPTION_ENGINE",
        "RECORD_AUDIO",
        "TRUST_OWN_TENANT",
        "INSIGHT_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")


def _start(
    privacy: str | None, *, transcript: dict | None = None, **extra: object
) -> dict:
    config: dict = {"llm": dict(_LOCAL_LLM), **extra}
    if privacy is not None:
        config["privacy"] = privacy
    if transcript is not None:
        config["transcript"] = transcript
    return hub_core.extract_start_call_config({"config": config})


class TestTranscriptionTier:
    @pytest.mark.parametrize("backend", ["mlx-whisper", "mlx", "parakeet", "parakeet-mlx"])
    def test_on_device_backends_are_local(self, backend: str) -> None:
        assert transcription_tier_of(backend, "10.0.0.5") == "local"

    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "LOCALHOST", "[::1]"])
    @pytest.mark.parametrize("backend", ["whisper.cpp", "whisper_cpp"])
    def test_whisper_cpp_on_loopback_is_local(self, backend: str, host: str) -> None:
        assert transcription_tier_of(backend, host) == "local"

    @pytest.mark.parametrize("host", ["10.0.0.5", "0.0.0.0", "192.168.1.20", "", None])
    def test_whisper_cpp_off_loopback_is_public(self, host: str | None) -> None:
        assert transcription_tier_of("whisper.cpp", host) == "public"

    @pytest.mark.parametrize("backend", ["groq", "groq-whisper", "groq_whisper", " Groq "])
    def test_groq_aliases_are_public(self, backend: str) -> None:
        assert transcription_tier_of(backend, "127.0.0.1") == "public"

    @pytest.mark.parametrize("backend", ["", "deepgram", "whisper", "openai"])
    def test_unknown_backend_is_never_local(self, backend: str) -> None:
        assert transcription_tier_of(backend, "127.0.0.1") == "public"

    def test_loopback_test_is_shared_with_the_llm_tier(self) -> None:
        assert is_loopback_host("localhost")
        assert not is_loopback_host("10.0.0.5")
        assert not is_loopback_host(None)

    def test_unknown_name_is_one_create_backend_rejects_too(self) -> None:
        """The gate and the runtime agree: a name create_backend cannot build is public."""
        with pytest.raises(ValueError, match="Unsupported transcription backend"):
            create_backend({"backend": "deepgram"})
        assert transcription_tier_of("deepgram") == "public"


class TestEnforceTranscriptionPrivacy:
    def test_no_ceiling_allows_everything(self) -> None:
        enforce_transcription_privacy(None, "groq", "10.0.0.5", client_slug=None)

    def test_message_names_lane_backend_and_client(self) -> None:
        with pytest.raises(PrivacyGateError) as excinfo:
            enforce_transcription_privacy("local", "groq", "127.0.0.1", client_slug="acme-corp")
        message = str(excinfo.value)
        assert "transcriptie" in message
        assert "groq" in message
        assert "acme-corp" in message
        assert "'public'" in message

    def test_message_names_host_for_whisper_cpp(self) -> None:
        with pytest.raises(PrivacyGateError, match="10.0.0.5"):
            enforce_transcription_privacy("local", "whisper.cpp", "10.0.0.5", client_slug=None)

    def test_unknown_ceiling_value_is_refused(self) -> None:
        with pytest.raises(PrivacyGateError, match="onbekend privacy-profiel"):
            enforce_transcription_privacy("strict", "mlx", None, client_slug=None)


class TestStartCallCeiling:
    def test_local_ceiling_refuses_groq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        with pytest.raises(PrivacyGateError, match="transcriptie"):
            _start("local")

    def test_local_ceiling_allows_whisper_cpp_on_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WHISPER_BACKEND", "whisper.cpp")
        monkeypatch.setenv("WHISPER_CPP_SERVER_HOST", "127.0.0.1")
        assert _start("local")["privacy"] == "local"

    def test_local_ceiling_allows_the_default_backend(self) -> None:
        assert _start("local")["privacy"] == "local"

    def test_local_ceiling_refuses_whisper_cpp_off_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WHISPER_BACKEND", "whisper.cpp")
        monkeypatch.setenv("WHISPER_CPP_SERVER_HOST", "10.0.0.5")
        with pytest.raises(PrivacyGateError, match="10.0.0.5"):
            _start("local")

    def test_tenant_ceiling_refuses_groq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        with pytest.raises(PrivacyGateError, match="transcriptie"):
            _start("tenant")

    def test_public_ceiling_allows_groq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        assert _start("public")["privacy"] == "public"

    def test_no_ceiling_allows_groq(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        assert "privacy" not in _start(None)

    def test_local_ceiling_refuses_unknown_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WHISPER_BACKEND", "deepgram")
        with pytest.raises(PrivacyGateError, match="transcriptie"):
            _start("local")

    def test_per_call_backend_override_is_gated(self) -> None:
        """transcript.backend wins over the env, exactly as build_module_configs applies it."""
        with pytest.raises(PrivacyGateError, match="groq"):
            _start("local", transcript={"backend": "groq"})

    def test_flat_per_call_backend_override_is_gated(self) -> None:
        with pytest.raises(PrivacyGateError, match="groq"):
            _start("local", transcript_backend="groq")

    def test_per_call_override_can_lower_a_public_env_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate checks the backend that runs, not the env one the call replaces."""
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        assert _start("local", transcript={"backend": "mlx-whisper"})["privacy"] == "local"

    def test_engine_whisper_cpp_forces_the_backend_the_gate_checks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TRANSCRIPTION_ENGINE=whisper.cpp overrules a groq WHISPER_BACKEND at runtime."""
        monkeypatch.setenv("TRANSCRIPTION_ENGINE", "whisper.cpp")
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        assert _start("local")["privacy"] == "local"

    def test_disabled_transcriber_skips_the_live_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No live transcriber starts, so its backend is never built (mirrors _insight_lane_active)."""
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        config = _start("local", modules={"transcriber": False, "pain_points": False})
        assert config["privacy"] == "local"

    def test_disabled_transcriber_still_gates_the_post_call_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reports module batch-transcribes self.wav with the env backend when recording."""
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        monkeypatch.setenv("RECORD_AUDIO", "true")
        with pytest.raises(PrivacyGateError, match="transcriptie"):
            _start("local", modules={"transcriber": False, "pain_points": False})

    def test_batch_route_ignores_the_per_call_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        monkeypatch.setenv("RECORD_AUDIO", "true")
        with pytest.raises(PrivacyGateError, match="groq"):
            _start("local", transcript={"backend": "mlx-whisper"})

    def test_klant_yaml_ceiling_applies_to_transcription(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        (tmp_path / "acme-corp").mkdir()
        (tmp_path / "acme-corp" / "klant.yaml").write_text(
            "bedrijf: Acme Corp B.V.\nprivacy: local\n", encoding="utf-8"
        )
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        with pytest.raises(PrivacyGateError, match="acme-corp"):
            hub_core.extract_start_call_config(
                {"config": {"client_slug": "acme-corp", "llm": dict(_LOCAL_LLM)}}
            )

    def test_refusal_is_http_400_through_start_call(
        self,
        tmp_path: Path,
        authed_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.setenv("WHISPER_BACKEND", "groq")
        hub_core.reset_config_state()
        try:
            response = authed_client.post(
                "/api/start-call",
                json={"config": {"privacy": "local", "llm": dict(_LOCAL_LLM)}},
            )
            assert response.status_code == 400
            assert "transcriptie" in response.json()["detail"]
            assert hub_core.status_state() == "waiting_for_config"
        finally:
            hub_core.reset_config_state()
