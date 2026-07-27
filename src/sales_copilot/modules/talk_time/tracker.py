from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sales_copilot.core.config import TalkTimeConfig

Speaker = Literal["self", "prospect"]
Phase = Literal["discovery", "pitch", "closing"]
Status = Literal["green", "amber", "red"]


@dataclass(frozen=True)
class SpeechEvent:
    speaker: Speaker
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True)
class TalkTimeState:
    phase: Phase
    rolling_self_pct: float
    rolling_prospect_pct: float
    cumulative_self_pct: float
    cumulative_prospect_pct: float
    current_monologue_ms: int
    monologue_speaker: Speaker | None
    call_duration_ms: int
    status: Status

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "talk_time",
            "rolling_self_pct": self.rolling_self_pct,
            "rolling_prospect_pct": self.rolling_prospect_pct,
            "cumulative_self_pct": self.cumulative_self_pct,
            "cumulative_prospect_pct": self.cumulative_prospect_pct,
            "current_monologue_ms": self.current_monologue_ms,
            "monologue_speaker": self.monologue_speaker,
            "call_duration_ms": self.call_duration_ms,
            "phase": self.phase,
            "status": self.status,
        }


@dataclass(frozen=True)
class CoachingAlert:
    alert_type: Literal["monologue_warning", "ratio_warning"]
    message: str
    severity: Status
    timestamp_ms: int

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "coaching_alert",
            "alert_type": self.alert_type,
            "message": self.message,
            "severity": self.severity,
            "timestamp_ms": self.timestamp_ms,
        }


class TalkTimeTracker:
    def __init__(self, config: TalkTimeConfig | None = None) -> None:
        self._config = config or TalkTimeConfig()
        self._phase: Phase = "discovery"
        self._events: list[SpeechEvent] = []
        self._cumulative_self_ms = 0
        self._cumulative_prospect_ms = 0
        self._call_start_ms: int | None = None
        self._monologue_ms = 0
        self._monologue_speaker: Speaker | None = None
        self._last_event_end_ms: int | None = None
        self._last_event_speaker: Speaker | None = None
        self._last_monologue_alert_ms: int | None = None
        self._last_ratio_alert_ms: int | None = None

    def set_phase(self, phase: Phase) -> None:
        if phase not in ("discovery", "pitch", "closing"):
            raise ValueError("phase must be discovery, pitch, or closing")
        self._phase = phase

    def mark_call_started(self, now_ms: int) -> None:
        self._call_start_ms = int(now_ms)

    def reset(self) -> None:
        self._events.clear()
        self._cumulative_self_ms = 0
        self._cumulative_prospect_ms = 0
        self._call_start_ms = None
        self._monologue_ms = 0
        self._monologue_speaker = None
        self._last_event_end_ms = None
        self._last_event_speaker = None
        self._last_monologue_alert_ms = None
        self._last_ratio_alert_ms = None

    def mark_call_ended(self) -> None:
        self.reset()

    def record_speech(self, speaker: Speaker, start_ms: int, end_ms: int) -> SpeechEvent:
        if speaker not in ("self", "prospect"):
            raise ValueError("speaker must be 'self' or 'prospect'")
        if end_ms < start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")

        event = SpeechEvent(speaker=speaker, start_ms=int(start_ms), end_ms=int(end_ms))
        self._events.append(event)
        if self._call_start_ms is None or event.start_ms < self._call_start_ms:
            self._call_start_ms = event.start_ms

        duration = event.duration_ms
        if speaker == "self":
            self._cumulative_self_ms += duration
        else:
            self._cumulative_prospect_ms += duration

        if self._last_event_speaker == speaker and self._last_event_end_ms is not None:
            if event.start_ms <= self._last_event_end_ms:
                self._monologue_ms += duration
            else:
                self._monologue_ms = duration
        else:
            self._monologue_ms = duration

        self._monologue_speaker = speaker
        self._last_event_end_ms = event.end_ms
        self._last_event_speaker = speaker
        return event

    def get_state(self, now_ms: int) -> TalkTimeState:
        rolling_self_ms, rolling_prospect_ms = self._rolling_durations(now_ms)
        rolling_total = rolling_self_ms + rolling_prospect_ms
        cumulative_total = self._cumulative_self_ms + self._cumulative_prospect_ms

        rolling_self_pct = rolling_self_ms / rolling_total if rolling_total else 0.0
        rolling_prospect_pct = 1.0 - rolling_self_pct if rolling_total else 0.0
        cumulative_self_pct = self._cumulative_self_ms / cumulative_total if cumulative_total else 0.0
        cumulative_prospect_pct = 1.0 - cumulative_self_pct if cumulative_total else 0.0

        call_duration_ms = self._call_duration(now_ms)
        current_monologue_ms, monologue_speaker = self._current_monologue(now_ms)

        status: Status = "green"
        if current_monologue_ms >= self._config.monologue_warning_seconds * 1000:
            status = "red"
        elif rolling_total:
            target_self = self._target_ratio()
            diff = abs(rolling_self_pct - target_self)
            if diff >= self._config.ratio_red_threshold:
                status = "red"
            elif diff >= self._config.ratio_amber_threshold:
                status = "amber"

        return TalkTimeState(
            phase=self._phase,
            rolling_self_pct=rolling_self_pct,
            rolling_prospect_pct=rolling_prospect_pct,
            cumulative_self_pct=cumulative_self_pct,
            cumulative_prospect_pct=cumulative_prospect_pct,
            current_monologue_ms=current_monologue_ms,
            monologue_speaker=monologue_speaker,
            call_duration_ms=call_duration_ms,
            status=status,
        )

    def check_alerts(self, now_ms: int) -> CoachingAlert | None:
        state = self.get_state(now_ms)
        if state.current_monologue_ms == 0 or state.monologue_speaker is None:
            self._last_monologue_alert_ms = None

        if state.current_monologue_ms >= self._config.monologue_warning_seconds * 1000:
            if self._should_emit(self._last_monologue_alert_ms, now_ms, 20000):
                self._last_monologue_alert_ms = now_ms
                return CoachingAlert(
                    alert_type="monologue_warning",
                    message="Time to listen",
                    severity="red",
                    timestamp_ms=now_ms,
                )

        if not (state.rolling_self_pct or state.rolling_prospect_pct):
            self._last_ratio_alert_ms = None
            return None

        target_self = self._target_ratio()
        diff = abs(state.rolling_self_pct - target_self)
        if diff < self._config.ratio_amber_threshold:
            self._last_ratio_alert_ms = None
            return None

        severity: Status = "red" if diff >= self._config.ratio_red_threshold else "amber"
        if not self._should_emit(
            self._last_ratio_alert_ms,
            now_ms,
            self._config.coaching_update_interval_ms,
        ):
            return None

        self._last_ratio_alert_ms = now_ms
        return CoachingAlert(
            alert_type="ratio_warning",
            message="Adjust talk-time ratio",
            severity=severity,
            timestamp_ms=now_ms,
        )

    def get_post_call_data(self) -> list[dict[str, object]]:
        return [
            {
                "speaker": event.speaker,
                "start_ms": event.start_ms,
                "end_ms": event.end_ms,
                "duration_ms": event.duration_ms,
            }
            for event in self._events
        ]

    def _rolling_durations(self, now_ms: int) -> tuple[int, int]:
        window_ms = self._config.rolling_window_seconds * 1000
        window_start = now_ms - window_ms
        self_ms = 0
        prospect_ms = 0
        for event in self._events:
            overlap = self._overlap(event, window_start, now_ms)
            if overlap <= 0:
                continue
            if event.speaker == "self":
                self_ms += overlap
            else:
                prospect_ms += overlap
        return self_ms, prospect_ms

    @staticmethod
    def _overlap(event: SpeechEvent, window_start: int, window_end: int) -> int:
        start = max(event.start_ms, window_start)
        end = min(event.end_ms, window_end)
        return max(0, end - start)

    def _call_duration(self, now_ms: int) -> int:
        if self._call_start_ms is None:
            return 0
        return max(0, int(now_ms) - self._call_start_ms)

    def _current_monologue(self, now_ms: int) -> tuple[int, Speaker | None]:
        if self._last_event_end_ms is None or now_ms > self._last_event_end_ms:
            return 0, None
        return self._monologue_ms, self._monologue_speaker

    def _target_ratio(self) -> float:
        if self._phase == "pitch":
            return self._config.pitch_target_self
        if self._phase == "closing":
            return self._config.closing_target_self
        return self._config.discovery_target_self

    @staticmethod
    def _should_emit(last_ms: int | None, now_ms: int, interval_ms: int) -> bool:
        if last_ms is None:
            return True
        return now_ms - last_ms >= interval_ms
