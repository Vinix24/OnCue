from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field

import numpy as np
import websockets

from sales_copilot.audio.capture import AudioStream, managed_audio_stream
from sales_copilot.audio.recorder import get_active_recorder
from sales_copilot.core.config import WebSocketConfig
from sales_copilot.modules.diarizer import get_diarizer
from sales_copilot.modules.talk_time.vad import load_silero_model
from sales_copilot.modules.transcriber.backends import create_backend
from sales_copilot.modules.transcriber.backends.base import TranscriptionBackend
from sales_copilot.modules.transcriber.engine import PartialTranscriptEvent, Speaker, TranscriptEvent
from sales_copilot.modules.transcriber.normalize import (
    NormalizationLists,
    get_default_normalization_lists,
    normalize,
)
from sales_copilot.websocket.hub_auth import channel_ws_url

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DirectWhisperEngine:
    audio_stream: AudioStream
    ws_config: WebSocketConfig
    speaker: Speaker | None
    backend: TranscriptionBackend | None = None
    sample_rate: int = 16000
    min_text_length: int = 3
    silence_gap_seconds: float = 1.0
    max_buffer_seconds: float = 2.5
    min_rms: float = 0.001
    vad_threshold: float = 0.5
    partial_emit_interval: float = 0.5
    _buffer: list[np.ndarray] = field(init=False, repr=False)
    _speech_start_ms: int | None = field(init=False, default=None, repr=False)
    _cursor_ms: int = field(init=False, default=0, repr=False)
    _last_speech_loop_ts: float | None = field(init=False, default=None, repr=False)
    _last_published_text: str | None = field(init=False, default=None, repr=False)
    _ws: websockets.ClientConnection | None = field(init=False, default=None, repr=False)
    _vad_model: object | None = field(init=False, default=None, repr=False)
    _last_partial_emit_ts: float | None = field(init=False, default=None, repr=False)
    _flush_count: int = field(init=False, default=0, repr=False)
    _transcribe_count: int = field(init=False, default=0, repr=False)
    _publish_count: int = field(init=False, default=0, repr=False)
    _loop_count: int = field(init=False, default=0, repr=False)
    _diarizer: object | None = field(init=False, default=None, repr=False)
    _normalization_lists: NormalizationLists | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._buffer = []
        self._speech_start_ms = None
        self._cursor_ms = 0
        self._last_speech_loop_ts = None
        self._last_published_text = None
        self._last_partial_emit_ts = None
        self._ws = None
        self._vad_model = self._load_vad()
        self.backend = self.backend or create_backend()
        self._diarizer = get_diarizer()
        self._normalization_lists = get_default_normalization_lists()

    async def run(self, stop_event: asyncio.Event, start_event: asyncio.Event | None = None) -> None:
        with managed_audio_stream(self.audio_stream):
            try:
                assert self.backend is not None
                await self.backend.start(stop_event)
                if self._diarizer is not None:
                    await self._diarizer.load()
                await self._ensure_ws()
                if start_event is not None and not start_event.is_set():
                    logger.info("Waiting for start_call warmup before processing audio...")
                    while not stop_event.is_set() and not start_event.is_set():
                        await asyncio.sleep(0.05)
                while not stop_event.is_set():
                    self._loop_count += 1
                    frames = self.audio_stream.read()
                    if frames is None:
                        await self._ensure_ws()
                        if self._should_flush_for_silence():
                            await self._flush_buffer()
                        await asyncio.sleep(0.01)
                        continue

                    mono = self._to_mono(frames)
                    chunk_ms = int(round((len(mono) / float(self.sample_rate)) * 1000))
                    if chunk_ms <= 0:
                        chunk_ms = 1
                    self._cursor_ms += chunk_ms
                    self._record_chunk(mono)

                    if self._is_speech(mono):
                        if self._speech_start_ms is None:
                            self._speech_start_ms = max(0, self._cursor_ms - chunk_ms)
                        self._buffer.append(mono)
                        now = asyncio.get_running_loop().time()
                        self._last_speech_loop_ts = now
                        if (
                            self._last_partial_emit_ts is None
                            or (now - self._last_partial_emit_ts) >= self.partial_emit_interval
                        ):
                            self._last_partial_emit_ts = now
                            await self._publish_partial(
                                PartialTranscriptEvent(
                                    type="partial_transcript",
                                    text="",
                                    speaker=self.speaker,
                                    start_ms=self._speech_start_ms,
                                    tentative_end_ms=self._cursor_ms,
                                    is_final=False,
                                )
                            )
                    elif self._should_flush_for_silence():
                        await self._flush_buffer()

                    if self._buffer_duration_seconds() >= self.max_buffer_seconds:
                        await self._flush_buffer()
                    # Keep the loop cooperative while frames arrive continuously.
                    await asyncio.sleep(0)
            finally:
                await self._close_ws()
                if self.backend is not None:
                    await self.backend.stop()
                if self._diarizer is not None:
                    await self._diarizer.unload()

    def set_speaker(self, speaker: Speaker | None) -> None:
        self.speaker = speaker

    async def _flush_buffer(self) -> None:
        if not self._buffer:
            self._speech_start_ms = None
            self._last_partial_emit_ts = None
            return
        self._flush_count += 1
        audio = np.concatenate(self._buffer, dtype=np.float32)
        start_ms = self._speech_start_ms if self._speech_start_ms is not None else max(0, self._cursor_ms - 1)
        end_ms = self._cursor_ms
        self._buffer.clear()
        self._speech_start_ms = None
        self._last_speech_loop_ts = None
        self._last_partial_emit_ts = None

        assert self.backend is not None
        text = await self.backend.transcribe(audio)
        cleaned = text.strip()
        if self._is_hallucination(cleaned):
            return
        if len(cleaned) < self.min_text_length:
            return
        self._transcribe_count += 1

        assert self._normalization_lists is not None
        cleaned, replacements = normalize(cleaned, self._normalization_lists)
        for source, target, rule in replacements:
            logger.debug("transcript normalization: %r -> %r (rule=%s)", source, target, rule)

        if cleaned == self._last_published_text:
            return
        self._last_published_text = cleaned

        event = TranscriptEvent(
            type="transcript",
            text=cleaned,
            speaker=self.speaker,
            start_ms=start_ms,
            end_ms=end_ms,
            is_final=True,
        )
        await self._publish(event)
        self._publish_count += 1

        # Speaker diarization — shadow-mode or production, never blocks transcript delivery
        if self._diarizer is not None:
            try:
                segments = await self._diarizer.diarize_chunk(audio.tobytes(), self.sample_rate)
                await self._publish_speaker_segments(segments, start_ms, end_ms)
            except Exception:
                logger.debug("Diarizer error for chunk [%d-%d], skipping", start_ms, end_ms, exc_info=True)

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

    async def _publish_speaker_segments(self, segments: list, start_ms: int, end_ms: int) -> None:
        """Emit speaker_segments WS event alongside transcript (shadow/production mode).

        The detector only subscribes to transcript events, so this never affects detection.
        Schema: {"type": "speaker_segments", "channel": "transcript", "segments": [...]}
        """
        payload = json.dumps(
            {
                "type": "speaker_segments",
                "channel": "transcript",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "segments": [
                    {
                        "speaker_id": s.speaker_id,
                        "start_ms": s.start_ms,
                        "end_ms": s.end_ms,
                        "confidence": s.confidence,
                    }
                    for s in segments
                ],
            }
        )
        ws = await self._ensure_ws()
        if ws is None:
            return
        try:
            await ws.send(payload)
        except Exception:
            logger.debug("Failed to publish speaker_segments", exc_info=True)

    async def _ensure_ws(self) -> websockets.ClientConnection | None:
        if self._ws is not None and self._is_ws_open(self._ws):
            return self._ws
        self._ws = None
        try:
            self._ws = await websockets.connect(
                channel_ws_url(self.ws_config, "transcript"),
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            )
        except Exception as exc:
            logger.warning("Direct whisper hub connect failed: %s", exc)
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

    def _is_speech(self, chunk: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(np.square(chunk), dtype=np.float32)))
        if rms < self.min_rms:
            return False
        if self._vad_model is None:
            return True
        try:
            import torch

            audio_tensor = torch.from_numpy(chunk.astype(np.float32))
            probability = self._vad_model(audio_tensor, self.sample_rate)
            score = float(probability.item()) if hasattr(probability, "item") else float(probability)
        except Exception:
            return True
        return score >= self.vad_threshold

    def _record_chunk(self, mono: np.ndarray) -> None:
        recorder = get_active_recorder()
        if recorder is None:
            return
        label = self.speaker if isinstance(self.speaker, str) else "unknown"
        try:
            recorder.write_chunk(label, mono, self._cursor_ms)
        except Exception:
            logger.debug("AudioRecorder write_chunk failed", exc_info=True)

    def _should_flush_for_silence(self) -> bool:
        if not self._buffer:
            return False
        if self._last_speech_loop_ts is None:
            return False
        now = asyncio.get_running_loop().time()
        return (now - self._last_speech_loop_ts) >= self.silence_gap_seconds

    def _buffer_duration_seconds(self) -> float:
        if not self._buffer:
            return 0.0
        total_samples = sum(len(chunk) for chunk in self._buffer)
        return total_samples / float(self.sample_rate)

    @staticmethod
    def _to_mono(frames: np.ndarray) -> np.ndarray:
        if frames.ndim == 1:
            mono = frames.astype(np.float32)
        else:
            mono = frames[:, 0].astype(np.float32)
        return np.clip(mono, -1.0, 1.0)

    @staticmethod
    def _load_vad():
        try:
            return load_silero_model()
        except Exception:
            logger.warning("Silero VAD unavailable in direct whisper mode, falling back to RMS gating.")
            return None

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

        # Reject text that is purely punctuation/symbols/spaces.
        if not any(char.isalnum() for char in cleaned):
            return True

        # Reject standalone fillers / single common words that the model
        # produces during silence or near-silence: "ehm", "ja", "oké",
        # "dank u wel", "pagina", "oh la la".
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

        # Reject CJK-only output (Chinese/Japanese/Korean characters): in a
        # Dutch sales call this is always a hallucination from training-data
        # leakage. Allow if there is also Latin-script content.
        cjk_count = sum(
            1
            for ch in cleaned
            if "一" <= ch <= "鿿"
            or "぀" <= ch <= "ヿ"
            or "가" <= ch <= "힯"
        )
        if cjk_count > 0 and cjk_count >= sum(1 for ch in cleaned if ch.isalpha()) / 2:
            return True

        # Reject repeated-token spam like "yt yt yt yt yt" or "ja ja ja ja"
        # — Whisper enters these loops during silence.
        tokens = [t for t in re.split(r"\s+", cleaned) if t]
        if len(tokens) >= 5:
            unique_tokens = {t.casefold().rstrip(".,!?") for t in tokens}
            if len(unique_tokens) <= 2:
                return True

        # Reject very short fragments after removing non-word chars.
        word_chars = re.findall(r"\w", cleaned, flags=re.UNICODE)
        return len(word_chars) < 3
