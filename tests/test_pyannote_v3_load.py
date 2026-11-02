"""Load path of the live pyannote diarizer across pyannote.audio 3.x and 4.x."""

from __future__ import annotations

import logging
import sys
import types

import pytest

from sales_copilot.modules.diarizer.pyannote_v3 import PyannoteV3Diarizer

LOGGER = "sales_copilot.modules.diarizer.pyannote_v3"


class _FakePipeline:
    def __init__(self) -> None:
        self.device = None

    def to(self, device: object) -> None:
        self.device = device


def _install(monkeypatch: pytest.MonkeyPatch, pipeline_cls: object | None) -> None:
    """Put a fake torch and pyannote.audio in sys.modules; None for the Pipeline blocks the import."""
    torch = types.ModuleType("torch")
    torch.device = lambda name: f"device:{name}"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "torch", torch)
    if pipeline_cls is None:
        monkeypatch.setitem(sys.modules, "pyannote.audio", None)
        return
    audio = types.ModuleType("pyannote.audio")
    audio.Pipeline = pipeline_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio", audio)


def _diarizer() -> PyannoteV3Diarizer:
    return PyannoteV3Diarizer(hf_token="hf_test", device="cpu")


def test_token_only_signature_uses_token(monkeypatch, caplog):
    calls: list[dict] = []

    class Pipeline:
        @classmethod
        def from_pretrained(cls, checkpoint, token=None, cache_dir=None):
            calls.append({"checkpoint": checkpoint, "token": token})
            return _FakePipeline()

    _install(monkeypatch, Pipeline)
    diarizer = _diarizer()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        diarizer._load_sync()

    assert calls == [{"checkpoint": "pyannote/speaker-diarization-3.1", "token": "hf_test"}]
    assert isinstance(diarizer._pipeline, _FakePipeline)
    assert diarizer._pipeline.device == "device:cpu"
    assert "model loaded" in caplog.text


def test_use_auth_token_only_signature_uses_use_auth_token(monkeypatch):
    calls: list[dict] = []

    class Pipeline:
        @classmethod
        def from_pretrained(cls, checkpoint, use_auth_token=None, cache_dir=None):
            calls.append({"use_auth_token": use_auth_token})
            return _FakePipeline()

    _install(monkeypatch, Pipeline)
    diarizer = _diarizer()
    diarizer._load_sync()

    assert calls == [{"use_auth_token": "hf_test"}]
    assert diarizer._pipeline is not None


def test_none_pipeline_logs_token_message(monkeypatch, caplog):
    class Pipeline:
        @classmethod
        def from_pretrained(cls, checkpoint, token=None):
            return None

    _install(monkeypatch, Pipeline)
    diarizer = _diarizer()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        diarizer._load_sync()

    assert diarizer._pipeline is None
    assert "missing or invalid HF token" in caplog.text
    assert "not installed" not in caplog.text


def test_missing_extra_logs_install_message(monkeypatch, caplog):
    _install(monkeypatch, None)
    diarizer = _diarizer()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        diarizer._load_sync()

    assert diarizer._pipeline is None
    assert "not installed" in caplog.text
    assert "diarization extra" in caplog.text


def test_gated_repo_error_logs_token_message(monkeypatch, caplog):
    class GatedRepoError(Exception):
        pass

    class Pipeline:
        @classmethod
        def from_pretrained(cls, checkpoint, token=None):
            raise GatedRepoError("403 Forbidden")

    _install(monkeypatch, Pipeline)
    diarizer = _diarizer()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        diarizer._load_sync()

    assert diarizer._pipeline is None
    assert "refused the token" in caplog.text
    assert "unexpected error" not in caplog.text


def test_unexpected_error_logs_with_traceback(monkeypatch, caplog):
    class Pipeline:
        @classmethod
        def from_pretrained(cls, checkpoint, token=None):
            raise RuntimeError("disk full")

    _install(monkeypatch, Pipeline)
    diarizer = _diarizer()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        diarizer._load_sync()

    assert diarizer._pipeline is None
    assert "unexpected error" in caplog.text
    assert "refused the token" not in caplog.text
    assert any(r.exc_info and "disk full" in str(r.exc_info[1]) for r in caplog.records)


def test_argument_mismatch_is_not_reported_as_token_problem(monkeypatch, caplog):
    """A TypeError from our own call must surface as an unexpected error, never as a bad token."""

    class Pipeline:
        @classmethod
        def from_pretrained(cls, checkpoint, **kwargs):
            raise TypeError("unexpected keyword argument")

    _install(monkeypatch, Pipeline)
    diarizer = _diarizer()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        diarizer._load_sync()

    assert "unexpected error" in caplog.text
    assert "refused the token" not in caplog.text


def _hub_http_error_pipeline(status_code: int) -> type:
    class HfHubHTTPError(Exception):
        def __init__(self, message: str, status: int) -> None:
            super().__init__(message)
            self.response = types.SimpleNamespace(status_code=status)

    class Pipeline:
        @classmethod
        def from_pretrained(cls, checkpoint, token=None):
            raise HfHubHTTPError(f"{status_code} from hub", status_code)

    return Pipeline


def test_hub_http_error_500_is_unexpected_error_not_token_message(monkeypatch, caplog):
    _install(monkeypatch, _hub_http_error_pipeline(500))
    diarizer = _diarizer()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        diarizer._load_sync()

    assert diarizer._pipeline is None
    assert "unexpected error" in caplog.text
    assert "refused the token" not in caplog.text


def test_hub_http_error_401_is_token_message(monkeypatch, caplog):
    _install(monkeypatch, _hub_http_error_pipeline(401))
    diarizer = _diarizer()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        diarizer._load_sync()

    assert diarizer._pipeline is None
    assert "refused the token" in caplog.text
    assert "unexpected error" not in caplog.text
