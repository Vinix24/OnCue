from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

import pytest

from sales_copilot.core.logging import _parse_log_level, configure_logging


def _cleanup_sales_copilot_handlers() -> None:
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, "_sales_copilot_handler", False):
            root_logger.removeHandler(handler)
            handler.close()


def test_parse_log_level_accepts_supported_values() -> None:
    assert _parse_log_level("debug") == logging.DEBUG
    assert _parse_log_level("INFO") == logging.INFO
    assert _parse_log_level("warning") == logging.WARNING
    assert _parse_log_level("ERROR") == logging.ERROR


def test_parse_log_level_rejects_invalid_value() -> None:
    with pytest.raises(ValueError):
        _parse_log_level("trace")


def test_configure_logging_writes_to_rotating_file_and_stdout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _cleanup_sales_copilot_handlers()
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    log_path = configure_logging(log_dir=tmp_path)

    root_logger = logging.getLogger()
    handlers = [handler for handler in root_logger.handlers if getattr(handler, "_sales_copilot_handler", False)]
    assert root_logger.level == logging.DEBUG
    assert len(handlers) == 2
    assert any(isinstance(handler, logging.StreamHandler) for handler in handlers)

    file_handlers = [handler for handler in handlers if isinstance(handler, RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 10 * 1024 * 1024
    assert file_handlers[0].backupCount == 5

    logging.getLogger("sales_copilot.test").info("structured logging works")
    for handler in handlers:
        handler.flush()

    assert log_path.read_text(encoding="utf-8").strip()
    assert "structured logging works" in log_path.read_text(encoding="utf-8")
    _cleanup_sales_copilot_handlers()


def test_configure_logging_replaces_previous_sales_copilot_handlers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _cleanup_sales_copilot_handlers()
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    configure_logging(log_dir=tmp_path)
    configure_logging(log_dir=tmp_path)

    root_logger = logging.getLogger()
    handlers = [handler for handler in root_logger.handlers if getattr(handler, "_sales_copilot_handler", False)]
    assert len(handlers) == 2
    _cleanup_sales_copilot_handlers()
