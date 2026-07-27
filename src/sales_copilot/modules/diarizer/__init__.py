"""Speaker diarization module — multi-speaker tagging voor transcriptie.

Default: shadow-mode (DIARIZATION_ENABLED=0) — geen diarization, geen impact op detector.
Opt-in: DIARIZATION_ENABLED=1 + HUGGINGFACE_TOKEN voor productie-gebruik.

Zie claudedocs/ADR-PYANNOTE-DIARIZATION.md.
"""

from sales_copilot.modules.diarizer.factory import get_diarizer
from sales_copilot.modules.diarizer.protocol import DiarizerProtocol, SpeakerSegment

__all__ = ["DiarizerProtocol", "SpeakerSegment", "get_diarizer"]
