"""Token-free speaker separation with sherpa-onnx.

sherpa-onnx runs the pyannote segmentation-3.0 model as ONNX plus a speaker-embedding
ONNX model (WeSpeaker ResNet34 VoxCeleb by default) and clusters the embeddings by
threshold. Both models come from the sherpa-onnx GitHub releases, so no Hugging Face
token is needed. Downloads happen on first use into the user cache dir.

Neither torch nor pyannote is imported here. ``sherpa_onnx`` itself is imported inside
:meth:`SherpaBackend.prepare`, so importing this module is always cheap.

Shared by ``scripts/post_process_call.py`` (through ``diarize_merge.diarize_audio``) and
``scripts/eval_diarization.py``.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
DEFAULT_EMBEDDING = "wespeaker"
DEFAULT_CLUSTER_THRESHOLD = 0.8

SEGMENTATION_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
_EMBEDDING_BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
EMBEDDING_MODELS = {
    "eres2net": "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx",
    "wespeaker": "wespeaker_en_voxceleb_resnet34.onnx",
    "titanet": "nemo_en_titanet_small.onnx",
}

SherpaTurn = tuple[float, float, str]


def default_cache_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "oncue" / "diarization-models"


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    logger.info("downloading %s", url)
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh, urllib.request.urlopen(url, timeout=60) as resp:  # noqa: S310 - fixed https URLs
            while block := resp.read(1 << 20):
                fh.write(block)
        os.replace(tmp_name, dest)
    finally:
        Path(tmp_name).unlink(missing_ok=True)


def ensure_sherpa_models(cache_dir: Path, embedding: str) -> tuple[Path, Path]:
    """Segmentation and embedding ONNX files in ``cache_dir``, downloaded on first use."""
    seg = cache_dir / "sherpa-onnx-pyannote-segmentation-3-0.onnx"
    if not seg.exists():
        archive = cache_dir / "segmentation.tar.bz2"
        _download(SEGMENTATION_URL, archive)
        try:
            with tarfile.open(archive, "r:bz2") as tar:
                member = next(m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "model.onnx")
                src = tar.extractfile(member)
                assert src is not None
                tmp = seg.with_suffix(".part")
                tmp.write_bytes(src.read())
                os.replace(tmp, seg)
        finally:
            archive.unlink(missing_ok=True)
    emb = cache_dir / EMBEDDING_MODELS[embedding]
    if not emb.exists():
        _download(_EMBEDDING_BASE + EMBEDDING_MODELS[embedding], emb)
    return seg, emb


@dataclass
class SherpaBackend:
    embedding: str
    oracle: bool
    device: str
    cache_dir: Path
    threshold: float = 0.5
    threads: int = 4
    name: str = field(init=False)
    _sd: object | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        if self.embedding not in EMBEDDING_MODELS:
            raise ValueError(f"unknown sherpa embedding {self.embedding!r}; choose from {sorted(EMBEDDING_MODELS)}")
        if self.device not in ("cpu", "cuda"):
            raise ValueError(f"sherpa-onnx has no {self.device} provider here (cpu or cuda)")
        self.name = f"sherpa:{self.embedding}" + ("@oracle" if self.oracle else "")

    def prepare(self, num_speakers: int | None) -> None:
        import sherpa_onnx  # noqa: PLC0415 - optional extra, lazy

        seg, emb = ensure_sherpa_models(self.cache_dir, self.embedding)
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg)),
                num_threads=self.threads,
                provider=self.device,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(emb), num_threads=self.threads, provider=self.device
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_speakers if (self.oracle and num_speakers) else -1, threshold=self.threshold
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise RuntimeError("invalid sherpa-onnx diarization config")
        self._sd = sherpa_onnx.OfflineSpeakerDiarization(config)

    def close(self) -> None:
        self._sd = None

    def process(self, audio: np.ndarray, sample_rate: int) -> list[SherpaTurn]:
        """``(start, end, "SPEAKER_NN")`` turns in chronological order."""
        sd = self._sd
        assert sd is not None and sample_rate == sd.sample_rate  # type: ignore[attr-defined]
        result = sd.process(audio).sort_by_start_time()  # type: ignore[attr-defined]
        return [(r.start, r.end, f"SPEAKER_{r.speaker:02d}") for r in result]


def load_audio_16k_mono(path: Path) -> np.ndarray:
    """Mono float32 at 16 kHz. 16-bit PCM WAV is read directly, anything else goes through ffmpeg."""
    try:
        with wave.open(str(path), "rb") as wf:
            rate, channels, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            raw = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError):
        rate, channels, width, raw = 0, 0, 0, b""
    if rate == SAMPLE_RATE and width == 2 and channels >= 1:
        pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        return pcm.reshape(-1, channels).mean(axis=1) if channels > 1 else pcm
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path)]
    cmd += ["-f", "f32le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("ffmpeg not found in PATH: needed for audio that is not 16 kHz 16-bit WAV") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg could not read {path}: {proc.stderr[-300:].decode(errors='replace')}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def diarize_wav_sherpa(
    wav_path: str | Path,
    *,
    threshold: float = DEFAULT_CLUSTER_THRESHOLD,
    embedding: str = DEFAULT_EMBEDDING,
    cache_dir: Path | None = None,
    threads: int = 4,
) -> list[SherpaTurn]:
    """Diarize a whole recording with sherpa-onnx, clustering by ``threshold`` (speaker count unknown)."""
    backend = SherpaBackend(
        embedding=embedding,
        oracle=False,
        device="cpu",
        cache_dir=cache_dir or default_cache_dir(),
        threshold=threshold,
        threads=threads,
    )
    audio = load_audio_16k_mono(Path(wav_path))
    backend.prepare(None)
    try:
        return backend.process(audio, SAMPLE_RATE)
    finally:
        backend.close()
