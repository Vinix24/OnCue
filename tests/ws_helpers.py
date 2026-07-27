"""Authenticated WebSocket URL helpers for integration tests."""

from __future__ import annotations

from sales_copilot.websocket.hub_auth import authenticated_ws_url


def ws_url(host: str, port: int, channel: str) -> str:
    return authenticated_ws_url(f"ws://{host}:{port}/ws/{channel}")
