from __future__ import annotations

import asyncio
import logging
from collections import deque

import numpy as np

from sales_copilot.audio.capture import AudioStream, managed_audio_stream
from sales_copilot.audio.recorder import get_active_recorder
from sales_copilot.modules.talk_time.vad import load_silero_model
from sales_copilot.modules.transcriber.engine import Speaker
from sales_copilot.modules.transcriber.inference_queue import InferenceQueueItem, SharedInferenceQueue

logger = logging.getLogger(__name__)


class TurnState:
    """Shared, single-event-loop turn tracker for cross-stream turn-change flushing.

    Both AudioBufferers (self + prospect) run as coroutines on the same asyncio
    loop, so plain attribute reads/writes are race-free -- no threads, no locks.
    Each bufferer marks its own speaker active/inactive; the other reads it to
    detect a genuine turn hand-off (the far speaker taking the floor), which is a
    much better utterance boundary than raw silence and keeps latency low without
    inflating the silence gap.
    """

    def __init__(self) -> None:
        self._onset_ts: dict[str, float] = {}

    def set_active(self, speaker: str, onset_ts: float) -> None:
        if not self._onset_ts.get(speaker):
            self._onset_ts[speaker] = onset_ts

    def set_inactive(self, speaker: str) -> None:
        self._onset_ts[speaker] = 0.0

    def other_sustained(self, my_speaker: str, now: float, min_dur_s: float) -> bool:
        """True if another speaker has held the floor continuously for min_dur_s.

        ``min_dur_s`` is the backchannel guard: a quick "ja"/"hm" from the far
        side must not trigger a turn-change flush of this speaker's segment.
        """
        for speaker, onset in self._onset_ts.items():
            if speaker != my_speaker and onset and (now - onset) >= min_dur_s:
                return True
        return False


class AudioBufferer:
    """Segments a live audio stream into utterance-sized chunks for Whisper.

    The core correctness rule: once speech starts, buffer the audio *continuously*
    until flush -- including sub-VAD dips inside the utterance -- with a pre-roll
    prefix and a hangover tail. Feeding Whisper only VAD-positive frames produces
    onset-clipped, discontinuous audio on which it hallucinates (often in English
    on Dutch input). A minimum-segment floor stops sub-second fragments from being
    transcribed in isolation. Flush boundaries, in order of preference: a turn
    hand-off (the other speaker takes the floor), end-of-utterance silence, and
    finally soft/hard latency caps for long monologues.
    """

    def __init__(
        self,
        audio_stream: AudioStream,
        queue: SharedInferenceQueue,
        priority: int,
        speaker: Speaker,
        sample_rate: int = 16000,
        max_buffer_seconds: float = 4.0,
        max_buffer_hard_seconds: float = 7.0,
        silence_gap_seconds: float = 1.0,
        min_segment_seconds: float = 1.2,
        long_silence_escape_seconds: float = 2.5,
        pre_roll_ms: int = 500,
        hangover_ms: int = 250,
        vad_enter: float = 0.5,
        vad_exit: float = 0.35,
        min_rms: float = 0.005,
        turn_backchannel_guard_ms: int = 700,
        transcribe_live: bool = True,
        turn_state: TurnState | None = None,
    ) -> None:
        self.audio_stream = audio_stream
        self.queue = queue
        self.priority = priority
        self.speaker = speaker
        self.sample_rate = sample_rate
        self.max_buffer_seconds = max_buffer_seconds
        self.max_buffer_hard_seconds = max(max_buffer_hard_seconds, max_buffer_seconds)
        self.silence_gap_seconds = max(0.1, silence_gap_seconds)
        self.min_segment_seconds = max(0.0, min_segment_seconds)
        self.long_silence_escape_seconds = max(self.silence_gap_seconds, long_silence_escape_seconds)
        self.hangover_s = max(0.0, hangover_ms / 1000.0)
        self.vad_enter = vad_enter
        self.vad_exit = min(vad_exit, vad_enter)
        self.min_rms = min_rms
        self.turn_backchannel_guard_s = max(0.0, turn_backchannel_guard_ms / 1000.0)
        self.transcribe_live = transcribe_live
        self.turn_state = turn_state

        self._buffer: list[np.ndarray] = []
        self._prehistory: deque[np.ndarray] = deque()
        self._prehistory_samples: int = 0
        self._preroll_samples: int = int(self.sample_rate * max(0, pre_roll_ms) / 1000)
        self._in_speech: bool = False
        self._seg_has_speech: bool = False
        self._speech_start_ms: int | None = None
        self._cursor_ms: int = 0
        self._last_voice_ts: float | None = None
        self._vad_model: object | None = self._load_vad()

    async def run(self, stop_event: asyncio.Event, start_event: asyncio.Event | None = None) -> None:
        with managed_audio_stream(self.audio_stream):
            if start_event is not None and not start_event.is_set():
                logger.info(
                    "AudioBufferer speaker=%s waiting for start_event before processing audio.",
                    self.speaker,
                )
                while not stop_event.is_set() and not start_event.is_set():
                    await asyncio.sleep(0.05)
            while not stop_event.is_set():
                frames = self.audio_stream.read()
                now = asyncio.get_running_loop().time()
                if frames is None:
                    # A None read (buffer underrun or stream EOF) carries no VAD signal
                    # of its own, but a genuinely ended utterance still needs to decay
                    # out of the in-speech state on the same hangover timeout as a
                    # low-VAD chunk would -- otherwise a stream that stops delivering
                    # frames mid-utterance never reaches the normal silence/escape
                    # flush paths and sits on the buffer until the hard cap.
                    self._decay_speech_state(now)
                    await self._maybe_flush(now)
                    await asyncio.sleep(0.01)
                    continue

                mono = self._to_mono(frames)
                chunk_ms = max(1, int(round((len(mono) / float(self.sample_rate)) * 1000)))
                self._cursor_ms += chunk_ms
                self._record_chunk(mono)

                if self._is_voiced(mono):
                    if not self._in_speech:
                        self._begin_segment(chunk_ms)
                    self._buffer.append(mono)
                    self._seg_has_speech = True
                    self._last_voice_ts = now
                    if self.turn_state is not None:
                        self.turn_state.set_active(self.speaker, now)
                elif self._in_speech:
                    # Mid-utterance dip: keep the tail (hangover) so the last word is
                    # not clipped. Past the hangover the utterance is considered ended.
                    if self._last_voice_ts is not None and (now - self._last_voice_ts) <= self.hangover_s:
                        self._buffer.append(mono)
                    else:
                        self._decay_speech_state(now)
                else:
                    self._push_prehistory(mono)

                await self._maybe_flush(now)
                await asyncio.sleep(0)

    def _decay_speech_state(self, now: float) -> None:
        """End the in-speech state once the hangover window has elapsed.

        Called both from a low-VAD chunk past its hangover and from a None
        read, so a stream that stops delivering frames entirely still times
        out of speech instead of only ever flushing via the hard cap.
        """
        if not self._in_speech:
            return
        if self._last_voice_ts is not None and (now - self._last_voice_ts) <= self.hangover_s:
            return
        self._in_speech = False
        if self.turn_state is not None:
            self.turn_state.set_inactive(self.speaker)

    def _begin_segment(self, chunk_ms: int) -> None:
        # Prepend the pre-roll ring so the onset (first word of an objection) is not
        # clipped by the VAD's reaction delay.
        self._buffer.extend(self._prehistory)
        preroll_samples = self._prehistory_samples
        self._prehistory.clear()
        self._prehistory_samples = 0
        self._in_speech = True
        preroll_ms = int(round((preroll_samples / float(self.sample_rate)) * 1000))
        if self._speech_start_ms is None:
            self._speech_start_ms = max(0, self._cursor_ms - chunk_ms - preroll_ms)

    def _push_prehistory(self, mono: np.ndarray) -> None:
        if self._preroll_samples <= 0:
            return
        self._prehistory.append(mono)
        self._prehistory_samples += len(mono)
        while self._prehistory_samples > self._preroll_samples and len(self._prehistory) > 1:
            dropped = self._prehistory.popleft()
            self._prehistory_samples -= len(dropped)

    async def _maybe_flush(self, now: float) -> None:
        if not self._seg_has_speech or not self._buffer:
            return
        dur = self._buffer_duration_seconds()
        silent_for = (now - self._last_voice_ts) if self._last_voice_ts is not None else 0.0

        # Hard latency cap: flush even mid-speech during a very long monologue.
        if dur >= self.max_buffer_hard_seconds:
            await self._flush_buffer(keep_speaking=self._in_speech)
            return
        # Soft cap: past the cap, flush at the first real pause so we do not cut a word.
        if dur >= self.max_buffer_seconds and not self._in_speech and silent_for >= 0.3:
            await self._flush_buffer(keep_speaking=False)
            return
        # Turn hand-off: the other speaker took the floor past the backchannel guard.
        if (
            self.turn_state is not None
            and dur >= self.min_segment_seconds
            and self.turn_state.other_sustained(self.speaker, now, self.turn_backchannel_guard_s)
        ):
            await self._flush_buffer(keep_speaking=self._in_speech)
            return
        # Normal end of utterance: enough silence AND enough accumulated speech.
        if not self._in_speech and silent_for >= self.silence_gap_seconds and dur >= self.min_segment_seconds:
            await self._flush_buffer(keep_speaking=False)
            return
        # Escape hatch: a short segment that never grew past the floor; flush it after a
        # long silence anyway (the downstream filler-filter drops junk like "ja"/"hm").
        if not self._in_speech and silent_for >= self.long_silence_escape_seconds:
            await self._flush_buffer(keep_speaking=False)
            return

    async def _flush_buffer(self, keep_speaking: bool) -> None:
        if not self._buffer:
            self._reset_segment(keep_speaking)
            return
        audio = np.concatenate(self._buffer, dtype=np.float32)
        start_ms = self._speech_start_ms if self._speech_start_ms is not None else max(0, self._cursor_ms - 1)
        end_ms = self._cursor_ms
        speech_start_ms = self._speech_start_ms
        self._reset_segment(keep_speaking)

        if not self.transcribe_live:
            return

        now_ms = int(asyncio.get_running_loop().time() * 1000)
        item = InferenceQueueItem(
            priority=self.priority,
            enqueued_at_ms=now_ms,
            audio=audio,
            speaker=self.speaker,
            start_ms=start_ms,
            end_ms=end_ms,
            speech_start_ms=speech_start_ms,
        )
        await self.queue.put(item)

    def _reset_segment(self, keep_speaking: bool) -> None:
        self._buffer.clear()
        if keep_speaking:
            # Flushed mid-utterance (hard/soft cap or turn-flush while still talking):
            # start the next segment at the cursor and stay in speech so continuing
            # audio keeps accumulating without a gap. seg_has_speech stays True --
            # this is a continuation of the same utterance, not a fresh segment, so
            # a hangover-only chunk arriving before the next voiced frame must still
            # be eligible for _maybe_flush's cap checks instead of being silently
            # stranded in the buffer.
            self._speech_start_ms = self._cursor_ms
        else:
            self._seg_has_speech = False
            self._in_speech = False
            self._speech_start_ms = None
            self._last_voice_ts = None

    def _is_voiced(self, chunk: np.ndarray) -> bool:
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
        # Hysteresis: a higher bar to enter speech than to stay in it, so brief dips
        # inside a word do not end the segment and noise does not start one.
        threshold = self.vad_exit if self._in_speech else self.vad_enter
        return score >= threshold

    def _record_chunk(self, mono: np.ndarray) -> None:
        recorder = get_active_recorder()
        if recorder is None:
            return
        label = self.speaker if isinstance(self.speaker, str) else "unknown"
        try:
            recorder.write_chunk(label, mono, self._cursor_ms)
        except Exception:
            logger.debug("AudioRecorder write_chunk failed", exc_info=True)

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
    def _load_vad() -> object | None:
        try:
            return load_silero_model()
        except Exception:
            logger.warning("Silero VAD unavailable in AudioBufferer, falling back to RMS gating.")
            return None
