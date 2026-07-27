"""Provider-aware PII redaction tests for outbound_policy.

Security matrix:
  - Local Ollama + allow_local=True  -> no redaction (data stays on machine)
  - Local Ollama + allow_local=False -> redacted (fail-closed default)
  - Remote Ollama + allow_local=True -> redacted (remote = cloud boundary)
  - Any cloud provider + allow_local=True -> redacted (cloud enforced)
  - ALLOW_RAW_LLM_PII truthy -> raw, regardless of provider or allow_local
  - Anti-leak: sanitize_for_outbound without allow_local always redacts,
    proving label_and_summarize.py's use of the default remains safe under ollama.

Trusted-tenant matrix (TRUST_OWN_TENANT):
  - azure + TRUST_OWN_TENANT=true + allow_local=True   -> raw (BYO-tenant, DPA in place)
  - vertex + TRUST_OWN_TENANT=true + allow_local=True  -> raw (BYO-tenant, DPA in place)
  - azure + TRUST_OWN_TENANT unset + allow_local=True  -> redact (default unchanged)
  - gemini + TRUST_OWN_TENANT=true + allow_local=True  -> redact (public provider, never trusted)
  - azure + TRUST_OWN_TENANT=true + NO allow_local     -> redact (anti-leak: gated behind allow_local)
  - outbound_is_trusted_tenant() unit: azure+flag->True, vertex+flag->True,
    gemini+flag->False, azure without flag->False, ollama+flag->False
"""

from __future__ import annotations

import importlib
import sys

import pytest

# ---------------------------------------------------------------------------
# PII fixtures
# ---------------------------------------------------------------------------

_NAME = "Jan de Vries"
_BSN = "123456782"
_IBAN = "NL91ABNA0417164300"
_PII_TEXT = f"{_NAME}, BSN {_BSN}, IBAN {_IBAN}, email jan@example.com, tel 0612345678."


def _has_pii(text: str) -> bool:
    return any(token in text for token in (_NAME, _BSN, _IBAN))


def _is_redacted(text: str) -> bool:
    return not _has_pii(text) and ("[NAAM]" in text or "[BSN]" in text or "[IBAN]" in text)


# ---------------------------------------------------------------------------
# Helper: reload outbound_policy after monkeypatching env
# ---------------------------------------------------------------------------

def _reload_policy():
    """Force module-level re-evaluation after env changes."""
    module_name = "sales_copilot.core.outbound_policy"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    import sales_copilot.core.outbound_policy as mod
    return mod


# ---------------------------------------------------------------------------
# outbound_is_local() unit tests
# ---------------------------------------------------------------------------


class TestOutboundIsLocal:
    def test_ollama_localhost_is_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        mod = _reload_policy()
        assert mod.outbound_is_local() is True

    def test_ollama_127001_is_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        mod = _reload_policy()
        assert mod.outbound_is_local() is True

    def test_ollama_ipv6_loopback_is_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://[::1]:11434")
        mod = _reload_policy()
        assert mod.outbound_is_local() is True

    def test_ollama_remote_ip_is_not_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://10.0.0.5:11434")
        mod = _reload_policy()
        assert mod.outbound_is_local() is False

    def test_ollama_remote_hostname_is_not_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://gpu-server.internal:11434")
        mod = _reload_policy()
        assert mod.outbound_is_local() is False

    def test_gemini_is_not_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        mod = _reload_policy()
        assert mod.outbound_is_local() is False

    def test_groq_is_not_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "groq")
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        mod = _reload_policy()
        assert mod.outbound_is_local() is False

    def test_openai_is_not_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        mod = _reload_policy()
        assert mod.outbound_is_local() is False

    def test_vertex_is_not_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "vertex")
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        mod = _reload_policy()
        assert mod.outbound_is_local() is False


# ---------------------------------------------------------------------------
# sanitize_for_outbound() provider-aware matrix
# ---------------------------------------------------------------------------


class TestSanitizeForOutbound:
    # --- Local Ollama + allow_local=True: pass through ---

    def test_local_ollama_allow_local_passes_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ollama on localhost + allow_local=True: PII passes unredacted (stays on machine)."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)

        assert result == _PII_TEXT, "Raw text must pass through for local Ollama with allow_local=True"

    def test_local_ollama_127001_allow_local_passes_raw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)

        assert result == _PII_TEXT

    # --- Local Ollama WITHOUT allow_local (fail-closed default) ---

    def test_local_ollama_default_still_redacts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail-closed: even with local Ollama, the default (allow_local=False) must redact."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT)  # no allow_local

        assert _is_redacted(result), f"Expected redaction but got: {result!r}"
        assert _NAME not in result
        assert _BSN not in result
        assert _IBAN not in result

    # --- Remote Ollama + allow_local=True: must still redact ---

    def test_remote_ollama_allow_local_still_redacts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Remote Ollama is a cloud boundary: allow_local=True must not skip redaction."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://10.0.0.5:11434")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)

        assert _is_redacted(result), f"Remote Ollama must redact PII: {result!r}"
        assert _BSN not in result
        assert _IBAN not in result

    # --- Cloud providers + allow_local=True: must always redact ---

    @pytest.mark.parametrize("provider", ["gemini", "groq", "openai", "vertex"])
    def test_cloud_provider_allow_local_still_redacts(
        self, provider: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cloud providers redact regardless of allow_local=True."""
        monkeypatch.setenv("LLM_PROVIDER", provider)
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)

        assert _is_redacted(result), f"Provider {provider!r} must redact PII: {result!r}"
        assert _BSN not in result
        assert _IBAN not in result
        assert _NAME not in result

    # --- ALLOW_RAW_LLM_PII override ---

    @pytest.mark.parametrize("raw_value", ["1", "true", "yes", "on", "TRUE", "YES"])
    def test_allow_raw_env_passes_through_regardless_of_provider(
        self, raw_value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ALLOW_RAW_LLM_PII bypasses redaction for any provider/allow_local combination."""
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.setenv("ALLOW_RAW_LLM_PII", raw_value)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=False)
        assert result == _PII_TEXT

        result_with_local = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)
        assert result_with_local == _PII_TEXT

    def test_allow_raw_env_with_local_ollama(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        monkeypatch.setenv("ALLOW_RAW_LLM_PII", "true")
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=False)
        assert result == _PII_TEXT

    # --- Anti-leak regression: label_and_summarize path ---

    def test_anti_leak_default_call_redacts_even_with_local_ollama(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anti-leak regression: sanitize_for_outbound() without allow_local ALWAYS redacts.

        label_and_summarize.py uses the bare call (no allow_local) and targets Gemini cloud
        even when LLM_PROVIDER=ollama. This proves PII cannot leak to Gemini in that scenario.
        """
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        # Simulate label_and_summarize.py's call pattern: no allow_local keyword
        result = mod.sanitize_for_outbound(_PII_TEXT)

        assert _BSN not in result, f"BSN must be redacted; got: {result!r}"
        assert _IBAN not in result, f"IBAN must be redacted; got: {result!r}"
        assert _NAME not in result, f"Name must be redacted; got: {result!r}"
        assert "[BSN]" in result or "[NAAM]" in result or "[IBAN]" in result


# ---------------------------------------------------------------------------
# outbound_is_trusted_tenant() unit tests
# ---------------------------------------------------------------------------


class TestOutboundIsTrustedTenant:
    def test_azure_with_flag_is_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        mod = _reload_policy()
        assert mod.outbound_is_trusted_tenant() is True

    def test_vertex_with_flag_is_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "vertex")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        mod = _reload_policy()
        assert mod.outbound_is_trusted_tenant() is True

    def test_gemini_with_flag_is_not_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Public provider never qualifies as trusted tenant."""
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        mod = _reload_policy()
        assert mod.outbound_is_trusted_tenant() is False

    def test_groq_with_flag_is_not_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "groq")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        mod = _reload_policy()
        assert mod.outbound_is_trusted_tenant() is False

    def test_openai_with_flag_is_not_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        mod = _reload_policy()
        assert mod.outbound_is_trusted_tenant() is False

    def test_ollama_with_flag_is_not_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Ollama uses outbound_is_local(); it is not a trusted tenant."""
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        mod = _reload_policy()
        assert mod.outbound_is_trusted_tenant() is False

    def test_azure_without_flag_is_not_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Correct provider alone is not enough — flag must be explicitly set."""
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.delenv("TRUST_OWN_TENANT", raising=False)
        mod = _reload_policy()
        assert mod.outbound_is_trusted_tenant() is False

    def test_azure_with_flag_false_is_not_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("TRUST_OWN_TENANT", "false")
        mod = _reload_policy()
        assert mod.outbound_is_trusted_tenant() is False

    @pytest.mark.parametrize("flag_value", ["1", "true", "yes", "on", "TRUE", "YES"])
    def test_azure_truthy_variants(
        self, flag_value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("TRUST_OWN_TENANT", flag_value)
        mod = _reload_policy()
        assert mod.outbound_is_trusted_tenant() is True


# ---------------------------------------------------------------------------
# sanitize_for_outbound() trusted-tenant matrix
# ---------------------------------------------------------------------------


class TestSanitizeForOutboundTrustedTenant:
    def test_azure_trusted_tenant_allow_local_passes_raw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """azure + TRUST_OWN_TENANT=true + allow_local=True: PII passes through (BYO-tenant)."""
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)

        assert result == _PII_TEXT, f"Azure BYO-tenant must pass raw PII; got: {result!r}"
        assert _NAME in result
        assert _BSN in result
        assert _IBAN in result

    def test_vertex_trusted_tenant_allow_local_passes_raw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """vertex + TRUST_OWN_TENANT=true + allow_local=True: PII passes through (BYO-tenant)."""
        monkeypatch.setenv("LLM_PROVIDER", "vertex")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)

        assert result == _PII_TEXT, f"Vertex BYO-tenant must pass raw PII; got: {result!r}"
        assert _NAME in result
        assert _BSN in result
        assert _IBAN in result

    def test_azure_no_flag_still_redacts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """azure + TRUST_OWN_TENANT unset + allow_local=True: default unchanged, must redact."""
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.delenv("TRUST_OWN_TENANT", raising=False)
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)

        assert _is_redacted(result), f"Azure without flag must redact; got: {result!r}"
        assert _BSN not in result
        assert _IBAN not in result
        assert _NAME not in result

    def test_gemini_with_flag_still_redacts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """gemini + TRUST_OWN_TENANT=true + allow_local=True: public provider, must redact."""
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)

        assert _is_redacted(result), f"Gemini (public) must always redact; got: {result!r}"
        assert _BSN not in result
        assert _IBAN not in result
        assert _NAME not in result

    @pytest.mark.parametrize("provider", ["groq", "openai"])
    def test_other_public_providers_with_flag_still_redact(
        self, provider: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Other public providers with TRUST_OWN_TENANT=true still redact."""
        monkeypatch.setenv("LLM_PROVIDER", provider)
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        result = mod.sanitize_for_outbound(_PII_TEXT, allow_local=True)

        assert _is_redacted(result), f"Provider {provider!r} with flag must still redact; got: {result!r}"
        assert _BSN not in result
        assert _NAME not in result

    def test_azure_trusted_tenant_without_allow_local_redacts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anti-leak: azure + TRUST_OWN_TENANT=true WITHOUT allow_local still redacts.

        Callers that hardcode a cloud destination (e.g. scripts/label_and_summarize.py)
        must not pass allow_local=True. The trusted-tenant no-op is gated behind allow_local,
        so those callers always redact even with TRUST_OWN_TENANT=true and LLM_PROVIDER=azure.
        """
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("TRUST_OWN_TENANT", "true")
        monkeypatch.delenv("ALLOW_RAW_LLM_PII", raising=False)
        mod = _reload_policy()

        # Simulate label_and_summarize.py's call pattern — no allow_local
        result = mod.sanitize_for_outbound(_PII_TEXT)

        assert _is_redacted(result), (
            f"Anti-leak: azure trusted-tenant without allow_local must redact; got: {result!r}"
        )
        assert _BSN not in result, f"BSN leaked: {result!r}"
        assert _IBAN not in result, f"IBAN leaked: {result!r}"
        assert _NAME not in result, f"Name leaked: {result!r}"
