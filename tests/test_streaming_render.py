"""Automated regression for the streaming-render path (replaces manual watch-the-screen QA).

PR #85 ("render streaming partials + reuse WS connection across a stream") established the
contract that with ``LLM_STREAMING=true`` the dashboard and presentation fill *incrementally*:
the suggestions panel renders ``suggestion_partial`` messages in place (streaming marker) before
the final ``suggestion`` finalizes them, and the deck grows a generated-slide section from
``update_generated_slide_partial`` messages before ``inject_generated_slide`` finalizes it.

The manual verification was "run the app with LLM_STREAMING=true and confirm suggestions + slides
fill the screen live". These tests automate that end to end: they drive the *real* streaming code
paths (SuggestionLLMClient.suggest / SlideGenerator.generate) over the *real* WebSocket hub, with a
consumer socket standing in for the browser, and assert that partial messages arrive incrementally
(not just one final message) and are well-formed per the ``docs/TTD.md`` section-7 schema the
dashboard/presentation JS consumes.

Only the LLM SDK seam (``LLMClient._create_partial`` / ``._create``) is mocked, so a regression in
the streaming drain, the partial-publish plumbing, the connection reuse, or the payload shape trips
these tests — no real LLM or network is involved.
"""

from __future__ import annotations

import asyncio
import json
import types
from collections.abc import Callable, Iterator

import pytest
import websockets

from sales_copilot.auth.feature_policy import FeaturePolicy
from sales_copilot.core.config import DetectorConfig, SlidesConfig, WebSocketConfig
from sales_copilot.modules.copilot.injector import SlideInjector
from sales_copilot.modules.copilot.slide_generator import GeneratedSlide, SlideGenerator
from sales_copilot.modules.detector.pipeline import PainPointEvent
from sales_copilot.modules.detector.suggestions import SuggestionEngine, SuggestionResponse
from sales_copilot.websocket import hub
from tests.ws_helpers import ws_url

# openai is an OpenAI-compatible (non-genai) provider, so it exercises the true partial-streaming
# branch of ``LLMClient.astream``. The autouse ``_dummy_llm_provider_keys`` conftest fixture pins
# OPENAI_API_KEY, so client construction succeeds without a real key; the SDK seam is mocked below.
_STREAMING_PROVIDER = "openai"
_STREAMING_MODEL = "gpt-4o-mini"

# Terminal messages that finalize each stream (the browser stops showing the "streaming" marker
# once these land). Collection stops as soon as one arrives.
_SUGGESTION_FINAL = "suggestion"
_SLIDE_FINAL = "inject_generated_slide"


async def _await_subscriber(channel: str, *, timeout: float = 3.0) -> None:
    """Block until the hub has registered a subscriber for ``channel``.

    ``websockets.connect`` returns once the handshake completes, but the server-side handler
    adds the socket to ``hub._subscribers`` a scheduling tick later. Polling here removes the
    connect/subscribe race so the very first partial is never broadcast before the consumer
    is actually subscribed.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hub._subscribers.get(channel):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"hub never registered a subscriber for channel {channel!r}")


async def _collect_stream(
    consumer: websockets.ClientConnection,
    *,
    terminal: Callable[[dict[str, object]], bool],
    per_recv_timeout: float = 3.0,
    max_messages: int = 20,
) -> list[dict[str, object]]:
    """Drain messages from ``consumer`` in wire order until the terminal message arrives.

    Returns the messages in the exact order the browser would render them, so the caller can
    assert partials precede the finalizing message.
    """
    messages: list[dict[str, object]] = []
    while len(messages) < max_messages:
        try:
            raw = await asyncio.wait_for(consumer.recv(), timeout=per_recv_timeout)
        except TimeoutError:
            break
        payload = json.loads(raw)
        messages.append(payload)
        if terminal(payload):
            break
    return messages


def _suggestion_partial_stream(**_kwargs: object) -> Iterator[SuggestionResponse]:
    """A deterministic 3-step suggestion stream: text grows, then a second question appears.

    Mirrors what instructor's ``create_partial`` yields (progressively-filled response models).
    The last yielded object is what ``suggest()`` returns as the final questions.
    """
    yield SuggestionResponse(questions=["Wat"])
    yield SuggestionResponse(questions=["Wat is de grootste blokkade?"])
    yield SuggestionResponse(questions=["Wat is de grootste blokkade?", "Wie beslist er uiteindelijk?"])


def _slide_partial_stream(**_kwargs: object) -> Iterator[GeneratedSlide]:
    """A deterministic 3-step generated-slide stream: description then metrics fill in."""
    yield GeneratedSlide(title="Sneller offerteren", description="", metrics=[])
    yield GeneratedSlide(title="Sneller offerteren", description="Automatisering", metrics=[])
    yield GeneratedSlide(
        title="Sneller offerteren",
        description="Automatisering verkortte de doorlooptijd.",
        metrics=["-42% offerte doorlooptijd", "6 uur per week bespaard"],
    )


class _NoCaseDB:
    """CaseDB stub with no matching case, forcing the dynamic-slide generation branch."""

    async def find_case(self, pain_point: str, industry: str | None = None) -> None:
        return None


@pytest.mark.asyncio
async def test_suggestion_partials_stream_to_dashboard_consumer_over_hub(running_hub: int) -> None:
    """Streaming suggestions reach a dashboard-style consumer incrementally over the real hub.

    Regression tripwire: if the streaming drain stops firing ``on_partial`` (regressing #79/#85),
    or the partial-publish plumbing / connection reuse breaks, the consumer sees only the final
    ``suggestion`` and ``len(partials) >= 2`` fails — exactly the "screen no longer fills live"
    symptom the manual QA step used to catch.
    """
    ws_config = WebSocketConfig(host="127.0.0.1", port=running_hub)
    config = DetectorConfig(
        llm_provider=_STREAMING_PROVIDER,
        llm_model=_STREAMING_MODEL,
        enable_suggestions=True,
    )
    assert config.llm_streaming is True

    engine = SuggestionEngine(config, ws_config)
    # Mock only the LLM SDK seam; the whole streaming path (suggest -> astream -> on_partial ->
    # _publish_partial_suggestion -> _publish_payload -> hub) is the real product code.
    engine.client._llm._create_partial = _suggestion_partial_stream

    consumer_uri = ws_url("127.0.0.1", running_hub, "suggestions")
    try:
        async with websockets.connect(consumer_uri) as consumer:
            await _await_subscriber("suggestions")

            engine._record_line("We verliezen vaak tijd in offertes.", 0)
            await engine._flush_pending()

            messages = await _collect_stream(
                consumer,
                terminal=lambda m: m.get("type") == _SUGGESTION_FINAL,
            )
    finally:
        await engine.close()

    partials = [m for m in messages if m.get("type") == "suggestion_partial"]
    finals = [m for m in messages if m.get("type") == _SUGGESTION_FINAL]

    # (a) Incremental delivery: multiple partials, not just one final message.
    assert len(partials) >= 2, f"expected incremental suggestion partials over the hub, got {messages!r}"
    assert len(finals) == 1, f"expected exactly one finalizing suggestion, got {finals!r}"
    # The final message arrives strictly after the last partial on the wire (finalizes in place).
    assert messages.index(partials[-1]) < messages.index(finals[0])

    # The stream actually grew rather than emitting the finished set at once.
    assert partials[0]["questions"] == ["Wat"]
    assert finals[0]["questions"] == [
        "Wat is de grootste blokkade?",
        "Wie beslist er uiteindelijk?",
    ]

    # (b) Well-formed per the dashboard-consumed schema: suggestions.js requires
    # ``Array.isArray(payload.questions)`` and filters to non-empty strings.
    for message in partials + finals:
        questions = message["questions"]
        assert isinstance(questions, list) and questions
        assert all(isinstance(q, str) and q.strip() for q in questions)
        assert isinstance(message["timestamp_ms"], int)


@pytest.mark.asyncio
async def test_generated_slide_partials_stream_to_presentation_consumer_over_hub(
    running_hub: int,
    pro_feature_policy: FeaturePolicy,
) -> None:
    """A dynamically generated slide reaches a presentation-style consumer incrementally.

    Regression tripwire: if the injector stops forwarding ``update_generated_slide_partial``
    messages (regressing the #85 in-place deck growth), the consumer only sees the final
    ``inject_generated_slide`` and ``len(partials) >= 2`` fails.
    """
    ws_config = WebSocketConfig(host="127.0.0.1", port=running_hub)
    event = PainPointEvent(
        category="offerteproces",
        confidence=0.9,
        trigger_phrase="offertes blijven hangen",
        timestamp_ms=200,
    )
    slide_generator = SlideGenerator(
        config=DetectorConfig(llm_provider=_STREAMING_PROVIDER, llm_model=_STREAMING_MODEL)
    )
    slide_generator._llm._create_partial = _slide_partial_stream

    injector = SlideInjector(
        types.SimpleNamespace(llm_client=None),
        _NoCaseDB(),
        ws_config,
        SlidesConfig(dynamic_slides=True),
        slide_generator=slide_generator,
        feature_policy=pro_feature_policy,
    )

    consumer_uri = ws_url("127.0.0.1", running_hub, "slide-control")
    try:
        async with websockets.connect(consumer_uri) as consumer:
            await _await_subscriber("slide-control")

            result = await injector.process_transcript(
                "offertes blijven hangen",
                "prospect",
                3210,
                precomputed_event=event,
            )

            messages = await _collect_stream(
                consumer,
                terminal=lambda m: m.get("action") == _SLIDE_FINAL,
            )
    finally:
        await injector.close()

    assert result is not None

    partials = [m for m in messages if m.get("action") == "update_generated_slide_partial"]
    finals = [m for m in messages if m.get("action") == _SLIDE_FINAL]

    # (a) Incremental delivery over the hub, not a single final injection.
    assert len(partials) >= 2, f"expected incremental generated-slide partials, got {messages!r}"
    assert len(finals) == 1, f"expected exactly one finalizing slide injection, got {finals!r}"
    assert messages.index(partials[-1]) < messages.index(finals[0])

    # The final slide carries the fully-streamed content the last partial had not yet completed.
    assert finals[0]["slide"]["description"].startswith("Automatisering verkortte")
    assert finals[0]["slide"]["metrics"] == [
        "-42% offerte doorlooptijd",
        "6 uur per week bespaard",
    ]

    # (b) Well-formed per the slide-control schema the presentation ws-client.js consumes:
    # it keys on the same ``pain_point`` and requires a string ``slide.title`` with a bounded
    # description + <=2 metric strings.
    for message in partials + finals:
        assert message["pain_point"] == "offerteproces"
        slide = message["slide"]
        assert isinstance(slide, dict)
        assert isinstance(slide["title"], str) and slide["title"].strip()
        assert isinstance(slide["description"], str)
        assert isinstance(slide["metrics"], list)
        assert all(isinstance(metric, str) for metric in slide["metrics"])
        assert len(slide["metrics"]) <= 2


@pytest.mark.asyncio
async def test_streaming_disabled_delivers_only_final_suggestion_over_hub(running_hub: int) -> None:
    """With ``LLM_STREAMING=false`` the consumer must see the final ``suggestion`` only.

    This is the negative control that gives the incremental-render assertions their teeth: when
    streaming is off (or broken), zero ``suggestion_partial`` messages reach the hub. If a future
    change silently disabled streaming, the two tests above would regress to exactly this shape.
    """
    ws_config = WebSocketConfig(host="127.0.0.1", port=running_hub)
    config = DetectorConfig(
        llm_provider=_STREAMING_PROVIDER,
        llm_model=_STREAMING_MODEL,
        enable_suggestions=True,
        llm_streaming=False,
    )
    engine = SuggestionEngine(config, ws_config)

    def _final_only(**_kwargs: object) -> SuggestionResponse:
        return SuggestionResponse(
            questions=["Wat is de grootste blokkade?", "Wie beslist er uiteindelijk?"]
        )

    engine.client._llm._create = _final_only

    consumer_uri = ws_url("127.0.0.1", running_hub, "suggestions")
    try:
        async with websockets.connect(consumer_uri) as consumer:
            await _await_subscriber("suggestions")

            engine._record_line("We verliezen vaak tijd in offertes.", 0)
            await engine._flush_pending()

            messages = await _collect_stream(
                consumer,
                terminal=lambda m: m.get("type") == _SUGGESTION_FINAL,
            )
    finally:
        await engine.close()

    assert [m for m in messages if m.get("type") == "suggestion_partial"] == []
    finals = [m for m in messages if m.get("type") == _SUGGESTION_FINAL]
    assert len(finals) == 1
    assert finals[0]["questions"] == [
        "Wat is de grootste blokkade?",
        "Wie beslist er uiteindelijk?",
    ]
