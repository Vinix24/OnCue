import asyncio
from unittest.mock import MagicMock, patch

import pytest

from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.detector import __main__ as detector_main
from sales_copilot.modules.detector.summary import ConversationSummary, SummaryEngine, SummaryLLMClient


class _FakeSummaryClient:
    def __init__(self, *, response: ConversationSummary | None = None, should_raise: bool = False) -> None:
        self._response = response
        self._should_raise = should_raise
        self.calls: list[tuple[list[str], list[object]]] = []

    async def summarize(self, transcript_lines, tracked_key_moments):  # noqa: ANN001
        self.calls.append((list(transcript_lines), list(tracked_key_moments)))
        if self._should_raise:
            raise RuntimeError("llm error")
        return self._response


@pytest.mark.asyncio
async def test_summary_engine_publishes_summary_with_tracked_key_moments() -> None:
    config = DetectorConfig(enable_summary=True)
    fake_client = _FakeSummaryClient(response=ConversationSummary(text="Korte samenvatting.", key_moments=[]))
    engine = SummaryEngine(
        config,
        WebSocketConfig(),
        client=fake_client,
        now_ms=lambda: 120000,
    )

    await engine.enqueue("We lopen vast op de planning", "prospect", 1000)
    await engine._handle_pain_point(  # noqa: SLF001
        {"type": "pain_point", "category": "planning", "timestamp_ms": 1000}
    )
    await engine._handle_pain_point(  # noqa: SLF001
        {"type": "pain_point", "category": "budget", "timestamp_ms": 2000}
    )
    await engine._handle_talk_time(  # noqa: SLF001
        {
            "type": "talk_time",
            "current_monologue_ms": 15000,
            "monologue_speaker": "self",
            "call_duration_ms": 15000,
        }
    )
    await engine._handle_talk_time(  # noqa: SLF001
        {
            "type": "talk_time",
            "current_monologue_ms": 9000,
            "monologue_speaker": "self",
            "call_duration_ms": 16000,
        }
    )
    await engine._handle_phase(  # noqa: SLF001
        {"type": "phase_change", "phase": "pitch", "timestamp_ms": 20000}
    )
    await engine._handle_objection(  # noqa: SLF001
        {"type": "objection", "category": "prijs", "timestamp_ms": 30000}
    )

    published: list[dict[str, object]] = []

    async def _capture(payload: dict[str, object]) -> None:
        published.append(payload)

    engine._publish_payload = _capture  # type: ignore[method-assign] # noqa: SLF001
    await engine._publish_summary_cycle()  # noqa: SLF001

    assert len(fake_client.calls) == 1
    assert len(published) == 1
    payload = published[0]
    assert payload["type"] == "summary"
    assert payload["text"] == "Korte samenvatting."
    moments = payload["key_moments"]
    assert isinstance(moments, list)
    assert moments[0]["type"] == "objection"
    assert moments[-1]["type"] == "pain_point"


@pytest.mark.asyncio
async def test_summary_engine_skips_cycle_when_llm_fails() -> None:
    config = DetectorConfig(enable_summary=True)
    engine = SummaryEngine(
        config,
        WebSocketConfig(),
        client=_FakeSummaryClient(should_raise=True),
    )
    await engine.enqueue("Dit gaat mis", "prospect", 10)

    called = {"published": False}

    async def _capture(payload: dict[str, object]) -> None:  # noqa: ARG001
        called["published"] = True

    engine._publish_payload = _capture  # type: ignore[method-assign] # noqa: SLF001
    await engine._publish_summary_cycle()  # noqa: SLF001

    assert called["published"] is False


@pytest.mark.asyncio
async def test_detector_main_does_not_open_summary_channel_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []

    class _FakeWS:
        async def recv(self) -> str:
            await asyncio.sleep(0.01)
            return ""

        async def close(self) -> None:
            return

    class _Connect:
        def __init__(self, url: str) -> None:
            self._url = url

        async def __aenter__(self) -> _FakeWS:
            urls.append(self._url)
            return _FakeWS()

        async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return

    class _FakeRouter:
        def __init__(self) -> None:
            self.routes: list[object] = []

    class _FakeCaseDB:
        def __init__(self, _path: str) -> None:
            return

        async def initialize(self) -> None:
            return

        async def list_cases(self) -> list[object]:
            return []

    class _FakeInjector:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return

        async def process_transcript(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            return

        async def close(self) -> None:
            return

    monkeypatch.setattr(detector_main.websockets, "connect", lambda url: _Connect(url))
    monkeypatch.setattr(detector_main, "PainPointRouter", lambda _cfg, *_args: _FakeRouter())
    monkeypatch.setattr(detector_main, "LLMConfirmClient", lambda *args, **kwargs: object())
    monkeypatch.setattr(detector_main, "PainPointDebouncer", lambda _seconds: object())
    monkeypatch.setattr(detector_main, "DetectionPipeline", lambda *args, **kwargs: object())
    monkeypatch.setattr(detector_main, "SQLiteCaseDB", _FakeCaseDB)
    monkeypatch.setattr(detector_main, "SlideInjector", _FakeInjector)

    stop_event = asyncio.Event()
    stop_event.set()
    await detector_main.main(
        detector_config=DetectorConfig(
            auto_phase_detection=False,
            enable_objection_detection=False,
            enable_suggestions=False,
            enable_summary=False,
        ),
        stop_event=stop_event,
        register_signals=False,
    )

    assert all(not url.endswith("/ws/summary") for url in urls)


def test_summary_llm_client_builds_vertex_client_without_error() -> None:
    """Regression: summary.py previously raised ValueError for vertex provider."""
    fake_genai_client = MagicMock()
    fake_patched = MagicMock()
    fake_patched.chat.completions.create = MagicMock()

    fake_genai_module = MagicMock()
    fake_genai_module.Client.return_value = fake_genai_client

    fake_instructor = MagicMock()
    fake_instructor.from_genai.return_value = fake_patched
    fake_instructor.Mode.GENAI_STRUCTURED_OUTPUTS = "GENAI_STRUCTURED_OUTPUTS"

    with (
        patch.dict("sys.modules", {"google": MagicMock(genai=fake_genai_module), "google.genai": fake_genai_module}),
        patch("sales_copilot.core.llm_client.instructor", fake_instructor),
    ):
        config = DetectorConfig(llm_provider="vertex")
        client = SummaryLLMClient(config)

    # The seam now also injects http_options=HttpOptions(timeout=...) for vertex, so assert the
    # BYO-tenant kwargs as a subset rather than an exact match.
    client_kwargs = fake_genai_module.Client.call_args.kwargs
    assert client_kwargs["vertexai"] is True
    assert client_kwargs["project"] == "vnx-sales-copilot"
    assert client_kwargs["location"] == "europe-west4"
    assert "http_options" in client_kwargs
    fake_instructor.from_genai.assert_called_once_with(
        fake_genai_client,
        mode="GENAI_STRUCTURED_OUTPUTS",
    )
    assert client.provider == "vertex"


@pytest.mark.asyncio
async def test_summary_llm_client_vertex_call_omits_temperature() -> None:
    """Vertex path must not pass temperature= kwarg (unsupported by genai SDK)."""
    fake_create = MagicMock(return_value=ConversationSummary(text="ok", key_moments=[]))
    fake_genai_client = MagicMock()
    fake_patched = MagicMock()
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
        config = DetectorConfig(llm_provider="vertex")
        llm_client = SummaryLLMClient(config)
        await llm_client.summarize(["prospect: test"], [])

    _, kwargs = fake_create.call_args
    assert "temperature" not in kwargs, "vertex path must not pass temperature kwarg"


def test_summary_llm_client_gemini_uses_from_genai_not_patch() -> None:
    """Gemini must use from_genai(GENAI_STRUCTURED_OUTPUTS), not instructor.patch."""
    fake_genai_client = MagicMock()
    fake_patched = MagicMock()
    fake_patched.chat.completions.create = MagicMock()

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
        SummaryLLMClient(config)

    fake_instructor.from_genai.assert_called_once_with(
        fake_genai_client,
        mode="GENAI_STRUCTURED_OUTPUTS",
    )
    fake_instructor.patch.assert_not_called()


@pytest.mark.asyncio
async def test_summary_llm_client_gemini_call_uses_messages_omits_temperature() -> None:
    """Gemini path must use messages format and omit temperature."""
    fake_create = MagicMock(return_value=ConversationSummary(text="ok", key_moments=[]))
    fake_genai_client = MagicMock()
    fake_patched = MagicMock()
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
        llm_client = SummaryLLMClient(config)
        await llm_client.summarize(["prospect: test"], [])

    _, kwargs = fake_create.call_args
    assert "messages" in kwargs, "gemini path must use messages format"
    assert "contents" not in kwargs, "gemini path must not use contents kwarg"
    assert "temperature" not in kwargs, "gemini path must not pass temperature kwarg"


@pytest.mark.asyncio
async def test_summary_llm_client_groq_call_uses_messages_with_temperature() -> None:
    """Groq (OpenAI-compatible) path must pass messages and temperature."""
    fake_create = MagicMock(return_value=ConversationSummary(text="ok", key_moments=[]))
    fake_openai_client = MagicMock()
    fake_patched = MagicMock()
    fake_patched.chat.completions.create = fake_create

    fake_openai_module = MagicMock()
    fake_openai_module.OpenAI.return_value = fake_openai_client

    fake_instructor = MagicMock()
    fake_instructor.from_openai.return_value = fake_patched

    with (
        patch.dict("sys.modules", {"openai": fake_openai_module}),
        patch("sales_copilot.core.llm_client.instructor", fake_instructor),
    ):
        config = DetectorConfig(llm_provider="groq", llm_model="llama-3.3-70b-versatile")
        llm_client = SummaryLLMClient(config)
        await llm_client.summarize(["prospect: we lopen vast"], [])

    fake_instructor.from_openai.assert_called_once_with(fake_openai_client)
    _, kwargs = fake_create.call_args
    assert "messages" in kwargs
    assert "temperature" in kwargs
    assert kwargs["model"] == "llama-3.3-70b-versatile"
