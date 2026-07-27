"""Shared skip predicate for auth tests that require the private dev signing anchor.

The OSS export ships neither the dev signing seed (``sales_copilot.auth._dev_keys``,
excluded from the public allowlist) nor — before the pre-launch pubkey-swap — an
embedded production pubkey. Tests that need a *dev* trust anchor (the ``_dev_keys``
module itself, a dev-signed license vector, or ``dev_pubkey_fingerprint()``) cannot
verify in that neutral build, so they skip there — the same pattern as the
``audio_fixture`` marker skipping when local fixtures are absent. In the owner's dev
checkout the dev key is present, so they run and pass — no coverage loss.

The predicate keys on the *dev* anchor specifically, NOT on ``trusted_pubkeys()``
being empty: after the pubkey-swap the export will ship a prod pubkey (non-empty
trust set) yet the dev-signed-vector tests still cannot verify against it.
"""

from __future__ import annotations

import pytest


def dev_anchor_present() -> bool:
    """True when this build carries the private dev signing anchor.

    Requires both the ``_dev_keys`` module to be importable (it is excluded from
    the OSS export) and ``dev_pubkey_fingerprint()`` to resolve a key.
    """
    try:
        from sales_copilot.auth import _dev_keys  # noqa: F401
    except ImportError:
        return False
    from sales_copilot.auth.build_pubkey import dev_pubkey_fingerprint

    return dev_pubkey_fingerprint() is not None


requires_dev_anchor = pytest.mark.skipif(
    not dev_anchor_present(),
    reason="requires the private dev signing anchor (_dev_keys); absent in the OSS export",
)
