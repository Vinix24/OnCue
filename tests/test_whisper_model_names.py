"""Whisper model aliases resolve for both backends; every call site uses the table."""

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path

import pytest

from sales_copilot.core.config import TranscriberConfig
from sales_copilot.modules.transcriber.backends import model_names
from sales_copilot.modules.transcriber.backends.model_names import (
    MODEL_MAP,
    download_hint,
    resolve_ggml_filename,
    resolve_mlx_repo,
)
from sales_copilot.modules.transcriber.backends.whisper_cpp_backend import WhisperCppBackend

EXPECTED = {
    "tiny": ("mlx-community/whisper-tiny-mlx", "ggml-tiny.bin"),
    "base": ("mlx-community/whisper-base-mlx", "ggml-base.bin"),
    "medium": ("mlx-community/whisper-medium-mlx", "ggml-medium.bin"),
    "large-v3-turbo": ("mlx-community/whisper-large-v3-turbo", "ggml-large-v3-turbo.bin"),
    "large-v3-turbo-q8_0": ("mlx-community/whisper-large-v3-turbo", "ggml-large-v3-turbo-q8_0.bin"),
    "large-v3": ("mlx-community/whisper-large-v3-mlx", "ggml-large-v3.bin"),
}


def test_table_covers_exactly_the_documented_aliases():
    assert set(MODEL_MAP) == set(EXPECTED)


@pytest.mark.parametrize("alias", sorted(EXPECTED))
def test_alias_resolves_on_both_backends(alias):
    repo, ggml = EXPECTED[alias]
    assert resolve_mlx_repo(alias) == repo
    assert resolve_ggml_filename(alias) == ggml


@pytest.mark.parametrize("repo", ["mlx-community/whisper-large-v3-mlx", "someone/custom-whisper"])
def test_full_repo_id_passes_through_unchanged(repo):
    assert resolve_mlx_repo(repo) == repo


@pytest.mark.parametrize("bad", ["large-v4", "", "  ", "Large_V3"])
def test_unknown_name_lists_valid_aliases(bad):
    with pytest.raises(ValueError) as err:
        resolve_mlx_repo(bad)
    for alias in EXPECTED:
        assert alias in str(err.value)
    with pytest.raises(ValueError, match="Valid aliases"):
        resolve_ggml_filename(bad)


def test_repo_id_is_refused_for_whisper_cpp():
    with pytest.raises(ValueError, match="WHISPER_CPP_MODEL_PATH"):
        resolve_ggml_filename("owner/name")


def test_download_hint_names_command_only_for_aliases():
    assert download_hint("large-v3") == (
        "bash vendor/whisper.cpp/models/download-ggml-model.sh large-v3"
    )
    assert download_hint("nope") == ""


def test_config_derives_ggml_path_from_model(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "large-v3")
    monkeypatch.delenv("WHISPER_CPP_MODEL_PATH", raising=False)
    cfg = TranscriberConfig.from_env()
    assert Path(cfg.whisper_cpp_model_path).name == "ggml-large-v3.bin"
    assert Path(cfg.whisper_cpp_model_path).parent.name == "models"


def test_config_explicit_model_path_wins(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "large-v3")
    monkeypatch.setenv("WHISPER_CPP_MODEL_PATH", "/x/custom.bin")
    assert TranscriberConfig.from_env().whisper_cpp_model_path == "/x/custom.bin"


def test_config_unknown_model_without_path_fails_fast(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "large-v4")
    monkeypatch.delenv("WHISPER_CPP_MODEL_PATH", raising=False)
    with pytest.raises(ValueError, match="Valid aliases"):
        TranscriberConfig.from_env()


def test_missing_ggml_file_error_names_download_command(tmp_path):
    binary = tmp_path / "whisper-cli"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    cfg = TranscriberConfig(
        model="large-v3",
        whisper_cpp_binary=str(binary),
        whisper_cpp_model_path=str(tmp_path / "ggml-large-v3.bin"),
    )
    with pytest.raises(FileNotFoundError) as err:
        WhisperCppBackend(cfg)
    assert "bash vendor/whisper.cpp/models/download-ggml-model.sh large-v3" in str(err.value)


# --- call sites -----------------------------------------------------------


@pytest.fixture
def captured(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "large-v3")
    monkeypatch.setenv("WHISPER_BACKEND", "mlx-whisper")
    monkeypatch.setenv("WHISPER_CPP_MODEL_PATH", "/x/m.bin")
    monkeypatch.setattr("sales_copilot.core.config.load_env", lambda *a, **k: None)
    calls: list[dict] = []

    class _Backend:
        async def warmup(self):
            return None

        async def stop(self):
            return None

        async def transcribe_file_segments(self, path):
            return [{"start": 0.0, "end": 1.0, "text": "x"}]

        async def transcribe_chunk(self, *a, **k):
            return None

    def fake_create(cfg):
        calls.append(cfg)
        return _Backend()

    monkeypatch.setattr(
        "sales_copilot.modules.transcriber.backends.create_backend", fake_create
    )
    return calls


EXPECTED_REPO = "mlx-community/whisper-large-v3-mlx"


def test_main_warmup_uses_table(captured, monkeypatch):
    import sales_copilot.__main__ as entry

    monkeypatch.setattr(entry, "create_backend", lambda cfg: captured.append(cfg) or _stub())
    asyncio.run(entry._eager_warmup_at_startup())
    assert captured[0]["model_repo"] == EXPECTED_REPO


def _stub():
    class _B:
        async def warmup(self):
            return None

        async def stop(self):
            return None

    return _B()


def test_record_server_uses_table(captured):
    mod = importlib.import_module("scripts.record_server")
    mod._build_backend()
    assert captured[0]["model_repo"] == EXPECTED_REPO


def test_post_process_call_uses_table(captured, tmp_path):
    mod = importlib.import_module("scripts.post_process_call")
    mod._transcribe_to_segments(tmp_path / "a.wav")
    assert captured[0]["model_repo"] == EXPECTED_REPO


def test_eval_diarization_uses_table(captured):
    mod = importlib.import_module("scripts.eval_diarization")
    import numpy as np

    asyncio.run(mod._transcribe_regions(np.zeros(16000, dtype=np.float32), []))
    assert captured[0]["model_repo"] == EXPECTED_REPO


def test_install_script_uses_table():
    mod = importlib.import_module("scripts.install_whisper_model")
    assert mod.MODEL_MAP is model_names.MODEL_MAP


def test_no_call_site_builds_the_repo_id_by_prefix():
    root = Path(__file__).resolve().parents[1]
    table = root / "src/sales_copilot/modules/transcriber/backends/model_names.py"
    pattern = re.compile(r"""mlx-community/whisper-\{""")
    offenders = []
    for base in ("src", "scripts"):
        for path in (root / base).rglob("*.py"):
            if path == table:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


# --- review fixes -----------------------------------------------------------


def _clear_model_env(monkeypatch):
    for name in ("WHISPER_MODEL", "WHISPER_BACKEND", "WHISPER_CPP_MODEL_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_mlx_backend_with_repo_id_loads_without_ggml_path(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("WHISPER_BACKEND", "mlx-whisper")
    monkeypatch.setenv("WHISPER_MODEL", "owner/custom-whisper")
    cfg = TranscriberConfig.from_env()
    assert cfg.model == "owner/custom-whisper"
    assert cfg.backend == "mlx-whisper"


def test_whisper_cpp_unknown_alias_still_lists_valid_aliases(monkeypatch):
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("WHISPER_BACKEND", "whisper_cpp")
    monkeypatch.setenv("WHISPER_MODEL", "owner/custom-whisper")
    with pytest.raises(ValueError, match="Valid aliases|alias"):
        TranscriberConfig.from_env()


def test_frozen_app_keeps_downloaded_model_path(monkeypatch):
    from sales_copilot.core import config as config_module

    _clear_model_env(monkeypatch)
    monkeypatch.setattr(config_module, "is_frozen_app", lambda: True)
    monkeypatch.setattr(config_module, "model_path", lambda: Path("/app-support/models/m.bin"))
    monkeypatch.setenv("WHISPER_MODEL", "owner/custom-whisper")
    assert TranscriberConfig.from_env().whisper_cpp_model_path == "/app-support/models/m.bin"


def test_explicit_missing_path_message_names_that_path_and_script_target(tmp_path):
    binary = tmp_path / "whisper-cli"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    custom = tmp_path / "custom" / "my-model.bin"
    cfg = TranscriberConfig(
        model="large-v3",
        whisper_cpp_binary=str(binary),
        whisper_cpp_model_path=str(custom),
    )
    with pytest.raises(FileNotFoundError) as err:
        WhisperCppBackend(cfg)
    text = str(err.value)
    assert f"not found: {custom}" in text
    assert "missing at that path" in text
    assert "download-ggml-model.sh large-v3" in text
    assert "vendor/whisper.cpp/models/ggml-large-v3.bin" in text


def test_detect_model_finds_mlx_model_in_hf_cache(monkeypatch, tmp_path):
    from huggingface_hub import constants

    from sales_copilot.wizard import detectors

    repo_id = model_names.resolve_mlx_repo("large-v3")
    cache = tmp_path / "hub"
    snap = cache / ("models--" + repo_id.replace("/", "--"))
    (snap / "refs").mkdir(parents=True)
    (snap / "snapshots" / "abc123").mkdir(parents=True)
    (snap / "refs" / "main").write_text("abc123")
    (snap / "snapshots" / "abc123" / "config.json").write_text("{}")
    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(cache))
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("WHISPER_BACKEND", "mlx-whisper")
    monkeypatch.setenv("WHISPER_MODEL", "large-v3")

    assert detectors._mlx_model_present("large-v3") is True
    assert detectors.detect_model().ok is True


def test_mlx_model_absent_from_cache_and_unknown_alias(monkeypatch, tmp_path):
    from huggingface_hub import constants

    from sales_copilot.wizard import detectors

    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "empty"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty"))
    assert detectors._mlx_model_present("large-v3") is False
    assert detectors._mlx_model_present("no-such-model") is False


def test_q8_0_name_written_by_the_windows_installer_resolves():
    """install.ps1 writes WHISPER_MODEL=large-v3-turbo-q8_0; mlx maps it to the plain turbo repo."""
    installer = (Path(__file__).resolve().parent.parent / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert "[string]$Model = 'large-v3-turbo-q8_0'" in installer
    assert resolve_mlx_repo("large-v3-turbo-q8_0") == resolve_mlx_repo("large-v3-turbo")
    assert resolve_ggml_filename("large-v3-turbo-q8_0") == "ggml-large-v3-turbo-q8_0.bin"
