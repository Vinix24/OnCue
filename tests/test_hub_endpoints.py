from __future__ import annotations

import asyncio
import json
from base64 import b64encode
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from sales_copilot.core import context_docs, feedback_store
from sales_copilot.core.autostart_monitor import (
    AutostartMonitorConfig,
    RuntimeAutostartMonitor,
    set_active_monitor,
)
from sales_copilot.websocket import hub, hub_auth, hub_core, hub_upload
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


def test_clients_endpoint_empty_when_no_folders(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)

    response = TestClient(hub.app).get("/api/v1/clients")

    assert response.status_code == 200
    assert response.json() == {"clients": [], "cloud_sync_warning": None}


def test_clients_endpoint_lists_existing_client_folders(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    (tmp_path / "acme-corp").mkdir()
    (tmp_path / "beta-nv").mkdir()

    response = TestClient(hub.app).get("/api/v1/clients")

    assert response.json() == {
        "clients": [
            {"slug": "acme-corp", "bedrijf": "acme-corp"},
            {"slug": "beta-nv", "bedrijf": "beta-nv"},
        ],
        "cloud_sync_warning": None,
    }


def test_clients_endpoint_reads_bedrijf_from_klant_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    client_dir = tmp_path / "acme-corp"
    client_dir.mkdir()
    (client_dir / "klant.yaml").write_text("bedrijf: Acme Corp B.V.\n", encoding="utf-8")

    response = TestClient(hub.app).get("/api/v1/clients")

    assert response.json()["clients"] == [{"slug": "acme-corp", "bedrijf": "Acme Corp B.V."}]


def test_clients_endpoint_falls_back_to_slug_on_invalid_klant_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    client_dir = tmp_path / "acme-corp"
    client_dir.mkdir()
    (client_dir / "klant.yaml").write_text("aflevering: hubspot\n", encoding="utf-8")

    response = TestClient(hub.app).get("/api/v1/clients")

    assert response.json()["clients"] == [{"slug": "acme-corp", "bedrijf": "acme-corp"}]


def test_clients_endpoint_reports_cloud_sync_warning(tmp_path: Path, monkeypatch) -> None:
    from sales_copilot.core.cloud_sync_warning import CloudSyncKind, CloudSyncWarning

    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    fake_warning = CloudSyncWarning(
        kind=CloudSyncKind.ICLOUD_DRIVE,
        path=tmp_path,
        message="Deze klantmap staat in iCloud Drive.",
    )
    monkeypatch.setattr(hub_upload, "klanten_root_cloud_sync_warning", lambda: fake_warning)

    response = TestClient(hub.app).get("/api/v1/clients")

    assert response.json()["cloud_sync_warning"] == {
        "kind": "icloud_drive",
        "message": "Deze klantmap staat in iCloud Drive.",
    }


def test_upload_and_clients_endpoint_share_the_same_slug(
    tmp_path: Path, authed_client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(hub_upload, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)

    authed_client.post(
        "/upload",
        data={"company_slug": "Acme Corp!"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert TestClient(hub.app).get("/api/v1/clients").json() == {
        "clients": [{"slug": "acme-corp", "bedrijf": "acme-corp"}],
        "cloud_sync_warning": None,
    }


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


def test_insights_ask_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    client = TestClient(hub.app)

    response = client.post("/api/insights/ask", json={"text": "wat weet hij al?"})

    assert response.status_code == 401


def test_insights_ask_broadcasts_onto_insights_channel(
    authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The vraag-box question must reach `InsightEngine` via the same `insights`
    channel it already publishes to -- see docs/TTD.md's `/ws/insights` ask doc."""
    broadcast_calls: list[tuple[str, dict]] = []

    async def _fake_broadcast(channel: str, data: dict) -> None:
        broadcast_calls.append((channel, data))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)

    response = authed_client.post(
        "/api/insights/ask", json={"text": "  wat weet hij al over Lime CRM?  ", "ts": 123}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert len(broadcast_calls) == 1
    channel, payload = broadcast_calls[0]
    assert channel == "insights"
    assert payload == {"type": "ask", "text": "wat weet hij al over Lime CRM?", "ts": 123}


def test_insights_ask_rejects_blank_text(authed_client: TestClient) -> None:
    response = authed_client.post("/api/insights/ask", json={"text": "   "})

    assert response.status_code == 422


def test_insights_ask_accepts_missing_ts(authed_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    broadcast_calls: list[tuple[str, dict]] = []

    async def _fake_broadcast(channel: str, data: dict) -> None:
        broadcast_calls.append((channel, data))

    monkeypatch.setattr(hub_core, "broadcast", _fake_broadcast)

    response = authed_client.post("/api/insights/ask", json={"text": "vat het gesprek samen"})

    assert response.status_code == 200
    assert broadcast_calls[0][1]["ts"] is None


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


# ---------------------------------------------------------------------------
# Autostart consent endpoints
# ---------------------------------------------------------------------------


def test_autostart_confirm_409_when_no_pending_prompt(authed_client: TestClient) -> None:
    """POST /api/autostart/confirm returns 409 when no monitor has a pending prompt."""
    set_active_monitor(None)
    hub.reset_config_state()

    response = authed_client.post("/api/autostart/confirm")

    assert response.status_code == 409
    assert "No pending autostart consent prompt" in response.json()["detail"]


def test_autostart_decline_200_always(authed_client: TestClient) -> None:
    """POST /api/autostart/decline always returns 200, even with no active monitor."""
    set_active_monitor(None)
    hub.reset_config_state()

    response = authed_client.post("/api/autostart/decline")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_autostart_confirm_200_when_pending_consent(authed_client: TestClient) -> None:
    """POST /api/autostart/confirm returns 200 when a monitor has a pending consent prompt."""
    hub.reset_config_state()

    armed = False
    process_name = None

    def on_session_arm(name: str) -> None:
        nonlocal armed, process_name
        armed = True
        process_name = name

    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=1, debounce_disarm_polls=1),
        on_session_arm=on_session_arm,
    )
    # Transition the monitor directly to consent_pending state so we can test
    # the confirm endpoint without depending on real process detection.
    monitor.state = "consent_pending"
    monitor._detected_process = "Microsoft Teams"
    monitor._active_trigger = "video"

    set_active_monitor(monitor)

    response = authed_client.post("/api/autostart/confirm")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert armed is True
    assert process_name == "Microsoft Teams"
    # Verify the monitor has advanced to armed state
    assert monitor.state == "armed"
    # Verify no race condition on a second call: confirm again = 409
    response2 = authed_client.post("/api/autostart/confirm")
    assert response2.status_code == 409

    set_active_monitor(None)


def test_autostart_decline_resets_pending_state(authed_client: TestClient) -> None:
    """POST /api/autostart/decline resets a pending consent to idle."""
    hub.reset_config_state()

    monitor = RuntimeAutostartMonitor(
        config=AutostartMonitorConfig(debounce_arm_polls=1, debounce_disarm_polls=1),
    )
    monitor.state = "consent_pending"
    monitor._detected_process = "Zoom"
    monitor._active_trigger = "video"

    set_active_monitor(monitor)

    response = authed_client.post("/api/autostart/decline")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert monitor.state == "idle"
    assert monitor._detected_process is None

    set_active_monitor(None)


_REPORT_SESSION = "5b0c2f0e-7d1a-4c55-9a1e-0f3a8d2c4b11"


def _write_report_file(directory: Path, session_id: str, body: str, stamp: str = "2026-10-01T10-00-00") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}_{session_id}_report.json"
    path.write_text(body, encoding="utf-8")
    return path


def test_report_endpoint_returns_the_local_file_as_is(
    tmp_path: Path, authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_copilot.modules.reports import generator

    reports = tmp_path / "reports"
    monkeypatch.setattr(generator, "REPORTS_DIR", reports)
    _write_report_file(reports, _REPORT_SESSION, '{"short_summary": "old"}', "2026-10-01T09-00-00")
    _write_report_file(reports, _REPORT_SESSION, '{"short_summary": "enriched"}')

    response = authed_client.get(f"/api/reports/{_REPORT_SESSION}")

    assert response.status_code == 200
    assert response.json() == {"short_summary": "enriched"}
    assert response.headers["cache-control"] == "no-store"


def test_report_endpoint_requires_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sales_copilot.modules.reports import generator

    reports = tmp_path / "reports"
    monkeypatch.setattr(generator, "REPORTS_DIR", reports)
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    _write_report_file(reports, _REPORT_SESSION, "{}")

    assert TestClient(hub.app).get(f"/api/reports/{_REPORT_SESSION}").status_code == 401
    wrong = TestClient(hub.app, headers={"X-Sales-Copilot-Token": "wrong"})
    assert wrong.get(f"/api/reports/{_REPORT_SESSION}").status_code == 401


def test_report_endpoint_unknown_session_is_404(
    tmp_path: Path, authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_copilot.modules.reports import generator

    monkeypatch.setattr(generator, "REPORTS_DIR", tmp_path / "missing")

    assert authed_client.get(f"/api/reports/{_REPORT_SESSION}").status_code == 404


@pytest.mark.parametrize("session_id", ["..", "%2e%2e%2f%2e%2e%2fsecret", "a.b", "*", "x%2Fy", "a" * 65, "a b"])
def test_report_endpoint_rejects_unsafe_session_ids(
    tmp_path: Path, authed_client: TestClient, monkeypatch: pytest.MonkeyPatch, session_id: str
) -> None:
    from sales_copilot.modules.reports import generator

    reports = tmp_path / "reports"
    monkeypatch.setattr(generator, "REPORTS_DIR", reports)
    _write_report_file(reports, _REPORT_SESSION, "{}")
    (tmp_path / "secret_report.json").write_text('{"secret": true}', encoding="utf-8")

    response = authed_client.get(f"/api/reports/{session_id}")

    assert response.status_code in (400, 404)
    assert "secret" not in response.text


def test_report_endpoint_does_not_serve_another_sessions_suffix_match(
    tmp_path: Path, authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_copilot.modules.reports import generator

    reports = tmp_path / "reports"
    monkeypatch.setattr(generator, "REPORTS_DIR", reports)
    _write_report_file(reports, "a_b", '{"owner": "a_b"}')

    assert authed_client.get("/api/reports/b").status_code == 404
    assert authed_client.get("/api/reports/a_b").json() == {"owner": "a_b"}


def test_report_endpoint_never_follows_a_symlink(
    tmp_path: Path, authed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sales_copilot.modules.reports import generator

    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    (reports / f"2026-10-01T10-00-00_{_REPORT_SESSION}_report.json").symlink_to(outside)
    monkeypatch.setattr(generator, "REPORTS_DIR", reports)

    assert authed_client.get(f"/api/reports/{_REPORT_SESSION}").status_code == 404
