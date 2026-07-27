import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

import sales_copilot.__main__ as app_main
from sales_copilot.__main__ import _build_provider_client, _enabled_modules, _parse_call_config
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
