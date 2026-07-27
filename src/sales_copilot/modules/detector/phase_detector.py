from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import websockets
from pydantic import BaseModel
from websockets.exceptions import ConnectionClosed

from sales_copilot.core.config import DetectorConfig, WebSocketConfig, load_yaml
from sales_copilot.core.llm_client import LLMClient
from sales_copilot.core.paths import resolve_app_path
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)

PhaseName = Literal["discovery", "pitch", "closing"]

_PHASES: set[str] = {"discovery", "pitch", "closing"}
_SYSTEM_PROMPT = (
    "You classify sales conversation phases. "
    "Classify this sales conversation phase: discovery, pitch, or closing. "
    "Return only one of these phases."
)


class PhaseClassification(BaseModel):
    phase: PhaseName


def _decode_payload(raw: str | bytes) -> object:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _extract_transcript_line(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "transcript":
        return None
    text = payload.get("text")
    speaker = payload.get("speaker")
    if not isinstance(text, str) or not text.strip():
        return None
    if isinstance(speaker, str) and speaker.strip():
        return f"{speaker}: {text.strip()}"
    return text.strip()


class PhaseLLMClient:
    def __init__(self, config: DetectorConfig) -> None:
        self._config = config
        self._provider = config.llm_provider.lower()
        self._llm = LLMClient(self._provider, timeout_ms=config.llm_timeout_ms)

    async def aclassify_phase(self, transcript_lines: list[str]) -> PhaseName | None:
        """Classify the conversation phase off the event loop, with a bounded wall-clock.

        Runs on the live coaching path, so it uses ``acreate`` to offload the blocking LLM
        call and cap it — a stalled phase request can no longer wedge the loop.
        """
        if not transcript_lines:
            return None
        try:
            result = await self._llm.acreate(
                model=self._config.llm_model,
                system_prompt=_SYSTEM_PROMPT,
                user_text=self._build_user_prompt(transcript_lines),
                response_model=PhaseClassification,
                temperature=self._config.llm_temperature,
                allow_local=True,
            )
        except Exception:
            return None
        phase = result.phase.strip().lower()
        if phase not in _PHASES:
            return None
        return phase  # type: ignore[return-value]

    @staticmethod
    def _build_user_prompt(transcript_lines: list[str]) -> str:
        # Assembled raw; the seam applies the outbound PII policy to the full user_text once.
        return "Recent transcript lines:\n" + "\n".join(f"- {line}" for line in transcript_lines)


class AutomaticPhaseDetector:
    def __init__(
        self,
        detector_config: DetectorConfig,
        ws_config: WebSocketConfig,
        *,
        llm_client: PhaseLLMClient | None = None,
        interval_seconds: int = 30,
        line_limit: int = 5,
    ) -> None:
        self._detector_config = detector_config
        self._ws_config = ws_config
        self._llm_client = llm_client or PhaseLLMClient(detector_config)
        self._interval_seconds = max(1, int(interval_seconds))
        self._line_limit = max(1, int(line_limit))
        self._recent_lines: deque[str] = deque(maxlen=self._line_limit)
        self._last_phase: PhaseName | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        transcript_url = channel_ws_url(self._ws_config, "transcript")
        phase_url = channel_ws_url(self._ws_config, "phase")
        backoff = 1.0
        while not stop_event.is_set():
            try:
                async with (
                    websockets.connect(transcript_url) as transcript_ws,
                    websockets.connect(phase_url) as phase_ws,
                ):
                    logger.info("Automatic phase detector connected.")
                    backoff = 1.0
                    consume_task = asyncio.create_task(
                        self._consume_transcripts(transcript_ws, stop_event),
                    )
                    classify_task = asyncio.create_task(
                        self._classify_loop(phase_ws, stop_event),
                    )
                    done, pending = await asyncio.wait(
                        {consume_task, classify_task},
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    for task in pending:
                        task.cancel()
                    for task in pending:
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
                    for task in done:
                        exc = task.exception()
                        if exc is not None:
                            raise exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if stop_event.is_set():
                    break
                logger.warning("Automatic phase detector disconnected: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _consume_transcripts(
        self,
        ws: websockets.ClientConnection,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.25)
            except TimeoutError:
                continue
            except ConnectionClosed:
                break
            line = _extract_transcript_line(_decode_payload(raw))
            if line:
                self._recent_lines.append(line)

    async def _classify_loop(
        self,
        phase_ws: websockets.ClientConnection,
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval_seconds)
                break
            except TimeoutError:
                await self._classify_and_emit(phase_ws)

    async def _classify_and_emit(self, phase_ws: websockets.ClientConnection) -> None:
        if not self._recent_lines:
            return
        phase = await self._llm_client.aclassify_phase(list(self._recent_lines))
        if phase is None or phase == self._last_phase:
            return
        payload = {"type": "phase_change", "phase": phase, "source": "auto"}
        await phase_ws.send(json.dumps(payload))
        self._last_phase = phase


# ---------------------------------------------------------------------------
# Trigger-based phase state machine
# Advances open → discovery → demo → close based on event counts.
# ---------------------------------------------------------------------------

TriggerPhaseName = Literal["open", "discovery", "demo", "close"]
_TRIGGER_PHASE_ORDER: list[str] = ["open", "discovery", "demo", "close"]
_DEFAULT_PHASES_PATH = resolve_app_path("config/phases.yaml")


@dataclass
class PhaseState:
    phase: str = "open"
    vraag_count: int = 0
    buying_signal_count: int = 0
    objection_count: int = 0
    objection_resolved_count: int = 0
    phase_entered_at: float = field(default_factory=time.monotonic)
    stall_emitted: bool = False


class TriggerBasedPhaseDetector:
    """Event-driven phase state machine: open → discovery → demo → close.

    Call process_event() with each detected event type. Returns a phase_change
    or phase_stalled dict when a transition occurs, None otherwise.

    Event types consumed: "vraag", "buying_signal", "objection", "objection_resolved"
    """

    def __init__(
        self,
        phases_config_path: str | Path | None = None,
        *,
        stall_seconds: float = 300.0,
    ) -> None:
        path = Path(phases_config_path) if phases_config_path else _DEFAULT_PHASES_PATH
        self._transitions = self._load_transitions(path)
        self._state = PhaseState()
        self._stall_seconds = stall_seconds

    def _load_transitions(self, path: Path) -> dict[str, list[dict]]:
        raw = load_yaml(path)
        phases = raw.get("phases", {})
        return {
            name: (cfg.get("transition_triggers") or [])
            for name, cfg in phases.items()
            if isinstance(cfg, dict)
        }

    def process_event(self, event_type: str, event_data: dict | None = None) -> dict | None:
        self._update_counters(event_type)
        result = self._check_transition()
        if result is not None:
            return result
        return self._check_stall()

    def _update_counters(self, event_type: str) -> None:
        if event_type == "vraag":
            self._state.vraag_count += 1
        elif event_type == "buying_signal":
            self._state.buying_signal_count += 1
        elif event_type == "objection":
            self._state.objection_count += 1
        elif event_type == "objection_resolved":
            self._state.objection_resolved_count += 1

    def _check_transition(self) -> dict | None:
        triggers = self._transitions.get(self._state.phase, [])
        for trigger in triggers:
            condition = (trigger.get("condition") or "").strip()
            if self._evaluate_condition(condition):
                to_phase = trigger.get("to", "")
                return self._transition_to(to_phase)
        return None

    def _evaluate_condition(self, condition: str) -> bool:
        if not condition:
            return False
        if ">=" in condition:
            left, right = condition.split(">=", 1)
            field_name = left.strip()
            try:
                threshold = int(right.strip())
            except ValueError:
                return False
            return getattr(self._state, field_name, 0) >= threshold
        if condition == "buying_signal_detected":
            return self._state.buying_signal_count >= 1
        if condition == "objection_resolved":
            return self._state.objection_resolved_count >= 1
        return False

    def _transition_to(self, to_phase: str) -> dict | None:
        try:
            current_idx = _TRIGGER_PHASE_ORDER.index(self._state.phase)
            target_idx = _TRIGGER_PHASE_ORDER.index(to_phase)
        except ValueError:
            return None
        if target_idx <= current_idx:
            return None
        old_phase = self._state.phase
        self._state.phase = to_phase
        self._state.phase_entered_at = time.monotonic()
        self._state.stall_emitted = False
        return {"type": "phase_change", "from": old_phase, "phase": to_phase, "source": "trigger"}

    def _check_stall(self) -> dict | None:
        if self._state.stall_emitted:
            return None
        elapsed = time.monotonic() - self._state.phase_entered_at
        if elapsed >= self._stall_seconds:
            self._state.stall_emitted = True
            return {
                "type": "phase_stalled",
                "phase": self._state.phase,
                "stalled_seconds": int(elapsed),
            }
        return None

    async def run(self, ws_in, ws_out) -> None:
        """Subscribe to detection events from ws_in; publish phase changes to ws_out."""
        try:
            async for raw in ws_in:
                payload = _decode_payload(raw)
                if not isinstance(payload, dict):
                    continue
                subcat = (payload.get("subcat") or "").strip()
                if not subcat:
                    continue
                result = self.process_event(event_type=subcat, event_data=payload)
                if result is not None and result.get("type") == "phase_change":
                    await ws_out.send(json.dumps({**result, "channel": "coaching"}))
        except ConnectionClosed:
            pass

    @property
    def current_phase(self) -> str:
        return self._state.phase
