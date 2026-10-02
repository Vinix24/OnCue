from __future__ import annotations

import asyncio
import json
import logging
import re

import websockets

from sales_copilot.core.config import WebSocketConfig
from sales_copilot.modules.transcriber.backends.base import TranscriptionBackend
from sales_copilot.modules.transcriber.engine import PartialTranscriptEvent, Speaker, TranscriptEvent
from sales_copilot.modules.transcriber.inference_queue import InferenceQueueItem, SharedInferenceQueue
from sales_copilot.modules.transcriber.normalize import (
    get_default_normalization_lists,
    normalize,
    with_call_terms,
)
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)


class InferenceWorker:
    def __init__(
        self,
        backend: TranscriptionBackend,
        queue: SharedInferenceQueue,
        ws_config: WebSocketConfig,
        min_text_length: int = 2,
        inference_timeout_s: float = 30.0,
    ) -> None:
        self._backend = backend
        self._queue = queue
        self._ws_config = ws_config
        self._min_text_length = min_text_length
        self._inference_timeout_s = inference_timeout_s
        self._ws: websockets.ClientConnection | None = None
        self._last_published: dict[Speaker, str | None] = {}
        self._base_normalization_lists = get_default_normalization_lists()
        self._normalization_lists = self._base_normalization_lists

    def set_call_terms(self, call_terms: tuple[str, ...]) -> None:
        """Set (or, with an empty tuple, clear) the per-conversation term list."""
        self._normalization_lists = with_call_terms(self._base_normalization_lists, call_terms)

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            await self._backend.start(stop_event)
            await self._ensure_ws()
            while not stop_event.is_set():
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                except TimeoutError:
                    continue
                await self._process_item(item)
            remaining = self._queue.qsize()
            if remaining > 0:
                logger.info(
                    "InferenceWorker exiting with %s unprocessed items in queue.", remaining
                )
        finally:
            await self._close_ws()
            await self._backend.stop()

    async def _process_item(self, item: InferenceQueueItem) -> None:
        await self._publish_partial(
            PartialTranscriptEvent(
                type="partial_transcript",
                text="",
                speaker=item.speaker,
                start_ms=item.start_ms,
                tentative_end_ms=item.end_ms,
                is_final=False,
            )
        )
        try:
            text = await asyncio.wait_for(
                self._backend.transcribe(item.audio),
                timeout=self._inference_timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "transcribe timed out after %.1fs, dropping item speaker=%s",
                self._inference_timeout_s,
                item.speaker,
            )
            return
        except Exception as exc:
            logger.warning("transcribe failed, dropping item speaker=%s: %s", item.speaker, exc)
            return

        cleaned = text.strip()
        if self._is_hallucination(cleaned):
            return
        if len(cleaned) < self._min_text_length:
            return

        cleaned, replacements = normalize(cleaned, self._normalization_lists)
        for source, target, rule in replacements:
            logger.debug("transcript normalization: %r -> %r (rule=%s)", source, target, rule)

        last = self._last_published.get(item.speaker)
        if cleaned == last:
            return
        self._last_published[item.speaker] = cleaned

        event = TranscriptEvent(
            type="transcript",
            text=cleaned,
            speaker=item.speaker,
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            is_final=True,
        )
        await self._publish(event)

    async def _publish(self, event: TranscriptEvent) -> None:
        payload = json.dumps(event.to_dict())
        ws = await self._ensure_ws()
        if ws is None:
            return
        try:
            await ws.send(payload)
        except Exception:
            await self._close_ws()
            ws = await self._ensure_ws()
            if ws is not None:
                await ws.send(payload)

    async def _publish_partial(self, event: PartialTranscriptEvent) -> None:
        payload = json.dumps(event.to_dict())
        ws = await self._ensure_ws()
        if ws is None:
            return
        try:
            await ws.send(payload)
        except Exception:
            await self._close_ws()
            ws = await self._ensure_ws()
            if ws is not None:
                try:
                    await ws.send(payload)
                except Exception:
                    pass

    async def _ensure_ws(self) -> websockets.ClientConnection | None:
        if self._ws is not None and self._is_ws_open(self._ws):
            return self._ws
        self._ws = None
        try:
            self._ws = await websockets.connect(
                channel_ws_url(self._ws_config, "transcript"),
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            )
        except Exception as exc:
            logger.warning("InferenceWorker hub connect failed: %s", exc)
            self._ws = None
        return self._ws

    @staticmethod
    def _is_ws_open(ws: websockets.ClientConnection) -> bool:
        closed_attr = getattr(ws, "closed", None)
        if isinstance(closed_attr, bool):
            return not closed_attr
        return getattr(ws, "close_code", None) is None

    async def _close_ws(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.close()
        finally:
            self._ws = None

    @staticmethod
    def _is_hallucination(text: str) -> bool:
        cleaned = text.strip()
        if not cleaned:
            return True

        lowered = cleaned.casefold()
        known_patterns = (
            "...",
            "***",
            "ondertitels",
            "ondertiteling",
            "tv gelderland",
            "omroep gelderland",
            "npo radio",
            "npo 1",
            "vertaald door",
            "geredigeerd door",
            "vertaling:",
            "ondertiteld door",
            "bedankt voor het kijken",
            "bedankt voor het luisteren",
            "tot de volgende keer",
            "mbc뉴스",
            "ご視聴",
            "thanks for watching",
            "thank you for watching",
            "subscribe to",
            "please subscribe",
        )
        if any(pattern in lowered for pattern in known_patterns):
            return True

        if not any(char.isalnum() for char in cleaned):
            return True

        filler_only = (
            "ehm",
            "ehm.",
            "uh",
            "hmm",
            "oh la la",
            "oh la la.",
            "dank u wel",
            "dank u wel.",
            "dank je wel",
            "pagina",
            "pagina.",
            "oké",
            "oke",
            "ja",
            "ja.",
            "nee",
            "nee.",
        )
        if lowered.replace(".", "").strip() in {p.replace(".", "").strip() for p in filler_only}:
            return True

        cjk_count = sum(
            1
            for ch in cleaned
            if "一" <= ch <= "鿿"
            or "぀" <= ch <= "ヿ"
            or "가" <= ch <= "힯"
        )
        if cjk_count > 0 and cjk_count >= sum(1 for ch in cleaned if ch.isalpha()) / 2:
            return True

        tokens = [t for t in re.split(r"\s+", cleaned) if t]
        if len(tokens) >= 5:
            unique_tokens = {t.casefold().rstrip(".,!?") for t in tokens}
            if len(unique_tokens) <= 2:
                return True

        word_chars = re.findall(r"\w", cleaned, flags=re.UNICODE)
        return len(word_chars) < 3
