"""Generate cryptographically secure secrets for OnCue.

Run this once after cloning or reinstalling:

    python scripts/generate_secrets.py

Copy the printed values into your .env file.
"""

import secrets


def _gen(label: str, comment: str) -> None:
    value = secrets.token_urlsafe(32)
    print(f"# {comment}")
    print(f"{label}={value}")
    print()


def main() -> None:
    print("# ── OnCue secrets ──────────────────────────────────")
    print("# Add these to your .env file (never commit .env to git).")
    print()
    # SALES_COPILOT_LICENSE is no longer self-minted: Pro keys are Ed25519-signed
    # and issued by the license server. Legacy SC- HMAC keys (which used
    # SALES_COPILOT_LICENSE_SECRET) are accepted only until the migration deadline
    # and are intentionally NOT generated here — that path enabled self-minting.
    _gen("AUDIT_HMAC_SECRET", "HMAC-SHA256 secret for signing audit-ledger records")
    _gen("SHUTDOWN_TOKEN", "Token required by dashboard for mutating API calls")
    print("# ────────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
