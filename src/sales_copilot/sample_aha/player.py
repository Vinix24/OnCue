"""Fast, hardware-free replay of a curated Dutch sales call.

The player reads a JSON transcript sample and publishes transcript, talk-time
and suggestion events to the running WebSocket hub. It is designed for the
"Toon me wat het doet" first-screen button: no microphone, no Whisper model
and no cloud LLM key are required.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import websockets

from sales_copilot.core.config import WebSocketConfig, env, env_float
from sales_copilot.core.paths import resolve_app_resource
from sales_copilot.websocket.hub_auth import get_hub_token

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_PATH = "data/samples/sample-aha.json"
DEFAULT_SPEED = 1.0


def default_sample_path() -> str:
    configured = env("SAMPLE_AHA_PATH")
    if configured:
        return configured
    return str(resolve_app_resource(DEFAULT_SAMPLE_PATH))


def default_speed() -> float:
    value = env_float("SAMPLE_AHA_SPEED", DEFAULT_SPEED)
    return value if value is not None and value > 0 else DEFAULT_SPEED


def _channel_url(host: str, port: int, channel: str) -> str:
    return f"ws://{host}:{port}/ws/{channel}"


def _auth_headers(token: str) -> dict[str, str]:
    credentials = base64.b64encode(f"token:{token}".encode()).decode("ascii")
    return {"Authorization": f"Basic {credentials}"}


def _talk_time_payload(
    self_ms: int,
    prospect_ms: int,
    call_duration_ms: int,
    phase: str = "discovery",
) -> dict[str, Any]:
    total_ms = max(1, self_ms + prospect_ms)
    cumulative_self = self_ms / total_ms
    cumulative_prospect = prospect_ms / total_ms
    return {
        "type": "talk_time",
        "rolling_self_pct": cumulative_self,
        "rolling_prospect_pct": cumulative_prospect,
        "cumulative_self_pct": cumulative_self,
        "cumulative_prospect_pct": cumulative_prospect,
        "current_monologue_ms": 0,
        "monologue_speaker": None,
        "call_duration_ms": call_duration_ms,
        "phase": phase,
        "status": "green",
    }


class SampleAhaPlayer:
    """Plays a curated transcript through the hub at an accelerated pace.

    Runs in its own thread with a private event loop so it never blocks the
    uvicorn/orchestrator loop.
    """

    def __init__(
        self,
        *,
        sample_path: str | Path,
        host: str = "127.0.0.1",
        port: int = 8760,
        token: str | None = None,
        speed: float = 1.0,
    ) -> None:
        self._sample_path = Path(sample_path)
        self._host = host
        self._port = port
        self._token = token or get_hub_token()
        self._speed = max(0.1, float(speed))
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_alive:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="sample-aha-player",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        try:
            asyncio.run(self._play())
        except Exception:
            logger.exception("Sample-aha player crashed")
        finally:
            self._end_call()

    async def _play(self) -> None:
        sample = self._load_sample()
        events = sample.get("events", [])
        suggestions = sample.get("suggestions", [])

        async with AsyncExitStack() as stack:
            auth_headers = _auth_headers(self._token)
            transcript_ws = await stack.enter_async_context(
                websockets.connect(
                    _channel_url(self._host, self._port, "transcript"),
                    additional_headers=auth_headers,
                )
            )
            talk_time_ws = await stack.enter_async_context(
                websockets.connect(
                    _channel_url(self._host, self._port, "talk-time"),
                    additional_headers=auth_headers,
                )
            )
            suggestions_ws = await stack.enter_async_context(
                websockets.connect(
                    _channel_url(self._host, self._port, "suggestions"),
                    additional_headers=auth_headers,
                )
            )

            self_ms = 0
            prospect_ms = 0
            call_duration_ms = 0
            suggestion_index = 0

            for idx, event in enumerate(events):
                if self._stop_event.is_set():
                    break

                delay_s = self._scaled_delay(event.get("delay_ms", 0))
                if delay_s > 0:
                    await self._interruptible_sleep(delay_s)

                event_type = event.get("type")
                if event_type == "transcript":
                    payload = {
                        "type": "transcript",
                        "text": str(event.get("text", "")).strip(),
                        "speaker": event.get("speaker", "prospect"),
                        "start_ms": int(event.get("start_ms", 0)),
                        "end_ms": int(event.get("end_ms", 0)),
                        "is_final": bool(event.get("is_final", True)),
                    }
                    await transcript_ws.send(json.dumps(payload))

                    duration = int(event.get("end_ms", 0)) - int(event.get("start_ms", 0))
                    duration = max(0, duration)
                    if payload["speaker"] == "self":
                        self_ms += duration
                    else:
                        prospect_ms += duration
                    call_duration_ms += duration

                    await talk_time_ws.send(
                        json.dumps(_talk_time_payload(self_ms, prospect_ms, call_duration_ms))
                    )

                while (
                    suggestion_index < len(suggestions)
                    and int(suggestions[suggestion_index].get("after_event_index", 0)) <= idx
                ):
                    suggestion = suggestions[suggestion_index]
                    questions = suggestion.get("questions") or [suggestion.get("text", "")]
                    if isinstance(questions, str):
                        questions = [questions]
                    questions = [q for q in questions if isinstance(q, str) and q.strip()]
                    if questions:
                        await suggestions_ws.send(
                            json.dumps(
                                {
                                    "type": "suggestion",
                                    "questions": questions,
                                    "timestamp_ms": call_duration_ms,
                                }
                            )
                        )
                    suggestion_index += 1

            # Leave a short moment for the detector to emit pain points/objections
            # before the call is ended.
            await self._interruptible_sleep(min(1.5, 1.5 / self._speed))

    def _load_sample(self) -> dict[str, Any]:
        if not self._sample_path.exists():
            raise FileNotFoundError(f"Sample not found: {self._sample_path}")
        with self._sample_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("Sample file must contain a JSON object")
        return data

    def _scaled_delay(self, delay_ms: Any) -> float:
        try:
            ms = float(delay_ms)
        except (TypeError, ValueError):
            ms = 0.0
        return max(0.0, ms / 1000.0) / self._speed

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small slices so stop() is responsive."""
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            if self._stop_event.is_set():
                break
            remaining = deadline - asyncio.get_running_loop().time()
            await asyncio.sleep(min(0.1, remaining))

    def _end_call(self) -> None:
        # Imported here, not at module scope: httpx is not a base/windows
        # dependency, and this demo player (no mic, no Whisper model, no cloud
        # LLM key required) must stay importable without it. Only this final
        # fire-and-forget POST needs the client.
        import httpx

        try:
            httpx.post(
                f"http://{self._host}:{self._port}/api/end-call",
                headers={"X-Sales-Copilot-Token": self._token},
                timeout=5.0,
            )
        except Exception:
            logger.warning("Sample-aha player could not end call automatically")


def start_player_from_env(speed: float | None = None) -> SampleAhaPlayer:
    """Convenience factory using the default hub config and sample path."""
    cfg = WebSocketConfig.from_env()
    return SampleAhaPlayer(
        sample_path=default_sample_path(),
        host=cfg.host,
        port=cfg.port,
        token=get_hub_token(),
        speed=speed if speed is not None else default_speed(),
    )
