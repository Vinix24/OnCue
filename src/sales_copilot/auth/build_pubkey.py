"""Build-time public-key authority for SCP license verification.

The set of trusted Ed25519 public keys is fixed at build time:

* The production key is embedded by ``scripts/embed_prod_pubkey.py`` from the
  operator-supplied ``SALES_COPILOT_PROD_PUBKEY_HEX`` environment variable.
* The development/test key is only trusted when the dev-only module
  ``sales_copilot.auth._dev_keys`` is present in the installed package.
* At runtime there is no environment door to add or override keys; the verifier
  reads only the embedded key material in this module and the optional
  ``_dev_keys`` module.

If neither a production key nor a build-time-enabled dev key is present, the
verifier has no trusted keys and every SCP key is rejected -> the application
runs in free tier (never crashes or locks out).
"""

from __future__ import annotations

from sales_copilot.auth import _embedded_keys
from sales_copilot.auth.audit import pubkey_fingerprint as _pubkey_fingerprint

_ED25519_PUBKEY_BYTES = 32

# ``_dev_keys`` is excluded from release artifacts. When present, this is a
# dev/test checkout and the dev public key is trusted in addition to any
# production key(s). When absent (packaged build), only production key(s) are
# trusted.
try:
    from sales_copilot.auth import _dev_keys
except ImportError:
    _dev_keys = None


def _hex_to_bytes(hex_value: str | None) -> bytes | None:
    if not hex_value:
        return None
    try:
        raw = bytes.fromhex(hex_value.strip())
    except ValueError:
        return None
    if len(raw) != _ED25519_PUBKEY_BYTES:
        return None
    return raw


def trusted_pubkeys() -> dict[int, bytes]:
    """Return the rotation-epoch -> public-key mapping trusted by this build.

    Production public key(s) from ``_embedded_keys.py`` are always trusted. If
    the dev-only module ``_dev_keys.py`` is present (it is excluded from release
    artifacts), the dev/test key is also trusted on epoch 0. When no trusted key
    is present the verifier sees an empty key map and every SCP key fails ->
    free tier.
    """
    keys: dict[int, bytes] = {}

    prod = _hex_to_bytes(_embedded_keys._PROD_PUBKEY_HEX)
    if prod is not None:
        keys[0] = prod

    if _dev_keys is not None:
        dev = _hex_to_bytes(_dev_keys._DEV_PUBKEY_HEX)
        if dev is not None:
            keys[0] = dev

    return keys


def has_production_pubkey() -> bool:
    """Return True when a production Ed25519 public key was embedded at build."""
    return _hex_to_bytes(_embedded_keys._PROD_PUBKEY_HEX) is not None


def dev_pubkey_fingerprint() -> str | None:
    """Return the SHA-256 fingerprint of this build's dev/test pubkey, if any.

    ``None`` when ``_dev_keys`` is absent (a packaged/release build) or its hex
    is malformed. Callers use this to recognise that a specific verified
    license was signed with the dev key -- e.g. ``feature_policy`` scopes its
    revocation fail-open exception to dev checkouts this way, without adding a
    new env-controlled flag.
    """
    if _dev_keys is None:
        return None
    dev_bytes = _hex_to_bytes(_dev_keys._DEV_PUBKEY_HEX)
    if dev_bytes is None:
        return None
    return _pubkey_fingerprint(dev_bytes)
