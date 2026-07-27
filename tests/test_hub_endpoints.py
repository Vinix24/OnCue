from __future__ import annotations

import asyncio
import json
from base64 import b64encode
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sales_copilot.core import feedback_store
from sales_copilot.websocket import hub, hub_auth, hub_upload
from sales_copilot.websocket.hub_auth import get_hub_token


def _ws_auth_headers() -> dict[str, str]:
    credentials = b64encode(f"token:{get_hub_token()}".encode()).decode()
    return {"authorization": f"Basic {credentials}"}


def test_presets_endpoint_returns_entries() -> None:
    client = TestClient(hub.app)

    response = client.get("/api/presets")

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert any(entry.get("name") == "discovery" for entry in data)
    discovery = next(entry for entry in data if entry.get("name") == "discovery")
    assert discovery.get("screen_mode") == "single"
    assert isinstance(discovery.get("modules"), dict)
    assert discovery.get("transcript", {}).get("backend") == "whisper.cpp"


def test_upload_endpoint_saves_file(tmp_path: Path, authed_client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(hub_upload, "UPLOAD_ROOT", tmp_path)

    response = authed_client.post(
        "/upload",
        data={"company_slug": "acme-co"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    payload = response.json()
    saved_path = tmp_path / payload["id"]
    assert saved_path.exists()
    assert saved_path.read_text(encoding="utf-8") == "hello"


def test_upload_endpoint_defaults_company_slug(tmp_path: Path, authed_client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(hub_upload, "UPLOAD_ROOT", tmp_path)

    response = authed_client.post(
        "/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"].startswith("default/")


def test_config_endpoint_returns_ws_config() -> None:
    client = TestClient(hub.app)

    response = client.get("/api/config")

    assert response.status_code == 200
    payload = response.json()
    assert "ws_host" in payload
    assert "ws_port" in payload


def test_config_channel_cannot_set_state() -> None:
    hub.reset_config_state()
    client = TestClient(hub.app)

    with client.websocket_connect("/ws/config", headers=_ws_auth_headers()) as ws:
        ws.send_json({"type": "start_call", "config": {"preset": "full_demo"}})

    assert hub.get_latest_config() is None
    status = client.get("/api/status").json()["state"]
    assert status == "waiting_for_config"


def test_end_call_api_returns_waiting_for_config(authed_client: TestClient) -> None:
    hub.reset_config_state()

    response = authed_client.post("/api/start-call", json={"config": {"preset": "sales", "install_code": ""}})
    assert response.status_code == 200

    response = authed_client.post("/api/end-call")
    assert response.status_code == 200
    assert authed_client.get("/api/status").json()["state"] == "waiting_for_config"


def test_end_call_api_signals_wait_for_end_call(authed_client: TestClient) -> None:
    hub.reset_config_state()

    response = authed_client.post("/api/start-call", json={"config": {"preset": "sales", "install_code": ""}})
    assert response.status_code == 200

    async def _wait(timeout: float) -> bool:
        return await hub.wait_for_end_call(timeout=timeout)

    # start-call clears the cross-thread end-call event; nothing is signalled yet.
    assert asyncio.run(_wait(0.1)) is False

    response = authed_client.post("/api/end-call")
    assert response.status_code == 200

    # The HTTP end-call handler must set _end_call_event so an orchestrator
    # awaiting wait_for_end_call() on the event loop is released (uvicorn handler
    # thread -> orchestrator loop signal path).
    assert asyncio.run(_wait(1.0)) is True


def test_start_call_api_accepts_flat_config_payload(authed_client: TestClient) -> None:
    hub.reset_config_state()

    payload = {
        "install_code": "",
        "screen_mode": "single",
        "language": "en",
        "modules": {"talk_time": True, "transcript": False},
    }
    response = authed_client.post("/api/start-call", json=payload)

    assert response.status_code == 200
    assert hub.get_latest_config() == payload


def test_start_call_api_notifies_waiter(authed_client: TestClient) -> None:
    hub.reset_config_state()

    async def _wait_for_config() -> dict[str, object] | None:
        return await hub.wait_for_start_call(timeout=1.0)

    waiter = asyncio.run(_wait_for_config())
    assert waiter is None

    payload = {"screen_mode": "single", "install_code": ""}
    response = authed_client.post("/api/start-call", json=payload)
    assert response.status_code == 200

    result = asyncio.run(_wait_for_config())
    assert result == payload


def _all_route_paths(app) -> set[str]:
    """Collect every route path reachable through ``app``, across FastAPI versions.

    FastAPI <0.139 flattened ``include_router`` routes into ``app.routes`` as
    top-level entries carrying a ``.path``. 0.139 keeps them behind an
    ``_IncludedRouter`` wrapper, so ``app.routes`` no longer exposes their paths
    directly — walk into ``.routes``/``.original_router.routes`` too. The
    endpoints resolve at runtime either way (verified by the TestClient tests
    above); this keeps the wiring assertion honest without pinning FastAPI.
    """
    paths: set[str] = set()
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
        nested = getattr(route, "routes", None) or getattr(
            getattr(route, "original_router", None), "routes", None
        )
        if nested:
            stack.extend(nested)
    return paths


def test_hub_wrapper_includes_split_routes() -> None:
    routes = _all_route_paths(hub.app)

    assert "/api/status" in routes
    assert "/api/presets" in routes
    assert "/api/llm-models" in routes
    assert "/api/config" in routes
    assert "/api/start-call" in routes
    assert "/api/end-call" in routes
    assert "/api/autostart/confirm" in routes
    assert "/api/autostart/decline" in routes
    assert "/api/phase" in routes
    assert "/api/swap-speakers" in routes
    assert "/upload" in routes
    assert "/api/v1/wizard/steps" in routes
    assert "/api/v1/wizard/state" in routes
    assert "/api/v1/wizard/restart" in routes
    assert "/api/v1/wizard/run/{step_id}" in routes
    assert "/api/hint-feedback" in routes
    assert "/ws/{channel}" in routes


# ---------------------------------------------------------------------------
# F02 security fix: auth token + CORS tests
# ---------------------------------------------------------------------------

_TOKEN = "test-shutdown-token-32-chars-ok!"


def test_shutdown_requires_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /api/shutdown without token returns 401 when SHUTDOWN_TOKEN is set."""
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    client = TestClient(hub.app)

    response = client.post("/api/shutdown")

    assert response.status_code == 401


def test_shutdown_with_wrong_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    client = TestClient(hub.app)

    response = client.post("/api/shutdown", headers={"X-Sales-Copilot-Token": "wrong"})

    assert response.status_code == 401


def test_start_call_requires_auth_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    hub.reset_config_state()
    client = TestClient(hub.app)

    response = client.post("/api/start-call", json={"config": {"preset": "sales", "install_code": ""}})

    assert response.status_code == 401


def test_upload_requires_auth_when_token_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /upload without token returns 401 when SHUTDOWN_TOKEN is set."""
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    monkeypatch.setattr(hub_upload, "UPLOAD_ROOT", tmp_path)
    client = TestClient(hub.app)

    response = client.post(
        "/upload",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 401


def test_start_call_allowed_with_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    hub.reset_config_state()
    client = TestClient(hub.app)

    response = client.post(
        "/api/start-call",
        json={"config": {"preset": "sales", "install_code": ""}},
        headers={"X-Sales-Copilot-Token": _TOKEN},
    )

    assert response.status_code == 200


def test_start_call_rejects_unknown_domain_preset(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/start-call",
        json={"config": {"preset_name": "../private", "install_code": ""}},
    )

    assert response.status_code == 400


def test_start_call_accepts_empty_install_code(authed_client: TestClient) -> None:
    hub.reset_config_state()

    response = authed_client.post(
        "/api/start-call",
        json={"config": {"preset": "sales", "install_code": ""}},
    )

    assert response.status_code == 200
    assert hub.get_latest_config() is not None
    assert hub.get_latest_config().get("install_code") == ""


def test_start_call_defaults_missing_install_code_to_empty(authed_client: TestClient) -> None:
    hub.reset_config_state()

    response = authed_client.post("/api/start-call", json={"config": {"preset": "sales"}})

    assert response.status_code == 200
    assert hub.get_latest_config().get("install_code") == ""


def test_start_call_rejects_non_string_install_code(authed_client: TestClient) -> None:
    hub.reset_config_state()

    response = authed_client.post(
        "/api/start-call",
        json={"config": {"preset": "sales", "install_code": 123}},
    )

    assert response.status_code == 400


def test_detection_feedback_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    client = TestClient(hub.app)

    response = client.post(
        "/api/detection-feedback",
        json={"detection_type": "objection", "predicted_category": "prijs"},
    )

    assert response.status_code == 401


def test_detection_feedback_returns_ok_when_opt_in_off(authed_client: TestClient) -> None:
    hub.reset_config_state()

    response = authed_client.post(
        "/api/detection-feedback",
        json={"detection_type": "objection", "predicted_category": "prijs"},
    )

    assert response.status_code == 200
    assert response.json()["forwarded"] is False


def test_detection_feedback_forwards_when_opt_in_on(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEASUREMENT_CORRECTION_OPT_IN", "true")
    monkeypatch.setenv("MEASUREMENT_CORRECTION_ENDPOINT", "http://localhost:9999/correction")
    posted: list[dict] = []
    monkeypatch.setattr(
        "sales_copilot.core.measurement_signals._post_json",
        lambda url, payload: posted.append(payload) or True,
    )
    hub.reset_config_state()

    response = authed_client.post(
        "/api/detection-feedback",
        json={
            "detection_type": "objection",
            "predicted_category": "prijs",
            "corrected_category": "timing",
        },
    )

    assert response.status_code == 200
    assert response.json()["forwarded"] is True
    assert len(posted) == 1
    payload = posted[0]
    assert payload["event_type"] == "detection_correction"
    assert "transcript" not in payload
    assert "trigger_phrase" not in payload


def test_session_token_endpoint_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    client = TestClient(hub.app)

    response = client.get("/api/v1/auth/session-token", headers={"host": "localhost:8760"})

    assert response.status_code == 200
    assert response.json()["token"] == _TOKEN


def test_hub_token_loads_env_before_selecting_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SHUTDOWN_TOKEN", raising=False)

    def _load_env() -> bool:
        monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
        return True

    monkeypatch.setattr(hub_auth, "load_env", _load_env)

    assert get_hub_token() == _TOKEN


def test_session_token_blocked_from_non_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    client = TestClient(hub.app)

    response = client.get("/api/v1/auth/session-token", headers={"host": "evil.com"})

    assert response.status_code == 403


def test_cors_allows_localhost_origin() -> None:
    client = TestClient(hub.app)

    response = client.options(
        "/api/status",
        headers={
            "Origin": "http://localhost:8760",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code in (200, 204)
    # The middleware must echo the exact allowed origin, not a wildcard — the
    # status code alone does not prove the CORS grant.
    assert response.headers["access-control-allow-origin"] == "http://localhost:8760"


def test_static_responses_include_security_headers() -> None:
    client = TestClient(hub.app)

    response = client.get("/dashboard/")

    assert response.status_code == 200
    assert "object-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_ws_channel_rejects_external_origin() -> None:
    """WS connection from a non-localhost origin must be closed with code 1008."""
    hub.reset_config_state()
    client = TestClient(hub.app)

    with pytest.raises(Exception):
        with client.websocket_connect(
            "/ws/transcript",
            headers={"origin": "https://evil.example.com"},
        ) as ws:
            ws.receive_text()


def test_ws_channel_allows_localhost_origin() -> None:
    hub.reset_config_state()
    client = TestClient(hub.app)

    with client.websocket_connect(
        "/ws/config",
        headers={"origin": "http://localhost:8760"},
    ) as ws:
        ws.send_json({"type": "end_call"})


def test_ws_channel_rejects_unauthenticated_no_origin() -> None:
    hub.reset_config_state()
    client = TestClient(hub.app)

    with pytest.raises(Exception):
        with client.websocket_connect("/ws/config"):
            pass


def test_ws_channel_allows_authenticated_no_origin() -> None:
    hub.reset_config_state()
    client = TestClient(hub.app)

    with client.websocket_connect("/ws/config", headers=_ws_auth_headers()):
        pass


def test_ws_channel_rejects_query_token() -> None:
    client = TestClient(hub.app)

    # F02 regression: a token supplied only in the query string must NOT
    # authenticate the socket. The hub closes pre-accept with policy code 1008.
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/ws/config?token={get_hub_token()}"):
            pass
    assert exc_info.value.code == 1008


# ---------------------------------------------------------------------------
# Hint feedback endpoint tests
# ---------------------------------------------------------------------------


def test_hint_feedback_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    client = TestClient(hub.app)

    response = client.post(
        "/api/hint-feedback",
        json={"hint": "Hoe ziet jullie proces eruit?", "feedback": "up"},
    )

    assert response.status_code == 401


def test_hint_feedback_writes_record(
    authed_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback_store, "_hints_path", lambda: tmp_path / "hints.ndjson")
    hub.reset_config_state()

    response = authed_client.post(
        "/api/hint-feedback",
        json={
            "hint": "Hoe ziet jullie huidige proces eruit?",
            "feedback": "up",
            "context_utterance": "We verliezen veel tijd met offertes.",
            "context_utterances": [
                "We verliezen veel tijd met offertes.",
                "Vooral handmatig opvolgen.",
            ],
            "session_id": "test-session-42",
            "phase": "discovery",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    path = tmp_path / "hints.ndjson"
    assert path.exists()
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["hint"] == "Hoe ziet jullie huidige proces eruit?"
    assert record["feedback"] == "up"
    assert record["label"] == "hint_positive"
    assert record["text"] == "We verliezen veel tijd met offertes."
    assert record["session_id"] == "test-session-42"
    assert record["phase"] == "discovery"
    assert record["source"] == "tester_dashboard"


def test_hint_feedback_rejects_invalid_direction(authed_client: TestClient) -> None:
    response = authed_client.post(
        "/api/hint-feedback",
        json={"hint": "x", "feedback": "sideways"},
    )

    assert response.status_code == 422


def test_hint_feedback_end_to_end_with_suggestions_channel(
    authed_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A suggestion broadcasted on /ws/suggestions can be voted on via the HTTP endpoint."""
    monkeypatch.setattr(feedback_store, "_hints_path", lambda: tmp_path / "hints.ndjson")
    hub.reset_config_state()

    # Simulate the detector publishing a suggestion on /ws/suggestions.  The
    # synchronous TestClient cannot open two websocket connections at once
    # without deadlocking, so we exercise the real broadcast path via the public
    # hub helper and then verify the feedback endpoint.
    asyncio.run(
        hub.broadcast(
            "suggestions",
            {"type": "suggestion", "questions": ["Wat is het budget?"], "timestamp_ms": 1234},
        )
    )

    response = authed_client.post(
        "/api/hint-feedback",
        json={
            "hint": "Wat is het budget?",
            "feedback": "down",
            "timestamp_ms": 1234,
            "context_utterance": "We willen eerst een demo zien.",
        },
    )

    assert response.status_code == 200
    path = tmp_path / "hints.ndjson"
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["feedback"] == "down"
    assert record["label"] == "hint_negative"
    assert record["timestamp_ms"] == 1234


def test_hint_feedback_defaults_session_id(
    authed_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(feedback_store, "_hints_path", lambda: tmp_path / "hints.ndjson")
    hub.reset_config_state()

    response = authed_client.post(
        "/api/hint-feedback",
        json={"hint": "x", "feedback": "up"},
    )

    assert response.status_code == 200
    record = json.loads((tmp_path / "hints.ndjson").read_text(encoding="utf-8").strip())
    assert record["session_id"]
