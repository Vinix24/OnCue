"""Shared authentication helpers for the WebSocket hub HTTP endpoints."""

from __future__ import annotations

import hmac
import os
import secrets
import stat
from base64 import b64decode
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit

from fastapi import HTTPException, Request

from sales_copilot.core.config import load_env
from sales_copilot.core.paths import resolve_app_path

_SHUTDOWN_TOKEN_ENV = "SHUTDOWN_TOKEN"
_TOKEN_FILE_ENV = "SALES_COPILOT_HUB_TOKEN_FILE"
_DEFAULT_TOKEN_FILE = resolve_app_path(".vnx-data/hub_token")


class _WebSocketConfig(Protocol):
    host: str
    port: int


def _token_file() -> Path:
    return Path(os.environ.get(_TOKEN_FILE_ENV, str(_DEFAULT_TOKEN_FILE)))


def get_hub_token() -> str:
    """Return the shared hub token, creating a private local token when needed."""

    load_env()
    configured = os.environ.get(_SHUTDOWN_TOKEN_ENV, "").strip()
    if configured:
        return configured

    path = _token_file()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(stat.S_IRWXU)
    except OSError:
        pass

    token = secrets.token_urlsafe(32)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(token)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return token
    except FileExistsError:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
        raise RuntimeError(f"Hub token file is empty: {path}") from None


def is_valid_token(provided: str) -> bool:
    """Constant-time validation for HTTP and WebSocket clients."""

    return hmac.compare_digest(get_hub_token(), provided or "")


def authenticated_ws_url(url: str) -> str:
    """Add HTTP Basic userinfo to an internal WebSocket URL."""

    parsed = urlsplit(url)
    netloc = f"token:{quote(get_hub_token(), safe='')}@{parsed.netloc}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def channel_ws_url(config: _WebSocketConfig, channel: str) -> str:
    """Build an authenticated URL for an internal hub channel."""

    return authenticated_ws_url(f"ws://{config.host}:{config.port}/ws/{quote(channel, safe='')}")


def websocket_token(authorization: str) -> str:
    """Extract a process token from WebSocket Basic auth."""

    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() == "basic" and encoded:
        try:
            username, separator, password = b64decode(encoded).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return ""
        if separator and username == "token":
            return password
    return ""


def require_token(request: Request) -> None:
    """FastAPI dependency: require the shared local hub token."""

    provided = request.headers.get("X-Sales-Copilot-Token", "")
    if not is_valid_token(provided):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: valid X-Sales-Copilot-Token header required",
        )
