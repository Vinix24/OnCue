"""Root pytest conftest — re-exports shared fixtures for auto-discovery."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.auth.license_format import TIER_FEATURES
from sales_copilot.websocket import hub  # noqa: E402
from tests.conftest_replay import running_hub as running_hub  # noqa: F401


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
def _dummy_llm_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Placeholder provider credentials for the test session.

    Two SDK-construction guards reject an empty key: the openai SDK (>=2.41)
    raises ``OpenAIError`` for groq/openai/ollama, and ``llm_client.build_client``
    raises ``ValueError`` for gemini when ``GOOGLE_API_KEY``/``GEMINI_API_KEY`` are
    empty. Tests that build one of those clients therefore need a non-empty
    placeholder. Real API calls are always mocked — only the constructor needs the
    value. A key already set in the environment is left untouched so a developer's
    real key is never overridden. Tests that exercise the empty-key guard itself
    (e.g. ``test_llm_client``) delete the vars explicitly, which overrides this.
    """
    for var in ("OPENAI_API_KEY", "GROQ_API_KEY", "OLLAMA_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
        if not os.environ.get(var):
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
