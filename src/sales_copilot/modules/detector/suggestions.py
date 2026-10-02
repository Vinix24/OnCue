from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import websockets
import yaml
from pydantic import BaseModel

from sales_copilot.core import lang_router
from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.core.context_docs import load_context_documents
from sales_copilot.core.llm_routing import resolve_llm_client
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)

SUGGESTIONS_DEBOUNCE_MS = 20_000
SUGGESTIONS_MAX_CONTEXT_LINES = 8
SUGGESTIONS_MAX_CONTEXT_CHARS = 1_800
# Dashboard placeholder, routed through the i18n catalog for the configured LANGUAGE.
SUGGESTIONS_EMPTY_STATE = lang_router.route_coaching_prompts().empty_state

# Phase → strategy mapping
_PHASE_TO_STRATEGY: dict[str, str] = {
    "early": "acknowledge",
    "discovery": "acknowledge",
    "mid": "reframe",
    "pitch": "reframe",
    "late": "evidence",
    "closing": "evidence",
}
_FALLBACK_STRATEGY = "acknowledge"

_CONFIG_DIR = resolve_app_path("config")


def _now_ms() -> int:
    return int(time.time() * 1000)


class SuggestionResponse(BaseModel):
    questions: list[str]


class SuggestionLLMClient:
    def __init__(
        self,
        config: DetectorConfig | None = None,
        *,
        context_docs: list[str] | None = None,
        language: str | None = None,
    ) -> None:
        self.config = config or DetectorConfig.from_env()
        self.resolved, self._llm = resolve_llm_client(
            "suggestions", self.config, prompt_cache=self.config.llm_prompt_cache
        )
        self.provider = self.resolved.provider
        self.model = self.resolved.model
        # Coaching prompts are selected per language by lang_router. ``language=None``
        # resolves to the configured LANGUAGE (falling back to nl per missing key).
        self._prompts = lang_router.route_coaching_prompts(language)
        self._context_block = self._load_context_docs(context_docs or [], provider=self.provider)
        self.system_prompt = self._build_system_prompt()

    async def suggest(
        self,
        transcript_lines: list[str],
        *,
        on_partial: Callable[[SuggestionResponse], Awaitable[None]] | None = None,
    ) -> list[str]:
        cleaned_lines = [line.strip() for line in transcript_lines if isinstance(line, str) and line.strip()]
        if not cleaned_lines:
            return []

        timeout_s = self.resolved.timeout_ms / 1000
        user_text = self._user_prompt(cleaned_lines)

        if not self.config.llm_streaming:
            try:
                result = await asyncio.wait_for(
                    self._llm.acreate(
                        model=self.model,
                        system_prompt=self.system_prompt,
                        user_text=user_text,
                        response_model=SuggestionResponse,
                        temperature=self.config.llm_temperature,
                        allow_local=True,
                        max_tokens=self.resolved.output_limit(),
                    ),
                    timeout=timeout_s,
                )
            except TimeoutError:
                logger.warning(
                    "SuggestionLLMClient timed out after %.2fs: provider=%s model=%s",
                    timeout_s,
                    self.provider,
                    self.model,
                )
                return []
            except Exception as exc:
                logger.error(
                    "SuggestionLLMClient call failed: provider=%s model=%s exc_type=%s msg=%s",
                    self.provider,
                    self.model,
                    type(exc).__name__,
                    str(exc)[:300],
                )
                return []
            if result is None:
                return []
            return _normalize_questions(result.questions)

        async def _drain() -> SuggestionResponse | None:
            last: SuggestionResponse | None = None
            async for partial in self._llm.astream(
                model=self.model,
                system_prompt=self.system_prompt,
                user_text=user_text,
                response_model=SuggestionResponse,
                temperature=self.config.llm_temperature,
                allow_local=True,
                max_tokens=self.resolved.output_limit(),
            ):
                last = partial
                if on_partial is not None:
                    await on_partial(partial)
            return last

        try:
            result = await asyncio.wait_for(_drain(), timeout=timeout_s)
        except TimeoutError:
            logger.warning(
                "SuggestionLLMClient stream timed out after %.2fs: provider=%s model=%s",
                timeout_s,
                self.provider,
                self.model,
            )
            return []
        except Exception as exc:
            logger.error(
                "SuggestionLLMClient stream failed: provider=%s model=%s exc_type=%s msg=%s",
                self.provider,
                self.model,
                type(exc).__name__,
                str(exc)[:300],
            )
            return []
        if result is None:
            return []
        return _normalize_questions(result.questions)

    def _user_prompt(self, transcript_lines: list[str]) -> str:
        # Assembled raw; the seam applies the outbound PII policy to the full user_text once.
        transcript = "\n".join(f"- {line}" for line in transcript_lines)
        prompts = getattr(self, "_prompts", None) or lang_router.route_coaching_prompts()
        return prompts.user_prompt.format(transcript=transcript)

    def _build_system_prompt(self) -> str:
        base = self._prompts.system_prompt
        if not self._context_block:
            return base
        return f"{base}\n\n{self._prompts.context_docs_label}\n{self._context_block}"

    @staticmethod
    def _load_context_docs(paths: list[str], *, provider: str | None = None) -> str:
        return load_context_documents(paths, provider=provider)


def _normalize_questions(questions: list[str]) -> list[str]:
    normalized: list[str] = []
    for question in questions:
        if not isinstance(question, str):
            continue
        cleaned = question.strip()
        if not cleaned or cleaned in normalized:
            continue
        normalized.append(cleaned)
        if len(normalized) == 3:
            break
    return normalized


class ObjectionResponsePicker:
    """Loads objection response templates and picks one based on conversation phase."""

    def __init__(self, yaml_path: Path | None = None) -> None:
        path = yaml_path or (_CONFIG_DIR / "objection_responses.yaml")
        self._responses = self._load(path)

    @staticmethod
    def _load(path: Path) -> dict[str, dict[str, list[str]]]:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError:
            logger.warning("ObjectionResponsePicker: could not read %s", path)
            return {}
        if not isinstance(raw, dict):
            return {}
        responses = raw.get("responses", {})
        if not isinstance(responses, dict):
            return {}
        return {
            subcategory: {
                strategy: (examples if isinstance(examples, list) else [])
                for strategy, examples in strategies.items()
                if isinstance(strategies, dict)
            }
            for subcategory, strategies in responses.items()
            if isinstance(strategies, dict)
        }

    def pick(self, subcategory: str, phase: str | None = None) -> str | None:
        """Return a random response for the subcategory+phase combination.

        Returns None when no template is found for the subcategory.
        """
        strategy = _PHASE_TO_STRATEGY.get((phase or "").lower(), _FALLBACK_STRATEGY)
        bucket = self._responses.get(subcategory, self._responses.get("anders", {}))
        examples = bucket.get(strategy, [])
        if not examples:
            # fallback within same subcategory
            for fallback_strategy in (_FALLBACK_STRATEGY, "reframe", "evidence"):
                examples = bucket.get(fallback_strategy, [])
                if examples:
                    break
        if not examples:
            return None
        return random.choice(examples)

    @property
    def subcategories(self) -> list[str]:
        return list(self._responses.keys())


class SuggestionEngine:
    def __init__(
        self,
        config: DetectorConfig,
        ws_config: WebSocketConfig,
        *,
        client: SuggestionLLMClient | None = None,
        context_docs: list[str] | None = None,
        now_ms: Callable[[], int] | None = None,
        debounce_ms: int = SUGGESTIONS_DEBOUNCE_MS,
        max_context_lines: int = SUGGESTIONS_MAX_CONTEXT_LINES,
        objection_picker: ObjectionResponsePicker | None = None,
    ) -> None:
        self.config = config
        self.ws_config = ws_config
        self.client = client or SuggestionLLMClient(config, context_docs=context_docs)
        self._now_ms = now_ms or _now_ms
        self._debounce_ms = debounce_ms
        self._max_context_lines = max_context_lines
        self._pending_lines: list[str] = []
        self._window_started_ms: int | None = None
        self._queue: asyncio.Queue[tuple[str, str, int]] = asyncio.Queue()
        self.objection_picker = objection_picker or ObjectionResponsePicker()
        self._suggestions_ws: websockets.ClientConnection | None = None

    def pick_objection_response(
        self,
        subcategory: str,
        phase: str | None = None,
    ) -> str | None:
        """Return a phase-appropriate coaching response for a detected objection."""
        return self.objection_picker.pick(subcategory, phase)

    async def enqueue(self, text: str, speaker: str, timestamp_ms: int) -> None:
        await self._queue.put((text, speaker, timestamp_ms))

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            timeout = self._next_timeout_seconds()
            try:
                text, speaker, timestamp_ms = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                await self._flush_if_due()
                continue

            if speaker != "prospect":
                continue
            self._record_line(text, timestamp_ms)
            await self._flush_if_due()

        await self._flush_pending()

    def _next_timeout_seconds(self) -> float:
        if self._window_started_ms is None:
            return 0.25
        remaining_ms = self._debounce_ms - (self._now_ms() - self._window_started_ms)
        return max(0.05, remaining_ms / 1000)

    def _record_line(self, text: str, timestamp_ms: int) -> None:
        cleaned = " ".join(text.split())
        if not cleaned:
            return
        if self._window_started_ms is None:
            self._window_started_ms = timestamp_ms or self._now_ms()
        self._pending_lines.append(cleaned)
        if len(self._pending_lines) > self._max_context_lines:
            self._pending_lines = self._pending_lines[-self._max_context_lines :]

    async def _flush_if_due(self) -> None:
        if self._window_started_ms is None:
            return
        if self._now_ms() - self._window_started_ms < self._debounce_ms:
            return
        await self._flush_pending()

    async def _flush_pending(self) -> None:
        if not self._pending_lines:
            self._window_started_ms = None
            return

        transcript_lines = self._build_transcript_context(self._pending_lines)
        self._pending_lines = []
        self._window_started_ms = None

        questions = await self.client.suggest(
            transcript_lines,
            on_partial=self._publish_partial_suggestion,
        )
        if not questions:
            return

        payload = {
            "type": "suggestion",
            "questions": questions,
            "timestamp_ms": self._now_ms(),
        }
        await self._publish_payload(payload)

    async def _publish_partial_suggestion(self, partial: SuggestionResponse) -> None:
        """Publish a still-streaming suggestion so the dashboard can render partial text.

        Best-effort: dropped silently if the partial hasn't produced any usable question text
        yet (fields are optional/``None`` until that part of the JSON has streamed in).
        """
        questions = _normalize_questions(partial.questions or [])
        if not questions:
            return
        await self._publish_payload(
            {
                "type": "suggestion_partial",
                "questions": questions,
                "timestamp_ms": self._now_ms(),
            }
        )

    def _build_transcript_context(self, transcript_lines: list[str]) -> list[str]:
        context: list[str] = []
        total_chars = 0
        for line in reversed(transcript_lines[-self._max_context_lines :]):
            total_chars += len(line)
            if total_chars > SUGGESTIONS_MAX_CONTEXT_CHARS:
                break
            context.append(line)
        return list(reversed(context))

    async def _publish_payload(self, payload: dict[str, object]) -> None:
        ws = await self._ensure_suggestions_ws()
        try:
            await ws.send(json.dumps(payload))
        except Exception:
            await self.close()
            raise

    @staticmethod
    def _is_ws_open(ws: websockets.ClientConnection) -> bool:
        # websockets >=12 dropped the .closed bool attribute in favor of
        # ClientConnection.close_code (None while open). Support both.
        closed_attr = getattr(ws, "closed", None)
        if isinstance(closed_attr, bool):
            return not closed_attr
        return getattr(ws, "close_code", None) is None

    async def _ensure_suggestions_ws(self) -> websockets.ClientConnection:
        if self._suggestions_ws is None or not self._is_ws_open(self._suggestions_ws):
            suggestions_url = channel_ws_url(self.ws_config, "suggestions")
            self._suggestions_ws = await websockets.connect(suggestions_url)
        return self._suggestions_ws

    async def close(self) -> None:
        """Close the held suggestions-channel connection, if any. Safe to call repeatedly."""
        if self._suggestions_ws is None:
            return
        try:
            await self._suggestions_ws.close()
        except Exception:
            pass
        self._suggestions_ws = None
