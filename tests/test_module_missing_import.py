"""A module whose optional dependency is missing must not stop OnCue.

Field finding (Windows notetaker profile, `.[windows]` plus aiosqlite): with
"AI detectie" on, the detector import failed inside ``_run_call`` and the
``finally: sys.exit(0)`` in ``main()`` turned that into a silent exit 0. The
process vanished without a message. These tests pin the two halves of the fix:
the missing module is reported and the call goes on, and ``main()`` no longer
turns a real error into exit 0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys

import pytest

import sales_copilot.__main__ as app_main
from sales_copilot.__main__ import _parse_call_config
from sales_copilot.core.config import WebSocketConfig, build_module_configs

_DETECTOR_MODULE = "sales_copilot.modules.detector.__main__"
_REPORTS_MODULE = "sales_copilot.modules.reports.__main__"


def _make_fake_ws_connect(published_payloads: list[dict]):
    class _FakeWS:
        async def send(self, data: str) -> None:
            published_payloads.append(json.loads(data))

    class _FakeCtx:
        async def __aenter__(self) -> _FakeWS:
            return _FakeWS()

        async def __aexit__(self, *_: object) -> None:
            pass

    return lambda _url: _FakeCtx()


def _block_import(monkeypatch: pytest.MonkeyPatch, module: str) -> None:
    """Make ``import <module>`` raise ImportError, as a missing package does."""
    monkeypatch.setitem(sys.modules, module, None)


async def _run_call_with(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enable_detector: bool,
    enable_reports: bool,
) -> tuple[list[dict], list[str]]:
    published: list[dict] = []
    monkeypatch.setattr(app_main.websockets, "connect", _make_fake_ws_connect(published))

    transcriber_started: list[str] = []
    call_ended = asyncio.Event()

    async def _transcriber(**_kwargs) -> None:
        transcriber_started.append("transcriber")
        await call_ended.wait()

    async def _wait_for_end_call(timeout: float = 0.25) -> bool:
        # Give the transcriber task a chance to start before the call ends.
        await asyncio.sleep(0.05)
        call_ended.set()
        return True

    monkeypatch.setattr(app_main, "transcriber_main", _transcriber)
    monkeypatch.setattr(app_main.hub, "wait_for_end_call", _wait_for_end_call)

    call_config = _parse_call_config(
        {
            "enable_talk_time": False,
            "enable_transcriber": True,
            "enable_detector": enable_detector,
            "enable_reports": enable_reports,
        }
    )
    await app_main._run_call(
        asyncio.Event(), call_config, build_module_configs(call_config), WebSocketConfig()
    )
    return published, transcriber_started


async def test_missing_detector_import_reports_failure_and_call_continues(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _block_import(monkeypatch, _DETECTOR_MODULE)

    with caplog.at_level(logging.ERROR, logger=app_main.logger.name):
        published, transcriber_started = await _run_call_with(
            monkeypatch, enable_detector=True, enable_reports=False
        )

    failures = [p for p in published if p.get("type") == "module_failure"]
    assert len(failures) == 1, published
    assert failures[0]["module"] == "detector"
    assert failures[0]["pip_extra"] == "detector"
    assert failures[0]["install_hint"] == 'pip install ".[detector]"'
    assert failures[0]["error"]
    assert transcriber_started == ["transcriber"]
    assert any(
        "Module 'detector' is not available" in r.getMessage() and "detector" in r.getMessage()
        for r in caplog.records
    )


async def test_missing_reports_import_reports_failure_and_call_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_import(monkeypatch, _REPORTS_MODULE)

    published, transcriber_started = await _run_call_with(
        monkeypatch, enable_detector=False, enable_reports=True
    )

    failures = [p for p in published if p.get("type") == "module_failure"]
    assert len(failures) == 1, published
    assert failures[0]["module"] == "reports"
    assert failures[0]["pip_extra"] == "slides"
    assert failures[0]["install_hint"] == 'pip install ".[slides]"'
    assert transcriber_started == ["transcriber"]


async def test_both_modules_missing_reports_each_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _block_import(monkeypatch, _DETECTOR_MODULE)
    _block_import(monkeypatch, _REPORTS_MODULE)

    published, transcriber_started = await _run_call_with(
        monkeypatch, enable_detector=True, enable_reports=True
    )

    failed = sorted(p["module"] for p in published if p.get("type") == "module_failure")
    assert failed == ["detector", "reports"]
    assert transcriber_started == ["transcriber"]


async def test_report_missing_module_survives_unreachable_hub(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No dashboard to tell: the failure is logged and nothing raises."""

    def _refuse(_url: str):
        raise OSError("hub down")

    monkeypatch.setattr(app_main.websockets, "connect", _refuse)
    crashed: set[str] = set()

    with caplog.at_level(logging.WARNING, logger=app_main.logger.name):
        await app_main._report_missing_module(
            "detector", ModuleNotFoundError("x", name="semantic_router"), crashed, WebSocketConfig()
        )

    assert crashed == {"detector"}
    assert any("Could not publish module_failure" in r.getMessage() for r in caplog.records)


def _stub_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main, "ensure_app_support_tree", lambda: None)
    monkeypatch.setattr(app_main, "seed_app_support_defaults", lambda: None)
    monkeypatch.setattr(app_main, "load_env", lambda: None)
    monkeypatch.setattr(app_main, "configure_logging", lambda: None)
    monkeypatch.setattr(app_main, "_start_hub", lambda _ws_config: None)
    monkeypatch.setattr(app_main, "open_front_door", lambda _port: None)
    monkeypatch.setattr(signal, "signal", lambda *_args: None)


def test_main_exits_nonzero_on_unexpected_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_startup(monkeypatch)

    async def _boom(_ws_config) -> None:
        raise ValueError("kapotte configuratie")

    monkeypatch.setattr(app_main, "_broadcast_license_status", _boom)

    with pytest.raises(SystemExit) as exc_info:
        app_main.main()

    assert exc_info.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "unexpected error" in err
    assert "ValueError" in err
    assert "kapotte configuratie" in err


def test_main_exits_nonzero_on_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_startup(monkeypatch)

    async def _boom(_ws_config) -> None:
        raise RuntimeError("event loop kapot")

    monkeypatch.setattr(app_main, "_broadcast_license_status", _boom)

    with pytest.raises(SystemExit) as exc_info:
        app_main.main()

    assert exc_info.value.code not in (0, None)


def test_main_exits_zero_on_keyboard_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A requested stop is not an error."""
    _stub_startup(monkeypatch)

    async def _interrupt(_ws_config) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(app_main, "_broadcast_license_status", _interrupt)

    with pytest.raises(SystemExit) as exc_info:
        app_main.main()

    assert exc_info.value.code == 0
