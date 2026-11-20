"""The embedding model loads without any request to Hugging Face when it is on disk."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector import router as router_module
from sales_copilot.modules.detector.model_cache import embedding_model_cached
from sales_copilot.modules.detector.router import build_encoder

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL = "paraphrase-multilingual-MiniLM-L12-v2"


def _snapshot(
    cache: Path,
    name: str = MODEL,
    *,
    with_config: bool = True,
    files: tuple[str, ...] = ("model.safetensors", "tokenizer.json"),
) -> None:
    snap = cache / f"models--sentence-transformers--{name}" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    if with_config:
        (snap / "config.json").write_text("{}")
    for filename in files:
        (snap / filename).write_text("x")


@pytest.fixture
def hf_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path))
    return tmp_path


def test_empty_cache_is_not_cached(hf_cache: Path) -> None:
    assert embedding_model_cached(MODEL) is False


def test_snapshot_without_config_is_not_cached(hf_cache: Path) -> None:
    _snapshot(hf_cache, with_config=False)
    assert embedding_model_cached(MODEL) is False


def test_snapshot_without_weights_is_not_cached(hf_cache: Path) -> None:
    _snapshot(hf_cache, files=("tokenizer.json",))
    assert embedding_model_cached(MODEL) is False


def test_snapshot_without_tokenizer_is_not_cached(hf_cache: Path) -> None:
    _snapshot(hf_cache, files=("model.safetensors", "tokenizer_config.json"))
    assert embedding_model_cached(MODEL) is False


def test_slow_tokenizer_with_vocab_and_pytorch_weights_is_cached(hf_cache: Path) -> None:
    _snapshot(hf_cache, files=("pytorch_model.bin", "tokenizer_config.json", "vocab.txt"))
    assert embedding_model_cached(MODEL) is True


def test_other_model_snapshot_is_not_cached(hf_cache: Path) -> None:
    _snapshot(hf_cache, "some-other-model")
    assert embedding_model_cached(MODEL) is False


def test_complete_snapshot_is_cached(hf_cache: Path) -> None:
    _snapshot(hf_cache)
    assert embedding_model_cached(MODEL) is True
    assert embedding_model_cached(f"sentence-transformers/{MODEL}") is True


def test_without_huggingface_hub_the_helper_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)
    monkeypatch.setitem(sys.modules, "huggingface_hub.constants", None)
    assert embedding_model_cached(MODEL) is False


def _capture_encoder(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(router_module, "HuggingFaceEncoder", lambda **kwargs: calls.append(kwargs) or object())
    return calls


def test_uncached_model_builds_encoder_online_and_logs(
    hf_cache: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _capture_encoder(monkeypatch)
    with caplog.at_level("INFO", logger=router_module.logger.name):
        build_encoder(MODEL)
    assert calls == [{"name": f"sentence-transformers/{MODEL}"}]
    assert sum("downloading" in r.getMessage() for r in caplog.records) == 1


def test_cached_model_builds_encoder_with_local_files_only(hf_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _snapshot(hf_cache)
    calls = _capture_encoder(monkeypatch)
    build_encoder(MODEL)
    assert calls == [
        {
            "name": f"sentence-transformers/{MODEL}",
            "tokenizer_kwargs": {"local_files_only": True},
            "model_kwargs": {"local_files_only": True},
        }
    ]


def test_incomplete_cached_snapshot_falls_back_to_online_build(
    hf_cache: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _snapshot(hf_cache)
    calls: list[dict] = []

    def _encoder(**kwargs):
        calls.append(kwargs)
        if "model_kwargs" in kwargs:
            raise OSError("snapshot incomplete")
        return object()

    monkeypatch.setattr(router_module, "HuggingFaceEncoder", _encoder)
    with caplog.at_level("WARNING", logger=router_module.logger.name):
        build_encoder(MODEL)
    assert [("model_kwargs" in c) for c in calls] == [True, False]
    assert sum("incomplete" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records) == 1


def test_non_oserror_from_cached_build_is_not_swallowed(hf_cache: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _snapshot(hf_cache)

    def _encoder(**kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(router_module, "HuggingFaceEncoder", _encoder)
    with pytest.raises(ValueError):
        build_encoder(MODEL)


def test_conftest_imports_without_semantic_router() -> None:
    code = (
        "import sys; sys.modules['semantic_router'] = None; "
        "sys.modules['semantic_router.encoders'] = None; import tests.conftest"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr


def test_cached_router_makes_no_outbound_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real model from the real cache; every network connection fails the test."""
    if not embedding_model_cached(DetectorConfig.embedding_model):
        pytest.skip("embedding model not in the local Hugging Face cache")
    from huggingface_hub import constants

    # Undo the session-wide offline switch: only local_files_only may keep us off the network.
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] in {
            "huggingface_hub",
            "transformers",
            "sentence_transformers",
        } and "HF_HUB_OFFLINE" in vars(module):
            monkeypatch.setattr(module, "HF_HUB_OFFLINE", False)
    assert constants.HF_HUB_OFFLINE is False

    attempts: list[object] = []
    loopback = {"localhost", "127.0.0.1", "::1", ""}
    real_connect, real_connect_ex, real_getaddrinfo = (
        socket.socket.connect,
        socket.socket.connect_ex,
        socket.getaddrinfo,
    )

    def _is_loopback(address: object) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        return not isinstance(address, tuple | str) or host in loopback or str(host).startswith("/")

    def _guard(real):
        def _guarded(first, *args, **kwargs):
            # socket methods get (self, address); getaddrinfo gets (host, port, ...)
            target = args[0] if isinstance(first, socket.socket) else first
            if not _is_loopback(target):
                attempts.append(target)
                raise AssertionError(f"outbound connection attempted: {target}")
            return real(first, *args, **kwargs)

        return _guarded

    monkeypatch.setattr(socket.socket, "connect", _guard(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guard(real_connect_ex))
    monkeypatch.setattr(socket, "getaddrinfo", _guard(real_getaddrinfo))

    router = router_module.PainPointRouter(DetectorConfig.from_env())
    router.classify("we zitten uren aan offertes")

    assert attempts == []
