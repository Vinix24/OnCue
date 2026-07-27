from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sales_copilot.modules.diarizer.protocol import DiarizerProtocol

logger = logging.getLogger(__name__)


def get_diarizer() -> DiarizerProtocol | None:
    """Returns a DiarizerProtocol when DIARIZATION_ENABLED=1, otherwise None.

    Shadow-mode (DIARIZATION_ENABLED=0, the default) returns None — the transcriber
    pipeline is completely unchanged. Production mode creates a PyannoteV3Diarizer
    that emits speaker_segments WS events; the detector still receives unchanged
    transcript events.
    """
    if os.getenv("DIARIZATION_ENABLED", "0") != "1":
        return None
    token = os.getenv("HUGGINGFACE_TOKEN")
    if not token or token.startswith("hf_PLACEHOLDER"):
        logger.warning(
            "DIARIZATION_ENABLED=1 but HUGGINGFACE_TOKEN is missing or placeholder — diarizer disabled"
        )
        return None
    from sales_copilot.modules.diarizer.pyannote_v3 import PyannoteV3Diarizer  # lazy import

    return PyannoteV3Diarizer(
        hf_token=token,
        min_speakers=int(os.getenv("DIARIZATION_MIN_SPEAKERS", "1")),
        max_speakers=int(os.getenv("DIARIZATION_MAX_SPEAKERS", "10")),
    )
