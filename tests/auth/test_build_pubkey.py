"""Build-time public-key authority: dev key gated by module presence, prod key deferred."""

from __future__ import annotations

import hashlib

from _dev_anchor import requires_dev_anchor

from sales_copilot.auth import _embedded_keys
from sales_copilot.auth import build_pubkey as build_pubkey_module
from sales_copilot.auth.build_pubkey import (
    dev_pubkey_fingerprint,
    has_production_pubkey,
    trusted_pubkeys,
)

# This whole module exercises the build-time dev-key authority; it needs the
# private _dev_keys seed present. That seed is excluded from the OSS export, so
# skip the module cleanly there (a bare `import _dev_keys` would otherwise error
# at collection). In the owner's dev checkout the seed is present -> tests run.
pytestmark = requires_dev_anchor

try:
    from sales_copilot.auth import _dev_keys
except ImportError:  # OSS export: dev seed excluded from the public allowlist
    _dev_keys = None

_DEV_PUBKEY_HEX = _dev_keys._DEV_PUBKEY_HEX if _dev_keys is not None else None


def test_dev_key_trusted_when_dev_module_present() -> None:
    keys = trusted_pubkeys()
    assert 0 in keys
    assert keys[0].hex() == _DEV_PUBKEY_HEX


def test_dev_key_not_trusted_when_dev_module_absent(monkeypatch) -> None:
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", None)
    assert trusted_pubkeys() == {}


def test_env_var_has_no_effect_on_trust(monkeypatch) -> None:
    """SCP_DEV_BUILD must not open the dev key; only _dev_keys presence does."""
    monkeypatch.setenv("SCP_DEV_BUILD", "1")
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", None)
    assert trusted_pubkeys() == {}


def test_release_build_with_prod_key_trusts_only_prod(monkeypatch) -> None:
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    prod_hex = "ab" * 32
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", prod_hex)
    keys = trusted_pubkeys()
    assert 0 in keys
    assert keys[0].hex() == prod_hex
    assert has_production_pubkey() is True


def test_release_build_without_prod_key_has_no_trusted_keys(monkeypatch) -> None:
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", None)
    assert trusted_pubkeys() == {}
    assert has_production_pubkey() is False


def test_invalid_hex_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", "not-hex")
    assert trusted_pubkeys() == {}


def test_wrong_length_pubkey_is_ignored(monkeypatch) -> None:
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    monkeypatch.setattr(_embedded_keys, "_PROD_PUBKEY_HEX", "ab" * 16)
    assert trusted_pubkeys() == {}


def test_dev_pubkey_fingerprint_present_when_dev_module_present() -> None:
    expected = hashlib.sha256(bytes.fromhex(_DEV_PUBKEY_HEX)).hexdigest()
    assert dev_pubkey_fingerprint() == expected


def test_dev_pubkey_fingerprint_none_when_dev_module_absent(monkeypatch) -> None:
    monkeypatch.setattr(build_pubkey_module, "_dev_keys", None)
    assert dev_pubkey_fingerprint() is None


def test_dev_pubkey_fingerprint_none_when_dev_hex_malformed(monkeypatch) -> None:
    monkeypatch.setattr(_dev_keys, "_DEV_PUBKEY_HEX", "not-hex")
    assert dev_pubkey_fingerprint() is None
