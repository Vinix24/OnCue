from __future__ import annotations

import asyncio
import json
import logging

import pytest

import sales_copilot.__main__ as app_main
from sales_copilot.__main__ import _guarded_task, _parse_call_config
from sales_copilot.core.config import WebSocketConfig, build_module_configs


def _make_fake_ws_connect(published_payloads: list[dict]):
    """Return a websockets.connect-compatible factory that captures sends.

    websockets.connect is used as ``async with websockets.connect(url) as ws``,
    so the mock must return an async context manager (not a coroutine).
    """

    class _FakeWS:
        async def send(self, data: str) -> None:
            published_payloads.append(json.loads(data))

    class _FakeCtx:
        def __init__(self, _url: str) -> None:
            pass

        async def __aenter__(self) -> _FakeWS:
            return _FakeWS()

        async def __aexit__(self, *_: object) -> None:
            pass

    def _fake_connect(url: str) -> _FakeCtx:
        return _FakeCtx(url)

    return _fake_connect


async def test_guarded_task_logs_crash_and_publishes_event(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    published_payloads: list[dict] = []
    monkeypatch.setattr(app_main.websockets, "connect", _make_fake_ws_connect(published_payloads))

    ws_config = WebSocketConfig()
    crashed_modules: set[str] = set()

    async def _crasher() -> None:
        raise RuntimeError("detector exploded")

    with caplog.at_level(logging.ERROR, logger="sales_copilot.__main__"):
        await _guarded_task(_crasher(), "detector", crashed_modules, ws_config)

    assert "detector" in crashed_modules

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "Expected ERROR log when module crashes"
    msg = error_records[0].getMessage()
    assert "detector" in msg
    assert "RuntimeError" in msg

    failure_events = [p for p in published_payloads if p.get("type") == "module_failure"]
    assert failure_events, f"Expected module_failure WS event, got: {published_payloads}"
    ev = failure_events[0]
    assert ev["module"] == "detector"
    assert "exploded" in ev["error"]
    assert "at_ms" in ev


async def test_guarded_task_warns_when_publish_fails_but_retains_crash(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A crash must stay recorded in crashed_modules even when the hub is
    unreachable. The failed publish is a logged warning, never a lost crash."""

    class _FailingCtx:
        def __init__(self, _url: str) -> None:
            pass

        async def __aenter__(self):
            raise ConnectionRefusedError("no hub listening")

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(app_main.websockets, "connect", lambda url: _FailingCtx(url))

    ws_config = WebSocketConfig()
    crashed_modules: set[str] = set()

    async def _crasher() -> None:
        raise RuntimeError("detector exploded")

    with caplog.at_level(logging.WARNING, logger="sales_copilot.__main__"):
        await _guarded_task(_crasher(), "detector", crashed_modules, ws_config)

    # The crash is retained regardless of whether the failure could be published.
    assert "detector" in crashed_modules

    publish_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "Could not publish module_failure" in r.getMessage()
    ]
    assert publish_warnings, "Expected a publish-failure WARNING when the hub is unreachable"
    assert "detector" in publish_warnings[0].getMessage()


async def test_guarded_task_reraises_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    published_payloads: list[dict] = []
    monkeypatch.setattr(app_main.websockets, "connect", _make_fake_ws_connect(published_payloads))

    ws_config = WebSocketConfig()
    crashed_modules: set[str] = set()

    async def _long_running() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(
        _guarded_task(_long_running(), "detector", crashed_modules, ws_config)
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "detector" not in crashed_modules
    assert not published_payloads


async def test_guarded_task_succeeding_module_not_marked_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_payloads: list[dict] = []
    monkeypatch.setattr(app_main.websockets, "connect", _make_fake_ws_connect(published_payloads))

    ws_config = WebSocketConfig()
    crashed_modules: set[str] = set()

    async def _fine() -> None:
        return

    await _guarded_task(_fine(), "talk_time", crashed_modules, ws_config)

    assert "talk_time" not in crashed_modules
    assert not published_payloads


async def test_run_call_publishes_module_failure_on_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published_payloads: list[dict] = []
    monkeypatch.setattr(app_main.websockets, "connect", _make_fake_ws_connect(published_payloads))
    monkeypatch.setattr(
        app_main.hub, "wait_for_end_call", lambda timeout=0.25: asyncio.sleep(0, result=True)
    )

    async def _failing_module(**_kwargs) -> None:
        raise RuntimeError("talk_time boom")

    monkeypatch.setattr(app_main, "talk_time_main", _failing_module)

    call_config = _parse_call_config(
        {
            "enable_talk_time": True,
            "enable_transcriber": False,
            "enable_detector": False,
            "enable_reports": False,
        }
    )
    configs = build_module_configs(call_config)
    stop_event = asyncio.Event()
    ws_config = WebSocketConfig()

    await app_main._run_call(stop_event, call_config, configs, ws_config)

    failure_events = [p for p in published_payloads if p.get("type") == "module_failure"]
    assert failure_events, f"Expected module_failure event; got: {published_payloads}"
    assert failure_events[0]["module"] == "talk_time"
    assert "boom" in failure_events[0]["error"]
