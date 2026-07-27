"""First-run front-door decision: wizard vs. dashboard.

A fresh install has no LLM provider key and no license key in ``.env``. Sending
a non-technical pilot user straight to ``/dashboard`` leaves them stuck. This
module answers one question — is the app configured yet? — so the launcher and
``main()`` can open the first-run wizard instead of the dashboard until it is.

One rule: the app is *configured* only when the configured provider has its API
key AND a license key is present. If either is missing the front door is the
wizard. Providers that need no API key (``ollama``/``vertex``) satisfy the
provider half automatically.
"""

from __future__ import annotations

from sales_copilot.core.env_writer import read_env_value
from sales_copilot.wizard.detectors import provider_key_env

WIZARD_PATH = "/dashboard/wizard/"
DASHBOARD_PATH = "/dashboard"

_LICENSE_ENV = "SALES_COPILOT_LICENSE"
_PROVIDER_ENV = "LLM_PROVIDER"


def _configured_provider() -> str | None:
    value = read_env_value(_PROVIDER_ENV)
    return value.strip().lower() if value and value.strip() else None


def provider_key_present() -> bool:
    """True when the configured provider has (or needs no) API key."""

    provider = _configured_provider()
    if provider is None:
        return False
    key_env = provider_key_env(provider)
    if key_env is None:
        # ollama / vertex authenticate without an API key in .env.
        return True
    value = read_env_value(key_env)
    return bool(value and value.strip())


def license_present() -> bool:
    """True when a non-empty license key is present in env/.env."""

    value = read_env_value(_LICENSE_ENV)
    return bool(value and value.strip())


def is_configured() -> bool:
    """True only when both the provider key and the license key are present."""

    return provider_key_present() and license_present()


def first_run_path() -> str:
    """Return the path to open on launch: the wizard until configured."""

    return DASHBOARD_PATH if is_configured() else WIZARD_PATH
