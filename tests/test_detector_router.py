import asyncio
import dataclasses
import logging
import time

import pytest

from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector.router import PainPointRouter, RouteMatch


@pytest.fixture(scope="session")
def pain_point_router() -> PainPointRouter:
    config = DetectorConfig.from_env()
    return PainPointRouter(config)


def test_loads_all_routes(pain_point_router: PainPointRouter) -> None:
    # 12 pain-point routes + the shared "negative" class, wired by default
    # since 2026-09-05 (structural precision fix, mirrors ObjectionRouter's
    # INCLUDE_NEGATIVES -- see tests/test_pain_point_router_negatives.py).
    assert len(pain_point_router.routes) == 13
    assert "negative" in {route.name for route in pain_point_router.routes}


def test_classifies_offerteproces(pain_point_router: PainPointRouter) -> None:
    match = pain_point_router.classify("we zitten uren aan offertes")

    assert match is not None
    assert match.category == "offerteproces"
    assert match.confidence >= 0.80


def test_returns_none_for_non_match(pain_point_router: PainPointRouter) -> None:
    assert pain_point_router.classify("het weer is mooi vandaag") is None


def test_classification_under_100ms(pain_point_router: PainPointRouter) -> None:
    pain_point_router.classify("warmup")
    start = time.perf_counter()
    pain_point_router.classify("we zitten uren aan offertes")
    duration = time.perf_counter() - start

    assert duration < 0.1


@pytest.mark.asyncio
async def test_classify_async_matches_sync(pain_point_router: PainPointRouter) -> None:
    text = "we zitten uren aan offertes"
    sync_match = pain_point_router.classify(text)
    async_match = await pain_point_router.classify_async(text)

    assert async_match is not None
    assert async_match == sync_match


# --- _keyword_match structural fix (2026-09-05) -----------------------------
# Live over-match: "een maand of vijf, zes" classified as pain point
# `rapportage` at a hardcoded confidence 0.95. Traced to _keyword_match's old
# `score = len(overlap) + (1 if long_token_overlap else 0)` with bar
# `score >= 2`: a SINGLE shared 7+ char token cleared that bar on its own, and
# every keyword match returned a constant 0.95/"high" regardless of overlap
# strength. See scripts/eval_pain_point_precision.py for the full precision
# measurement.


def test_single_token_overlap_no_longer_matches(pain_point_router: PainPointRouter) -> None:
    """A single shared token (even a long, on-topic one like "offertes") is
    coincidence, not evidence — it must not produce a keyword match at all,
    let alone a high-tier one. The old algorithm returned high/0.95 here."""
    match = pain_point_router._keyword_match("we zitten uren aan offertes")  # noqa: SLF001
    assert match is None


def test_genuine_multi_word_overlap_still_matches(pain_point_router: PainPointRouter) -> None:
    """Real multi-word evidence for a route still classifies, at a tier that
    reflects the actual overlap instead of an unconditional 0.95/"high"."""
    moderate = pain_point_router._keyword_match(  # noqa: SLF001
        "de rapportages kosten ons enorm veel dagen"
    )
    assert moderate is not None
    assert moderate.category == "rapportage"
    assert moderate.tier == "uncertain"

    strong = pain_point_router._keyword_match(  # noqa: SLF001
        "het management vraagt best vaak om cijfers die we niet kunnen leveren"
    )
    assert strong is not None
    assert strong.category == "rapportage"
    assert strong.tier == "high"


def test_keyword_confidence_varies_with_overlap_strength(pain_point_router: PainPointRouter) -> None:
    """Confidence must reflect actual overlap, not a hardcoded constant."""
    moderate = pain_point_router._keyword_match(  # noqa: SLF001
        "de rapportages kosten ons enorm veel dagen"
    )
    strong = pain_point_router._keyword_match(  # noqa: SLF001
        "het management vraagt best vaak om cijfers die we niet kunnen leveren"
    )
    assert moderate is not None and strong is not None
    assert moderate.confidence != 0.95
    assert strong.confidence != 0.95
    assert moderate.confidence < strong.confidence


def test_regression_case_does_not_classify_as_rapportage(pain_point_router: PainPointRouter) -> None:
    """The exact live over-match reported 2026-09-05 must not recur."""
    match = pain_point_router.classify("een maand of vijf, zes")
    assert match is None or match.category != "rapportage"


# --- below-threshold visibility (D-c197e0c3) --------------------------------
#
# On 2026-09-05, 9 live transcripts arrived with 0 detections and the log gave
# no way to tell "detector never connected" from "everything scored below the
# threshold" -- both discard the winning category and score on the way to
# returning None. These tests drive a chunk under the threshold through both
# the keyword and the embedding path and assert the DEBUG line survives with
# the runner-up route name, its score, and the threshold it missed -- not just
# that some log record exists.


def test_keyword_match_logs_best_candidate_below_threshold(
    pain_point_router: PainPointRouter, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        pain_point_router, "config", dataclasses.replace(pain_point_router.config, confidence_threshold_low=0.999)
    )

    with caplog.at_level(logging.DEBUG, logger="sales_copilot.modules.detector.router"):
        match = pain_point_router._keyword_match("de rapportages kosten ons enorm veel dagen")  # noqa: SLF001

    assert match is None
    debug_records = [r for r in caplog.records if "Keyword match below threshold" in r.getMessage()]
    assert debug_records, "expected a DEBUG line naming the discarded keyword candidate"
    msg = debug_records[0].getMessage()
    assert "best_route=rapportage" in msg
    assert "threshold=0.999" in msg
    assert "score=" in msg


def test_classify_logs_best_candidate_below_threshold_for_embedding_path(
    pain_point_router: PainPointRouter, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # "we zitten uren aan offertes" has only a single shared token, so
    # _keyword_match already returns None here (see
    # test_single_token_overlap_no_longer_matches) -- classify() falls through
    # to the embedding path, which is the branch under test.
    monkeypatch.setattr(
        pain_point_router, "config", dataclasses.replace(pain_point_router.config, confidence_threshold_low=0.999)
    )

    with caplog.at_level(logging.DEBUG, logger="sales_copilot.modules.detector.router"):
        match = pain_point_router.classify("we zitten uren aan offertes")

    assert match is None
    debug_records = [r for r in caplog.records if "Embedding match below threshold" in r.getMessage()]
    assert debug_records, "expected a DEBUG line naming the discarded embedding candidate"
    msg = debug_records[0].getMessage()
    assert "best_route=offerteproces" in msg
    assert "threshold=0.999" in msg
    assert "score=" in msg


# --- silent-drop visibility (D-fdbb9dfd) -------------------------------------
#
# T0 review found two more exits in classify() that discarded a chunk without
# a log line: the embedding router returning no route at all (choice is None),
# and a match resolving to the negative class (added in #236). Both currently
# collapse to the same silent `return None` as every other rejection, so "the
# embedding router found nothing" and "something matched but was filtered as
# negative" are indistinguishable from the outside -- exactly the ambiguity
# that produced "0 detections" on 2026-09-05. These tests force each state via
# monkeypatch and assert the DEBUG line survives with enough detail to tell
# the two apart from the log alone.


def test_classify_logs_when_embedding_router_returns_no_route(
    pain_point_router: PainPointRouter, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # "we zitten uren aan offertes" has only a single shared token, so
    # _keyword_match already returns None here (see
    # test_single_token_overlap_no_longer_matches) -- classify() falls through
    # to the embedding path, which is the branch under test.
    monkeypatch.setattr(pain_point_router, "_ensure_router", lambda: (lambda _text: None))

    with caplog.at_level(logging.DEBUG, logger="sales_copilot.modules.detector.router"):
        match = pain_point_router.classify("we zitten uren aan offertes")

    assert match is None
    debug_records = [r for r in caplog.records if "no route" in r.getMessage().lower()]
    assert debug_records, "expected a DEBUG line when the embedding router returns no route at all"


def test_classify_logs_when_match_resolves_to_negative_class(
    pain_point_router: PainPointRouter, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    negative_match = RouteMatch(category="negative", confidence=0.91, tier="high", source="keyword")
    monkeypatch.setattr(pain_point_router, "_keyword_match", lambda _text: negative_match)

    with caplog.at_level(logging.DEBUG, logger="sales_copilot.modules.detector.router"):
        match = pain_point_router.classify("een maand of vijf, zes")

    assert match is None
    debug_records = [r for r in caplog.records if "negative class" in r.getMessage().lower()]
    assert debug_records, "expected a DEBUG line naming the discarded negative-class candidate"
    msg = debug_records[0].getMessage()
    assert "negative" in msg
    assert "0.91" in msg or "0.910" in msg


@pytest.mark.asyncio
async def test_classify_async_times_out_to_none() -> None:
    """A stalled model load/inference is bounded and degrades to no match.

    Guards the detector event loop: classify runs off-loop in a thread and a hang
    must return None rather than wedging all detection.
    """
    router = PainPointRouter(DetectorConfig.from_env())
    router._CLASSIFY_TIMEOUT_S = 0.05  # noqa: SLF001

    def _slow(_text: str) -> RouteMatch:
        time.sleep(0.3)
        return RouteMatch(category="x", confidence=1.0, tier="high")

    router.classify = _slow  # type: ignore[method-assign]  # shadow with a slow stand-in

    result = await asyncio.wait_for(router.classify_async("iets"), timeout=2.0)
    assert result is None
