# Module 1 — Architecture Contract (Source of Truth)
Module 1 ships **local talk-time coaching** (audio capture → VAD → talk-time state → coaching alerts) over localhost WebSockets. This contract is the source of truth for boundaries, interfaces, models, schemas, and file locations.

## Components + File Locations (must match `docs/TTD.md`)

- AudioCapture — `src/sales_copilot/audio/capture.py` (implementations in `src/sales_copilot/audio/*.py`)
- VADProcessor — `src/sales_copilot/modules/talk_time/vad.py`
- TalkTimeTracker — `src/sales_copilot/modules/talk_time/tracker.py`
- TalkTimePublisher — `src/sales_copilot/modules/talk_time/publisher.py`
- WebSocketHub — `src/sales_copilot/websocket/hub.py`

## Protocol Interfaces + Data Models (exact fields/types)

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import AsyncIterator, Literal, Protocol
import numpy as np
Speaker = Literal["self", "prospect"]
Phase = Literal["discovery", "pitch", "closing"]
@dataclass
class SpeechEvent:
    speaker: Speaker
    start_ms: int
    end_ms: int
    duration_ms: int
@dataclass
class TalkTimeState:
    phase: Phase
    rolling_self_pct: float
    rolling_prospect_pct: float
    cumulative_self_pct: float
    cumulative_prospect_pct: float
    current_monologue_ms: int
    monologue_speaker: str | None
    call_duration_ms: int
    status: Literal["green", "amber", "red"]
@dataclass
class CoachingAlert:
    type: Literal["monologue_warning", "ratio_warning"]
    message: str
    severity: Literal["amber", "red"]
    timestamp_ms: int
class AudioCapture(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def get_mic_stream(self) -> AsyncIterator[np.ndarray]: ...
    def get_system_stream(self) -> AsyncIterator[np.ndarray]: ...
class VADProcessor(Protocol):
    async def events(
        self, speaker: Speaker, stream: AsyncIterator[np.ndarray]
    ) -> AsyncIterator[SpeechEvent]: ...
class TalkTimeTracker(Protocol):
    def set_phase(self, phase: Phase) -> None: ...
    def ingest(self, event: SpeechEvent) -> None: ...
    def get_state(self, call_duration_ms: int) -> TalkTimeState: ...
    def maybe_alert(self, call_duration_ms: int) -> CoachingAlert | None: ...
class TalkTimePublisher(Protocol):
    async def publish_state(self, state: TalkTimeState) -> None: ...
    async def publish_alert(self, alert: CoachingAlert) -> None: ...
class WebSocketHub(Protocol):
    host: str  # default "127.0.0.1"
    port: int  # default 8760
```

## WebSocket Message Schemas (exact JSON)

### `/ws/talk-time`

```json
{
    "type": "talk_time",
    "rolling_self_pct": 0.35,
    "rolling_prospect_pct": 0.65,
    "cumulative_self_pct": 0.38,
    "cumulative_prospect_pct": 0.62,
    "current_monologue_ms": 0,
    "monologue_speaker": null,
    "call_duration_ms": 754000,
    "phase": "discovery",
    "status": "green"
}
```

### `/ws/coaching`

```json
{
    "type": "coaching_alert",
    "alert_type": "monologue_warning | ratio_warning",
    "message": "Time to listen",
    "severity": "amber | red",
    "timestamp_ms": 76000
}
```

Mapping: `CoachingAlert.type` → WebSocket `alert_type`.

### `/ws/phase` (dashboard → backend)

```json
{
    "type": "phase_change",
    "phase": "discovery | pitch | closing"
}
```

## Module 1 Ship Gate (quality criteria)

Module 1 is shippable when:
- This contract matches `docs/TTD.md` (interfaces, models, file locations, WS schemas)
- Hub binds to `127.0.0.1` by default (localhost only)
- `TalkTimeState` is emitted to `/ws/talk-time` every 5 seconds
- `CoachingAlert` is emitted to `/ws/coaching` on threshold breach
- Quality gate passes: `ruff check src/` and `python -m pytest tests/ -v`

## Non-goals / Out of Scope

- Transcription (`/ws/transcript`), pain points (`/ws/pain-points`), slide control (`/ws/slide-control`), persistence/DB schemas, any non-local networking
