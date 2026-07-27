import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sales_copilot.core.config import DetectorConfig, WebSocketConfig
from sales_copilot.modules.detector.suggestions import (
    SUGGESTIONS_MAX_OUTPUT_TOKENS,
    ObjectionResponsePicker,
    SuggestionEngine,
    SuggestionLLMClient,
    SuggestionResponse,
    _normalize_questions,
)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        if self.closed:
            raise RuntimeError("send on closed websocket")
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True


class _FakeSuggestionClient:
    def __init__(self, questions: list[str]) -> None:
        self.questions = questions
        self.calls: list[list[str]] = []
        self.on_partial_calls: list[object] = []

    async def suggest(
        self,
        transcript_lines: list[str],
        *,
        on_partial=None,
    ) -> list[str]:
        self.calls.append(list(transcript_lines))
        self.on_partial_calls.append(on_partial)
        return list(self.questions)


class _Clock:
    def __init__(self) -> None:
        self.value = 0

    def now(self) -> int:
        return self.value

    def advance(self, ms: int) -> None:
        self.value += ms


@pytest.mark.asyncio
async def test_suggestion_engine_generates_questions_from_recent_prospect_lines() -> None:
    clock = _Clock()
    client = _FakeSuggestionClient(
        [
            "Hoe ziet jullie huidige proces er vandaag uit?",
            "Wat maakt dit nu het meest urgent?",
        ]
    )
    engine = SuggestionEngine(
        DetectorConfig(enable_suggestions=True),
        WebSocketConfig(),
        client=client,
        now_ms=clock.now,
        debounce_ms=20,
    )
    published: list[dict[str, object]] = []

    async def _capture(payload: dict[str, object]) -> None:
        published.append(payload)

    engine._publish_payload = _capture  # type: ignore[method-assign]

    stop_event = asyncio.Event()
    task = asyncio.create_task(engine.run(stop_event))

    await engine.enqueue("We verliezen vaak tijd in offertes.", "prospect", 0)
    await engine.enqueue("Kun je dat verder toelichten?", "self", 5)
    await engine.enqueue("Vooral door handmatig opvolgen.", "prospect", 10)

    await asyncio.sleep(0.01)
    clock.advance(25)
    await asyncio.sleep(0.08)
    stop_event.set()
    await task

    assert client.calls == [["We verliezen vaak tijd in offertes.", "Vooral door handmatig opvolgen."]]
    assert published == [
        {
            "type": "suggestion",
            "questions": [
                "Hoe ziet jullie huidige proces er vandaag uit?",
                "Wat maakt dit nu het meest urgent?",
            ],
            "timestamp_ms": 25,
        }
    ]


@pytest.mark.asyncio
async def test_suggestion_engine_skips_publish_when_client_returns_no_questions() -> None:
    clock = _Clock()
    client = _FakeSuggestionClient([])
    engine = SuggestionEngine(
        DetectorConfig(enable_suggestions=True),
        WebSocketConfig(),
        client=client,
        now_ms=clock.now,
        debounce_ms=20,
    )
    published: list[dict[str, object]] = []

    async def _capture(payload: dict[str, object]) -> None:
        published.append(payload)

    engine._publish_payload = _capture  # type: ignore[method-assign]

    stop_event = asyncio.Event()
    task = asyncio.create_task(engine.run(stop_event))

    await engine.enqueue("We missen inzicht in statusupdates.", "prospect", 0)
    await asyncio.sleep(0.01)
    clock.advance(25)
    await asyncio.sleep(0.08)
    stop_event.set()
    await task

    assert len(client.calls) == 1
    assert published == []


def test_normalize_questions_limits_to_unique_three() -> None:
    normalized = _normalize_questions(
        [
            "Welke impact heeft dit op het team?",
            "Welke impact heeft dit op het team?",
            "",
            "Wat gebeurt er als dit zo blijft?",
            "Wie voelt dit probleem het sterkst?",
            "Deze vierde vraag moet wegvallen.",
        ]
    )

    assert normalized == [
        "Welke impact heeft dit op het team?",
        "Wat gebeurt er als dit zo blijft?",
        "Wie voelt dit probleem het sterkst?",
    ]


def test_suggestion_client_user_prompt_mentions_salesperson_follow_up() -> None:
    client = SuggestionLLMClient.__new__(SuggestionLLMClient)

    prompt = client._user_prompt(["We lopen vast in onboarding.", "Dat kost ons veel tijd."])

    assert "Based on what the prospect said" in prompt
    assert "suggest 2-3 follow-up questions for the salesperson" in prompt
    assert "- We lopen vast in onboarding." in prompt


# ---------------------------------------------------------------------------
# SuggestionLLMClient.suggest — streaming (default) vs. LLM_STREAMING=false
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggest_streaming_default_yields_partials_and_final_object() -> None:
    """llm_streaming defaults True -> suggest() drains astream(), invoking on_partial for
    every partial and returning the final, fully-formed object's questions."""
    config = DetectorConfig(llm_provider="openrouter", llm_model="m")
    assert config.llm_streaming is True
    client = SuggestionLLMClient(config)

    def _fake_create_partial(**kwargs):
        yield SuggestionResponse(questions=["Wat"])
        yield SuggestionResponse(questions=["Wat is de grootste blokkade?"])

    client._llm._create_partial = _fake_create_partial

    partials: list[SuggestionResponse] = []

    async def _on_partial(partial: SuggestionResponse) -> None:
        partials.append(partial)

    questions = await client.suggest(["We lopen vast in offertes."], on_partial=_on_partial)

    assert questions == ["Wat is de grootste blokkade?"]
    assert [p.questions for p in partials] == [["Wat"], ["Wat is de grootste blokkade?"]]


@pytest.mark.asyncio
async def test_suggest_llm_streaming_false_uses_acreate_and_skips_partial_callback() -> None:
    """LLM_STREAMING=false must still produce the same final object via the non-streaming
    acreate() path, and on_partial must never fire (there is nothing partial to report)."""
    config = DetectorConfig(llm_provider="openrouter", llm_model="m", llm_streaming=False)
    client = SuggestionLLMClient(config)

    def _fake_create(**kwargs):
        return SuggestionResponse(questions=["Wat is de grootste blokkade?"])

    client._llm._create = _fake_create

    partial_calls: list[SuggestionResponse] = []

    async def _on_partial(partial: SuggestionResponse) -> None:
        partial_calls.append(partial)

    questions = await client.suggest(["We lopen vast in offertes."], on_partial=_on_partial)

    assert questions == ["Wat is de grootste blokkade?"]
    assert partial_calls == []


@pytest.mark.asyncio
async def test_suggest_passes_max_output_tokens_cap_in_both_modes() -> None:
    streaming_config = DetectorConfig(llm_provider="openrouter", llm_model="m")
    streaming_client = SuggestionLLMClient(streaming_config)
    streaming_calls: list[dict] = []

    def _fake_create_partial(**kwargs):
        streaming_calls.append(kwargs)
        yield SuggestionResponse(questions=["Q"])

    streaming_client._llm._create_partial = _fake_create_partial
    await streaming_client.suggest(["We lopen vast."])
    assert streaming_calls[0]["max_tokens"] == SUGGESTIONS_MAX_OUTPUT_TOKENS

    non_streaming_config = DetectorConfig(llm_provider="openrouter", llm_model="m", llm_streaming=False)
    non_streaming_client = SuggestionLLMClient(non_streaming_config)
    non_streaming_calls: list[dict] = []

    def _fake_create(**kwargs):
        non_streaming_calls.append(kwargs)
        return SuggestionResponse(questions=["Q"])

    non_streaming_client._llm._create = _fake_create
    await non_streaming_client.suggest(["We lopen vast."])
    assert non_streaming_calls[0]["max_tokens"] == SUGGESTIONS_MAX_OUTPUT_TOKENS


# ---------------------------------------------------------------------------
# SuggestionEngine._flush_pending — partial-event emission
# ---------------------------------------------------------------------------


class _StreamingFakeSuggestionClient:
    """Fake that actually drives ``on_partial`` -- mirrors SuggestionLLMClient.suggest()'s
    streaming contract, unlike ``_FakeSuggestionClient`` which ignores the callback."""

    def __init__(self, partials: list[list[str]], final: list[str]) -> None:
        self._partials = partials
        self._final = final

    async def suggest(self, transcript_lines: list[str], *, on_partial=None) -> list[str]:
        if on_partial is not None:
            for questions in self._partials:
                await on_partial(SuggestionResponse(questions=questions))
        return list(self._final)


@pytest.mark.asyncio
async def test_suggestion_engine_publishes_partial_events_during_generation() -> None:
    clock = _Clock()
    client = _StreamingFakeSuggestionClient(
        partials=[["Wat"], ["Wat is de grootste blokkade?"]],
        final=["Wat is de grootste blokkade?", "Wie beslist er uiteindelijk?"],
    )
    engine = SuggestionEngine(
        DetectorConfig(enable_suggestions=True),
        WebSocketConfig(),
        client=client,
        now_ms=clock.now,
        debounce_ms=20,
    )
    published: list[dict[str, object]] = []

    async def _capture(payload: dict[str, object]) -> None:
        published.append(payload)

    engine._publish_payload = _capture  # type: ignore[method-assign]

    stop_event = asyncio.Event()
    task = asyncio.create_task(engine.run(stop_event))

    await engine.enqueue("We verliezen vaak tijd in offertes.", "prospect", 0)
    await asyncio.sleep(0.01)
    clock.advance(25)
    await asyncio.sleep(0.08)
    stop_event.set()
    await task

    partial_events = [p for p in published if p["type"] == "suggestion_partial"]
    final_events = [p for p in published if p["type"] == "suggestion"]

    assert [p["questions"] for p in partial_events] == [["Wat"], ["Wat is de grootste blokkade?"]]
    assert len(final_events) == 1
    assert final_events[0]["questions"] == [
        "Wat is de grootste blokkade?",
        "Wie beslist er uiteindelijk?",
    ]


@pytest.mark.asyncio
async def test_publish_partial_suggestion_skips_when_no_usable_questions_yet() -> None:
    """Partial fields are optional/None until that part of the JSON has streamed in -- an
    empty-questions partial must not publish a hollow event."""
    engine = SuggestionEngine(
        DetectorConfig(),
        WebSocketConfig(),
        client=_FakeSuggestionClient([]),
    )
    published: list[dict[str, object]] = []

    async def _capture(payload: dict[str, object]) -> None:
        published.append(payload)

    engine._publish_payload = _capture  # type: ignore[method-assign]

    await engine._publish_partial_suggestion(SuggestionResponse(questions=[]))

    assert published == []


# ---------------------------------------------------------------------------
# SuggestionEngine._publish_payload — WS connection reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_payload_reuses_open_connection_across_multiple_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming fires _publish_payload once per partial plus once for the final message;
    all of them must share a single WS connection instead of connecting per message."""
    engine = SuggestionEngine(DetectorConfig(enable_suggestions=True), WebSocketConfig())
    socket = _FakeWebSocket()
    connect_calls: list[str] = []

    async def fake_connect(url: str) -> _FakeWebSocket:
        connect_calls.append(url)
        return socket

    monkeypatch.setattr("sales_copilot.modules.detector.suggestions.websockets.connect", fake_connect)

    await engine._publish_payload({"type": "suggestion_partial", "questions": ["Wat"]})
    await engine._publish_payload({"type": "suggestion_partial", "questions": ["Wat is de blokkade?"]})
    await engine._publish_payload({"type": "suggestion", "questions": ["Wat is de blokkade?"]})

    assert len(connect_calls) == 1
    assert len(socket.sent) == 3
    assert [json.loads(m)["type"] for m in socket.sent] == [
        "suggestion_partial",
        "suggestion_partial",
        "suggestion",
    ]


@pytest.mark.asyncio
async def test_publish_payload_reconnects_after_connection_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SuggestionEngine(DetectorConfig(enable_suggestions=True), WebSocketConfig())
    sockets = [_FakeWebSocket(), _FakeWebSocket()]
    connect_calls: list[str] = []

    async def fake_connect(url: str) -> _FakeWebSocket:
        connect_calls.append(url)
        return sockets[len(connect_calls) - 1]

    monkeypatch.setattr("sales_copilot.modules.detector.suggestions.websockets.connect", fake_connect)

    await engine._publish_payload({"type": "suggestion", "questions": ["Eerste"]})
    assert len(connect_calls) == 1

    # Simulate the held connection dying between generations (e.g. hub restart).
    sockets[0].closed = True

    await engine._publish_payload({"type": "suggestion", "questions": ["Tweede"]})
    assert len(connect_calls) == 2
    assert json.loads(sockets[1].sent[0])["questions"] == ["Tweede"]


@pytest.mark.asyncio
async def test_publish_payload_closes_and_reraises_on_send_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SuggestionEngine(DetectorConfig(enable_suggestions=True), WebSocketConfig())

    class _FailingWebSocket(_FakeWebSocket):
        async def send(self, data: str) -> None:
            raise ConnectionError("boom")

    failing_socket = _FailingWebSocket()

    async def fake_connect(url: str) -> _FailingWebSocket:
        return failing_socket

    monkeypatch.setattr("sales_copilot.modules.detector.suggestions.websockets.connect", fake_connect)

    with pytest.raises(ConnectionError):
        await engine._publish_payload({"type": "suggestion", "questions": ["Eerste"]})

    assert failing_socket.closed is True
    assert engine._suggestions_ws is None


@pytest.mark.asyncio
async def test_suggestion_engine_close_closes_held_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = SuggestionEngine(DetectorConfig(enable_suggestions=True), WebSocketConfig())
    socket = _FakeWebSocket()

    async def fake_connect(url: str) -> _FakeWebSocket:
        return socket

    monkeypatch.setattr("sales_copilot.modules.detector.suggestions.websockets.connect", fake_connect)

    await engine._publish_payload({"type": "suggestion", "questions": ["Eerste"]})
    assert socket.closed is False

    await engine.close()
    assert socket.closed is True
    assert engine._suggestions_ws is None

    # Safe to call again once nothing is held.
    await engine.close()


@pytest.mark.asyncio
async def test_streaming_off_publish_path_still_reuses_single_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM_STREAMING=false only ever publishes the final `suggestion` message (no partials);
    the connection-reuse plumbing must not change that single-message behavior."""
    clock = _Clock()
    client = _FakeSuggestionClient(["Enige vraag?"])
    engine = SuggestionEngine(
        DetectorConfig(enable_suggestions=True, llm_streaming=False),
        WebSocketConfig(),
        client=client,
        now_ms=clock.now,
        debounce_ms=20,
    )
    socket = _FakeWebSocket()
    connect_calls: list[str] = []

    async def fake_connect(url: str) -> _FakeWebSocket:
        connect_calls.append(url)
        return socket

    monkeypatch.setattr("sales_copilot.modules.detector.suggestions.websockets.connect", fake_connect)

    stop_event = asyncio.Event()
    task = asyncio.create_task(engine.run(stop_event))

    await engine.enqueue("We verliezen vaak tijd in offertes.", "prospect", 0)
    await asyncio.sleep(0.01)
    clock.advance(25)
    await asyncio.sleep(0.08)
    stop_event.set()
    await task

    assert len(connect_calls) == 1
    assert len(socket.sent) == 1
    assert json.loads(socket.sent[0])["type"] == "suggestion"


# ---------------------------------------------------------------------------
# ObjectionResponsePicker tests
# ---------------------------------------------------------------------------

_YAML_PATH = Path(__file__).parent.parent / "config" / "objection_responses.yaml"
_YAML_EN_PATH = Path(__file__).parent.parent / "config" / "objection_responses_en.yaml"

# The curated response packs are Pro content and are absent from the OSS
# export; tests that assert on their content only run where the pack exists.
_requires_curated_pack = pytest.mark.skipif(
    not _YAML_PATH.exists(),
    reason="curated Pro response pack absent (OSS export ships detection without the paid kaartenbak)",
)


@_requires_curated_pack
def test_acknowledge_picked_early_phase() -> None:
    picker = ObjectionResponsePicker(_YAML_PATH)
    response = picker.pick("prijs", phase="early")
    assert response is not None
    assert isinstance(response, str)
    assert len(response) > 0
    # early → acknowledge bucket (not a reframe question mark at the start is not guaranteed,
    # but the response must come from the acknowledge list for "prijs")
    acknowledge_examples = [
        "Ik begrijp dat budget belangrijk is",
        "De investering is reëel",
        "Budget is altijd een afweging",
    ]
    assert any(fragment in response for fragment in acknowledge_examples), (
        f"Expected an acknowledge response for 'prijs/early', got: {response!r}"
    )


@_requires_curated_pack
def test_reframe_picked_mid_phase() -> None:
    picker = ObjectionResponsePicker(_YAML_PATH)
    response = picker.pick("prijs", phase="mid")
    assert response is not None
    reframe_examples = [
        "20% meer offertes",
        "kosten of als investering",
        "Welke prijs betaal je",
    ]
    assert any(fragment in response for fragment in reframe_examples), (
        f"Expected a reframe response for 'prijs/mid', got: {response!r}"
    )


@_requires_curated_pack
def test_evidence_picked_late_phase() -> None:
    picker = ObjectionResponsePicker(_YAML_PATH)
    response = picker.pick("prijs", phase="late")
    assert response is not None
    evidence_examples = [
        "Voorbeeld Elektrotechniek",
        "6 uur per week",
        "kost niets doen",
    ]
    assert any(fragment in response for fragment in evidence_examples), (
        f"Expected an evidence response for 'prijs/late', got: {response!r}"
    )


@_requires_curated_pack
def test_fallback_to_acknowledge_no_phase() -> None:
    picker = ObjectionResponsePicker(_YAML_PATH)
    response = picker.pick("timing", phase=None)
    assert response is not None
    acknowledge_examples = [
        "Ik snap dat het nu niet handig uitkomt",
        "hangt veel af van wat er",
        "Timing is belangrijk",
    ]
    assert any(fragment in response for fragment in acknowledge_examples), (
        f"Expected an acknowledge fallback for 'timing/None', got: {response!r}"
    )


@_requires_curated_pack
def test_load_yaml_responses() -> None:
    picker = ObjectionResponsePicker(_YAML_PATH)
    required = {"prijs", "timing", "concurrent", "scope", "autoriteit", "anders"}
    missing = required - set(picker.subcategories)
    assert not missing, f"Missing subcategories in objection_responses.yaml: {missing}"
    for subcat in required:
        for strategy in ("acknowledge", "reframe", "evidence"):
            result = picker.pick(subcat, phase={"acknowledge": "early", "reframe": "mid", "evidence": "late"}[strategy])
            assert result is not None, f"No response for {subcat}/{strategy}"


@_requires_curated_pack
def test_en_yaml_responses() -> None:
    picker = ObjectionResponsePicker(_YAML_EN_PATH)
    required = {"price", "timing", "competitor", "scope", "authority", "other"}
    missing = required - set(picker.subcategories)
    assert not missing, f"Missing subcategories in objection_responses_en.yaml: {missing}"
    for phase in ("early", "mid", "late"):
        response = picker.pick("price", phase=phase)
        assert response is not None, f"No EN response for price/{phase}"
        assert isinstance(response, str) and len(response) > 0


def test_suggestion_llm_client_gemini_uses_from_genai_not_patch() -> None:
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
        SuggestionLLMClient(config)

    fake_instructor.from_genai.assert_called_once_with(
        fake_genai_client,
        mode="GENAI_STRUCTURED_OUTPUTS",
    )
    fake_instructor.patch.assert_not_called()


@pytest.mark.asyncio
async def test_suggestion_llm_client_gemini_call_uses_messages_omits_temperature() -> None:
    """Gemini path must use messages format, no temperature (GENAI_STRUCTURED_OUTPUTS contract)."""
    fake_response = SuggestionResponse(questions=["Wat zijn de gevolgen?"])
    fake_create = MagicMock(return_value=fake_response)
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
        client = SuggestionLLMClient(config)
        await client.suggest(["We lopen vast in offertes."])

    _, kwargs = fake_create.call_args
    assert "messages" in kwargs
    assert "contents" not in kwargs
    assert "temperature" not in kwargs
