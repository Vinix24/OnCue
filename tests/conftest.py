"""Root pytest conftest — re-exports shared fixtures for auto-discovery."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.auth.license_format import TIER_FEATURES
from sales_copilot.core import config as config_module
from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.model_cache import embedding_model_cached
from sales_copilot.websocket import hub  # noqa: E402
from tests.conftest_replay import running_hub as running_hub  # noqa: F401

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Names that select or authenticate an LLM/provider lane. A developer's shell or `.env`
# carries real values for these; a test must start from the code defaults instead.
# GCP_PROJECT/GCP_LOCATION are in because `vertex` needs them to build a client at all:
# python-dotenv walks up from litellm's site-packages dir into the checkout, so a local
# `.env` handed every test a project id that CI does not have (PR #428).
_PROVIDER_ENV_NAME = re.compile(
    r"^(?:.*_PROVIDER|.*_API_KEY|(?:.*_)?LLM_MODEL|INSIGHT_MODEL|HUGGINGFACE_TOKEN|HF_TOKEN|SUPABASE_KEY"
    r"|GCP_PROJECT|GCP_LOCATION)$"
)


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's `.env` and provider credentials out of every test.

    ``load_env()`` runs ``load_dotenv(override=False)`` against ``./.env`` and writes the
    result into this process' ``os.environ``, so a local ``LLM_PROVIDER=openrouter`` made a
    test pass here and fail in CI (PR #295). Two guards:

    * ``load_dotenv`` calls that resolve to a file inside the repo checkout read nothing.
      A test that passes its own path (``tmp_path``) still gets a real load.
    * every provider, API-key and model-selector variable is removed from the environment.
      A test that needs one sets it with ``monkeypatch``.
    """
    real_load_dotenv = config_module.load_dotenv

    def _load_dotenv(dotenv_path=None, *args, **kwargs):
        if dotenv_path is None or Path(dotenv_path).resolve().is_relative_to(_REPO_ROOT):
            return False
        return real_load_dotenv(dotenv_path, *args, **kwargs)

    monkeypatch.setattr(config_module, "load_dotenv", _load_dotenv)
    for name in [n for n in os.environ if _PROVIDER_ENV_NAME.match(n)]:
        monkeypatch.delenv(name)
    # An operator's LANGUAGE=nl / CALL_LANGUAGE=nl (an install that stays Dutch) must not
    # decide a test either: the default is English, and a Dutch test sets it itself.
    for name in ("LANGUAGE", "CALL_LANGUAGE", "WHISPER_LANGUAGE"):
        monkeypatch.delenv(name, raising=False)


def _embedding_model_cached() -> bool:
    """True when the default embedding model has a complete snapshot in the local HF cache."""
    return embedding_model_cached(os.environ.get("EMBEDDING_MODEL") or DetectorConfig.embedding_model)


@contextmanager
def _hf_offline_when_cached() -> Iterator[None]:
    """Switch Hugging Face to offline for the duration when the embedding model is on disk."""
    if not _embedding_model_cached():
        yield
        return
    patch = pytest.MonkeyPatch()
    patch.setenv("HF_HUB_OFFLINE", "1")
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] in {"huggingface_hub", "transformers", "sentence_transformers"} and hasattr(
            module, "HF_HUB_OFFLINE"
        ):
            patch.setattr(module, "HF_HUB_OFFLINE", True)
    try:
        yield
    finally:
        patch.undo()


@pytest.fixture(autouse=True, scope="session")
def _huggingface_offline_when_cached() -> Iterator[None]:
    """Send no request to huggingface.co when the embedding model is already on disk.

    Loading the model otherwise costs a metadata round-trip per file (11 calls for
    ``tests/test_detector_router.py``). A fresh runner without the cache keeps the online
    load, and a runner without ``huggingface_hub`` does nothing. ``huggingface_hub`` and
    ``transformers`` read the flag at import time, so the already-imported module attributes
    are switched as well as the environment.
    """
    with _hf_offline_when_cached():
        yield


@pytest.fixture(autouse=True)
def _dev_build_license_flow_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the test suite through the real license-verification flow.

    This checkout is a dev build (``_dev_keys`` present), so
    ``FeaturePolicy.current_tier()`` now defaults to unconditional
    ``"enterprise"`` for the owner's own build (see
    ``sales_copilot.auth.feature_policy``). The existing test suite exercises
    the real free/pro/revocation license chain, so pin
    ``SALES_COPILOT_DEV_TIER=license`` here to keep that coverage meaningful.
    Tests that specifically cover the dev-build admin-override behaviour
    itself (or a simulated prod build) override this env var explicitly.
    """
    monkeypatch.setenv("SALES_COPILOT_DEV_TIER", "license")


@pytest.fixture(autouse=True)
def _dummy_llm_provider_keys(monkeypatch: pytest.MonkeyPatch, _hermetic_env: None) -> None:
    """Placeholder provider credentials for the test session.

    Two SDK-construction guards reject an empty key: the openai SDK (>=2.41)
    raises ``OpenAIError`` for groq/openai/ollama, and ``llm_client.build_client``
    raises ``ValueError`` for gemini when ``GOOGLE_API_KEY``/``GEMINI_API_KEY`` are
    empty. Tests that build one of those clients therefore need a non-empty
    placeholder. Real API calls are always mocked — only the constructor needs the
    value. ``_hermetic_env`` has already removed any real key, so these placeholders are
    all a test sees. Tests that exercise the empty-key guard itself
    (e.g. ``test_llm_client``) delete the vars explicitly, which overrides this.
    """
    for var in ("OPENAI_API_KEY", "GROQ_API_KEY", "OLLAMA_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setenv(var, "test-key")


class _TierFeaturePolicy(FeaturePolicy):
    """Test-only FeaturePolicy stub that resolves allows() from TIER_FEATURES directly.

    Bypasses license key verification entirely — safe for tests, no private
    signer imports required.
    """

    def __init__(self, tier: str) -> None:
        self._tier = tier

    def current_tier(self) -> str:
        return self._tier

    def allows(self, feature_id: str) -> bool:
        return feature_id in TIER_FEATURES.get(self._tier, frozenset())


@pytest.fixture()
def pro_feature_policy() -> FeaturePolicy:
    """A FeaturePolicy stub that grants all Pro-tier features."""
    return _TierFeaturePolicy("pro")


@pytest.fixture()
def free_feature_policy() -> FeaturePolicy:
    """A FeaturePolicy stub that grants no features (free tier)."""
    return _TierFeaturePolicy("free")


# Known token used by auth fixtures — long enough to pass HMAC compare.
_TEST_TOKEN = "test-shutdown-token-32-chars-ok!"


@pytest.fixture()
def authed_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with SHUTDOWN_TOKEN set and token pre-loaded in default headers.

    Use this fixture for any test that POSTs to a require_token-protected route.
    Ensures the test is resilient regardless of whether SHUTDOWN_TOKEN is
    already present in the environment before the test runs.
    """
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TEST_TOKEN)
    return TestClient(hub.app, headers={"X-Sales-Copilot-Token": _TEST_TOKEN})


@pytest.fixture(autouse=True)
def _reset_hub_call_and_sample_state() -> Iterator[None]:
    """Give every test a hub with no call and no sample player, whatever ran before.

    ``hub_core._call_active`` and ``hub_api._sample_player`` are module globals. The
    start-call endpoint tests (test_hub_endpoints, auth/test_feature_policy_bypass) leave
    ``_call_active`` True, and ``/api/sample-aha/start`` answers 409 in that case. Under
    xdist the order of tests per worker differs from a serial run, so the sample-aha e2e
    failed on CI only when it landed behind one of them. Reset before and after, so a test
    is also protected from a leak that happened outside a fixture.
    """
    from sales_copilot.websocket import hub_api, hub_core

    def _reset() -> None:
        player = hub_api._sample_player
        hub_api._sample_player = None
        if player is not None:
            player.stop()
        hub_core.reset_config_state()

    _reset()
    yield
    _reset()
