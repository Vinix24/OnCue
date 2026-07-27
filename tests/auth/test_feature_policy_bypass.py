"""§7.1 Bypass-test matrix: Pro-only capture cannot be activated without entitlement.

Covers all four entry vectors:
  1. HTTP  POST /api/start-call  (canonical gate)
  2. WebSocket /ws/config        (structural closure — HTTP-only for call state)
  3. Env-config + direct factory (DualAudioCapture defense-in-depth layer)
  4. Entitlement endpoint        (GET /api/v1/license/features)

Does NOT import any license signer — the client holds no signing seed (licenses
are issued server-side by the Worker), so tests here never mint a key.
"""

from __future__ import annotations

import json
from base64 import b64encode
from pathlib import Path
from typing import Any

import pytest
from _dev_anchor import requires_dev_anchor
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sales_copilot.audio.blackhole import BlackHoleStream
from sales_copilot.audio.calltap import CallTapStream
from sales_copilot.audio.capture import AudioConfig, DualAudioCapture
from sales_copilot.auth import revocation_cache
from sales_copilot.auth.feature_policy import FEATURE_CALLTAP, FeaturePolicy
from sales_copilot.auth.license_format import (
    FEATURE_IDS,
    FEATURE_LIVE_COACHING,
    FEATURE_SCRIPT_TRACKING_COMPUTE,
    TIER_FEATURES,
)
from sales_copilot.websocket import hub, hub_core
from sales_copilot.websocket.hub_auth import get_hub_token

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_TOKEN = "test-shutdown-token-32-chars-ok!"

_VECTOR = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "license_pro_test_vector.json").read_text(
        encoding="utf-8"
    )
)

_CALLTAP_CONFIG_PAYLOAD = {"config": {"prospect_source": "audiotee_call"}}
_NORMAL_CONFIG_PAYLOAD = {"config": {"prospect_source": "blackhole"}}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _authed_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with a fixed token and the token pre-set in default headers."""
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TEST_TOKEN)
    return TestClient(hub.app, headers={"X-Sales-Copilot-Token": _TEST_TOKEN})


def _ws_auth_headers() -> dict[str, str]:
    credentials = b64encode(f"token:{get_hub_token()}".encode()).decode()
    return {"authorization": f"Basic {credentials}"}


def _isolate_revocation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point revocation cache at a temp file and stub the network fetch."""
    monkeypatch.setattr(revocation_cache, "_CACHE_PATH", tmp_path / "rc.json")
    monkeypatch.setattr(
        revocation_cache,
        "_fetch_revocation",
        lambda license_id, base_url: {"revoked": False, "expires_at": 0},
    )


class _EnterprisePolicy(FeaturePolicy):
    """Stub that reports tier='enterprise' and allows all enterprise features.

    No cryptography is involved: the real signing private key is not accessible
    from the public repo, so we simulate enterprise by subclassing FeaturePolicy
    and overriding current_tier.
    """

    def current_tier(self) -> str:  # type: ignore[override]
        return "enterprise"

    def allows(self, feature_id: str) -> bool:
        return feature_id in TIER_FEATURES.get("enterprise", set())


# ---------------------------------------------------------------------------
# Vector 1 — HTTP POST /api/start-call
# ---------------------------------------------------------------------------


class TestHttpEntitlementGate:
    """POST /api/start-call must 403 on calltap without a Pro key and 200 with one."""

    def test_free_calltap_returns_403(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolate_revocation(monkeypatch, tmp_path)
        # Empty, not delenv: this dev checkout's .env carries a valid Pro license
        # and the app's config.load_dotenv(override=False) re-injects it at
        # TestClient startup after a delenv, flipping this "free" gate to Pro (200).
        # A present-but-empty value is never overridden, so the gate stays free.
        monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
        hub.reset_config_state()
        client = _authed_client(monkeypatch)

        response = client.post("/api/start-call", json=_CALLTAP_CONFIG_PAYLOAD)

        assert response.status_code == 403
        body = response.json()
        assert body["error"] == "feature_required"
        assert body["feature"] == FEATURE_CALLTAP

    def test_free_calltap_body_contains_calltap_feature_id(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_revocation(monkeypatch, tmp_path)
        # Empty, not delenv — see test_free_calltap_returns_403 for why the .env
        # Pro license would otherwise be reloaded at TestClient startup.
        monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
        hub.reset_config_state()
        client = _authed_client(monkeypatch)

        response = client.post("/api/start-call", json=_CALLTAP_CONFIG_PAYLOAD)

        assert response.json()["feature"] == "audio.calltap"

    @requires_dev_anchor
    def test_pro_calltap_returns_200(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolate_revocation(monkeypatch, tmp_path)
        monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])
        hub.reset_config_state()
        client = _authed_client(monkeypatch)

        response = client.post("/api/start-call", json=_CALLTAP_CONFIG_PAYLOAD)

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_enterprise_calltap_returns_200(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_revocation(monkeypatch, tmp_path)
        stub = _EnterprisePolicy()
        monkeypatch.setattr(hub_core, "get_feature_policy", lambda: stub)
        hub.reset_config_state()
        client = _authed_client(monkeypatch)

        response = client.post("/api/start-call", json=_CALLTAP_CONFIG_PAYLOAD)

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_free_normal_config_returns_200_no_false_positive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A free-tier session with blackhole source must not be gated."""
        _isolate_revocation(monkeypatch, tmp_path)
        # Empty, not delenv — see test_free_calltap_returns_403 for why the .env
        # Pro license would otherwise be reloaded at TestClient startup.
        monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
        hub.reset_config_state()
        client = _authed_client(monkeypatch)

        response = client.post("/api/start-call", json=_NORMAL_CONFIG_PAYLOAD)

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_free_absent_prospect_source_returns_200_no_false_positive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A free-tier session with no prospect_source key must not be gated."""
        _isolate_revocation(monkeypatch, tmp_path)
        # Empty, not delenv — see test_free_calltap_returns_403 for why the .env
        # Pro license would otherwise be reloaded at TestClient startup.
        monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
        hub.reset_config_state()
        client = _authed_client(monkeypatch)

        response = client.post("/api/start-call", json={"config": {}})

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Vector 2 — WebSocket /ws/config bypass attempt
# ---------------------------------------------------------------------------


class TestWebSocketBypassClosed:
    """The /ws/config channel is HTTP-only for call state.

    A client that sends a message with type 'start_call' (or any type other than
    {call_started, call_ended, swap_speakers}) is closed with code 1008. This
    documents that the WebSocket bypass vector is structurally closed — no
    entitlement is ever consulted because the message is rejected before any call
    logic runs.
    """

    def test_ws_config_start_call_type_closes_1008(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHUTDOWN_TOKEN", _TEST_TOKEN)
        hub.reset_config_state()
        client = TestClient(hub.app)

        with client.websocket_connect("/ws/config", headers=_ws_auth_headers()) as ws:
            ws.send_json({"type": "start_call", "config": {"prospect_source": "audiotee_call"}})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()

        assert exc_info.value.code == 1008

    def test_ws_config_start_call_does_not_activate_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After the rejected publish, hub state must remain waiting_for_config."""
        monkeypatch.setenv("SHUTDOWN_TOKEN", _TEST_TOKEN)
        hub.reset_config_state()
        client = TestClient(hub.app)

        with client.websocket_connect("/ws/config", headers=_ws_auth_headers()) as ws:
            ws.send_json({"type": "start_call", "config": {"prospect_source": "audiotee_call"}})
            try:
                ws.receive_text()
            except WebSocketDisconnect:
                pass

        status = client.get("/api/status").json()["state"]
        assert status == "waiting_for_config"
        assert hub.get_latest_config() is None

    def test_ws_config_close_reason_is_http_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHUTDOWN_TOKEN", _TEST_TOKEN)
        hub.reset_config_state()
        client = TestClient(hub.app)

        with client.websocket_connect("/ws/config", headers=_ws_auth_headers()) as ws:
            ws.send_json({"type": "start_call"})
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()

        assert "HTTP-only" in (exc_info.value.reason or "")


# ---------------------------------------------------------------------------
# Vector 3 — Env-config + direct factory (DualAudioCapture defense-in-depth)
# ---------------------------------------------------------------------------


class TestFactoryEntitlementLayer:
    """DualAudioCapture must degrade to BlackHoleStream and call on_warning when
    the policy denies audio.calltap, regardless of how the config was supplied.
    """

    def _make_config(self) -> AudioConfig:
        return AudioConfig(prospect_source="audiotee_call")

    def _free_policy(self) -> FeaturePolicy:
        """A FeaturePolicy whose current_tier always returns 'free'."""

        class _FreePolicy(FeaturePolicy):
            def current_tier(self) -> str:  # type: ignore[override]
                return "free"

            def allows(self, feature_id: str) -> bool:
                return feature_id in TIER_FEATURES.get("free", set())

        return _FreePolicy()

    def _pro_policy(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FeaturePolicy:
        """A real FeaturePolicy backed by the public Pro test vector key."""
        _isolate_revocation(monkeypatch, tmp_path)
        monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])
        return FeaturePolicy()

    def test_free_factory_yields_blackhole_not_calltap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        warnings: list[dict[str, Any]] = []
        config = self._make_config()

        _, system = DualAudioCapture(config, feature_policy=self._free_policy()).create(
            on_warning=warnings.append
        )

        assert isinstance(system, BlackHoleStream)

    def test_free_factory_calls_on_warning(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        warnings: list[dict[str, Any]] = []
        config = self._make_config()

        DualAudioCapture(config, feature_policy=self._free_policy()).create(
            on_warning=warnings.append
        )

        assert len(warnings) == 1
        assert warnings[0]["type"] == "audio_warning"
        assert warnings[0]["stream"] == "prospect"

    @requires_dev_anchor
    def test_pro_factory_yields_calltap_not_blackhole(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config = self._make_config()
        policy = self._pro_policy(monkeypatch, tmp_path)

        _, system = DualAudioCapture(config, feature_policy=policy).create(
            on_warning=lambda w: None
        )

        assert isinstance(system, CallTapStream)

    def test_enterprise_factory_yields_calltap(self) -> None:
        config = self._make_config()

        _, system = DualAudioCapture(config, feature_policy=_EnterprisePolicy()).create(
            on_warning=lambda w: None
        )

        assert isinstance(system, CallTapStream)

    def test_free_factory_on_warning_not_required(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """create() without on_warning must not raise when policy denies calltap."""
        config = self._make_config()

        mic, system = DualAudioCapture(config, feature_policy=self._free_policy()).create()

        assert isinstance(system, BlackHoleStream)

    def test_free_blackhole_source_no_warning_fired(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A normal blackhole config with a free policy must not trigger an entitlement/tier warning."""
        warnings: list[dict[str, Any]] = []
        config = AudioConfig(prospect_source="blackhole")

        DualAudioCapture(config, feature_policy=self._free_policy()).create(
            on_warning=warnings.append
        )

        # Audio-device warnings (type "audio_warning") such as "no meeting-app
        # found, falling back to BlackHole" depend on the host environment and are
        # not the subject of this test. Filter them out and assert that no
        # entitlement/tier warning was fired.
        non_audio_warnings = [w for w in warnings if w.get("type") != "audio_warning"]
        entitlement_warnings = [
            w
            for w in warnings
            if "licentie" in (w.get("message") or "").lower()
            or "license" in (w.get("message") or "").lower()
        ]
        assert non_audio_warnings == []
        assert entitlement_warnings == []


# ---------------------------------------------------------------------------
# Vector 4 — GET /api/v1/license/features entitlement endpoint
# ---------------------------------------------------------------------------


class TestEntitlementEndpoint:
    """GET /api/v1/license/features must reflect tier and features accurately."""

    def test_free_tier_returns_only_script_tracking_compute(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Free is no longer empty: the Phase 1 two-layer script-tracking gate
        (salesprep-pro design doc section 3) grants `.compute` to every tier so
        coverage is always computed. Every other feature, including
        `coaching.script_tracking.live`, stays Pro-gated."""
        _isolate_revocation(monkeypatch, tmp_path)
        # Empty, not delenv — see test_free_calltap_returns_403 for why the .env
        # Pro license would otherwise be reloaded at TestClient startup.
        monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
        client = TestClient(hub.app)

        response = client.get("/api/v1/license/features")

        assert response.status_code == 200
        body = response.json()
        assert body["tier"] == "free"
        assert body["features"] == [FEATURE_SCRIPT_TRACKING_COMPUTE]
        assert FEATURE_LIVE_COACHING in body["all_features"]

    def test_free_tier_all_features_equals_feature_ids(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_revocation(monkeypatch, tmp_path)
        # Empty, not delenv — see test_free_calltap_returns_403 for why the .env
        # Pro license would otherwise be reloaded at TestClient startup.
        monkeypatch.setenv("SALES_COPILOT_LICENSE", "")
        client = TestClient(hub.app)

        response = client.get("/api/v1/license/features")

        body = response.json()
        # Equality, not presence: the advertised catalogue must be exactly the
        # canonical FEATURE_IDS set, so a new feature can't silently drift out of
        # /license/features (presence would have missed compliance.central_audit
        # and dynamic_slides being added/removed).
        assert set(body["all_features"]) == set(FEATURE_IDS)

    @requires_dev_anchor
    def test_pro_tier_returns_both_feature_ids(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_revocation(monkeypatch, tmp_path)
        monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])
        client = TestClient(hub.app)

        response = client.get("/api/v1/license/features")

        assert response.status_code == 200
        body = response.json()
        assert body["tier"] == "pro"
        assert "audio.calltap" in body["features"]
        assert "compliance.central_audit" in body["features"]
        assert FEATURE_LIVE_COACHING in body["features"]

    def test_enterprise_tier_returns_both_feature_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stub = _EnterprisePolicy()
        monkeypatch.setattr("sales_copilot.websocket.hub_api.get_feature_policy", lambda: stub)
        client = TestClient(hub.app)

        response = client.get("/api/v1/license/features")

        assert response.status_code == 200
        body = response.json()
        assert body["tier"] == "enterprise"
        assert "audio.calltap" in body["features"]
        assert "compliance.central_audit" in body["features"]
        assert FEATURE_LIVE_COACHING in body["features"]

    def test_features_list_is_sorted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_revocation(monkeypatch, tmp_path)
        monkeypatch.setenv("SALES_COPILOT_LICENSE", _VECTOR["key"])
        client = TestClient(hub.app)

        body = client.get("/api/v1/license/features").json()

        assert body["features"] == sorted(body["features"])
        assert body["all_features"] == sorted(body["all_features"])
