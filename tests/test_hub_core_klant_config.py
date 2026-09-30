"""klantmap-als-eenheid D2: hub_core.extract_start_call_config's klant.yaml merge + privacy-poort.

Unit-level tests call ``hub_core.extract_start_call_config`` directly against a fake
``KLANTEN_ROOT`` (fast, no server); ``TestPrivacyGateHttpStatus`` additionally proves the
400 status code end-to-end through the real ``/api/start-call`` route, since the
ValueError->HTTPException mapping lives in ``hub_api.py``, not in the function under test
everywhere else in this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sales_copilot.__main__ import _parse_call_config
from sales_copilot.core import context_docs
from sales_copilot.websocket import hub_core


def _write_klant_yaml(root: Path, slug: str, content: str) -> None:
    client_dir = root / slug
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "klant.yaml").write_text(content, encoding="utf-8")


class TestNoClientSelected:
    def test_no_client_slug_leaves_config_untouched(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)

        config = hub_core.extract_start_call_config(
            {"config": {"prospect_company": "Untouched BV"}}
        )

        assert config["prospect_company"] == "Untouched BV"
        assert "call_terms" not in config
        assert "privacy" not in config

    def test_client_without_klant_yaml_leaves_config_untouched(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        (tmp_path / "acme-corp").mkdir()

        config = hub_core.extract_start_call_config(
            {"config": {"client_slug": "acme-corp", "prospect_company": "Untouched BV"}}
        )

        assert config["client_slug"] == "acme-corp"
        assert config["prospect_company"] == "Untouched BV"
        assert "call_terms" not in config


class TestKlantYamlDerivesFields:
    def test_client_slug_alone_fills_company_industry_terms(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        _write_klant_yaml(
            tmp_path,
            "acme-corp",
            "bedrijf: Acme Corp B.V.\nbranche: manufacturing\ntermen:\n  - CRM\n  - facturatie\n",
        )

        config = hub_core.extract_start_call_config({"config": {"client_slug": "acme-corp"}})

        assert config["prospect"]["company"] == "Acme Corp B.V."
        assert config["prospect_company"] == "Acme Corp B.V."
        assert config["prospect"]["industry"] == "manufacturing"
        assert config["call_terms"] == ["CRM", "facturatie"]

    def test_no_branche_leaves_industry_unset(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\n")

        config = hub_core.extract_start_call_config({"config": {"client_slug": "acme-corp"}})

        assert config["prospect"]["company"] == "Acme Corp B.V."
        assert "industry" not in config["prospect"]

    def test_klant_yaml_wins_conflict_over_caller_supplied_value(
        self, tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """API-caller-supplied prospect_company differs from klant.yaml's bedrijf ->
        klant.yaml wins, and the conflict is logged."""
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\n")

        with caplog.at_level("WARNING"):
            config = hub_core.extract_start_call_config(
                {
                    "config": {
                        "client_slug": "acme-corp",
                        "prospect": {"company": "Caller Typed Corp"},
                    }
                }
            )

        assert config["prospect"]["company"] == "Acme Corp B.V."
        assert any("Caller Typed Corp" in message for message in caplog.messages)

    def test_api_caller_without_client_slug_behaves_as_before(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """An API client with its own prospect_company and NO client_slug is
        completely unaffected by klantmap-als-eenheid -- the pre-D2 behaviour."""
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)

        config = hub_core.extract_start_call_config(
            {"config": {"prospect": {"company": "Whatever Inc"}}}
        )

        assert config["prospect"]["company"] == "Whatever Inc"

    def test_client_slug_alone_fills_call_config_end_to_end(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The full chain (hub_core.extract_start_call_config -> __main__._parse_call_config)
        lands prospect_company, prospect_industry and client_slug on the CallConfig the
        orchestrator actually builds modules from -- the acceptance test named in the plan."""
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        _write_klant_yaml(
            tmp_path,
            "acme-corp",
            "bedrijf: Acme Corp B.V.\nbranche: manufacturing\nprivacy: local\n"
            "termen:\n  - CRM\n",
        )

        config = hub_core.extract_start_call_config(
            {
                "config": {
                    "client_slug": "acme-corp",
                    "llm": {"provider": "ollama", "model": "llama3"},
                }
            }
        )
        call_config = _parse_call_config(config)

        assert call_config.prospect_company == "Acme Corp B.V."
        assert call_config.prospect_industry == "manufacturing"
        assert call_config.client_slug == "acme-corp"
        assert config["call_terms"] == ["CRM"]
        assert config["privacy"] == "local"

    def test_aflevering_lokaal_is_reflected_in_config(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\naflevering: lokaal\n")

        config = hub_core.extract_start_call_config({"config": {"client_slug": "acme-corp"}})

        assert config["aflevering"] == "lokaal"

    def test_invalid_klant_yaml_raises_value_error(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        _write_klant_yaml(tmp_path, "acme-corp", "aflevering: hubspot\n")  # missing required bedrijf

        with pytest.raises(ValueError):
            hub_core.extract_start_call_config({"config": {"client_slug": "acme-corp"}})


class TestPrivacyGate:
    def test_privacy_local_with_public_provider_raises(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.delenv("TRUST_OWN_TENANT", raising=False)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

        with pytest.raises(ValueError, match="acme-corp"):
            hub_core.extract_start_call_config(
                {
                    "config": {
                        "client_slug": "acme-corp",
                        "llm": {"provider": "gemini", "model": "gemini-2.5-flash"},
                    }
                }
            )

    def test_privacy_local_with_local_ollama_provider_passes(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

        config = hub_core.extract_start_call_config(
            {
                "config": {
                    "client_slug": "acme-corp",
                    "llm": {"provider": "ollama", "model": "llama3"},
                }
            }
        )

        assert config["privacy"] == "local"

    def test_privacy_tenant_with_trusted_azure_passes(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: tenant\n")

        config = hub_core.extract_start_call_config(
            {
                "config": {
                    "client_slug": "acme-corp",
                    "llm": {"provider": "azure", "model": "gpt-4o"},
                }
            }
        )

        assert config["privacy"] == "tenant"

    def test_privacy_tenant_with_public_provider_raises(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.delenv("TRUST_OWN_TENANT", raising=False)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: tenant\n")

        with pytest.raises(ValueError):
            hub_core.extract_start_call_config(
                {
                    "config": {
                        "client_slug": "acme-corp",
                        "llm": {"provider": "openai", "model": "gpt-4o"},
                    }
                }
            )

    def test_no_privacy_field_never_blocks(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\n")

        config = hub_core.extract_start_call_config(
            {
                "config": {
                    "client_slug": "acme-corp",
                    "llm": {"provider": "openai", "model": "gpt-4o"},
                }
            }
        )

        assert "privacy" not in config

    def test_missing_provider_is_treated_as_public(self, tmp_path: Path, monkeypatch) -> None:
        """No llm.provider set at all resolves to "" -> tier "public" -- the strictest
        default, so an unconfigured provider never accidentally satisfies a ceiling."""
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

        with pytest.raises(ValueError):
            hub_core.extract_start_call_config({"config": {"client_slug": "acme-corp"}})


class TestPrivacyGateMultiLane:
    """PR #246 fix-forward: the privacy-poort tests EVERY provider that receives this
    conversation's text, not just the detector-lane ``llm.provider`` from the start-call
    payload.

    Gap 1: the deep-insight lane (``modules/insight/engine.py``) runs its OWN provider
    (``INSIGHT_PROVIDER``) and reads the client dossier -- a ``privacy: local`` client
    with a compliant detector provider but a public ``INSIGHT_PROVIDER`` used to sail
    through.

    Gap 2: an omitted ``llm.provider`` in the start-call payload used to test a literal
    ``""`` (always "public") instead of the global ``LLM_PROVIDER`` the detector will
    actually run with -- a false rejection for an API caller relying on the operator's
    local default.
    """

    def test_insight_lane_public_provider_raises_even_with_compliant_detector(
        self, tmp_path: Path, monkeypatch, pro_feature_policy
    ) -> None:
        monkeypatch.setattr(hub_core, "_feature_policy", pro_feature_policy)
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        monkeypatch.setenv("INSIGHT_ENABLED", "true")
        monkeypatch.setenv("INSIGHT_PROVIDER", "openrouter")
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

        with pytest.raises(ValueError) as exc_info:
            hub_core.extract_start_call_config(
                {
                    "config": {
                        "client_slug": "acme-corp",
                        "llm": {"provider": "ollama", "model": "llama3"},
                    }
                }
            )

        message = str(exc_info.value)
        assert "acme-corp" in message
        assert "insight" in message

    def test_insight_lane_disabled_never_blocks_a_compliant_detector(
        self, tmp_path: Path, monkeypatch, free_feature_policy
    ) -> None:
        """INSIGHT_ENABLED unset (the default): the lane never runs, so a public
        INSIGHT_PROVIDER left over in the environment must not be tested at all."""
        monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        monkeypatch.delenv("INSIGHT_ENABLED", raising=False)
        monkeypatch.setenv("INSIGHT_PROVIDER", "openrouter")
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

        config = hub_core.extract_start_call_config(
            {
                "config": {
                    "client_slug": "acme-corp",
                    "llm": {"provider": "ollama", "model": "llama3"},
                }
            }
        )

        assert config["privacy"] == "local"

    def test_missing_llm_provider_falls_back_to_global_llm_provider_env(
        self, tmp_path: Path, monkeypatch, free_feature_policy
    ) -> None:
        """No ``llm`` key at all in the payload, global ``LLM_PROVIDER=ollama`` on
        loopback, insight lane off -> the detector will actually run on ollama, so a
        ``privacy: local`` client must be let through, not rejected against a literal
        ``""``."""
        monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.delenv("INSIGHT_ENABLED", raising=False)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

        config = hub_core.extract_start_call_config(
            {"config": {"client_slug": "acme-corp"}}
        )

        assert config["privacy"] == "local"

    def test_tenant_privacy_trusted_vertex_passes_for_both_lanes(
        self, tmp_path: Path, monkeypatch, pro_feature_policy
    ) -> None:
        monkeypatch.setattr(hub_core, "_feature_policy", pro_feature_policy)
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        monkeypatch.setenv("INSIGHT_ENABLED", "true")
        monkeypatch.delenv("INSIGHT_PROVIDER", raising=False)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: tenant\n")

        config = hub_core.extract_start_call_config(
            {
                "config": {
                    "client_slug": "acme-corp",
                    "llm": {"provider": "vertex", "model": "gemini-2.5-pro"},
                }
            }
        )

        assert config["privacy"] == "tenant"


class TestPrivacyGateHttpStatus:
    """End-to-end: the ValueError from extract_start_call_config maps to HTTP 400
    through the real /api/start-call route (hub_api.start_call_api)."""

    def test_privacy_violation_returns_400(
        self, tmp_path: Path, authed_client: TestClient, monkeypatch
    ) -> None:
        monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
        monkeypatch.delenv("TRUST_OWN_TENANT", raising=False)
        _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")
        hub_core.reset_config_state()

        try:
            response = authed_client.post(
                "/api/start-call",
                json={
                    "config": {
                        "client_slug": "acme-corp",
                        "llm": {"provider": "gemini", "model": "gemini-2.5-flash"},
                    }
                },
            )
            assert response.status_code == 400
            assert "acme-corp" in response.json()["detail"]
            assert hub_core.status_state() == "waiting_for_config"
        finally:
            hub_core.reset_config_state()
