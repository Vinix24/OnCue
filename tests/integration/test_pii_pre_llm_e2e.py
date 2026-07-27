"""Integration test: PII redaction MUST happen before any LLM/router call.

F01 security fix (commit 0163451). DPIA Art. 9 obligation for recruitment preset.

Covers two enforcement layers:
  1. `DetectionPipeline.process` -> router.classify + llm_client.confirm receive `clean`, not the raw transcript.
  2. `WindowClassifier._call_model_sync` -> preset.redact() runs before the LLM `_create` call.

Test payload contains realistic Dutch PII (BSN passes elfproef, IBAN passes mod-97). Both
strings MUST be absent from anything that crosses the LLM boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.preset import Preset, load_preset
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.pipeline import DetectionPipeline
from sales_copilot.modules.detector.router import RouteMatch
from sales_copilot.modules.detector.window_classifier import (
    WindowAnalysis,
    WindowClassifier,
)

# Realistic NL PII. BSN 123456782 satisfies the elfproef
# (1*9 + 2*8 + 3*7 + 4*6 + 5*5 + 6*4 + 7*3 + 8*2 + 2*-1 = 154, 154 % 11 == 0).
# IBAN NL91ABNA0417164300 passes the mod-97 check.
_BSN = "123456782"
_IBAN = "NL91ABNA0417164300"
_PAYLOAD = (
    f"Jan de Vries, BSN {_BSN}, IBAN {_IBAN}, "
    "zoekt werk vanaf 12 mei. Telefoon 0612345678."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingRouter:
    """Spy: records every text classify() receives."""

    def __init__(self, match: RouteMatch | None) -> None:
        self.match = match
        self.received: list[str] = []

    def classify(self, text: str) -> RouteMatch | None:
        self.received.append(text)
        return self.match


class _CapturingLLM:
    """Spy: records every text confirm() receives."""

    def __init__(self, confirmation: Any) -> None:
        self.confirmation = confirmation
        self.received: list[str] = []

    def confirm(self, fragment: str) -> Any:
        self.received.append(fragment)
        return self.confirmation


@dataclass
class _PermissiveDebouncer(PainPointDebouncer):
    """Always allow events through, record categories for assertion."""

    def __init__(self) -> None:
        super().__init__(cooldown_seconds=45)
        self.recorded: list[str] = []

    def should_trigger(self, category: str) -> bool:  # pragma: no cover - trivial
        return True

    def record_trigger(self, category: str) -> None:
        self.recorded.append(category)


class _SpyAuditWriter:
    """Silent audit writer. Required for recruitment preset (preset wires it implicitly)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.calls.append(record)


def _assert_no_raw_pii(text: str, *, label: str) -> None:
    """Raise AssertionError if any raw-PII fragment leaks into `text`."""
    leaks = []
    for token in (_BSN, _IBAN, "Jan de Vries", "0612345678"):
        if token in text:
            leaks.append(token)
    assert not leaks, (
        f"PII leaked into {label}: {leaks!r}. "
        f"Full text passed to layer: {text!r}"
    )


# ---------------------------------------------------------------------------
# Layer 1: DetectionPipeline (recruitment preset)
# ---------------------------------------------------------------------------


def test_recruitment_pipeline_redacts_bsn_iban_before_router() -> None:
    """Router MUST receive redacted text. Raw BSN + IBAN must not leak."""
    spy = _SpyAuditWriter()
    capturing_router = _CapturingRouter(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="recruitment"),
        router=capturing_router,
        llm_client=_CapturingLLM(None),
        debouncer=_PermissiveDebouncer(),
        audit_writer=spy,
    )

    pipeline.process(_PAYLOAD, speaker="prospect")

    assert len(capturing_router.received) == 1
    sent_to_router = capturing_router.received[0]
    _assert_no_raw_pii(sent_to_router, label="router.classify")
    # Positive assertion: redaction tokens present
    assert "[BSN]" in sent_to_router
    assert "[IBAN]" in sent_to_router
    assert "[NAAM]" in sent_to_router
    assert "[TELEFOON]" in sent_to_router


def test_recruitment_pipeline_redacts_bsn_iban_before_llm() -> None:
    """LLM confirm() MUST receive redacted text on uncertain matches."""
    spy = _SpyAuditWriter()
    # Route to uncertain band so LLM is invoked.
    uncertain = RouteMatch("kandidaat-kwalificatie", 0.7, "uncertain")
    capturing_llm = _CapturingLLM(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="recruitment"),
        router=_CapturingRouter(uncertain),
        llm_client=capturing_llm,
        debouncer=_PermissiveDebouncer(),
        audit_writer=spy,
    )

    pipeline.process(_PAYLOAD, speaker="prospect")

    assert len(capturing_llm.received) == 1, "LLM was not invoked despite uncertain band"
    sent_to_llm = capturing_llm.received[0]
    _assert_no_raw_pii(sent_to_llm, label="LLMClient.confirm")
    assert "[BSN]" in sent_to_llm
    assert "[IBAN]" in sent_to_llm


def test_recruitment_pipeline_passes_redacted_string_identical_to_both_layers() -> None:
    """Same redacted string reaches router AND llm — single redaction, two callers."""
    spy = _SpyAuditWriter()
    uncertain = RouteMatch("kandidaat-kwalificatie", 0.7, "uncertain")
    capturing_router = _CapturingRouter(uncertain)
    capturing_llm = _CapturingLLM(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="recruitment"),
        router=capturing_router,
        llm_client=capturing_llm,
        debouncer=_PermissiveDebouncer(),
        audit_writer=spy,
    )

    pipeline.process(_PAYLOAD, speaker="prospect")

    assert capturing_router.received == capturing_llm.received
    assert spy.calls, "audit_writer should have at least one record"


def test_sales_pipeline_does_not_redact_bsn_iban() -> None:
    """Sales preset has apply_pii_filter=False — raw text must pass through (no audit writer)."""
    capturing_router = _CapturingRouter(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="sales"),
        router=capturing_router,
        llm_client=_CapturingLLM(None),
        debouncer=_PermissiveDebouncer(),
    )

    pipeline.process(_PAYLOAD, speaker="prospect")

    assert capturing_router.received == [_PAYLOAD.strip()]


# ---------------------------------------------------------------------------
# Layer 2: WindowClassifier._call_model_sync (DPIA double-layer)
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_window_classifier() -> WindowClassifier:
    """WindowClassifier wired to recruitment preset with the seam's SDK call stubbed.

    The groq client constructs offline; only the final ``_create`` is replaced so the test
    captures the prompt that would cross the LLM boundary without hitting the network.
    """

    captured: dict[str, list[str]] = {"prompts": []}

    def _fake_create(**kwargs: Any) -> WindowAnalysis:
        prompt = ""
        if "contents" in kwargs:
            prompt = kwargs["contents"]
        elif "messages" in kwargs:
            for msg in kwargs["messages"]:
                if msg.get("role") == "user":
                    prompt = msg["content"]
                    break
        captured["prompts"].append(prompt)
        return WindowAnalysis(detections=[])

    config = DetectorConfig(preset_name="recruitment", llm_provider="groq", llm_model="test-model")
    # Test-only preset object: the real recruitment pack is Pro content and is
    # absent from the OSS export. What this test exercises is the redaction
    # SEAM (preset.redact() runs before _create), which only needs
    # apply_pii_filter=True — not the curated pack content.
    preset = Preset(
        name="recruitment",
        pain_points={"routes": []},
        objections={"routes": []},
        buying_signals={"routes": []},
        doubts={"routes": []},
        ui_labels={"prospect": "kandidaat", "self": "consultant"},
        system_prompt_addendum="",
        apply_pii_filter=True,
    )
    classifier = WindowClassifier(config=config, provider="groq", preset=preset)
    classifier._llm._create = _fake_create
    classifier._captured_prompts = captured["prompts"]  # type: ignore[attr-defined]
    return classifier


async def test_window_classifier_redacts_pii_before_llm(_stub_window_classifier: WindowClassifier) -> None:
    """preset.redact() must run before the LLM call. BSN + IBAN must not appear in prompt."""
    classifier = _stub_window_classifier
    await classifier._call_model_sync(_PAYLOAD, phase="discovery")

    prompts = classifier._captured_prompts  # type: ignore[attr-defined]
    assert len(prompts) == 1
    prompt = prompts[0]
    _assert_no_raw_pii(prompt, label="WindowClassifier user_prompt")
    assert "[BSN]" in prompt
    assert "[IBAN]" in prompt
    assert "[NAAM]" in prompt


async def test_window_classifier_sales_preset_redacts_at_outbound_boundary() -> None:
    """Cloud LLM calls redact PII even when the local sales pipeline keeps raw text."""
    captured: list[str] = []

    def _fake_create(**kwargs: Any) -> WindowAnalysis:
        if "messages" in kwargs:
            for msg in kwargs["messages"]:
                if msg.get("role") == "user":
                    captured.append(msg["content"])
                    break
        elif "contents" in kwargs:
            captured.append(kwargs["contents"])
        return WindowAnalysis(detections=[])

    config = DetectorConfig(preset_name="sales", llm_provider="groq", llm_model="test-model")
    classifier = WindowClassifier(config=config, provider="groq", preset=load_preset("sales"))
    classifier._llm._create = _fake_create
    await classifier._call_model_sync(_PAYLOAD, phase="discovery")

    assert len(captured) == 1
    _assert_no_raw_pii(captured[0], label="sales WindowClassifier user_prompt")
    assert "[BSN]" in captured[0]
    assert "[IBAN]" in captured[0]
