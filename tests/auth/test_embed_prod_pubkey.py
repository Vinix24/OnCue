"""Build script ``scripts/embed_prod_pubkey.py`` generates the embedded key module."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))

from embed_prod_pubkey import main as embed_main  # noqa: E402


@pytest.fixture()
def generated_module(tmp_path: Path) -> Path:
    out = tmp_path / "_embedded_keys.py"
    assert embed_main(["--prod-hex", "ab" * 32, "--out", str(out)]) == 0
    return out


def _load_module(path: Path) -> ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("_embedded_keys", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prod_key_embedded(generated_module: Path) -> None:
    mod = _load_module(generated_module)
    assert mod._PROD_PUBKEY_HEX == "ab" * 32


def test_no_prod_key_is_rejected(tmp_path: Path, capsys) -> None:
    out = tmp_path / "_embedded_keys.py"
    assert embed_main(["--out", str(out)]) == 1


def test_invalid_hex_is_rejected(tmp_path: Path, capsys) -> None:
    out = tmp_path / "_embedded_keys.py"
    assert embed_main(["--prod-hex", "not-hex", "--out", str(out)]) == 1
