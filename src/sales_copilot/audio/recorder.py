"""Per-session audio recorder that writes one WAV per labelled stream.

The orchestrator owns the active recorder for the lifetime of a call. Audio
producers (transcriber, talk-time) push chunks to the active recorder via
``get_active_recorder``. Buffers are flushed to disk every ``flush_seconds``
seconds (default 5) to bound memory while keeping write rate low.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import stat
import threading
import wave
from pathlib import Path

import numpy as np

from sales_copilot.core.paths import resolve_app_path

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


class AudioRecorder:
    def __init__(
        self,
        session_id: str,
        output_dir: str | Path,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        flush_seconds: float = 5.0,
    ) -> None:
        self._session_id = session_id
        self._dir = Path(output_dir) / session_id
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        self._flush_ms = int(max(1.0, flush_seconds) * 1000)
        self._lock = threading.Lock()
        self._files: dict[str, wave.Wave_write] = {}
        self._buffers: dict[str, list[np.ndarray]] = {}
        self._stream_labels: list[str] = []
        self._last_flush_ms: dict[str, int] = {}
        self._frames_written: dict[str, int] = {}
        self._started_at = _now_iso()
        self._ended_at: str | None = None
        self._closed = False
        self._dir.mkdir(parents=True, exist_ok=True)
        self._dir.chmod(stat.S_IRWXU)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def directory(self) -> Path:
        return self._dir

    def write_chunk(
        self,
        stream_label: str,
        samples: np.ndarray,
        timestamp_ms: int,
    ) -> None:
        if self._closed:
            return
        if samples is None or len(samples) == 0:
            return
        with self._lock:
            if self._closed:
                return
            if stream_label not in self._files:
                self._open_stream_locked(stream_label)
            mono = self._to_mono(samples)
            self._buffers[stream_label].append(mono)
            last = self._last_flush_ms.get(stream_label, 0)
            if timestamp_ms - last >= self._flush_ms:
                self._flush_label_locked(stream_label)
                self._last_flush_ms[stream_label] = timestamp_ms

    def finalize(self, *, silent: bool = False) -> Path:
        """Flush, close, and write metadata for this recorder.

        ``silent`` must only be set by a caller that is discarding a recorder
        it knows was never the active recorder of a real, started call (see
        ``start_session_recording``'s replace-the-previous-recorder cleanup).
        Every other caller -- ``stop_session_recording`` and
        ``RecorderEngine.stop`` -- finalizes a recorder that a real recording
        session was deliberately ended on, so a zero-frame result there is
        always worth a loud warning, whether or not any stream ever attached.
        Gating the warning on ``self._stream_labels`` being non-empty (the
        pre-fix behaviour) silently downgraded exactly that case -- mic/system
        capture never attaching at all, so no stream was ever opened -- to an
        info log, which is the single most likely real-world cause of an
        empty recording and the one operators most need to see.
        """
        with self._lock:
            if self._closed:
                return self._dir
            for label in list(self._files.keys()):
                self._flush_label_locked(label)
            for wf in self._files.values():
                try:
                    wf.close()
                except Exception:
                    pass
            self._files.clear()
            self._buffers.clear()
            self._ended_at = _now_iso()
            self._closed = True
            self._write_metadata()
            total_frames = sum(self._frames_written.values())
        if total_frames == 0 and not silent:
            # Recording was enabled and started, but no audio ever arrived --
            # e.g. the mic/system stream never actually attached. Leaving
            # behind silent-but-valid-looking WAV files with no signal that
            # anything was off is worse than an explicit warning: the operator
            # who enabled RECORD_AUDIO would otherwise only discover the empty
            # recording when they go looking for it after the call.
            if self._stream_labels:
                logger.warning(
                    "AudioRecorder session=%s finalized with zero audio frames captured "
                    "across %s; %s/*.wav will be empty. Check that the mic/system audio "
                    "streams actually started.",
                    self._session_id,
                    self._stream_labels,
                    self._dir,
                )
            else:
                logger.warning(
                    "AudioRecorder session=%s finalized with zero audio frames captured "
                    "and no audio stream ever attached; %s will contain no recording at "
                    "all. Check that the mic/system audio capture actually started for "
                    "this call.",
                    self._session_id,
                    self._dir,
                )
        else:
            logger.info(
                "AudioRecorder finalized session=%s dir=%s streams=%s",
                self._session_id,
                self._dir,
                self._stream_labels,
            )
        return self._dir

    def _open_stream_locked(self, label: str) -> None:
        path = self._dir / f"{label}.wav"
        wf = wave.open(str(path), "wb")
        wf.setnchannels(self._channels)
        wf.setsampwidth(2)
        wf.setframerate(self._sample_rate)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self._files[label] = wf
        self._buffers[label] = []
        self._stream_labels.append(label)
        self._last_flush_ms[label] = 0
        self._frames_written[label] = 0

    def _flush_label_locked(self, label: str) -> None:
        chunks = self._buffers.get(label) or []
        if not chunks:
            return
        audio = np.concatenate(chunks).astype(np.float32, copy=False)
        audio = np.clip(audio, -1.0, 1.0)
        int16 = (audio * 32767.0).astype(np.int16)
        self._files[label].writeframes(int16.tobytes())
        self._frames_written[label] += int(int16.shape[0])
        self._buffers[label] = []

    def _write_metadata(self) -> None:
        duration_ms_per_label = {
            label: int(round(self._frames_written[label] * 1000 / self._sample_rate))
            for label in self._stream_labels
        }
        max_duration_ms = max(duration_ms_per_label.values(), default=0)
        metadata = {
            "session_id": self._session_id,
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "duration_ms": max_duration_ms,
            "duration_ms_per_stream": duration_ms_per_label,
            "sample_rate": self._sample_rate,
            "channels": self._channels,
            "stream_labels": list(self._stream_labels),
        }
        metadata_path = self._dir / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
        metadata_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @staticmethod
    def _to_mono(samples: np.ndarray) -> np.ndarray:
        arr = np.asarray(samples)
        if arr.ndim == 1:
            return arr.astype(np.float32, copy=False)
        return arr[:, 0].astype(np.float32, copy=False)


_active_recorder: AudioRecorder | None = None
_active_lock = threading.Lock()


def start_session_recording(
    session_id: str,
    output_dir: str | Path = resolve_app_path("data/sessions"),
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    flush_seconds: float = 5.0,
) -> AudioRecorder:
    global _active_recorder
    with _active_lock:
        if _active_recorder is not None:
            try:
                # silent=True: this recorder is being replaced, not deliberately
                # ended -- it may never have been the active recorder of a real
                # call (e.g. defensive cleanup on an unexpected double-start), so
                # a zero-frame result here is not itself evidence of a broken
                # capture path and must not trigger the loud "recording captured
                # nothing" warning that a genuine call-end finalize does.
                _active_recorder.finalize(silent=True)
            except Exception:
                logger.exception("Failed to finalize previous recorder")
        _active_recorder = AudioRecorder(
            session_id,
            output_dir,
            sample_rate=sample_rate,
            channels=channels,
            flush_seconds=flush_seconds,
        )
        logger.info(
            "AudioRecorder started session=%s dir=%s",
            session_id,
            _active_recorder.directory,
        )
        return _active_recorder


def stop_session_recording() -> Path | None:
    global _active_recorder
    with _active_lock:
        recorder = _active_recorder
        _active_recorder = None
    if recorder is None:
        return None
    try:
        return recorder.finalize()
    except Exception:
        logger.exception("Failed to finalize recorder")
        return recorder.directory


def get_active_recorder() -> AudioRecorder | None:
    return _active_recorder
