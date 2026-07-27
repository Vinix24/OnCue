import json
from unittest.mock import MagicMock, patch

import pytest

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import DetectorConfig, SlidesConfig, WebSocketConfig
from sales_copilot.core.llm_client import LLMClient
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.copilot.slide_generator import (
    SLIDE_MAX_OUTPUT_TOKENS,
    GeneratedSlide,
    SlideGenerator,
)
from sales_copilot.modules.detector.pipeline import PainPointEvent


class _StubLLMClient:
    """Mimics a sibling LLM-client site: exposes an LLMClient whose SDK seam is captured."""

    def __init__(self, response: GeneratedSlide) -> None:
        self.config = DetectorConfig(llm_provider="openai", llm_model="gpt-4o-mini")
        self.provider = "openai"
        self.response = response
        self.calls: list[dict[str, object]] = []
        self.partial_calls: list[dict[str, object]] = []
        self._llm = LLMClient("openai", timeout_ms=7000)
        self._llm._create = self._capture
        self._llm._create_partial = self._capture_partial

    def _capture(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def _capture_partial(self, **kwargs):
        self.partial_calls.append(kwargs)
        yield self.response


class _StubPipeline:
    def __init__(self, event: PainPointEvent | None) -> None:
        self._event = event
        self.llm_client = object()

    def process(self, text: str, speaker: str) -> PainPointEvent | None:
        return self._event


class _StubCaseDB:
    async def find_case(self, pain_point: str, industry: str | None = None):
        return None


class _StubSlideGenerator:
    def __init__(self, generated: GeneratedSlide | None) -> None:
        self.generated = generated
        self.calls: list[str] = []

    async def generate(self, category: str, *, on_partial=None) -> GeneratedSlide | None:
        self.calls.append(category)
        return self.generated

    def slide_payload(self, category: str, generated: GeneratedSlide) -> dict[str, object]:
        return {
            "title": generated.title,
            "description": generated.description,
            "metrics": generated.metrics,
        }


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_slide_generator_returns_structured_llm_output() -> None:
    generated = GeneratedSlide(
        title="Operational uplift",
        description="Reduced backlog and improved SLA attainment.",
        metrics=["-35% backlog", "98% SLA", "ignored"],
    )
    llm = _StubLLMClient(generated)
    generator = SlideGenerator(llm_client=llm)

    result = await generator.generate("capacity")

    assert result == generated
    # llm_streaming defaults True, so generation goes through astream() -> _create_partial.
    assert len(llm.partial_calls) == 1
    assert llm.calls == []
    call = llm.partial_calls[0]
    assert call["messages"][1]["content"] == (
        "Generate a case study slide for pain point 'capacity'. "
        "Include title, description, and 1-2 metrics."
    )
    assert call["max_tokens"] == 512

    payload = generator.slide_payload("capacity", result)
    assert payload["title"] == "Operational uplift"
    assert payload["metrics"] == ["-35% backlog", "98% SLA"]


@pytest.mark.asyncio
async def test_injector_skips_generation_when_dynamic_slides_disabled(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    event = PainPointEvent(
        category="capacity",
        confidence=0.9,
        trigger_phrase="capacity pressure",
        timestamp_ms=100,
    )
    pipeline = _StubPipeline(event)
    case_db = _StubCaseDB()
    slide_generator = _StubSlideGenerator(
        GeneratedSlide(
            title="Unused",
            description="Unused",
            metrics=["unused"],
        )
    )
    injector = SlideInjector(
        pipeline,
        case_db,
        WebSocketConfig(),
        SlidesConfig(dynamic_slides=False),
        slide_generator=slide_generator,
        feature_policy=pro_feature_policy,
    )

    pain_socket = _FakeWebSocket()

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return pain_socket
        raise AssertionError(f"unexpected websocket connect to {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("text", "prospect", 999)

    assert result is not None
    assert result.slide_control_msg is None
    assert slide_generator.calls == []
    assert len(pain_socket.sent) == 1


@pytest.mark.asyncio
async def test_injector_emits_generated_slide_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    event = PainPointEvent(
        category="offerteproces",
        confidence=0.88,
        trigger_phrase="offertes blijven hangen",
        timestamp_ms=200,
    )
    pipeline = _StubPipeline(event)
    case_db = _StubCaseDB()
    generated = GeneratedSlide(
        title="Sneller van lead naar offerte",
        description="Automatisering verkortte de doorlooptijd.",
        metrics=["-42% offerte doorlooptijd"],
    )
    slide_generator = _StubSlideGenerator(generated)
    injector = SlideInjector(
        pipeline,
        case_db,
        WebSocketConfig(),
        SlidesConfig(dynamic_slides=True),
        slide_generator=slide_generator,
        feature_policy=pro_feature_policy,
    )

    pain_socket = _FakeWebSocket()
    slide_socket = _FakeWebSocket()

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return pain_socket
        if url.endswith("/ws/slide-control"):
            return slide_socket
        raise AssertionError(f"unexpected websocket connect to {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("text", "prospect", 3210)

    assert result is not None
    assert slide_generator.calls == ["offerteproces"]
    assert len(slide_socket.sent) == 1

    payload = json.loads(slide_socket.sent[0])
    assert payload["action"] == "inject_generated_slide"
    assert payload["pain_point"] == "offerteproces"
    assert payload["slide"]["title"] == "Sneller van lead naar offerte"


@pytest.mark.asyncio
async def test_injector_falls_back_when_generation_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    event = PainPointEvent(
        category="budget",
        confidence=0.8,
        trigger_phrase="te duur",
        timestamp_ms=300,
    )
    pipeline = _StubPipeline(event)
    case_db = _StubCaseDB()
    slide_generator = _StubSlideGenerator(None)
    injector = SlideInjector(
        pipeline,
        case_db,
        WebSocketConfig(),
        SlidesConfig(dynamic_slides=True),
        slide_generator=slide_generator,
        feature_policy=pro_feature_policy,
    )

    pain_socket = _FakeWebSocket()

    async def fake_connect(url: str) -> _FakeWebSocket:
        if url.endswith("/ws/pain-points"):
            return pain_socket
        raise AssertionError(f"unexpected websocket connect to {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("text", "prospect", 111)

    assert result is not None
    assert result.slide_control_msg is None
    assert slide_generator.calls == ["budget"]
    assert len(pain_socket.sent) == 1


class _StubGroqLLMClient:
    def __init__(self, response: GeneratedSlide) -> None:
        self.config = DetectorConfig(llm_provider="groq", llm_model="llama-3.3-70b-versatile")
        self.provider = "groq"
        self.response = response
        self.calls: list[dict] = []
        self._llm = LLMClient("groq", timeout_ms=7000)
        self._llm._create = self._capture
        self._llm._create_partial = self._capture_partial

    def _capture(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def _capture_partial(self, **kwargs):
        self.calls.append(kwargs)
        yield self.response


@pytest.mark.asyncio
async def test_slide_generator_inherits_model_from_llm_client_config() -> None:
    """SlideGenerator must use llm_client.config.llm_model, not env LLM_MODEL."""
    generated = GeneratedSlide(
        title="Case",
        description="desc",
        metrics=["50% faster"],
    )
    groq_client = _StubGroqLLMClient(generated)
    generator = SlideGenerator(llm_client=groq_client)

    assert generator._model == "llama-3.3-70b-versatile"
    assert generator.provider == "groq"

    await generator.generate("offerteproces")

    assert len(groq_client.calls) == 1
    assert groq_client.calls[0]["model"] == "llama-3.3-70b-versatile"


@pytest.mark.asyncio
async def test_slide_generator_gemini_uses_from_genai_messages_format() -> None:
    """Gemini path must use from_genai + messages format, no temperature."""
    fake_genai_client = MagicMock()
    fake_patched = MagicMock()
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return GeneratedSlide(title="T", description="D", metrics=["M"])

    fake_patched.chat.completions.create = fake_create

    fake_genai_module = MagicMock()
    fake_genai_module.Client.return_value = fake_genai_client

    fake_instructor = MagicMock()
    fake_instructor.from_genai.return_value = fake_patched
    fake_instructor.Mode.GENAI_STRUCTURED_OUTPUTS = "GENAI_STRUCTURED_OUTPUTS"

    with (
        patch.dict("sys.modules", {"google": MagicMock(genai=fake_genai_module), "google.genai": fake_genai_module}),
        patch("sales_copilot.core.llm_client.instructor", fake_instructor),
    ):
        config = DetectorConfig(llm_provider="gemini", llm_model="gemini-2.5-flash")
        generator = SlideGenerator(config)
        await generator.generate("capaciteit")

    fake_instructor.from_genai.assert_called_once_with(
        fake_genai_client,
        mode="GENAI_STRUCTURED_OUTPUTS",
    )
    fake_instructor.patch.assert_not_called()
    assert "messages" in captured
    assert "contents" not in captured
    assert "temperature" not in captured


# ---------------------------------------------------------------------------
# SlideGenerator.generate — streaming (default) vs. LLM_STREAMING=false
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_streaming_default_yields_partials_and_final_object() -> None:
    generated = GeneratedSlide(
        title="Sneller offerteren",
        description="Automatisering halveert doorlooptijd.",
        metrics=["-50% doorlooptijd"],
    )
    llm = _StubLLMClient(generated)
    generator = SlideGenerator(llm_client=llm)

    def _fake_create_partial(**kwargs):
        llm.partial_calls.append(kwargs)
        yield GeneratedSlide(title="Sneller", description="", metrics=[])
        yield generated

    llm._llm._create_partial = _fake_create_partial

    partials: list[GeneratedSlide] = []

    async def _on_partial(partial: GeneratedSlide) -> None:
        partials.append(partial)

    result = await generator.generate("offerteproces", on_partial=_on_partial)

    assert result == generated
    assert len(partials) == 2
    assert partials[0].title == "Sneller"
    assert partials[-1] == generated


@pytest.mark.asyncio
async def test_generate_llm_streaming_false_uses_acreate_and_skips_partial_callback() -> None:
    generated = GeneratedSlide(title="T", description="D", metrics=["M"])
    llm = _StubLLMClient(generated)
    llm.config = DetectorConfig(llm_provider="openai", llm_model="gpt-4o-mini", llm_streaming=False)
    generator = SlideGenerator(config=llm.config, llm_client=llm)

    partial_calls: list[GeneratedSlide] = []

    async def _on_partial(partial: GeneratedSlide) -> None:
        partial_calls.append(partial)

    result = await generator.generate("offerteproces", on_partial=_on_partial)

    assert result == generated
    assert len(llm.calls) == 1
    assert partial_calls == []


@pytest.mark.asyncio
async def test_generate_passes_max_output_tokens_cap_in_both_modes() -> None:
    generated = GeneratedSlide(title="T", description="D", metrics=["M"])

    streaming_llm = _StubLLMClient(generated)

    def _fake_create_partial(**kwargs):
        streaming_llm.partial_calls.append(kwargs)
        yield generated

    streaming_llm._llm._create_partial = _fake_create_partial
    streaming_generator = SlideGenerator(llm_client=streaming_llm)
    await streaming_generator.generate("offerteproces")
    assert streaming_llm.partial_calls[0]["max_tokens"] == SLIDE_MAX_OUTPUT_TOKENS

    non_streaming_llm = _StubLLMClient(generated)
    non_streaming_llm.config = DetectorConfig(
        llm_provider="openai", llm_model="gpt-4o-mini", llm_streaming=False
    )
    non_streaming_generator = SlideGenerator(config=non_streaming_llm.config, llm_client=non_streaming_llm)
    await non_streaming_generator.generate("offerteproces")
    assert non_streaming_llm.calls[0]["max_tokens"] == SLIDE_MAX_OUTPUT_TOKENS


def test_partial_slide_payload_handles_unset_fields() -> None:
    """Partial-streamed instances have every field optional -- unlike slide_payload(), this
    must not crash on a None title/description/metrics before that JSON has arrived."""
    llm = _StubLLMClient(GeneratedSlide(title="T", description="D", metrics=["M"]))
    generator = SlideGenerator(llm_client=llm)
    # model_construct bypasses validation, mirroring what a real Partial[GeneratedSlide]
    # instance looks like before any JSON key has streamed in (every field unset/None).
    empty_partial = GeneratedSlide.model_construct(title=None, description=None, metrics=None)

    payload = generator.partial_slide_payload("capaciteit", empty_partial)

    assert payload == {"title": "Case: capaciteit", "description": "", "metrics": []}


def test_partial_slide_payload_truncates_like_slide_payload() -> None:
    llm = _StubLLMClient(GeneratedSlide(title="T", description="D", metrics=["M"]))
    generator = SlideGenerator(llm_client=llm)
    partial = GeneratedSlide(
        title="Operational uplift",
        description="Reduced backlog and improved SLA attainment.",
        metrics=["-35% backlog", "98% SLA", "ignored"],
    )

    payload = generator.partial_slide_payload("capacity", partial)

    assert payload["title"] == "Operational uplift"
    assert payload["metrics"] == ["-35% backlog", "98% SLA"]


# ---------------------------------------------------------------------------
# SlideInjector — partial-slide emission wiring
# ---------------------------------------------------------------------------


class _FakeSlideControlWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        pass


class _StreamingStubSlideGenerator:
    """Fake that actually drives on_partial -- mirrors SlideGenerator.generate()'s streaming
    contract, unlike _StubSlideGenerator which ignores the callback."""

    def __init__(self, partials: list[GeneratedSlide], final: GeneratedSlide | None) -> None:
        self._partials = partials
        self._final = final
        self.calls: list[str] = []

    async def generate(self, category: str, *, on_partial=None) -> GeneratedSlide | None:
        self.calls.append(category)
        if on_partial is not None:
            for partial in self._partials:
                await on_partial(partial)
        return self._final

    def slide_payload(self, category: str, generated: GeneratedSlide) -> dict[str, object]:
        return {"title": generated.title, "description": generated.description, "metrics": generated.metrics}

    def partial_slide_payload(self, category: str, partial: GeneratedSlide) -> dict[str, object]:
        return {"title": partial.title, "description": partial.description, "metrics": partial.metrics}


@pytest.mark.asyncio
async def test_injector_publishes_partial_slide_events_before_final_injection(
    monkeypatch: pytest.MonkeyPatch,
    pro_feature_policy: FeaturePolicy,
) -> None:
    event = PainPointEvent(
        category="offerteproces",
        confidence=0.88,
        trigger_phrase="offertes blijven hangen",
        timestamp_ms=200,
    )
    pipeline = _StubPipeline(event)
    case_db = _StubCaseDB()
    final = GeneratedSlide(
        title="Sneller van lead naar offerte",
        description="Automatisering verkortte de doorlooptijd.",
        metrics=["-42% offerte doorlooptijd"],
    )
    partials = [
        GeneratedSlide(title="Sneller", description="", metrics=[]),
        GeneratedSlide(title="Sneller van lead", description="Automatisering", metrics=[]),
    ]
    slide_generator = _StreamingStubSlideGenerator(partials, final)
    injector = SlideInjector(
        pipeline,
        case_db,
        WebSocketConfig(),
        SlidesConfig(dynamic_slides=True),
        slide_generator=slide_generator,
        feature_policy=pro_feature_policy,
    )

    pain_socket = _FakeWebSocket()
    slide_socket = _FakeSlideControlWebSocket()

    async def fake_connect(url: str) -> object:
        if url.endswith("/ws/pain-points"):
            return pain_socket
        if url.endswith("/ws/slide-control"):
            return slide_socket
        raise AssertionError(f"unexpected websocket connect to {url}")

    monkeypatch.setattr("sales_copilot.modules.copilot.injector.websockets.connect", fake_connect)

    result = await injector.process_transcript("text", "prospect", 3210)

    assert result is not None
    messages = [json.loads(raw) for raw in slide_socket.sent]
    partial_messages = [m for m in messages if m["action"] == "update_generated_slide_partial"]
    final_messages = [m for m in messages if m["action"] == "inject_generated_slide"]

    assert len(partial_messages) == 2
    assert partial_messages[0]["slide"]["title"] == "Sneller"
    assert partial_messages[1]["slide"]["title"] == "Sneller van lead"
    assert all(m["pain_point"] == "offerteproces" for m in partial_messages)
    assert len(final_messages) == 1
    assert final_messages[0]["slide"]["title"] == "Sneller van lead naar offerte"
    # Partials arrive strictly before the final injection message on the wire.
    assert messages.index(partial_messages[-1]) < messages.index(final_messages[0])
