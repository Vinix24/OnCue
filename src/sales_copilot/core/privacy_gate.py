"""Enforce a client's ``klant.yaml`` privacy ceiling against the configured LLM provider.

Part of "klantmap-als-eenheid" D2. ``klant.yaml`` (D1, ``core/klant_config.py``) lets a
client folder declare a ``privacy`` ceiling: ``local``, ``tenant`` or ``public``. This
module is the minimal gate the plan calls for -- reject a start-call before any capture
starts if the configured provider does not meet that ceiling; it does not attempt the
full per-task privacy plafond that llm-routering-per-taak owns.

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


class PrivacyGateError(ValueError):
    """Raised when a client's ``klant.yaml`` privacy ceiling rejects the configured provider.

    A ``ValueError`` subclass, like ``KlantConfigError`` -- the HTTP layer
    (``hub_api.start_call_api``) already maps any ``ValueError`` from
    ``extract_start_call_config`` to a 400, so this needs no separate handler.
    """


def privacy_allows(privacy: str | None, provider: str) -> bool:
    """Does ``provider`` satisfy the ``privacy`` ceiling?

    ``privacy=None`` (no ``klant.yaml``, or a ``klant.yaml`` without an explicit
    ``privacy`` field) always allows -- there is no ceiling to enforce.
    """
    if privacy is None:
        return True
    return _TIER_RANK[tier_of(provider)] <= _TIER_RANK[privacy]


def enforce_privacy(
    privacy: str | None, provider: str, *, client_slug: str, lane: str | None = None
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
    raise PrivacyGateError(
        f"klant '{client_slug}' vereist privacy={privacy!r}, maar de geconfigureerde "
        f"provider {provider!r}{lane_suffix} is {tier!r} -- start-call geweigerd"
    )
