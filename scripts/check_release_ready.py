#!/usr/bin/env python3
"""Pre-ship release-readiness gate for the SCP license system.

An operator runs this immediately before packaging a release artifact, after
``scripts/embed_prod_pubkey.py`` has baked the production Ed25519 public key
into ``src/sales_copilot/auth/_embedded_keys.py``.

This gate is deliberately NOT part of ``scripts/ci.sh``. A development checkout
legitimately ships with ``_PROD_PUBKEY_HEX = None`` -- the production key is
embedded only at release-build time -- so running it on every push would fail
every dev CI run. The auth-hardening regression checks that *do* run on every
push live in ``scripts/ci_auth_hardening_check.py``.

The gate asserts the invariants that must hold for a shippable release. The
critical one: a production public key must be embedded. Without it,
``build_pubkey.trusted_pubkeys()`` returns an empty map in the packaged build
(where ``_dev_keys`` is absent), so EVERY real, correctly signed license is
rejected -- silently dropping every paying customer to the free tier.

Exit code 0 => release-ready. Any non-zero exit => do not ship.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Ensure this script can import sales_copilot from the current checkout.
sys.path.insert(0, str(SRC))

from sales_copilot.auth import _embedded_keys, build_pubkey  # noqa: E402
from sales_copilot.auth.audit import pubkey_fingerprint  # noqa: E402

_ED25519_PUBKEY_BYTES = 32


def _prod_pubkey_bytes() -> bytes | None:
    """Return the embedded production pubkey as raw bytes, or None if unusable."""
    hex_value = _embedded_keys._PROD_PUBKEY_HEX
    if not hex_value:
        return None
    try:
        raw = bytes.fromhex(hex_value.strip())
    except ValueError:
        return None
    if len(raw) != _ED25519_PUBKEY_BYTES:
        return None
    return raw


def _check_production_pubkey_embedded() -> tuple[bool, str]:
    """A production Ed25519 public key must be baked into the release build."""
    if build_pubkey.has_production_pubkey():
        return True, "production public key is embedded (_PROD_PUBKEY_HEX)"
    return (
        False,
        "no production public key embedded in "
        "sales_copilot.auth._embedded_keys._PROD_PUBKEY_HEX. In a packaged "
        "build (_dev_keys absent) trusted_pubkeys() would be empty, so every "
        "real license is rejected and all paying users silently drop to free. "
        "Run scripts/embed_prod_pubkey.py with SALES_COPILOT_PROD_PUBKEY_HEX "
        "set before packaging.",
    )


def _check_production_key_is_not_dev_key() -> tuple[bool, str]:
    """The embedded production key must not be the development/test key.

    Embedding the well-known dev pubkey into the production slot would make the
    release trust dev-signed keys and reject every server-signed production
    license. Compared by fingerprint so hex casing/spacing cannot mask a match.
    """
    prod = _prod_pubkey_bytes()
    if prod is None:
        # Absence is reported by _check_production_pubkey_embedded; there is no
        # production key to compare against the dev key here.
        return True, "no production key to compare against the dev key"
    dev_fp = build_pubkey.dev_pubkey_fingerprint()
    if dev_fp is not None and pubkey_fingerprint(prod) == dev_fp:
        return (
            False,
            "embedded production public key equals the development/test key. "
            "A release must trust the server-signed production key, not the "
            "dev key. Re-run scripts/embed_prod_pubkey.py with the real "
            "production SALES_COPILOT_PROD_PUBKEY_HEX.",
        )
    return True, "embedded production key differs from the development key"


_CHECKS = (
    ("production-pubkey-embedded", _check_production_pubkey_embedded),
    ("production-key-not-dev-key", _check_production_key_is_not_dev_key),
)


def run_checks() -> int:
    """Run every release invariant. Return 0 when all pass, 1 otherwise."""
    failures = 0
    for name, check in _CHECKS:
        passed, message = check()
        marker = "PASS" if passed else "FAIL"
        stream = sys.stdout if passed else sys.stderr
        print(f"[{marker}] {name}: {message}", file=stream)
        if not passed:
            failures += 1

    if failures:
        print(
            f"\nRelease NOT ready: {failures} of {len(_CHECKS)} invariant(s) failed. "
            "Do not package this build.",
            file=sys.stderr,
        )
        return 1

    print(f"\nRelease ready: all {len(_CHECKS)} invariant(s) passed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pre-ship release-readiness gate for the SCP license system. Fails "
            "when the build is not safe to package -- most importantly when no "
            "production public key is embedded, which would reject every real "
            "license and drop all paying users to free."
        )
    )
    parser.parse_args(argv)
    return run_checks()


if __name__ == "__main__":
    raise SystemExit(main())
