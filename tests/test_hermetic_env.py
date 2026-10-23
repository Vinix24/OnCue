"""The test process starts clean: no operator `.env`, no real provider credentials, no Hugging Face traffic.

Regression for OI-007 (a local ``LLM_PROVIDER=openrouter`` in the checkout's ``.env`` made
``test_missing_provider_is_treated_as_public`` green here and red in CI, PR #295) and OI-006
(``tests/test_detector_router.py`` made 11 metadata requests to huggingface.co per run).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

from sales_copilot.core.config import DetectorConfig, load_env
from tests import conftest
from tests.conftest import _embedding_model_cached, _hf_offline_when_cached


def test_load_env_reads_nothing_from_the_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``.env`` inside the checkout (what ``load_env()`` finds from the repo cwd) is not loaded."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".env").write_text("LLM_PROVIDER=openrouter\nOPENROUTER_API_KEY=operator-secret\n")
    monkeypatch.setattr(conftest, "_REPO_ROOT", checkout)
    monkeypatch.chdir(checkout)

    assert load_env() is False
    assert "LLM_PROVIDER" not in os.environ
    assert "OPENROUTER_API_KEY" not in os.environ
    assert DetectorConfig.from_env().llm_provider == "none"


def test_load_env_still_reads_an_explicit_path_outside_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / "custom.env"
    env_file.write_text("SALES_COPILOT_HERMETIC_PROBE=loaded\n")
    monkeypatch.delenv("SALES_COPILOT_HERMETIC_PROBE", raising=False)
    try:
        assert load_env(env_file) is True
        assert os.environ["SALES_COPILOT_HERMETIC_PROBE"] == "loaded"
    finally:
        os.environ.pop("SALES_COPILOT_HERMETIC_PROBE", None)


def test_provider_and_key_variables_are_stripped_from_each_test() -> None:
    """The autouse fixture removed everything that selects or authenticates a lane.

    The placeholder keys from ``_dummy_llm_provider_keys`` are the only credentials left.
    """
    leftovers = {
        name: value
        for name, value in os.environ.items()
        if name.endswith(("_PROVIDER", "LLM_MODEL", "_API_KEY")) and value != "test-key"
    }
    assert leftovers == {}


def test_provider_variable_from_the_shell_does_not_reach_the_test(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test that wants a provider sets it itself; the default stays ``none``."""
    assert DetectorConfig.from_env().llm_provider == "none"
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    assert DetectorConfig.from_env().llm_provider == "groq"


@pytest.mark.skipif(not _embedding_model_cached(), reason="embedding model not in the local Hugging Face cache")
def test_embedding_model_loads_without_a_huggingface_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a filled cache, loading the router's encoder makes zero requests to huggingface.co."""
    from sales_copilot.modules.detector.router import PainPointRouter

    assert os.environ.get("HF_HUB_OFFLINE") == "1"

    hosts: list[str] = []
    original_send = httpx.Client.send

    def _send(self: httpx.Client, request: httpx.Request, **kwargs):
        hosts.append(request.url.host)
        return original_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", _send)
    router = PainPointRouter(DetectorConfig.from_env())
    router.classify("we zitten uren aan offertes")

    assert [host for host in hosts if "huggingface" in host or host.endswith("hf.co")] == []


def test_huggingface_fixture_does_nothing_without_huggingface_hub(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows smoke job installs without ``huggingface_hub``; the session fixture must not need it."""
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", None)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

    assert _embedding_model_cached() is False
    with _hf_offline_when_cached():
        assert "HF_HUB_OFFLINE" not in os.environ
