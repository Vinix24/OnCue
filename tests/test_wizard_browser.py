"""Tests for the browser-served permissions wizard (W3)."""

from __future__ import annotations

import asyncio
import json
from base64 import b64encode
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import websockets
from fastapi.testclient import TestClient

from sales_copilot.websocket import hub
from sales_copilot.wizard.cli import _DOCTOR_STEP_DEFINITION, _PRO_STEP_DEFINITIONS
from sales_copilot.wizard.serialize import get_step_descriptors
from sales_copilot.wizard.state import WizardState, load_state, save_state
from sales_copilot.wizard.steps import StepResult, WizardStep

# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------


def test_step_descriptors_include_core_pro_doctor(free_feature_policy: object) -> None:
    descriptors = get_step_descriptors(free_feature_policy)
    ids = [d["step_id"] for d in descriptors]
    assert ids == ["mic", "route", "model", "provider", "autostart", "calltap", "doctor"]


def test_step_descriptors_lock_pro_steps_for_free(free_feature_policy: object) -> None:
    descriptors = get_step_descriptors(free_feature_policy)
    by_id = {d["step_id"]: d for d in descriptors}
    assert by_id["mic"]["locked"] is False
    assert by_id["route"]["locked"] is False
    assert by_id["model"]["locked"] is False
    assert by_id["provider"]["locked"] is False
    assert by_id["autostart"]["locked"] is True
    assert by_id["calltap"]["locked"] is True
    assert by_id["doctor"]["locked"] is False


def test_step_descriptors_unlock_pro_steps_for_pro(pro_feature_policy: object) -> None:
    descriptors = get_step_descriptors(pro_feature_policy)
    by_id = {d["step_id"]: d for d in descriptors}
    assert by_id["autostart"]["locked"] is False
    assert by_id["calltap"]["locked"] is False
    assert by_id["autostart"]["active"] is True
    assert by_id["calltap"]["active"] is True


def test_step_descriptors_never_duplicate_definitions() -> None:
    """The descriptor list must derive from the same objects used by get_steps."""
    from sales_copilot.wizard.cli import _CORE_STEP_DEFINITIONS

    descriptors = get_step_descriptors()
    source_ids = [s.step_id for s in (*_CORE_STEP_DEFINITIONS, *_PRO_STEP_DEFINITIONS, _DOCTOR_STEP_DEFINITION)]
    assert [d["step_id"] for d in descriptors] == source_ids


# ---------------------------------------------------------------------------
# HTTP API tests
# ---------------------------------------------------------------------------


def _patch_policy(monkeypatch: pytest.MonkeyPatch, policy: object) -> None:
    """Patch get_feature_policy in every module that imported it directly."""
    monkeypatch.setattr("sales_copilot.auth.feature_policy.get_feature_policy", lambda: policy)
    monkeypatch.setattr("sales_copilot.websocket.hub_wizard.get_feature_policy", lambda: policy)
    monkeypatch.setattr("sales_copilot.wizard.serialize.get_feature_policy", lambda: policy)


def test_get_wizard_steps_endpoint(
    monkeypatch: pytest.MonkeyPatch, free_feature_policy: object
) -> None:
    _patch_policy(monkeypatch, free_feature_policy)
    client = TestClient(hub.app)
    response = client.get("/api/v1/wizard/steps")
    assert response.status_code == 200
    data = response.json()
    assert data["tier"] == "free"
    assert [s["step_id"] for s in data["steps"]] == [
        "mic",
        "route",
        "model",
        "provider",
        "autostart",
        "calltap",
        "doctor",
    ]
    assert all("title" in s and "locked" in s and "active" in s for s in data["steps"])


def test_get_wizard_steps_endpoint_shows_unlocked_pro_for_pro(
    monkeypatch: pytest.MonkeyPatch, pro_feature_policy: object
) -> None:
    _patch_policy(monkeypatch, pro_feature_policy)
    client = TestClient(hub.app)
    response = client.get("/api/v1/wizard/steps")
    assert response.status_code == 200
    data = response.json()
    assert data["tier"] == "pro"
    autostart = next(s for s in data["steps"] if s["step_id"] == "autostart")
    calltap = next(s for s in data["steps"] if s["step_id"] == "calltap")
    assert autostart["locked"] is False
    assert calltap["locked"] is False
    assert autostart["active"] is True
    assert calltap["active"] is True


def test_get_wizard_state_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_path = tmp_path / "wizard_state.json"
    save_state(WizardState(completed=["mic"], results={"mic": {"ok": True, "message": "ok"}}), state_path)
    monkeypatch.setattr("sales_copilot.wizard.state.DEFAULT_STATE_PATH", state_path)

    client = TestClient(hub.app)
    response = client.get("/api/v1/wizard/state")
    assert response.status_code == 200
    data = response.json()
    assert data["completed"] == ["mic"]
    assert data["results"]["mic"]["ok"] is True


def test_restart_wizard_endpoint_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-shutdown-token-32-chars-ok!")
    client = TestClient(hub.app)

    response = client.post("/api/v1/wizard/restart")
    assert response.status_code == 401


def test_restart_wizard_endpoint_clears_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authed_client: TestClient,
) -> None:
    state_path = tmp_path / "wizard_state.json"
    save_state(WizardState(completed=["mic"], results={"mic": {"ok": True}}), state_path)
    monkeypatch.setattr("sales_copilot.wizard.state.DEFAULT_STATE_PATH", state_path)

    response = authed_client.post("/api/v1/wizard/restart")
    assert response.status_code == 200
    data = response.json()
    assert data["completed"] == []
    assert data["results"] == {}

    loaded = load_state(state_path)
    assert loaded.completed == []


def test_run_wizard_step_endpoint_runs_and_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authed_client: TestClient,
) -> None:
    state_path = tmp_path / "wizard_state.json"
    monkeypatch.setattr("sales_copilot.wizard.state.DEFAULT_STATE_PATH", state_path)

    fake_step = WizardStep(
        step_id="route",
        title="Route",
        auto_detect=lambda: StepResult(ok=True, message="route OK"),
    )

    with patch("sales_copilot.websocket.hub_wizard.get_steps", return_value=[fake_step]):
        response = authed_client.post("/api/v1/wizard/run/route")

    assert response.status_code == 200
    data = response.json()
    assert data["step_id"] == "route"
    assert data["ok"] is True
    assert "route OK" in data["message"]

    loaded = load_state(state_path)
    assert "route" in loaded.completed
    assert loaded.results["route"]["ok"] is True


def test_run_unknown_wizard_step_returns_error(authed_client: TestClient) -> None:
    with patch("sales_copilot.websocket.hub_wizard.get_steps", return_value=[]):
        response = authed_client.post("/api/v1/wizard/run/no-such-step")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert "Onbekende" in data["message"]


def test_run_wizard_step_endpoint_stops_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authed_client: TestClient,
) -> None:
    state_path = tmp_path / "wizard_state.json"
    monkeypatch.setattr("sales_copilot.wizard.state.DEFAULT_STATE_PATH", state_path)

    fake_step = WizardStep(
        step_id="mic",
        title="Microfoon",
        auto_detect=lambda: StepResult(ok=False, message="no mic"),
        needs_meter=True,
        proof=lambda **kwargs: StepResult(ok=True, message="proof should not run"),
    )

    with patch("sales_copilot.websocket.hub_wizard.get_steps", return_value=[fake_step]):
        response = authed_client.post("/api/v1/wizard/run/mic")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is False
    assert data["message"] == "no mic"

    loaded = load_state(state_path)
    assert "mic" in loaded.completed
    assert loaded.results["mic"]["ok"] is False


# ---------------------------------------------------------------------------
# WebSocket meter tests
# ---------------------------------------------------------------------------


async def test_run_mic_step_streams_meter_samples(
    running_hub: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mic proof forwards dBFS samples to /ws/wizard while it runs."""

    state_path = tmp_path / "wizard_state.json"
    monkeypatch.setattr("sales_copilot.wizard.state.DEFAULT_STATE_PATH", state_path)
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-shutdown-token-32-chars-ok!")

    from sales_copilot.websocket.hub_auth import get_hub_token

    def _fake_proof(*, emitter=None, **kwargs):
        if emitter is not None:
            emitter(-30.0)
            emitter(-12.5)
        return StepResult(ok=True, message="mic proof OK", details={"peak_db": -12.5})

    fake_step = WizardStep(
        step_id="mic",
        title="Microfoon",
        auto_detect=lambda: StepResult(ok=True, message="mic detected"),
        needs_meter=True,
        proof=_fake_proof,
    )

    base_url = f"http://127.0.0.1:{running_hub}"
    ws_uri = f"ws://127.0.0.1:{running_hub}/ws/wizard"
    token = get_hub_token()
    credentials = b64encode(f"token:{token}".encode()).decode()

    with patch("sales_copilot.websocket.hub_wizard.get_steps", return_value=[fake_step]):
        async with websockets.connect(
            ws_uri,
            origin="http://localhost:8760",
            additional_headers={"authorization": f"Basic {credentials}"},
        ) as ws:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{base_url}/api/v1/wizard/run/mic",
                    headers={"X-Sales-Copilot-Token": token},
                )

            samples: list[dict] = []
            for _ in range(5):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    payload = json.loads(raw)
                    if payload.get("type") == "meter":
                        samples.append(payload)
                except TimeoutError:
                    break

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "mic proof OK" in data["message"]

    assert len(samples) >= 2
    assert samples[0]["dbfs"] == -30.0
    assert samples[1]["dbfs"] == -12.5


# ---------------------------------------------------------------------------
# Doctor / resume tests
# ---------------------------------------------------------------------------


def test_doctor_step_reuses_persisted_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authed_client: TestClient,
) -> None:
    state_path = tmp_path / "wizard_state.json"
    monkeypatch.setenv("WIZARD_STATE_PATH", str(state_path))

    save_state(
        WizardState(
            completed=["mic", "route"],
            results={
                "mic": {"ok": True, "message": "mic ok"},
                "route": {"ok": True, "message": "route ok"},
            },
        ),
        state_path,
    )

    detector_calls: list[str] = []

    def _make_detector(step_id: str):
        def _detect() -> StepResult:
            detector_calls.append(step_id)
            return StepResult(ok=True, message=f"{step_id} live")
        return _detect

    steps = [
        WizardStep(step_id="mic", title="Microfoon", auto_detect=_make_detector("mic")),
        WizardStep(step_id="route", title="Route", auto_detect=_make_detector("route")),
        WizardStep(step_id="doctor", title="Doctor", auto_detect=lambda results, steps, detectors: StepResult(
            ok=True,
            message=f"doctor saw {sorted(results)} and re-ran {detector_calls}",
        )),
    ]

    with patch("sales_copilot.websocket.hub_wizard.get_steps", return_value=steps):
        response = authed_client.post("/api/v1/wizard/run/doctor")

    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "doctor saw" in data["message"]


# ---------------------------------------------------------------------------
# Browser page structure test
# ---------------------------------------------------------------------------


_WIZARD_HTML = Path(__file__).parent.parent / "dashboard" / "wizard" / "index.html"


class _ElementFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        el_id = attrs_dict.get("id")
        if el_id:
            self.elements[el_id] = {"tag": tag, **attrs_dict}


def test_wizard_page_has_required_mount_points() -> None:
    parser = _ElementFinder()
    parser.feed(_WIZARD_HTML.read_text(encoding="utf-8"))

    assert "wizard-steps" in parser.elements, "wizard-steps mount point missing"
    assert "tier-badge" in parser.elements, "tier-badge missing"
    assert "restart-btn" in parser.elements, "restart button missing"
    assert parser.elements["restart-btn"].get("type") == "button"


def test_wizard_page_loads_vanilla_stylesheets() -> None:
    text = _WIZARD_HTML.read_text(encoding="utf-8")
    assert "dashboard.css" in text
    assert "wizard.css" in text
    assert "wizard.js" not in text, "page must be self-contained vanilla HTML/CSS/JS"
