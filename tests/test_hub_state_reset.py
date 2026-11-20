"""The autouse reset in conftest keeps hub call and sample state from leaking."""

from __future__ import annotations

from sales_copilot.websocket import hub_api, hub_core


def test_leak_is_planted_for_next_test() -> None:
    hub_core.apply_start_call({"preset_name": "sales"})
    assert hub_core._call_active is True


def test_next_test_starts_without_call_or_sample_player() -> None:
    assert hub_core._call_active is False
    assert hub_api._sample_player is None
    assert hub_core.get_latest_config() is None
