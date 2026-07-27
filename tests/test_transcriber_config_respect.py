from __future__ import annotations

import logging

import pytest

from sales_copilot.__main__ import _parse_call_config


def test_transcriber_false_respected_when_detector_disabled() -> None:
    config = _parse_call_config({"enable_transcriber": False, "enable_detector": False})

    assert config.enable_transcriber is False


def test_transcriber_false_respected_when_detector_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="sales_copilot.__main__"):
        config = _parse_call_config(
            {"modules": {"transcript": False, "pain_points": True}}
        )

    assert config.enable_transcriber is False
    assert config.enable_detector is True
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected a warning about disabled transcriber with active detector"
    assert any("transcriber" in r.getMessage().lower() for r in warning_records)


def test_transcriber_default_is_enabled() -> None:
    config = _parse_call_config({})

    assert config.enable_transcriber is True


def test_transcriber_true_with_detector_true_unchanged() -> None:
    config = _parse_call_config({"enable_transcriber": True, "enable_detector": True})

    assert config.enable_transcriber is True
    assert config.enable_detector is True


def test_transcriber_false_via_modules_dict(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="sales_copilot.__main__"):
        config = _parse_call_config(
            {"modules": {"transcript": False, "transcriber": False, "pain_points": True}}
        )

    assert config.enable_transcriber is False
    assert config.enable_detector is True
