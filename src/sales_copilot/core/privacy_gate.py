"""Enforce a client's ``klant.yaml`` privacy ceiling against the configured LLM provider.

Part of "klantmap-als-eenheid" D2. ``klant.yaml`` (D1, ``core/klant_config.py``) lets a
client folder declare a ``privacy`` ceiling: ``local``, ``tenant`` or ``public``. This
module is the gate -- it rejects a start-call before any capture starts if a provider
does not meet that ceiling. llm-routering-per-taak (``core/llm_routing.py``) applies the
same ``enforce_privacy`` to every task the resolver hands out, at resolve time and again
on every call (``LLMClient``).

The ceiling is a ranking, not an equality check: ``local`` is the strictest (only Ollama
on loopback satisfies it), ``tenant`` accepts ``local`` or ``tenant`` providers, and
``public`` (or no ``klant.yaml`` privacy set at all) accepts anything. The tier
classification itself is NOT re-derived here -- it comes from
``outbound_policy.tier_of()``, the same function ``PII_REDACTION``'s ``cloud_only`` mode
uses, so the privacy-poort and the PII policy can never disagree about what counts as
local, tenant or public.
"""

from __future__ import annotations

from sales_copilot.core.outbound_policy import tier_of

_TIER_RANK: dict[str, int] = {"local": 0, "tenant": 1, "public": 2}

#: The three privacy profiles a conversation can carry, strictest first.
PRIVACY_PROFILES: tuple[str, ...] = tuple(_TIER_RANK)


class PrivacyGateError(ValueError):
    """Raised when a client's ``klant.yaml`` privacy ceiling rejects the configured provider.

    A ``ValueError`` subclass, like ``KlantConfigError`` -- the HTTP layer
    (``hub_api.start_call_api``) already maps any ``ValueError`` from
    ``extract_start_call_config`` to a 400, so this needs no separate handler.
    """


def validate_privacy(privacy: object) -> str | None:
    """Return ``privacy`` normalized, or raise ``PrivacyGateError`` for an unknown profile.

    ``None`` means "no ceiling" and passes through. Anything that is not one of
    ``PRIVACY_PROFILES`` is refused rather than treated as "no ceiling": an unknown
    profile must never silently lift the plafond.
    """
    if privacy is None:
        return None
    if not isinstance(privacy, str) or privacy.strip().lower() not in _TIER_RANK:
        raise PrivacyGateError(
            f"onbekend privacy-profiel {privacy!r}; kies uit {list(PRIVACY_PROFILES)}"
        )
    return privacy.strip().lower()


def privacy_allows(privacy: str | None, provider: str) -> bool:
    """Does ``provider`` satisfy the ``privacy`` ceiling?

    ``privacy=None`` (no ``klant.yaml``, or a ``klant.yaml`` without an explicit
    ``privacy`` field) always allows -- there is no ceiling to enforce.
    ``provider="none"`` (a task switched off: ``LLMClient`` builds no client and sends
    nothing) also always allows. An empty provider does not: it is unconfigured, not off,
    and ``tier_of("")`` is ``public``.
    """
    if privacy is None:
        return True
    if (provider or "").strip().lower() == "none":
        return True
    return _TIER_RANK[tier_of(provider)] <= _TIER_RANK[validate_privacy(privacy)]


def enforce_privacy(
    privacy: str | None, provider: str, *, client_slug: str | None, lane: str | None = None
) -> None:
    """Raise ``PrivacyGateError`` when ``provider`` does not satisfy ``privacy``.

    ``client_slug`` is only used to name the offending client in the error message.
    ``lane`` names which text-receiving lane this ``provider`` belongs to (e.g.
    ``"detector"`` or ``"insight"``) so a caller checking more than one lane's provider
    against the same ceiling (``hub_core._apply_klant_config`` checks both) gets an error
    message that says which one crossed the ceiling. Omitted, the message is unchanged
    from before multi-lane checking existed.
    """
    if privacy_allows(privacy, provider):
        return
    tier = tier_of(provider)
    lane_suffix = f" ({lane}-lane)" if lane else ""
    subject = f"klant '{client_slug}'" if client_slug else "dit gesprek"
    raise PrivacyGateError(
        f"{subject} vereist privacy={privacy!r}, maar de geconfigureerde "
        f"provider {provider!r}{lane_suffix} is {tier!r} -- geweigerd, er gaat niets naar deze provider"
    )
