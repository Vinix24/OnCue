"""Shared logging configuration for runtime entry points."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sales_copilot.core.config import env
from sales_copilot.core.paths import resolve_app_path

LOG_FILE_NAME = "copilot.log"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
MAX_LOG_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5
_HANDLER_MARKER = "_sales_copilot_handler"


def _parse_log_level(value: str | None) -> int:
    normalized = (value or "INFO").strip().upper()
    levels = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    if normalized not in levels:
        raise ValueError("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR")
    return levels[normalized]


def configure_logging(*, log_dir: Path | None = None) -> Path:
    target_dir = log_dir or Path(env("LOG_DIR") or resolve_app_path("data/logs"))
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / LOG_FILE_NAME

    root_logger = logging.getLogger()
    root_logger.setLevel(_parse_log_level(env("LOG_LEVEL", "INFO")))

    for handler in list(root_logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root_logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    setattr(stream_handler, _HANDLER_MARKER, True)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARKER, True)

    root_logger.addHandler(stream_handler)
    root_logger.addHandler(file_handler)
    return log_path
