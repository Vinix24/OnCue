"""Direct unit tests for the hub auth primitives.

These cover the fail-safe branches of ``websocket_token`` (Basic-auth parsing)
and ``is_valid_token`` (constant-time compare) that the integration WS tests
only exercise indirectly. Every malformed input must deny, never crash.
"""

from __future__ import annotations

from base64 import b64encode

import pytest

from sales_copilot.websocket.hub_auth import is_valid_token, websocket_token

_TOKEN = "test-hub-auth-token-32-chars-okay!"


def _basic(raw: str) -> str:
    return f"Basic {b64encode(raw.encode()).decode()}"


# ---------------------------------------------------------------------------
# websocket_token — Basic-auth parsing (item 33)
# ---------------------------------------------------------------------------


def test_websocket_token_extracts_password_for_token_user() -> None:
    assert websocket_token(_basic("token:secret-value")) == "secret-value"


def test_websocket_token_preserves_colons_in_password() -> None:
    # partition(":") keeps everything after the first colon as the password.
    assert websocket_token(_basic("token:a:b:c")) == "a:b:c"


def test_websocket_token_rejects_non_basic_scheme() -> None:
    assert websocket_token(f"Bearer {b64encode(b'token:secret').decode()}") == ""


def test_websocket_token_rejects_wrong_username() -> None:
    assert websocket_token(_basic("user:secret")) == ""


def test_websocket_token_rejects_missing_colon_separator() -> None:
    # No ':' means no separator -> deny rather than treat the whole blob as user.
    assert websocket_token(_basic("tokensecret")) == ""


def test_websocket_token_rejects_empty_credentials() -> None:
    assert websocket_token("Basic ") == ""


def test_websocket_token_rejects_empty_authorization() -> None:
    assert websocket_token("") == ""


def test_websocket_token_rejects_non_base64_payload() -> None:
    # b64decode raises on malformed input; the helper must swallow it and deny.
    assert websocket_token("Basic !!!not-base64!!!") == ""


def test_websocket_token_rejects_non_utf8_payload() -> None:
    # Valid base64 that decodes to non-UTF-8 bytes -> UnicodeDecodeError -> deny.
    encoded = b64encode(b"\xff\xfe").decode()
    assert websocket_token(f"Basic {encoded}") == ""


def test_websocket_token_is_case_insensitive_scheme() -> None:
    assert websocket_token(_basic("token:secret").replace("Basic", "basic")) == "secret"


# ---------------------------------------------------------------------------
# is_valid_token — constant-time compare edge cases (item 34)
# ---------------------------------------------------------------------------


def test_is_valid_token_accepts_exact_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    assert is_valid_token(_TOKEN) is True


def test_is_valid_token_rejects_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    assert is_valid_token("") is False


def test_is_valid_token_rejects_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    # The signature is typed str, but defensive `provided or ""` must not raise
    # on None — it must deny.
    assert is_valid_token(None) is False  # type: ignore[arg-type]


def test_is_valid_token_rejects_wrong_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHUTDOWN_TOKEN", _TOKEN)
    assert is_valid_token("definitely-not-the-token") is False
