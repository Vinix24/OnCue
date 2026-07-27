"""Round-trip tests for the private local signer ``scripts/sign_license.py``.

These prove that a key minted by the offline signer is byte-compatible with what
the client verifier accepts — the same guarantee the Cloudflare Worker gives.

Trust is established with an EPHEMERAL keypair generated in-test and injected as
the verifier's epoch-0 trusted key (the same ``trusted_pubkeys`` monkeypatch the
existing ``test_license_verifier`` suite uses), so these run in the owner's dev
checkout without depending on ``_dev_keys`` or any embedded production pubkey.

``scripts/sign_license.py`` is export-excluded (it is a private signing tool), so
in the OSS export the module is absent and these tests SKIP rather than fail —
the same skip pattern as ``_dev_anchor.requires_dev_anchor``.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from sales_copilot.auth import license_verifier
from sales_copilot.auth.license_verifier import decode_and_verify

_SIGNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sign_license.py"

requires_local_signer = pytest.mark.skipif(
    not _SIGNER_PATH.is_file(),
    reason="requires scripts/sign_license.py (private local signer); absent in the OSS export",
)


def _load_signer() -> ModuleType:
    """Import the private signer straight from its file path (not a package)."""
    spec = importlib.util.spec_from_file_location("sign_license", _SIGNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@requires_local_signer
def test_local_signed_pro_key_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    signer = _load_signer()
    priv_hex, pub_hex = signer.generate_keypair()
    scp_key, license_id_hex, expires_at = signer.sign_license(priv_hex, tier="pro", days=14)

    # Inject THIS ephemeral keypair's public key as the trusted epoch-0 key, the
    # same way the verifier suite establishes trust for a signed vector.
    monkeypatch.setattr(license_verifier, "trusted_pubkeys", lambda: {0: bytes.fromhex(pub_hex)})

    lk = decode_and_verify(scp_key)
    assert lk is not None
    assert lk.tier == "pro"
    assert lk.license_id == license_id_hex

    now = datetime.now(UTC)
    assert lk.expires_at > now  # not expired
    days_left = (lk.expires_at - now).total_seconds() / 86_400
    assert 13.9 < days_left <= 14.0  # ~14 days, minus the sub-second test latency
    assert lk.expires_at == expires_at  # matches the signer's own reported expiry


@requires_local_signer
def test_expired_local_key_degrades_to_free(monkeypatch: pytest.MonkeyPatch) -> None:
    signer = _load_signer()
    priv_hex, pub_hex = signer.generate_keypair()
    scp_key, _, _ = signer.sign_license(priv_hex, tier="pro", days=-1)

    monkeypatch.setattr(license_verifier, "trusted_pubkeys", lambda: {0: bytes.fromhex(pub_hex)})

    # The signature verifies against the injected epoch-0 key, so a None result
    # here is the EXPIRY branch, not a bad signature -> feature_policy resolves
    # this to free tier (the degrade path the client relies on).
    assert decode_and_verify(scp_key) is None
