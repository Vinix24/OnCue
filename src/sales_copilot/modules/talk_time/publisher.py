from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import websockets

from sales_copilot.core.config import TalkTimeConfig, WebSocketConfig
from sales_copilot.modules.talk_time.tracker import TalkTimeTracker
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)


class TalkTimePublisher:
    def __init__(
        self,
        tracker: TalkTimeTracker,
        *,
        config: TalkTimeConfig | None = None,
        ws_config: WebSocketConfig | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._tracker = tracker
        self._config = config or TalkTimeConfig()
        self._ws_config = ws_config or WebSocketConfig()
        self._now_ms = now_ms or self._default_now_ms

        self._talk_time_ws: websockets.ClientConnection | None = None
        self._coaching_ws: websockets.ClientConnection | None = None
        self._phase_ws: websockets.ClientConnection | None = None
        self._phase_task: asyncio.Task[None] | None = None
        self._start_time = time.monotonic()

    async def __aenter__(self) -> TalkTimePublisher:
        self._talk_time_ws = await websockets.connect(self._channel_url("talk-time"))
        self._coaching_ws = await websockets.connect(self._channel_url("coaching"))
        self._phase_ws = await websockets.connect(self._channel_url("phase"))
        self._phase_task = asyncio.create_task(self._listen_for_phase_changes())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._phase_task is not None:
            self._phase_task.cancel()
            try:
                await self._phase_task
            except asyncio.CancelledError:
                pass

        await self._close_ws(self._talk_time_ws)
        await self._close_ws(self._coaching_ws)
        await self._close_ws(self._phase_ws)

    async def run(self) -> None:
        interval = max(1, int(self._config.coaching_update_interval_ms)) / 1000.0
        while True:
            now_ms = self._now_ms()
            state = self._tracker.get_state(now_ms)
            await self._send_json(self._talk_time_ws, state.to_dict())

            alert = self._tracker.check_alerts(now_ms)
            if alert is not None:
                await self._send_json(self._coaching_ws, alert.to_dict())

            await asyncio.sleep(interval)

    def build_snapshot_payload(self, now_ms: int) -> dict[str, object]:
        state = self._tracker.get_state(now_ms)
        return {
            "type": "talk_time_snapshot",
            "call_duration_ms": state.call_duration_ms,
            "rolling_self_pct": state.rolling_self_pct,
            "rolling_prospect_pct": state.rolling_prospect_pct,
            "cumulative_self_pct": state.cumulative_self_pct,
            "cumulative_prospect_pct": state.cumulative_prospect_pct,
            "status": state.status,
            "phase": state.phase,
            "monologue_ms": state.current_monologue_ms,
            "monologue_speaker": state.monologue_speaker,
        }

    async def publish_snapshot(self, now_ms: int) -> None:
        payload = self.build_snapshot_payload(now_ms)
        logger.debug(
            "Publishing talk_time_snapshot: duration_ms=%d rolling_self_pct=%.4f rolling_prospect_pct=%.4f",
            int(payload["call_duration_ms"]),
            float(payload["rolling_self_pct"]),
            float(payload["rolling_prospect_pct"]),
        )
        await self._send_json(self._talk_time_ws, payload)

    async def publish_alert_if_needed(self, now_ms: int) -> None:
        alert = self._tracker.check_alerts(now_ms)
        if alert is None:
            return
        await self._send_json(self._coaching_ws, alert.to_dict())

    def _channel_url(self, channel: str) -> str:
        return channel_ws_url(self._ws_config, channel)

    async def _listen_for_phase_changes(self) -> None:
        if self._phase_ws is None:
            return
        while True:
            raw = await self._phase_ws.recv()
            payload = self._decode_payload(raw)
            if isinstance(payload, dict):
                self._handle_phase_payload(payload)

    def _handle_phase_payload(self, payload: dict) -> None:
        if payload.get("type") != "phase_change":
            return
        phase = payload.get("phase")
        if phase in {"discovery", "pitch", "closing"}:
            self._tracker.set_phase(phase)

    def _default_now_ms(self) -> int:
        return int((time.monotonic() - self._start_time) * 1000)

    @staticmethod
    def _decode_payload(raw: str | bytes) -> object:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    @staticmethod
    async def _close_ws(ws: websockets.ClientConnection | None) -> None:
        if ws is None:
            return
        try:
            await ws.close()
        except Exception:
            pass

    @staticmethod
    async def _send_json(
        ws: websockets.ClientConnection | None,
        payload: dict[str, object],
    ) -> None:
        if ws is None:
            raise RuntimeError("WebSocket connection not initialized")
        await ws.send(json.dumps(payload))
