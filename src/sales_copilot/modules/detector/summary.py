from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Literal

import websockets
from pydantic import BaseModel, Field
from websockets.exceptions import ConnectionClosed

from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.core.llm_routing import resolve_llm_client
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)

SUMMARY_INTERVAL_SECONDS = 60
MAX_SUMMARY_LINES = 40
MAX_SUMMARY_CHARS = 4000

SummaryMomentType = Literal["pain_point", "monologue", "phase_transition", "objection"]


class KeyMoment(BaseModel):
    type: SummaryMomentType
    timestamp_ms: int
    description: str


class ConversationSummary(BaseModel):
    text: str
    key_moments: list[KeyMoment] = Field(default_factory=list)


class SummaryLLMClient:
    _SYSTEM_PROMPT = (
        "Summarize this sales conversation segment in 2-3 Dutch sentences. "
        "Identify any key moments."
    )

    def __init__(self, config: DetectorConfig) -> None:
        self.config = config
        # Post-segment, not live: its own timeout default (30 s) instead of LLM_TIMEOUT_MS.
        self.resolved, self._llm = resolve_llm_client("summary", config)
        self.provider = self.resolved.provider

    async def summarize(
        self,
        transcript_lines: list[str],
        tracked_key_moments: list[KeyMoment],
    ) -> ConversationSummary | None:
        cleaned_lines = [line.strip() for line in transcript_lines if isinstance(line, str) and line.strip()]
        if not cleaned_lines:
            return None

        timeout_s = self.resolved.timeout_ms / 1000
        try:
            summary = await asyncio.wait_for(
                self._llm.acreate(
                    model=self.resolved.model,
                    system_prompt=self._SYSTEM_PROMPT,
                    user_text=self._user_prompt(cleaned_lines, tracked_key_moments),
                    response_model=ConversationSummary,
                    temperature=self.config.llm_temperature,
                    allow_local=True,
                    max_tokens=self.resolved.output_limit(),
                ),
                timeout=timeout_s,
            )
        except TimeoutError:
            logger.warning("Summary generation timed out after %.2fs.", timeout_s)
            return None
        except Exception:
            logger.warning("Summary generation failed.", exc_info=True)
            return None
        if not summary.text.strip():
            return None
        return summary

    def _user_prompt(self, transcript_lines: list[str], tracked_key_moments: list[KeyMoment]) -> str:
        # Assembled raw; the seam applies the outbound PII policy to the full user_text once.
        transcript_block = "\n".join(f"- {line}" for line in transcript_lines)
        if tracked_key_moments:
            moments_block = "\n".join(
                f"- [{moment.type}] {moment.timestamp_ms}: {moment.description}"
                for moment in tracked_key_moments
            )
        else:
            moments_block = "- Geen key moments geregistreerd in dit segment."
        return (
            "Gesprekssegment:\n"
            f"{transcript_block}\n\n"
            "Geregistreerde key moments (event streams):\n"
            f"{moments_block}\n\n"
            "Gebruik deze key moments in je antwoord en behoud timestamp_ms waarden."
        )


class SummaryEngine:
    def __init__(
        self,
        config: DetectorConfig,
        ws_config: WebSocketConfig,
        *,
        client: SummaryLLMClient | None = None,
        interval_seconds: int = SUMMARY_INTERVAL_SECONDS,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.config = config
        self.ws_config = ws_config
        self.client = client or SummaryLLMClient(config)
        self._interval_seconds = max(5, int(interval_seconds))
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = asyncio.Lock()
        self._summary_ws: websockets.ClientConnection | None = None

        self._segment_lines: list[str] = []
        self._first_pain_point: KeyMoment | None = None
        self._longest_monologue: KeyMoment | None = None
        self._longest_monologue_ms = 0
        self._phase_transitions: list[KeyMoment] = []
        self._objections: list[KeyMoment] = []
        self._last_phase: str | None = None

    async def enqueue(self, text: str, speaker: str, timestamp_ms: int) -> None:
        del timestamp_ms
        cleaned = " ".join(str(text).split())
        if not cleaned:
            return
        async with self._lock:
            self._segment_lines.append(f"{speaker}: {cleaned}")
            self._segment_lines = self._segment_lines[-MAX_SUMMARY_LINES:]

    async def run(self, stop_event: asyncio.Event) -> None:
        tasks = [
            asyncio.create_task(self._summary_loop(stop_event), name="summary-loop"),
            asyncio.create_task(
                self._consume_channel("pain-points", self._handle_pain_point, stop_event),
                name="summary-pain-points",
            ),
            asyncio.create_task(
                self._consume_channel("talk-time", self._handle_talk_time, stop_event),
                name="summary-talk-time",
            ),
            asyncio.create_task(
                self._consume_channel("phase", self._handle_phase, stop_event),
                name="summary-phase",
            ),
            asyncio.create_task(
                self._consume_channel("objections", self._handle_objection, stop_event),
                name="summary-objections",
            ),
        ]
        try:
            # Per-channel resilience: one consumer raising must not tear the
            # whole summary engine down. Each channel already has its own
            # reconnect/backoff loop, so a failed channel is logged and the
            # surviving channels keep running until stop_event fires.
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results):
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, BaseException):
                    logger.warning(
                        "Summary engine task %s exited with error; other channels continue.",
                        task.get_name(),
                        exc_info=result,
                    )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await self.close()

    async def close(self) -> None:
        if self._summary_ws is None:
            return
        try:
            await self._summary_ws.close()
        except Exception:
            pass
        self._summary_ws = None

    async def _summary_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
                break
            except TimeoutError:
                await self._publish_summary_cycle()

    async def _publish_summary_cycle(self) -> None:
        async with self._lock:
            transcript_lines = self._segment_lines
            self._segment_lines = []
            key_moments = self._all_key_moments()

        if not transcript_lines:
            return

        context_lines = self._build_transcript_context(transcript_lines)
        try:
            result = await self.client.summarize(context_lines, key_moments)
        except Exception:
            logger.warning("Summary cycle failed; skipping this cycle.", exc_info=True)
            return
        if result is None:
            return

        payload = {
            "type": "summary",
            "text": result.text.strip(),
            "key_moments": [
                moment.model_dump()
                for moment in sorted(key_moments, key=lambda item: item.timestamp_ms, reverse=True)
            ],
            "timestamp_ms": self._now_ms(),
        }
        await self._publish_payload(payload)

    def _build_transcript_context(self, transcript_lines: list[str]) -> list[str]:
        context: list[str] = []
        total_chars = 0
        for line in reversed(transcript_lines):
            total_chars += len(line)
            if total_chars > MAX_SUMMARY_CHARS:
                break
            context.append(line)
        return list(reversed(context))

    def _all_key_moments(self) -> list[KeyMoment]:
        moments: list[KeyMoment] = []
        if self._first_pain_point is not None:
            moments.append(self._first_pain_point)
        if self._longest_monologue is not None:
            moments.append(self._longest_monologue)
        moments.extend(self._phase_transitions)
        moments.extend(self._objections)
        return sorted(moments, key=lambda item: item.timestamp_ms)

    async def _consume_channel(
        self,
        channel: str,
        handler: Callable[[object], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        url = channel_ws_url(self.ws_config, channel)
        backoff = 1.0
        while not stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:
                    backoff = 1.0
                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
                        except TimeoutError:
                            continue
                        except ConnectionClosed:
                            break
                        payload = self._decode_payload(raw)
                        try:
                            await handler(payload)
                        except Exception:
                            logger.warning("Summary %s handler failed.", channel, exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if stop_event.is_set():
                    break
                logger.warning("Summary stream disconnected for %s: %s", channel, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle_pain_point(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "pain_point":
            return
        if self._first_pain_point is not None:
            return

        category = payload.get("category")
        if isinstance(category, str) and category.strip():
            description = f"Eerste pain point gedetecteerd: {category}."
        else:
            description = "Eerste pain point gedetecteerd."

        timestamp_ms = payload.get("timestamp_ms")
        if not isinstance(timestamp_ms, (int, float)):
            timestamp_ms = self._now_ms()

        async with self._lock:
            if self._first_pain_point is None:
                self._first_pain_point = KeyMoment(
                    type="pain_point",
                    timestamp_ms=int(timestamp_ms),
                    description=description,
                )

    async def _handle_talk_time(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "talk_time":
            return

        monologue_ms = payload.get("current_monologue_ms")
        if not isinstance(monologue_ms, (int, float)):
            return

        monologue_ms_int = int(monologue_ms)
        if monologue_ms_int <= self._longest_monologue_ms:
            return

        speaker = payload.get("monologue_speaker")
        if not isinstance(speaker, str) or not speaker:
            speaker = "onbekend"

        timestamp_ms = payload.get("call_duration_ms")
        if not isinstance(timestamp_ms, (int, float)):
            timestamp_ms = self._now_ms()

        description = (
            "Langste monoloog tot nu toe: "
            f"{speaker} sprak {round(monologue_ms_int / 1000, 1)}s aaneengesloten."
        )
        async with self._lock:
            if monologue_ms_int > self._longest_monologue_ms:
                self._longest_monologue_ms = monologue_ms_int
                self._longest_monologue = KeyMoment(
                    type="monologue",
                    timestamp_ms=int(timestamp_ms),
                    description=description,
                )

    async def _handle_phase(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "phase_change":
            return

        phase = payload.get("phase")
        if not isinstance(phase, str) or not phase.strip():
            return
        if phase == self._last_phase:
            return

        timestamp_ms = payload.get("timestamp_ms")
        if not isinstance(timestamp_ms, (int, float)):
            timestamp_ms = self._now_ms()

        async with self._lock:
            if phase == self._last_phase:
                return
            self._phase_transitions.append(
                KeyMoment(
                    type="phase_transition",
                    timestamp_ms=int(timestamp_ms),
                    description=f"Fase-overgang: {phase}.",
                )
            )
            self._last_phase = phase

    async def _handle_objection(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if payload.get("type") != "objection":
            return

        category = payload.get("category")
        if isinstance(category, str) and category.strip():
            description = f"Bezwaar gedetecteerd: {category}."
        else:
            description = "Bezwaar gedetecteerd."

        timestamp_ms = payload.get("timestamp_ms")
        if not isinstance(timestamp_ms, (int, float)):
            timestamp_ms = self._now_ms()

        async with self._lock:
            self._objections.append(
                KeyMoment(
                    type="objection",
                    timestamp_ms=int(timestamp_ms),
                    description=description,
                )
            )

    async def _ensure_summary_ws(self) -> websockets.ClientConnection:
        if self._summary_ws is None or getattr(self._summary_ws, "close_code", None) is not None:
            self._summary_ws = await websockets.connect(
                channel_ws_url(self.ws_config, "summary")
            )
        return self._summary_ws

    async def _publish_payload(self, payload: dict[str, object]) -> None:
        ws = await self._ensure_summary_ws()
        await ws.send(json.dumps(payload))

    @staticmethod
    def _decode_payload(raw: str | bytes) -> object:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
