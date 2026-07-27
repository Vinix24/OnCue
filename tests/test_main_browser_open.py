"""Tests for main()'s front-door browser open (open_front_door)."""

from __future__ import annotations

import pytest

from sales_copilot import __main__ as main_mod


def test_opens_wizard_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SALES_COPILOT_NO_BROWSER", raising=False)
    monkeypatch.setattr(
        "sales_copilot.wizard.front_door.first_run_path", lambda: "/dashboard/wizard/"
    )
    opened: list[str] = []
    url = main_mod.open_front_door(8760, opener=opened.append, wait_ready=lambda _p: True)
    assert url == "http://localhost:8760/dashboard/wizard/"
    assert opened == ["http://localhost:8760/dashboard/wizard/"]


def test_opens_dashboard_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SALES_COPILOT_NO_BROWSER", raising=False)
    monkeypatch.setattr("sales_copilot.wizard.front_door.first_run_path", lambda: "/dashboard")
    opened: list[str] = []
    url = main_mod.open_front_door(8761, opener=opened.append, wait_ready=lambda _p: True)
    assert url == "http://localhost:8761/dashboard"
    assert opened == ["http://localhost:8761/dashboard"]


def test_no_browser_flag_skips_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SALES_COPILOT_NO_BROWSER", "1")
    opened: list[str] = []
    url = main_mod.open_front_door(8760, opener=opened.append, wait_ready=lambda _p: True)
    assert url is None
    assert opened == []


def test_skips_open_when_hub_never_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SALES_COPILOT_NO_BROWSER", raising=False)
    monkeypatch.setattr(
        "sales_copilot.wizard.front_door.first_run_path", lambda: "/dashboard/wizard/"
    )
    opened: list[str] = []
    url = main_mod.open_front_door(8760, opener=opened.append, wait_ready=lambda _p: False)
    assert url is None
    assert opened == []
