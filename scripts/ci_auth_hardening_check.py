#!/usr/bin/env python3
"""CI acceptance check: ensure the dev/test key is not trusted in release builds.

This script simulates a release artifact by running Python in a temporary copy
of ``src/`` with the dev-only module ``sales_copilot.auth._dev_keys`` removed.
It proves three security properties:

1. ``_dev_keys`` cannot be imported from the release artifact.
2. An SCP key signed by a keypair whose public key is not embedded is rejected
   even when ``SCP_DEV_BUILD=1`` is set, showing there is no client signer and
   no runtime environment door back to an untrusted key.
3. A production-signed SCP key is still accepted, proving the verifier is not
   simply rejecting every key.
"""

from __future__ import annotations

import base64
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Ensure this script can import sales_copilot from the current checkout.
sys.path.insert(0, str(SRC))

from sales_copilot.auth.license_format import (  # noqa: E402
    BASE32_GROUP_SIZE,
    SCP_PREFIX,
    SCP_VERSION,
)


def _release_src_tree() -> Path:
    """Return a temp copy of ``src/`` with ``_dev_keys.py`` excluded."""
    tmp = Path(tempfile.mkdtemp(prefix="sales_copilot_release_"))
    release_src = tmp / "src"
    shutil.copytree(
        SRC,
        release_src,
        ignore=shutil.ignore_patterns("_dev_keys.py", "*.pyc", "__pycache__"),
    )
    # Belt-and-suspenders: ensure the excluded file really is gone.
    excluded = release_src / "sales_copilot" / "auth" / "_dev_keys.py"
    if excluded.exists():
        excluded.unlink()
    return release_src


def _run_in_release_env(release_src: Path, code: str, extra_env: dict[str, str] | None = None) -> int:
    """Run ``code`` in a subprocess using the simulated release source tree."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(release_src)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stdout, end="")
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


def _check_dev_module_absent(release_src: Path) -> int:
    code = """
import sys
try:
    import sales_copilot.auth._dev_keys
except ImportError:
    print("PASS: _dev_keys is absent from release artifact")
    sys.exit(0)
print("FAIL: _dev_keys is importable in release artifact", file=sys.stderr)
sys.exit(1)
"""
    return _run_in_release_env(release_src, code, extra_env={"SCP_DEV_BUILD": "1"})


def _check_untrusted_key_rejected(release_src: Path, untrusted_key: str) -> int:
    code = """
import os
import sys
from sales_copilot.auth.license_verifier import decode_and_verify
if decode_and_verify(os.environ['UNTRUSTED_KEY']) is None:
    print("PASS: untrusted-pubkey key rejected even with SCP_DEV_BUILD=1")
    sys.exit(0)
print("FAIL: untrusted-pubkey key accepted via runtime env", file=sys.stderr)
sys.exit(1)
"""
    return _run_in_release_env(
        release_src, code, extra_env={"SCP_DEV_BUILD": "1", "UNTRUSTED_KEY": untrusted_key}
    )


def _check_prod_key_accepted(release_src: Path, prod_key: str, prod_pubkey_hex: str) -> int:
    code = """
import os
import sys
from sales_copilot.auth import _embedded_keys
_embedded_keys._PROD_PUBKEY_HEX = os.environ['PROD_PUBKEY_HEX']
from sales_copilot.auth.license_verifier import decode_and_verify
lk = decode_and_verify(os.environ['PROD_KEY'])
if lk is not None and lk.tier == 'pro':
    print("PASS: production-signed key verifies")
    sys.exit(0)
print("FAIL: production-signed key rejected", file=sys.stderr)
sys.exit(1)
"""
    return _run_in_release_env(
        release_src,
        code,
        extra_env={"PROD_KEY": prod_key, "PROD_PUBKEY_HEX": prod_pubkey_hex},
    )


def _build_prod_scp_key(priv: Ed25519PrivateKey) -> str:
    issued = int(datetime.now(UTC).timestamp())
    expires = issued + 30 * 86_400
    lid = os.urandom(16)
    payload = bytes([SCP_VERSION, 1, 0]) + struct.pack(">II", issued, expires) + lid
    signature = priv.sign(payload)
    body = base64.b32encode(payload + signature).decode().rstrip("=")
    groups = [
        body[i : i + BASE32_GROUP_SIZE] for i in range(0, len(body), BASE32_GROUP_SIZE)
    ]
    return f"{SCP_PREFIX}-" + "-".join(groups)


def main() -> int:
    release_src = _release_src_tree()
    try:
        # A key signed by a keypair whose public key is never embedded must be
        # rejected: the client has no signer and there is no runtime env door.
        untrusted_key = _build_prod_scp_key(Ed25519PrivateKey.generate())

        prod_priv = Ed25519PrivateKey.generate()
        prod_pubkey = prod_priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        prod_key = _build_prod_scp_key(prod_priv)

        checks = [
            _check_dev_module_absent(release_src),
            _check_untrusted_key_rejected(release_src, untrusted_key),
            _check_prod_key_accepted(release_src, prod_key, prod_pubkey.hex()),
        ]
        if any(c != 0 for c in checks):
            return 1
        print("All release-build auth hardening checks passed.")
        return 0
    finally:
        shutil.rmtree(release_src.parent, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
