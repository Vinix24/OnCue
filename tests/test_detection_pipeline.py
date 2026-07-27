from __future__ import annotations

from dataclasses import dataclass

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.debouncer import PainPointDebouncer
from sales_copilot.modules.detector.pipeline import DetectionPipeline
from sales_copilot.modules.detector.router import RouteMatch


class _SpyAuditWriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def write(self, record: dict) -> None:
        self.calls.append(record)


@dataclass
class _FakeRouter:
    match: RouteMatch | None

    def classify(self, text: str) -> RouteMatch | None:
        return self.match


@dataclass
class _FakeLLM:
    confirmation: object | None
    called: int = 0

    def confirm(self, fragment: str):
        self.called += 1
        return self.confirmation


class _FakeDebouncer(PainPointDebouncer):
    def __init__(self, should: bool) -> None:
        super().__init__(cooldown_seconds=45)
        self._should = should
        self.recorded: list[str] = []

    def should_trigger(self, category: str) -> bool:
        return self._should

    def record_trigger(self, category: str) -> None:
        self.recorded.append(category)


def test_pipeline_skips_self_speaker() -> None:
    config = DetectorConfig(only_classify_prospect=True)
    pipeline = DetectionPipeline(
        config=config,
        router=_FakeRouter(RouteMatch("offerteproces", 0.9, "high")),
        llm_client=_FakeLLM(None),
        debouncer=_FakeDebouncer(True),
    )

    assert pipeline.process("text", speaker="self") is None


def test_pipeline_uses_requested_session_id() -> None:
    audit_writer = _SpyAuditWriter()
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="recruitment"),
        router=_FakeRouter(None),
        llm_client=_FakeLLM(None),
        debouncer=_FakeDebouncer(True),
        audit_writer=audit_writer,
        session_id="shared-call-id",
    )

    pipeline.process("geen match", speaker="prospect")

    assert audit_writer.calls[0]["session_id"] == "shared-call-id"


def test_pipeline_high_confidence_skips_llm() -> None:
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.9, "high")),
        llm_client=llm,
        debouncer=_FakeDebouncer(True),
    )

    event = pipeline.process("we zitten uren aan offertes", speaker="prospect")

    assert event is not None
    assert event.category == "offerteproces"
    assert llm.called == 0


def test_pipeline_uncertain_calls_llm() -> None:
    confirmation = type(
        "Detection",
        (),
        {"category": "offerteproces", "confidence": 0.7, "trigger_phrase": "offertes"},
    )
    llm = _FakeLLM(confirmation)
    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.7, "uncertain")),
        llm_client=llm,
        debouncer=_FakeDebouncer(True),
    )

    event = pipeline.process("we zitten uren aan offertes", speaker="prospect")

    assert event is not None
    assert event.category == "offerteproces"
    assert llm.called == 1


def test_pipeline_low_confidence_returns_none() -> None:
    llm = _FakeLLM(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.3, "none")),
        llm_client=llm,
        debouncer=_FakeDebouncer(True),
    )

    assert pipeline.process("we zitten uren aan offertes", speaker="prospect") is None
    assert llm.called == 0


def test_pipeline_respects_debounce() -> None:
    pipeline = DetectionPipeline(
        config=DetectorConfig(),
        router=_FakeRouter(RouteMatch("offerteproces", 0.9, "high")),
        llm_client=_FakeLLM(None),
        debouncer=_FakeDebouncer(False),
    )

    assert pipeline.process("we zitten uren aan offertes", speaker="prospect") is None


# ---------------------------------------------------------------------------
# Audit-ledger backward compat: sales + coach do NOT write audit records
# ---------------------------------------------------------------------------


def test_sales_preset_does_not_write_audit() -> None:
    spy = _SpyAuditWriter()
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="sales"),
        router=_FakeRouter(RouteMatch("offerteproces", 0.9, "high")),
        llm_client=_FakeLLM(None),
        debouncer=_FakeDebouncer(True),
        audit_writer=spy,
    )
    pipeline.process("we zitten uren aan offertes", speaker="prospect")
    assert len(spy.calls) == 0, "Sales preset must never write audit records"


def test_coach_preset_does_not_write_audit() -> None:
    spy = _SpyAuditWriter()
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="coach"),
        router=_FakeRouter(RouteMatch("coaching-categorie", 0.9, "high")),
        llm_client=_FakeLLM(None),
        debouncer=_FakeDebouncer(True),
        audit_writer=spy,
    )
    pipeline.process("coaching tekst hier", speaker="prospect")
    assert len(spy.calls) == 0, "Coach preset must never write audit records"


def test_recruitment_preset_writes_audit_on_detection() -> None:
    spy = _SpyAuditWriter()
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="recruitment"),
        router=_FakeRouter(RouteMatch("kandidaat-kwalificatie", 0.9, "high")),
        llm_client=_FakeLLM(None),
        debouncer=_FakeDebouncer(True),
        audit_writer=spy,
    )
    event = pipeline.process("kandidaat heeft 5 jaar Python ervaring", speaker="prospect")
    assert event is not None
    assert len(spy.calls) == 1
    record = spy.calls[0]
    assert record["detection_count"] == 1
    assert record["event_type"] == "detection"
    assert "session_id" in record


def test_recruitment_preset_writes_audit_on_no_match() -> None:
    spy = _SpyAuditWriter()
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="recruitment"),
        router=_FakeRouter(None),
        llm_client=_FakeLLM(None),
        debouncer=_FakeDebouncer(True),
        audit_writer=spy,
    )
    event = pipeline.process("willekeurige tekst zonder match", speaker="prospect")
    assert event is None
    assert len(spy.calls) == 1
    assert spy.calls[0]["detection_count"] == 0
    assert spy.calls[0]["event_type"] == "text_processed"


# ---------------------------------------------------------------------------
# F01 security fix: recruitment preset MUST redact PII before LLM/router call
# ---------------------------------------------------------------------------


class _CapturingRouter:
    """Router that records the exact text it receives."""

    def __init__(self, match) -> None:
        self.match = match
        self.received: list[str] = []

    def classify(self, text: str):
        self.received.append(text)
        return self.match


class _CapturingLLM:
    """LLM client that records the exact text it receives."""

    def __init__(self, confirmation) -> None:
        self.confirmation = confirmation
        self.received: list[str] = []

    def confirm(self, fragment: str):
        self.received.append(fragment)
        return self.confirmation


def test_recruitment_pipeline_sends_redacted_text_to_router() -> None:
    """Router must receive PII-redacted text, not the raw transcript."""
    spy = _SpyAuditWriter()
    capturing_router = _CapturingRouter(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="recruitment"),
        router=capturing_router,
        llm_client=_FakeLLM(None),
        debouncer=_FakeDebouncer(False),
        audit_writer=spy,
    )

    raw = "Jan van der Berg telefoon 0612345678"
    pipeline.process(raw, speaker="prospect")

    assert len(capturing_router.received) == 1
    sent = capturing_router.received[0]
    assert "Jan van der Berg" not in sent, "PII naam must be redacted before router"
    assert "0612345678" not in sent, "PII telefoon must be redacted before router"
    assert "[NAAM]" in sent
    assert "[TELEFOON]" in sent


def test_recruitment_pipeline_sends_redacted_text_to_llm() -> None:
    """LLM confirm() must receive PII-redacted text, not the raw transcript."""
    spy = _SpyAuditWriter()
    capturing_llm = _CapturingLLM(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="recruitment"),
        router=_FakeRouter(RouteMatch("kandidaat-kwalificatie", 0.7, "uncertain")),
        llm_client=capturing_llm,
        debouncer=_FakeDebouncer(True),
        audit_writer=spy,
    )

    raw = "Jan van der Berg telefoon 0612345678"
    pipeline.process(raw, speaker="prospect")

    assert len(capturing_llm.received) == 1
    sent = capturing_llm.received[0]
    assert "Jan van der Berg" not in sent, "PII naam must be redacted before LLM"
    assert "0612345678" not in sent, "PII telefoon must be redacted before LLM"
    assert "[NAAM]" in sent
    assert "[TELEFOON]" in sent


def test_sales_pipeline_sends_original_text_to_router() -> None:
    """Sales preset has apply_pii_filter=False — original text must pass through."""
    capturing_router = _CapturingRouter(None)
    pipeline = DetectionPipeline(
        config=DetectorConfig(preset_name="sales"),
        router=capturing_router,
        llm_client=_FakeLLM(None),
        debouncer=_FakeDebouncer(False),
    )

    raw = "Jan van der Berg belt straks terug"
    pipeline.process(raw, speaker="prospect")

    assert len(capturing_router.received) == 1
    assert capturing_router.received[0] == raw.strip(), (
        "Sales preset must pass original text (no PII filter)"
    )
