"""Central policy for text leaving the local process.

PII redaction is a three-mode policy, read from the ``PII_REDACTION`` env var. The default is
a safe middle ground: redact before public cloud, pass raw only when the destination keeps the
data inside the operator's boundary.

Modes (``resolve_pii_mode()``):
  - ``cloud_only`` (DEFAULT): redact before public cloud providers (gemini/groq/openai); pass
    raw when the destination is local (Ollama on a loopback address, via ``outbound_is_local()``)
    or a trusted BYO-tenant (azure/vertex with ``TRUST_OWN_TENANT``, via
    ``outbound_is_trusted_tenant()``). Raw pass-through still requires the caller to opt in with
    ``allow_local=True``.
  - ``always``: redact before EVERY provider (compliance mode), ignoring local/trusted-tenant.
  - ``off``: never redact (explicit operator opt-out).

Backward compatibility: if ``ALLOW_RAW_LLM_PII`` is truthy it forces mode ``off`` regardless of
``PII_REDACTION`` (it was the original global raw-override). An unrecognised ``PII_REDACTION``
value falls back to the safe default ``cloud_only``.

Never silent: every time redaction is SKIPPED — mode ``off``, or a raw pass-through via local
loopback / trusted tenant — a ``logger.warning`` is emitted once per process (guarded by
``_warned_reasons``) naming why it was skipped. A SKIP is a security-relevant event and must be
auditable in the logs.

``outbound_is_local()`` is NOT a global "disable redaction" switch. It returns True only when:
  - LLM_PROVIDER == "ollama"  AND
  - OLLAMA_BASE_URL host is a loopback address (localhost, 127.0.0.1, ::1).

``outbound_is_trusted_tenant()`` is NOT a global "disable redaction" switch. It returns True
only when:
  - TRUST_OWN_TENANT is truthy (1/true/yes/on)  AND
  - LLM_PROVIDER is in {"azure", "vertex"}.
  Public providers (gemini/groq/openai) and ollama always return False here.

Every public cloud provider (gemini, groq, openai) returns False for both functions.

Two entry points:
  - ``apply_outbound_pii(text, provider=..., allow_local=...)`` — the seam used by the
    provider-agnostic ``LLMClient``; the destination provider is explicit.
  - ``sanitize_for_outbound(text, allow_local=...)`` — kept for callers (context docs,
    hardcoded-cloud scripts) that rely on the env ``LLM_PROVIDER`` as the destination. It
    delegates to ``apply_outbound_pii`` so all three modes apply consistently.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from sales_copilot.core.pii_filter import redact_pii

logger = logging.getLogger(__name__)

_ALLOW_RAW_ENV = "ALLOW_RAW_LLM_PII"
_TRUST_TENANT_ENV = "TRUST_OWN_TENANT"
_PII_MODE_ENV = "PII_REDACTION"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TRUSTED_TENANT_PROVIDERS = frozenset({"azure", "vertex"})
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_VALID_MODES = frozenset({"cloud_only", "always", "off"})
_DEFAULT_MODE = "cloud_only"

# Skip-reasons already logged this process. A SKIP must never be silent, but it also must not
# spam the live coaching log on every transcript line.
_warned_reasons: set[str] = set()


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _env_provider() -> str:
    return (os.getenv("LLM_PROVIDER", "openrouter") or "openrouter").strip().lower()


def _warn_skip_once(reason_key: str, message: str) -> None:
    if reason_key in _warned_reasons:
        return
    _warned_reasons.add(reason_key)
    logger.warning("PII redaction SKIPPED: %s", message)


def resolve_pii_mode() -> str:
    """Return the active redaction mode: ``cloud_only`` (default), ``always`` or ``off``.

    ``ALLOW_RAW_LLM_PII`` truthy forces ``off`` (backward compatibility). An unknown
    ``PII_REDACTION`` value falls back to the safe default ``cloud_only``.
    """
    if _is_truthy(os.getenv(_ALLOW_RAW_ENV)):
        return "off"
    mode = (os.getenv(_PII_MODE_ENV, _DEFAULT_MODE) or _DEFAULT_MODE).strip().lower()
    if mode not in _VALID_MODES:
        return _DEFAULT_MODE
    return mode


def _destination_is_local(provider: str) -> bool:
    if provider != "ollama":
        return False
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") or "http://localhost:11434"
    host = urlparse(base_url).hostname or ""
    return host in _LOCAL_HOSTS


def _destination_is_trusted_tenant(provider: str) -> bool:
    if not _is_truthy(os.getenv(_TRUST_TENANT_ENV)):
        return False
    return provider in _TRUSTED_TENANT_PROVIDERS


def outbound_is_local() -> bool:
    """Return True only when the configured LLM destination is Ollama on a loopback address.

    Any cloud provider (gemini, groq, openai, vertex) returns False. Remote Ollama
    (e.g. OLLAMA_BASE_URL=http://10.0.0.5:11434) also returns False.
    """
    return _destination_is_local(_env_provider())


def outbound_is_trusted_tenant() -> bool:
    """Return True only when the provider is a BYO-tenant in {azure, vertex} AND TRUST_OWN_TENANT is set.

    Rationale: a customer's own Azure OpenAI or Google Vertex tenant keeps inference inside
    their existing data-processing boundary. Operators with a DPA covering that tenant may
    opt out of PII redaction for those providers by setting TRUST_OWN_TENANT=true.

    Public multi-tenant APIs (gemini, groq, openai) ALWAYS return False here — they are
    outside the customer's data boundary regardless of any flag. Ollama also returns False;
    use outbound_is_local() for that path.
    """
    return _destination_is_trusted_tenant(_env_provider())


def apply_outbound_pii(text: str, *, provider: str, allow_local: bool = True) -> str:
    """Apply the active PII mode to text bound for ``provider``.

    See the module docstring for the three modes. A skipped redaction is logged once per
    process — never silent.
    """
    provider = (provider or "").strip().lower()
    mode = resolve_pii_mode()

    if mode == "off":
        _warn_skip_once(
            "mode_off",
            f"mode=off (PII_REDACTION=off or {_ALLOW_RAW_ENV} set); sending raw text to provider={provider!r}",
        )
        return text

    if mode == "always":
        clean, _ = redact_pii(text)
        return clean

    # cloud_only (default + fallback for unknown values)
    if allow_local and _destination_is_local(provider):
        _warn_skip_once(
            "local_loopback",
            f"mode=cloud_only; local loopback destination provider={provider!r} (text stays on the machine)",
        )
        return text
    if allow_local and _destination_is_trusted_tenant(provider):
        _warn_skip_once(
            "trusted_tenant",
            f"mode=cloud_only; trusted BYO-tenant provider={provider!r} ({_TRUST_TENANT_ENV} set)",
        )
        return text

    clean, _ = redact_pii(text)
    return clean


def sanitize_for_outbound(text: str, *, allow_local: bool = False) -> str:
    """Redact PII by default before text is sent to the env-configured LLM provider.

    Delegates to ``apply_outbound_pii`` using ``LLM_PROVIDER`` as the destination, so the three
    redaction modes apply consistently. Pass ``allow_local=True`` only at call-sites whose text
    goes to the configured provider-agnostic LLM. Callers that hardcode a cloud client (e.g.
    scripts/label_and_summarize.py) keep the default ``allow_local=False`` and always redact in
    ``cloud_only``/``always`` mode regardless of any env flag.
    """
    return apply_outbound_pii(text, provider=_env_provider(), allow_local=allow_local)
