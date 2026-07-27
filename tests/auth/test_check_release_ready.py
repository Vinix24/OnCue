"""Pre-ship gate ``scripts/check_release_ready.py`` fails without a prod pubkey."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))

from _dev_anchor import requires_dev_anchor  # noqa: E402
from check_release_ready import main, run_checks  # noqa: E402

from sales_copilot.auth import _embedded_keys  # noqa: E402

_VALID_PROD_HEX = "ab" * 32


def test_run_checks_fails_without_prod_pubkey(monkeypatch) -> None:
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", None)
    assert run_checks() != 0


def test_run_checks_passes_with_prod_pubkey(monkeypatch) -> None:
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", _VALID_PROD_HEX)
    assert run_checks() == 0


@requires_dev_anchor
def test_run_checks_fails_when_prod_pubkey_is_dev_key(monkeypatch) -> None:
    """Embedding the dev/test key into the prod slot is rejected as unshippable."""
    from sales_copilot.auth import _dev_keys

    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", _dev_keys._DEV_PUBKEY_HEX)
    assert run_checks() != 0


def test_main_reports_failure_without_prod_pubkey(monkeypatch, capsys) -> None:
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", None)
    assert main([]) != 0
    captured = capsys.readouterr()
    assert "_PROD_PUBKEY_HEX" in captured.err
    assert "production public key" in captured.err


def test_main_passes_with_prod_pubkey(monkeypatch) -> None:
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", _VALID_PROD_HEX)
    assert main([]) == 0
