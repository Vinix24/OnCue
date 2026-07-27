"""Local append-only store for hint-quality feedback.

The tester dashboard lets a non-technical user record a call, watch live
suggestions, and vote per hint with a thumbs-up or thumbs-down. Each vote is
written to ``data/feedback/hints.ndjson`` as a structured record that can be
used directly as eval-set input or converted to the formats expected by the
objection/pain-point eval harnesses.

All fields that may carry transcript text stay local; nothing is forwarded to
external endpoints.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from sales_copilot.core.paths import resolve_app_path

logger = logging.getLogger(__name__)

_FEEDBACK_DIR_NAME = "data/feedback"
_HINTS_FILE_NAME = "hints.ndjson"

_write_lock = threading.Lock()


class HintFeedbackRecord(BaseModel):
    """One thumbs-up/down vote on a coaching hint.

    The record is designed to be useful for three purposes:

    1. Hint-quality evaluation — ``hint`` + ``feedback``/``label``.
    2. Objection/pain-point mining — ``text`` holds the triggering utterance
       and is compatible with ``mine_objection_eval_set.py``.
    3. Near-miss analysis — ``context_utterances`` preserves the full recent
       prospect context that led to the hint.
    """

    recorded_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    session_id: str | None = Field(default=None)
    timestamp_ms: int | None = None
    hint: str
    feedback: str  # "up" or "down"
    context_utterance: str = ""
    context_utterances: list[str] = Field(default_factory=list)
    phase: str | None = None

    # Eval-harness compatibility fields. ``text``/``label`` mirror the schema
    # used by ``mine_objection_eval_set.py`` so the file can be loaded as an
    # auxiliary eval set (extra fields are preserved by the harness loaders).
    text: str = ""
    label: str = ""
    source: str = "tester_feedback"

    model_config = {"extra": "allow"}

    @field_validator("feedback")
    @classmethod
    def _validate_feedback(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"up", "down"}:
            raise ValueError("feedback must be 'up' or 'down'")
        return normalized

    def model_post_init(self, __context: Any) -> None:
        # Ensure every record has a session id.
        if not self.session_id:
            self.session_id = uuid.uuid4().hex[:16]
        # Derive eval-harness fields when not explicitly provided.
        if not self.context_utterance and self.context_utterances:
            self.context_utterance = self.context_utterances[-1]
        if not self.text:
            self.text = self.context_utterance or self.hint
        if not self.label:
            self.label = "hint_positive" if self.feedback == "up" else "hint_negative"


def _feedback_dir() -> Path:
    return resolve_app_path(_FEEDBACK_DIR_NAME)


def _hints_path() -> Path:
    return _feedback_dir() / _HINTS_FILE_NAME


def append_hint_feedback(record: HintFeedbackRecord) -> Path:
    """Append ``record`` to the local hints feedback NDJSON file.

    The file and parent directories are created on first use. The write is
    protected by a module-level lock so concurrent calls from the hub do not
    corrupt the NDJSON stream.
    """
    path = _hints_path()
    with _write_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = record.model_dump_json(exclude_none=True) + "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    logger.info("Appended hint feedback to %s (session=%s feedback=%s)", path, record.session_id, record.feedback)
    return path


def load_hint_feedback(limit: int | None = None) -> list[HintFeedbackRecord]:
    """Read all (or the last ``limit``) hint feedback records from disk."""
    path = _hints_path()
    if not path.exists():
        return []
    records: list[HintFeedbackRecord] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(HintFeedbackRecord.model_validate_json(line))
            except Exception:
                logger.warning("Skipping malformed feedback line in %s", path)
    if limit is not None:
        records = records[-limit:]
    return records
