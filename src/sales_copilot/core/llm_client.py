"""Central provider-agnostic LLM client seam.

Single source of truth for building an instructor-patched LLM client and issuing
structured-output calls. Replaces the per-module ``_build_client`` / ``_build_create``
duplication that had drifted across the detector and copilot modules.

Responsibilities:
  - ``build_client(provider, timeout_ms=...)`` — construct the underlying SDK client for
    one of the seven supported providers with a hard timeout injected:
      * gemini / vertex  -> ``google.genai.Client(..., http_options=HttpOptions(timeout=ms))``
        (google-genai expresses the timeout in milliseconds).
      * openai / azure / groq / ollama / openrouter -> ``OpenAI(...)`` / ``AzureOpenAI(...)``
        with ``timeout=seconds`` (httpx seconds).
  - ``build_create(client, provider)`` — patch the client with ``instructor``. gemini/vertex
    use ``from_genai`` with ``GENAI_STRUCTURED_OUTPUTS``; ollama uses ``from_openai`` with
    ``Mode.JSON`` (local models emit tool-calls that omit required schema fields, so the
    default ``Mode.TOOLS`` cannot parse them); every other provider uses ``from_openai``
    with its default mode (tool-calling).
  - ``LLMClient`` — a thin wrapper that owns one patched client and exposes ``create`` /
    ``acreate``. It applies the outbound PII policy to the user text, assembles the
    ``[system, user]`` message list, and omits the ``temperature`` kwarg for gemini/vertex
    (the genai structured-output adapter rejects it).

Why the timeout matters: before this seam the live LLM calls had no timeout, so one stalled
Gemini/Vertex request could wedge the whole detection pipeline. ``acreate`` additionally caps
the call on the event loop with ``asyncio.wait_for`` so a hung thread cannot block coaching.

Prompt caching: when ``prompt_cache`` is enabled (default) and the provider/model pair supports
it (openrouter + an ``anthropic/...`` model), the system message -- the stable prefix a caller
builds once at construction time -- carries an Anthropic ``cache_control: {"type": "ephemeral"}``
breakpoint. The volatile per-turn user message never does. ``last_cache_read_tokens`` /
``last_usage["cache_read_tokens"]`` report whether a given call actually hit the cache.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from collections.abc import AsyncIterator
from typing import Any

import httpx
import instructor

from sales_copilot.core.outbound_policy import apply_outbound_pii
from sales_copilot.core.thinking_policy import ThinkingPolicy, thinking_request_kwargs

logger = logging.getLogger(__name__)

# Providers whose genai structured-output adapter rejects a ``temperature`` kwarg.
_GENAI_PROVIDERS = frozenset({"gemini", "vertex"})

# Providers that pass an Anthropic-style ``cache_control`` breakpoint through to the
# underlying model unchanged. OpenRouter does this only for the Anthropic-backed models it
# proxies; gemini/vertex use a wholly different (CachedContent-object) caching mechanism and
# openai/azure/groq/ollama have no equivalent wire field, so this stays deliberately narrow
# rather than guessing at other gateways' caching conventions.
_CACHE_CONTROL_PROVIDERS = frozenset({"openrouter"})

# Ollama's runtime context window default (independent of any model's trained max, e.g.
# gemma4:e4b supports up to 131072). At 4096, thinking-capable local models spend the whole
# window on their reasoning trace; instructor's validation-error retry then re-injects the
# failed completion plus the error, ballooning prompt tokens further, so the model hits
# ``finish_reason='length'`` before emitting valid JSON and ``summarize()`` silently returns
# None. 32768 comfortably covers a ~1h Dutch call (~13k transcript tokens) plus headroom for
# the prompt, the reasoning trace, and one retry-replay, while staying well under the 131072
# model ceiling.
_DEFAULT_OLLAMA_NUM_CTX = 32768

# Process-level cache of (base_url, base_model, num_ctx) tuples for which the derived,
# num_ctx-baked Ollama model has already been created. ``POST /api/create`` is idempotent on
# the Ollama side but a real round trip we don't want to pay on every ``create()`` call — every
# detector/copilot module builds its own ``LLMClient`` against the same provider config, so
# this cache is shared across all of them for the lifetime of the process.
_ollama_ensure_lock = threading.Lock()
_ollama_ensured_models: set[tuple[str, str, int]] = set()


def _require_key(env_var: str, provider: str) -> str:
    """Return a non-empty API key from ``env_var`` or raise a clear, actionable error.

    The OpenAI SDK (>=2.41) raises a cryptic ``Missing credentials`` at client
    construction when the key is empty. Guarding here turns that into a message
    that names the missing env var and the selected provider, so a misconfigured
    ``.env`` fails loudly at start_call instead of deep in the SDK.
    """
    key = os.getenv(env_var, "").strip()
    if not key:
        raise ValueError(
            f"LLM provider '{provider}' is selected but {env_var} is empty. "
            f"Set {env_var} in your .env, or switch LLM_PROVIDER to a configured provider."
        )
    return key


def build_client(provider: str, *, timeout_ms: int) -> Any:
    """Construct the underlying SDK client for ``provider`` with a hard timeout injected.

    Args:
        provider: one of gemini, vertex, openai, azure, groq, ollama, openrouter
            (case-insensitive).
        timeout_ms: request timeout in milliseconds. For gemini/vertex this is forwarded to
            ``HttpOptions(timeout=...)`` (milliseconds). For the OpenAI-compatible providers it
            is converted to seconds and passed as the httpx ``timeout``.

    Raises:
        ValueError: for an unknown provider, or when the selected provider's API key env var is empty.
    """
    provider = provider.strip().lower()
    if provider in {"none", ""}:
        return None
    timeout_s = timeout_ms / 1000.0

    if provider == "gemini":
        from google import genai
        from google.genai import types

        api_key = (os.getenv("GOOGLE_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")).strip()
        if not api_key:
            raise ValueError(
                "LLM provider 'gemini' is selected but GOOGLE_API_KEY/GEMINI_API_KEY are empty. "
                "Set one in your .env, or use 'vertex' (which authenticates via GOOGLE_APPLICATION_CREDENTIALS)."
            )
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
    if provider == "vertex":
        from google import genai
        from google.genai import types

        return genai.Client(
            vertexai=True,
            project=os.environ.get("GCP_PROJECT", "vnx-sales-copilot"),
            location=os.environ.get("GCP_LOCATION", "europe-west4"),
            http_options=types.HttpOptions(timeout=timeout_ms),
        )
    if provider == "openai":
        from openai import OpenAI

        return OpenAI(api_key=_require_key("OPENAI_API_KEY", "openai"), timeout=timeout_s)
    if provider == "azure":
        from openai import AzureOpenAI

        return AzureOpenAI(
            api_key=_require_key("AZURE_OPENAI_API_KEY", "azure"),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            timeout=timeout_s,
        )
    if provider == "groq":
        from openai import OpenAI

        return OpenAI(
            api_key=_require_key("GROQ_API_KEY", "groq"),
            base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
            timeout=timeout_s,
        )
    if provider == "openrouter":
        from openai import OpenAI

        return OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=_require_key("OPENROUTER_API_KEY", "openrouter"),
            timeout=timeout_s,
        )
    if provider == "ollama":
        from openai import OpenAI

        return OpenAI(
            # Ollama runs locally and needs no real key; the OpenAI SDK still
            # rejects an empty one, so fall back to a harmless placeholder.
            api_key=os.getenv("OLLAMA_API_KEY", "").strip() or "ollama",
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            timeout=timeout_s,
        )
    raise ValueError(f"Unsupported LLM provider: {provider}")


def build_create(client: Any, provider: str):
    """Return the instructor-patched ``chat.completions.create`` callable for ``client``."""
    provider = provider.strip().lower()
    if provider in _GENAI_PROVIDERS:
        patched = instructor.from_genai(client, mode=instructor.Mode.GENAI_STRUCTURED_OUTPUTS)
        return patched.chat.completions.create
    if provider == "ollama":
        # Local Ollama models (e.g. gemma3) emit a tool-call that omits required schema
        # fields, then fall back to prose without tool_calls; the default Mode.TOOLS can
        # parse neither and instructor raises/returns None. Mode.JSON asks the model to
        # answer in a JSON blob directly, which these models honor reliably.
        patched = instructor.from_openai(client, mode=instructor.Mode.JSON)
        return patched.chat.completions.create
    patched = instructor.from_openai(client)
    return patched.chat.completions.create


def build_create_partial(client: Any, provider: str) -> Any:
    """Return the instructor-patched ``create_partial`` generator callable for ``client``.

    Mirrors ``build_create``'s provider routing for the OpenAI-compatible providers, but
    returns ``None`` for gemini/vertex: the genai structured-output adapter's partial-
    streaming story differs from instructor's ``Partial[...]``-over-tool-calls mechanism and
    is not wired through this seam. ``LLMClient.astream`` falls back to the non-streaming
    ``acreate`` path for those two providers instead.

    The returned callable is synchronous (the SDK clients built by ``build_client`` are
    synchronous): calling it returns a blocking ``Generator`` that yields progressively-filled
    ``Partial[response_model]`` instances as the underlying HTTP stream arrives.
    """
    provider = provider.strip().lower()
    if provider in _GENAI_PROVIDERS:
        return None
    if provider == "ollama":
        patched = instructor.from_openai(client, mode=instructor.Mode.JSON)
        return patched.create_partial
    patched = instructor.from_openai(client)
    return patched.create_partial


def _ollama_num_ctx() -> int:
    """Return the configured Ollama context window, defaulting to ``_DEFAULT_OLLAMA_NUM_CTX``.

    Reads ``OLLAMA_NUM_CTX`` directly from the environment (same convention as
    ``OLLAMA_BASE_URL``/``OLLAMA_API_KEY`` in ``build_client``) rather than threading it
    through ``DetectorConfig`` — this is an Ollama-runtime quirk, not a detector setting.
    """
    raw = os.getenv("OLLAMA_NUM_CTX", "").strip()
    if not raw:
        return _DEFAULT_OLLAMA_NUM_CTX
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid OLLAMA_NUM_CTX=%r (not an integer); falling back to default %d.",
            raw,
            _DEFAULT_OLLAMA_NUM_CTX,
        )
        return _DEFAULT_OLLAMA_NUM_CTX
    if value <= 0:
        logger.warning(
            "OLLAMA_NUM_CTX=%d must be positive; falling back to default %d.",
            value,
            _DEFAULT_OLLAMA_NUM_CTX,
        )
        return _DEFAULT_OLLAMA_NUM_CTX
    return value


def _derived_ollama_model_name(base_model: str, num_ctx: int) -> str:
    """Return a deterministic Ollama model name with ``num_ctx`` baked in.

    Verified empirically against Ollama 0.31.1: the OpenAI-compatible
    ``/v1/chat/completions`` endpoint silently ignores both
    ``extra_body={"options": {"num_ctx": ...}}`` and a bare top-level ``num_ctx``/``n_ctx``
    field — the request succeeds but ``ollama ps`` keeps reporting the runtime-default 4096
    context. The only mechanism that actually changes the loaded context window is a model
    whose Modelfile bakes in ``PARAMETER num_ctx``, so callers derive one from the configured
    base model instead of requesting the base model directly.
    """
    # Colons split Ollama names into repo:tag, and the base model itself may already carry a
    # tag (e.g. "gemma4:e4b"), so flatten every character outside Ollama's accepted name
    # charset to avoid any repo/tag ambiguity in the derived name.
    safe_base = re.sub(r"[^a-zA-Z0-9._-]", "-", base_model)
    return f"{safe_base}-numctx{num_ctx}"


def _ensure_ollama_num_ctx_model(base_url: str, base_model: str, num_ctx: int) -> str:
    """Idempotently create a derived Ollama model with ``num_ctx`` baked in and return its name.

    Calls ``POST {base_url}/api/create`` (with any ``/v1`` suffix stripped, since ``/api/*`` is
    Ollama's native — not OpenAI-compatible — surface) at most once per
    ``(base_url, base_model, num_ctx)`` per process. On any failure (Ollama not running, an
    older server without JSON-body ``/api/create`` support, ...) this logs a warning and
    returns ``base_model`` unchanged so the caller's request still goes out — degraded to
    Ollama's 4096 runtime default rather than failing the call outright.
    """
    derived = _derived_ollama_model_name(base_model, num_ctx)
    cache_key = (base_url, base_model, num_ctx)
    if cache_key in _ollama_ensured_models:
        return derived
    with _ollama_ensure_lock:
        if cache_key in _ollama_ensured_models:
            return derived
        create_url = base_url.rstrip("/")
        if create_url.endswith("/v1"):
            create_url = create_url[: -len("/v1")]
        try:
            response = httpx.post(
                f"{create_url}/api/create",
                json={
                    "model": derived,
                    "from": base_model,
                    "parameters": {"num_ctx": num_ctx},
                    "stream": False,
                },
                timeout=30.0,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning(
                "Could not bake num_ctx=%d into derived Ollama model %r (base=%r); falling "
                "back to the base model at Ollama's runtime-default context window.",
                num_ctx,
                derived,
                base_model,
                exc_info=True,
            )
            return base_model
        _ollama_ensured_models.add(cache_key)
    return derived


def _model_supports_prompt_cache(provider: str, model: str) -> bool:
    """Whether an Anthropic-style ``cache_control`` breakpoint is safe to send.

    Narrow on purpose: true only for the openrouter provider with an
    ``anthropic/...`` model string. Sending the marker anywhere else is at best a
    silent no-op and at worst an unrecognized-field rejection from a provider
    that never asked for it.
    """
    return (
        provider.strip().lower() in _CACHE_CONTROL_PROVIDERS
        and model.strip().lower().startswith("anthropic/")
    )


def _system_message(system_prompt: str, *, cacheable: bool) -> dict[str, Any]:
    """Build the system message, marking it as the cacheable stable prefix when supported.

    ``system_prompt`` is the caller's full stable prefix -- callers such as
    ``SuggestionLLMClient``/``LLMConfirmClient`` already bake any context-doc "cards" into it
    once at construction time, so this is the one point where the stable/volatile boundary is
    drawn: the system message may carry a cache breakpoint, the per-turn user message (built by
    the caller) never does.
    """
    if not cacheable:
        return {"role": "system", "content": system_prompt}
    return {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


def _cache_read_tokens(usage: Any) -> int:
    """Best-effort extraction of cached (prompt-cache-hit) input tokens.

    Anthropic's native usage shape exposes ``cache_read_input_tokens`` directly.
    OpenAI-compatible gateways -- OpenRouter included -- mirror OpenAI's
    ``prompt_tokens_details.cached_tokens`` convention instead. Neither field is present for a
    provider/response with no caching involved, so this returns ``0`` rather than ``None``:
    ``last_usage["cache_read_tokens"]`` is always a comparable int.
    """
    direct = getattr(usage, "cache_read_input_tokens", None)
    if direct is not None:
        return int(direct)
    details = getattr(usage, "prompt_tokens_details", None)
    nested = getattr(details, "cached_tokens", None) if details is not None else None
    return int(nested) if nested is not None else 0


def _extract_usage(response: Any) -> dict[str, int] | None:
    """Best-effort extraction of token usage from an instructor response object."""
    if response is None:
        return None

    raw = getattr(response, "_raw_response", None)
    usage = getattr(raw, "usage", None)
    if usage is None:
        # Fall back to a usage attribute directly on the response object.
        usage = getattr(response, "usage", None)
    if usage is None:
        return None

    prompt_tokens = (
        getattr(usage, "prompt_tokens", None)
        or getattr(usage, "input_tokens", None)
        or 0
    )
    completion_tokens = (
        getattr(usage, "completion_tokens", None)
        or getattr(usage, "output_tokens", None)
        or 0
    )
    total_tokens = getattr(usage, "total_tokens", None) or (
        prompt_tokens + completion_tokens
    )
    return {
        "input_tokens": int(prompt_tokens),
        "output_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        "cache_read_tokens": _cache_read_tokens(usage),
    }


class LLMClient:
    """Owns one instructor-patched provider client and issues structured-output calls.

    All call sites route their LLM traffic through this seam so provider construction,
    timeout injection, PII redaction, and the gemini/vertex temperature quirk live in exactly
    one place.
    """

    def __init__(self, provider: str, *, timeout_ms: int, prompt_cache: bool = True) -> None:
        self.provider = provider.strip().lower()
        self._prompt_cache_enabled = prompt_cache
        self._timeout_ms = timeout_ms
        self._timeout_s = timeout_ms / 1000.0
        # Async callers cap the in-flight thread slightly above the SDK-level timeout so a
        # hung request cannot wedge the event loop.
        self.live_timeout_s = self._timeout_s + 2.0
        self._client = build_client(self.provider, timeout_ms=timeout_ms)
        if self._client is None:
            self._create = None
        else:
            self._create = build_create(self._client, self.provider)
        # Built lazily on the first ``astream`` call, not here: most callers (llm_confirm,
        # summary, window_classifier, ...) never stream, and patching the same client with
        # instructor a second time at construction time is wasted work no caller asked for.
        self._create_partial: Any = None
        # Only ollama needs this: the derived-model num_ctx workaround POSTs to the same host
        # ``build_client`` pointed the OpenAI-compatible client at.
        self._ollama_base_url = (
            os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").strip()
            if self.provider == "ollama"
            else None
        )
        self._last_usage: dict[str, int] | None = None
        self._last_ttft_ms: float | None = None

    @property
    def last_usage(self) -> dict[str, int] | None:
        """Token usage from the most recent successful structured-output call."""
        return self._last_usage

    @property
    def last_cache_read_tokens(self) -> int | None:
        """Cached input tokens read on the most recent successful call.

        ``None`` before any call has completed, or when the response carried no usage block at
        all. ``0`` is a legitimate value distinct from that -- it means usage was present but
        the prompt cache was not hit (cold cache, caching disabled, or a provider/model this
        seam does not mark cacheable).
        """
        if self._last_usage is None:
            return None
        return self._last_usage.get("cache_read_tokens", 0)

    @property
    def last_ttft_ms(self) -> float | None:
        """Wall-clock ms from an ``astream`` call's start to its first yielded partial.

        ``None`` before any streaming call has completed at least one yield, or when the
        provider is 'none'. For the gemini/vertex fallback (no true token-level streaming
        through this seam) this is the full-completion latency rather than a true TTFT.
        """
        return self._last_ttft_ms

    def _log_cache_hit(self, model: str) -> None:
        """Log the cache-read token count from ``self._last_usage``, when nonzero.

        The one place this seam surfaces whether a prompt-cache breakpoint was actually
        honored -- silent otherwise, since a cold cache or a provider this seam doesn't mark
        cacheable both legitimately report 0.
        """
        cache_read = (self._last_usage or {}).get("cache_read_tokens", 0)
        if cache_read:
            logger.debug(
                "Prompt cache hit: %d cached input tokens (provider=%s model=%s)",
                cache_read,
                self.provider,
                model,
            )

    def create(
        self,
        *,
        model: str,
        system_prompt: str,
        user_text: str,
        response_model: Any,
        temperature: float | None = None,
        allow_local: bool = True,
        thinking: ThinkingPolicy | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        """Issue a structured-output call.

        The PII policy is applied to ``user_text`` only — ``system_prompt`` is static and never
        carries PII. ``temperature`` is omitted for gemini/vertex (the genai adapter rejects it)
        and forwarded for every other provider when not ``None``. ``thinking`` is optional and
        defaults to ``None``, which keeps every existing caller's behavior unchanged: no thinking
        kwargs are added, and Ollama keeps sizing its context window from ``OLLAMA_NUM_CTX``.
        When given, it is translated into the correct per-provider request shape via
        ``thinking_request_kwargs`` (gemini/vertex/openrouter) or, for Ollama, used directly as
        the ``num_ctx`` override for the derived-model seam. ``max_tokens`` defaults to ``None``
        (provider default, unchanged for every existing caller) and is forwarded only for the
        OpenAI-compatible providers (openai/azure/groq/ollama/openrouter) — the genai adapter for
        gemini/vertex has no equivalent top-level kwarg on this seam, so it is silently ignored
        there rather than raising. A caller with a thinking-capable model should pass a generous
        value here: a low cap truncates the reasoning trace before the final answer and the
        structured-output call comes back with ``finish_reason='length'``.
        """
        safe_text = apply_outbound_pii(user_text, provider=self.provider, allow_local=allow_local)
        cacheable = self._prompt_cache_enabled and _model_supports_prompt_cache(self.provider, model)
        messages = [
            _system_message(system_prompt, cacheable=cacheable),
            {"role": "user", "content": safe_text},
        ]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "response_model": response_model,
        }
        if self.provider == "ollama":
            num_ctx = (
                thinking.reasoning_budget
                if thinking is not None and thinking.reasoning_budget is not None
                else _ollama_num_ctx()
            )
            kwargs["model"] = _ensure_ollama_num_ctx_model(self._ollama_base_url, model, num_ctx)
        if self._create is None:
            logger.warning("LLM provider is 'none'; skipping LLM call.")
            return None
        if self.provider not in _GENAI_PROVIDERS and temperature is not None:
            kwargs["temperature"] = temperature
        if self.provider not in _GENAI_PROVIDERS and max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if thinking is not None:
            extra = thinking_request_kwargs(
                self.provider,
                thinking_on=thinking.thinking_on,
                reasoning_budget=thinking.reasoning_budget,
            )
            budget = extra.pop("thinking_budget", None)
            if budget is not None:
                # thinking_policy.py stays provider-SDK-free (architecture boundary): it
                # returns a plain int, and this is the one place that wraps it into the
                # real google-genai type.
                from google.genai import types

                extra["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
            kwargs.update(extra)
        response = self._create(**kwargs)
        self._last_usage = _extract_usage(response)
        self._log_cache_hit(model)
        return response

    async def acreate(self, **kwargs: Any) -> Any:
        """Async wrapper used on the live path: offloads ``create`` and caps the wall-clock."""
        if self._create is None:
            logger.warning("LLM provider is 'none'; skipping async LLM call.")
            return None
        return await asyncio.wait_for(
            asyncio.to_thread(self.create, **kwargs),
            timeout=self.live_timeout_s,
        )

    async def astream(
        self,
        *,
        model: str,
        system_prompt: str,
        user_text: str,
        response_model: Any,
        temperature: float | None = None,
        allow_local: bool = True,
        thinking: ThinkingPolicy | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[Any]:
        """Stream a structured-output call, yielding progressively-filled ``response_model``
        instances as tokens arrive.

        For the OpenAI-compatible providers (openai/azure/groq/ollama/openrouter) this drives
        instructor's partial-streaming (``create_partial`` over ``Partial[response_model]``) so
        a caller can render partial text before the full object validates. The underlying SDK
        client is synchronous, so the blocking HTTP stream is iterated on a worker thread and
        bridged into this async generator via a queue — the event loop is never blocked on
        network I/O.

        gemini/vertex: the genai structured-output adapter does not go through instructor's
        ``Partial[...]``-over-tool-calls mechanism, so this transparently falls back to the
        non-streaming ``acreate`` path and yields exactly one item (the full object).

        Records ``last_ttft_ms`` — the wall-clock ms from call start to the first yielded item.
        For the gemini/vertex fallback this is the full-completion latency (there is no earlier
        token boundary to measure through this seam). The ``live_timeout_s`` wall-clock cap
        ``acreate`` uses bounds the full stream here too, not each individual chunk.
        """
        self._last_ttft_ms = None
        if self._create is None:
            logger.warning("LLM provider is 'none'; skipping streaming LLM call.")
            return

        loop = asyncio.get_running_loop()

        if self.provider not in _GENAI_PROVIDERS and self._create_partial is None:
            self._create_partial = build_create_partial(self._client, self.provider)

        if self.provider in _GENAI_PROVIDERS or self._create_partial is None:
            start = loop.time()
            result = await self.acreate(
                model=model,
                system_prompt=system_prompt,
                user_text=user_text,
                response_model=response_model,
                temperature=temperature,
                allow_local=allow_local,
                thinking=thinking,
                max_tokens=max_tokens,
            )
            if result is not None:
                self._last_ttft_ms = (loop.time() - start) * 1000
                yield result
            return

        safe_text = apply_outbound_pii(user_text, provider=self.provider, allow_local=allow_local)
        cacheable = self._prompt_cache_enabled and _model_supports_prompt_cache(self.provider, model)
        messages = [
            _system_message(system_prompt, cacheable=cacheable),
            {"role": "user", "content": safe_text},
        ]
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if thinking is not None:
            extra = thinking_request_kwargs(
                self.provider,
                thinking_on=thinking.thinking_on,
                reasoning_budget=thinking.reasoning_budget,
            )
            extra.pop("thinking_budget", None)  # gemini/vertex-only; genai providers never reach here
            kwargs.update(extra)

        ollama_num_ctx: int | None = None
        if self.provider == "ollama":
            ollama_num_ctx = (
                thinking.reasoning_budget
                if thinking is not None and thinking.reasoning_budget is not None
                else _ollama_num_ctx()
            )

        create_partial = self._create_partial
        queue: asyncio.Queue[Any] = asyncio.Queue()
        sentinel = object()
        start = loop.time()

        def _produce() -> None:
            try:
                call_kwargs = dict(kwargs)
                if ollama_num_ctx is not None:
                    call_kwargs["model"] = _ensure_ollama_num_ctx_model(
                        self._ollama_base_url, model, ollama_num_ctx
                    )
                for item in create_partial(response_model=response_model, **call_kwargs):
                    loop.call_soon_threadsafe(queue.put_nowait, item)
            except Exception as exc:  # noqa: BLE001 -- forwarded to the consumer, never swallowed
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        loop.run_in_executor(None, _produce)

        deadline = start + self.live_timeout_s
        first = True
        last_item: Any = None
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"LLM stream exceeded the {self.live_timeout_s:.1f}s wall-clock cap")
            item = await asyncio.wait_for(queue.get(), timeout=remaining)
            if item is sentinel:
                break
            if isinstance(item, Exception):
                raise item
            if first:
                self._last_ttft_ms = (loop.time() - start) * 1000
                first = False
            last_item = item
            yield item
        if last_item is not None:
            self._last_usage = _extract_usage(last_item)
            self._log_cache_hit(model)
