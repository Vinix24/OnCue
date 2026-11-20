"""The vocabulary budget check never stops startup, whatever the tokenizer does."""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

import pytest

from sales_copilot.modules.transcriber import vocabulary
from sales_copilot.modules.transcriber.vocabulary import (
    TokenizerUnavailableError,
    VocabularyConfig,
    check_budget,
    warn_if_over_budget,
)

CONFIG = VocabularyConfig(enabled=True, language="nl", description="", prompt_prefix="Termen", terms=("a", "b"))


def _fake_tokenizer(monkeypatch: pytest.MonkeyPatch, loader: Any) -> None:
    module = types.ModuleType("mlx_whisper.tokenizer")
    module.get_encoding = loader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.ModuleType("mlx_whisper"))
    monkeypatch.setitem(sys.modules, "mlx_whisper.tokenizer", module)
    vocabulary._logged.clear()


@pytest.mark.parametrize("error", [FileNotFoundError("multilingual.tiktoken"), RuntimeError("broken")])
def test_loader_failure_does_not_raise_and_logs_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception
) -> None:
    def loader(_name: str) -> Any:
        raise error

    _fake_tokenizer(monkeypatch, loader)
    with caplog.at_level(logging.INFO):
        assert warn_if_over_budget(CONFIG, "x.yaml") is None
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    with pytest.raises(TokenizerUnavailableError):
        check_budget(CONFIG)


def test_count_failure_stays_visible_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        def encode(self, _text: str) -> list[int]:
            raise ValueError("bad")

    _fake_tokenizer(monkeypatch, lambda _name: Broken())
    with caplog.at_level(logging.INFO):
        assert warn_if_over_budget(CONFIG) is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    with pytest.raises(TokenizerUnavailableError):
        check_budget(CONFIG)


def test_missing_tokenizer_package_logs_info_only(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "mlx_whisper", None)
    monkeypatch.setitem(sys.modules, "mlx_whisper.tokenizer", None)
    vocabulary._logged.clear()
    with caplog.at_level(logging.INFO):
        assert warn_if_over_budget(CONFIG) is None
    assert [r.levelno for r in caplog.records] == [logging.INFO]
