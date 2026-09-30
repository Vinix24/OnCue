import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sales_copilot.__main__ as app_main
from sales_copilot.__main__ import (
    _build_provider_client,
    _disabled_modules,
    _enabled_modules,
    _parse_call_config,
    _start_or_signal_recording,
)
from sales_copilot.core.config import build_module_configs


def test_parse_call_config_defaults() -> None:
    config = _parse_call_config({})

    assert config.screens == 2
    assert config.enable_talk_time is True
    assert config.context_docs == []


def test_parse_call_config_ignores_arbitrary_context_doc_paths() -> None:
    config = _parse_call_config({"context_docs": ["a.md"], "screens": 1})

    assert config.context_docs == []
    assert config.screens == 1


def test_parse_call_config_resolves_uploaded_context_doc_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    upload_root = tmp_path / "clients"
    upload_root.mkdir()
    document = upload_root / "safe-id.md"
    document.write_text("safe context", encoding="utf-8")
    monkeypatch.setattr("sales_copilot.core.context_docs.UPLOAD_ROOT", upload_root)

    config = _parse_call_config({"context_doc_ids": ["safe-id.md"]})

    assert config.context_docs == [str(document)]


def test_parse_call_config_transcript_backend() -> None:
    config = _parse_call_config({"transcript": {"backend": "whisper.cpp"}})

    assert config.transcript_backend == "whisper.cpp"


def test_enabled_modules_respects_toggles() -> None:
    config = _parse_call_config(
        {
            "enable_talk_time": True,
            "enable_transcriber": False,
            "enable_detector": False,
            "enable_reports": False,
        }
    )

    assert _enabled_modules(config) == ["talk_time"]


def test_parse_call_config_enables_transcriber_for_detector_dependency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="sales_copilot.__main__"):
        config = _parse_call_config(
            {
                "modules": {
                    "transcript": False,
                    "pain_points": True,
                }
            }
        )

    assert config.enable_detector is True
    assert config.enable_transcriber is False
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected a warning about disabled transcriber with active detector"
    assert any("transcriber" in r.getMessage().lower() for r in warning_records)


# --- disabled-module visibility (D-4d04b337) ---------------------------------
#
# Regression coverage for the 2026-09-05 incident: a start_call payload with
# modules.pain_points=false ran four whole calls with pain-point detection off
# and left zero trace in the log. "Enabled modules" only shows what's on; a
# name silently missing from it is easy to miss.


def test_disabled_modules_empty_when_everything_enabled() -> None:
    payload: dict = {}
    config = _parse_call_config(payload)

    assert _disabled_modules(payload, config) == []


def test_disabled_modules_names_explicit_modules_dict_flag() -> None:
    payload = {"modules": {"pain_points": False}}
    config = _parse_call_config(payload)

    disabled = _disabled_modules(payload, config)

    assert len(disabled) == 1
    assert disabled[0].startswith("detector (")
    assert "modules.pain_points" in disabled[0]
    assert "explicit in start_call payload" in disabled[0]


def test_disabled_modules_names_legacy_top_level_flag() -> None:
    payload = {"enable_reports": False}
    config = _parse_call_config(payload)

    disabled = _disabled_modules(payload, config)

    assert len(disabled) == 1
    assert disabled[0].startswith("reports (")
    assert "payload.enable_reports" in disabled[0]
    assert "explicit in start_call payload" in disabled[0]


def test_disabled_modules_lists_every_disabled_known_module() -> None:
    payload = {
        "modules": {"talk_time": False, "pain_points": False},
        "enable_reports": False,
    }
    config = _parse_call_config(payload)

    disabled = _disabled_modules(payload, config)
    names = {entry.split(" ", 1)[0] for entry in disabled}

    assert names == {"talk_time", "detector", "reports"}


def test_parse_call_config_warns_when_detector_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reproduces the actual 2026-09-05 incident: a start_call payload with
    modules.pain_points=false must now produce a warning that names the
    detector, instead of a call running silently with detection off."""
    with caplog.at_level(logging.WARNING, logger="sales_copilot.__main__"):
        config = _parse_call_config({"modules": {"pain_points": False}})

    assert config.enable_detector is False
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "Expected a warning when the detector is disabled"
    assert any("detector" in r.getMessage().lower() for r in warning_records)


def test_parse_call_config_no_detector_warning_when_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="sales_copilot.__main__"):
        config = _parse_call_config({})

    assert config.enable_detector is True
    detector_disabled_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "detector is disabled" in r.getMessage().lower()
    ]
    assert not detector_disabled_warnings


@pytest.mark.asyncio
async def test_run_call_handles_module_task_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_module(**_kwargs) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(app_main, "talk_time_main", failing_module)

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

    run_task = asyncio.create_task(app_main._run_call(stop_event, call_config, configs))
    await asyncio.sleep(0.1)
    stop_event.set()
    await run_task


@pytest.mark.asyncio
async def test_eager_warmup_at_startup_invokes_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator-level eager warmup must call backend.warmup() at
    backend startup so the model is loaded before the operator clicks
    Start Call."""

    warmup_calls: list[str] = []
    stop_calls: list[str] = []

    class _FakeBackend:
        async def warmup(self) -> None:
            warmup_calls.append("warmup")

        async def stop(self) -> None:
            stop_calls.append("stop")

    def _create_backend(*_args, **_kwargs) -> _FakeBackend:
        return _FakeBackend()

    monkeypatch.setattr(app_main, "create_backend", _create_backend)

    await app_main._eager_warmup_at_startup()

    assert warmup_calls == ["warmup"]
    assert stop_calls == ["stop"]


@pytest.mark.asyncio
async def test_eager_warmup_swallows_backend_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing eager warmup must not crash the orchestrator — first
    call simply pays the model-load cost."""

    class _BrokenBackend:
        async def warmup(self) -> None:
            raise RuntimeError("model load exploded")

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(app_main, "create_backend", lambda *a, **k: _BrokenBackend())

    # Must not raise.
    await app_main._eager_warmup_at_startup()


def test_build_provider_client_azure_constructs_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_provider_client('azure') must instantiate AzureOpenAI without raising.

    A misconfigured Azure tenant (bad endpoint/key) surfaces as a module_warning
    via _health_check_providers instead of a silent crash — this test confirms
    the happy-path (client construction) works end-to-end with patched SDK.
    """
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-key-456")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://corp.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    monkeypatch.setenv("LLM_TIMEOUT_MS", "7000")

    sentinel = MagicMock(name="AzureOpenAI_instance")

    with patch("openai.AzureOpenAI", return_value=sentinel) as mock_cls:
        _build_provider_client("azure")

    mock_cls.assert_called_once_with(
        api_key="az-key-456",
        azure_endpoint="https://corp.openai.azure.com",
        api_version="2024-10-21",
        timeout=7.0,
    )


def test_start_or_signal_recording_returns_none_and_logs_when_disabled(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """RECORD_AUDIO=false must produce an explicit signal, not silence.

    Reproduces the "operator who never sets it gets no signal that anything
    was off" complaint (2026-09-06 field report): before this fix, the
    disabled path logged nothing at all.
    """
    caplog.set_level(logging.INFO)

    result = _start_or_signal_recording(
        False, "session-1", tmp_path, sample_rate=16000, flush_seconds=5.0
    )

    assert result is None
    assert "RECORD_AUDIO=false" in caplog.text


def test_start_or_signal_recording_starts_and_logs_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    started: dict[str, object] = {}

    def _fake_start(session_id, output_dir, *, sample_rate, flush_seconds):  # noqa: ANN001
        started.update(
            session_id=session_id, output_dir=output_dir, sample_rate=sample_rate, flush_seconds=flush_seconds
        )

    monkeypatch.setattr(app_main, "start_session_recording", _fake_start)

    result = _start_or_signal_recording(
        True, "session-2", tmp_path, sample_rate=16000, flush_seconds=5.0
    )

    assert result == "session-2"
    assert started == {
        "session_id": "session-2",
        "output_dir": tmp_path,
        "sample_rate": 16000,
        "flush_seconds": 5.0,
    }
    assert "RECORD_AUDIO=true" in caplog.text


def test_start_or_signal_recording_swallows_start_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def _fake_start(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise OSError("disk full")

    monkeypatch.setattr(app_main, "start_session_recording", _fake_start)
    caplog.set_level(logging.WARNING)

    result = _start_or_signal_recording(
        True, "session-3", tmp_path, sample_rate=16000, flush_seconds=5.0
    )

    assert result is None
    assert "AudioRecorder start failed" in caplog.text
