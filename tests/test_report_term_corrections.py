"""termenlijst-in-uitwerking D2: term correction inside ``enrich_report`` and in the report.

The provider SDK behind ``LLMClient`` is replaced by a local fake; the seam itself, the PII
policy and the validation stay real. Covers: valid corrections reach the report and invalid
ones are refused; by default (no ``REPORT_TERMS_*`` route of their own, see
``test_report_terms_routing.py``) the candidates ride in the ONE existing call, through ``apply_outbound_pii``
(PII modes ``off`` and ``cloud_only``); without candidates the request is the one from before;
fail-open; the field on disk with ``REPORT_REDACT_PII``; reports from before the field still
load; the ``report_enriched`` event carries counts and no correction text; delivery carries
the field; and the term list is read from the client folder after the call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from sales_copilot.core import context_docs
from sales_copilot.core import llm_client as llm_client_mod
from sales_copilot.core.config import DetectorConfig
from sales_copilot.core.llm_routing import TASKS, task_env_keys
from sales_copilot.modules.reports import __main__ as reports_main
from sales_copilot.modules.reports import enrichment as enrichment_mod
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.enrichment import ReportEnrichment, enrich_report
from sales_copilot.modules.reports.junk import JunkVerdict
from sales_copilot.modules.reports.term_corrections import TERM_CORRECTION_PROMPT, TermCorrection

TERMS = ("ROI", "Roy", "Teamleader", "Sentrix", "Bram")

SEGMENTS = (
    ("self", "We plannen alles in Team Leader."),
    ("prospect", "Dan vraag ik het aan ROI van inkoop."),
    ("prospect", "Roy belt je morgen terug."),
    ("self", "   "),
    ("self", "Het draait bij Zentrix sinds maart."),
    ("prospect", "Prima, tot dan."),
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for task in TASKS:
        for key in task_env_keys(task):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "TRUST_OWN_TENANT",
        "ALLOW_RAW_LLM_PII",
        "REPORT_REDACT_PII",
        "REPORT_DELIVERY_DIR",
        "REPORT_DELIVERY_ENDPOINT",
        "DOSSIER_AUTO_SAVE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PII_REDACTION", "off")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def _fix(source: str, occurrence: int, target: str, segment_index: int, reason: str = " context ") -> dict[str, Any]:
    return {
        "segment_index": segment_index,
        "source": source,
        "occurrence": occurrence,
        "target": target,
        "reason": reason,
    }


def _answer(corrections: list[dict[str, Any]] | None = None) -> ReportEnrichment:
    return ReportEnrichment(
        gesprek_gevoerd=True,
        short_summary="Planning besproken.",
        overview="Het ging over de planning.",
        keywords=["planning"],
        action_items=[],
        term_corrections=[TermCorrection(**c) for c in corrections or []],
    )


class _Sdk:
    """Stands in for the provider SDK behind ``LLMClient``; the seam itself stays real."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply: Callable[[dict[str, Any]], Any] = lambda kwargs: _answer()

    def build_client(self, provider: str, *, timeout_ms: int) -> object:
        return object()

    def build_create(self, client: object, provider: str):
        def _create(**kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return self.reply(kwargs)

        return _create

    def system(self) -> str:
        return self.calls[-1]["messages"][0]["content"]

    def sent(self) -> str:
        return self.calls[-1]["messages"][1]["content"]


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _Sdk:
    fake = _Sdk()
    monkeypatch.setattr(llm_client_mod, "build_client", fake.build_client)
    monkeypatch.setattr(llm_client_mod, "build_create", fake.build_create)
    return fake


@pytest.fixture()
def reports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "reports"
    monkeypatch.setattr(generator, "REPORTS_DIR", directory)
    return directory


def _config() -> DetectorConfig:
    return DetectorConfig(llm_provider="openrouter", llm_model="report-model", llm_timeout_ms=7000)


def _session(segments: tuple[tuple[str, str], ...] = SEGMENTS) -> generator.SessionData:
    return generator.SessionData(
        session_id="session-d2",
        call_start_ms=0,
        call_end_ms=60000,
        prospect_name=None,
        prospect_company="Acme BV",
        context_docs=[],
        speech_events=[],
        phase_events=[],
        pain_points=[],
        monologues=[],
        transcript=[
            generator.TranscriptSegment(speaker, text, index * 1000, index * 1000 + 900)
            for index, (speaker, text) in enumerate(segments)
        ],
    )


# ---------------------------------------------------------------------------
# enrich_report
# ---------------------------------------------------------------------------


async def test_valid_corrections_reach_the_result_and_invalid_ones_are_refused(
    sdk: _Sdk, caplog: pytest.LogCaptureFixture
) -> None:
    sdk.reply = lambda kwargs: _answer(
        [
            _fix("Team Leader", 1, "teamleader", 0),
            _fix("ROI", 1, "Roy", 1),
            _fix("Zentrix", 1, "Sentrix", 4),
            _fix("Team Leader", 2, "Teamleader", 0),  # no second occurrence
            _fix("Roy", 1, "ROI", 2),  # ROI was no candidate there: "Roy" stays a person
            _fix("Zentrix", 1, "Centrix", 4),  # not in the list
            _fix("bij Zentrix", 1, "Sentrix", 4),  # would drop "bij"
            _fix("tot", 1, "Roy", 5),  # a segment without candidates
        ]
    )
    caplog.set_level(logging.INFO)

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert [(c.segment_index, c.source, c.occurrence, c.target, c.reason) for c in result.term_corrections] == [
        (0, "Team Leader", 1, "Teamleader", "context"),
        (1, "ROI", 1, "Roy", "context"),
        (4, "Zentrix", 1, "Sentrix", "context"),
    ]
    assert result.term_corrections_pii_limited == 0
    assert result.short_summary == "Planning besproken."
    assert (
        "3 accepted, refused {'source_not_at_position': 1, 'target_not_candidate': 2, "
        "'target_not_in_list': 1, 'word_loss': 1}" in caplog.text
    )
    assert "Zentrix" not in caplog.text and "Team Leader" not in caplog.text


async def test_the_candidates_ride_in_the_one_existing_call(sdk: _Sdk) -> None:
    await enrich_report(_session(), _config(), terms=TERMS)

    assert len(sdk.calls) == 1
    assert sdk.calls[0]["response_model"] is ReportEnrichment
    assert sdk.system().endswith(TERM_CORRECTION_PROMPT)
    assert sdk.sent().split("\n") == [
        "segment 0 (Seller): We plannen alles in Team Leader.",
        "candidates for segment 0: Teamleader",
        "segment 1 (Prospect): Dan vraag ik het aan ROI van inkoop.",
        "candidates for segment 1: ROI; Roy",
        "segment 2 (Prospect): Roy belt je morgen terug.",
        "candidates for segment 2: Roy",
        "segment 4 (Seller): Het draait bij Zentrix sinds maart.",
        "candidates for segment 4: Sentrix",
        "segment 5 (Prospect): Prima, tot dan.",
    ]


@pytest.mark.parametrize("terms", [(), ("Wattelaar",)])
async def test_without_candidates_the_request_is_the_one_from_before(sdk: _Sdk, terms: tuple[str, ...]) -> None:
    sdk.reply = lambda kwargs: _answer([_fix("Zentrix", 1, "Sentrix", 4)])

    result = await enrich_report(_session(), _config(), terms=terms)

    assert sdk.system() == enrichment_mod._SYSTEM_PROMPT
    assert sdk.sent().split("\n")[0] == "Seller: We plannen alles in Team Leader."
    assert "candidates" not in sdk.sent()
    assert result.term_corrections == []


async def test_a_failed_enrichment_has_no_term_corrections(sdk: _Sdk) -> None:
    def _boom(kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("provider down")

    sdk.reply = _boom

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert result == ReportEnrichment.fail_open()
    assert result.term_corrections == []


async def test_a_failing_prefilter_costs_only_the_term_correction(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _broken(lines: Any, entries: Any) -> Any:
        raise RuntimeError("prefilter broke")

    monkeypatch.setattr(enrichment_mod, "plan_term_corrections", _broken)
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config(), terms=TERMS)

    assert result.short_summary == "Planning besproken."
    assert result.term_corrections == []
    assert "candidates" not in sdk.sent()
    assert "Report term prefilter failed" in caplog.text


def test_the_model_never_sees_the_pii_limited_count_in_its_schema() -> None:
    properties = ReportEnrichment.model_json_schema()["properties"]

    assert "term_corrections" in properties
    assert "term_corrections_pii_limited" not in properties


# ---------------------------------------------------------------------------
# PII: the candidates go through apply_outbound_pii with the resolved provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "cloud_only"])
async def test_candidates_go_through_apply_outbound_pii(mode: str, sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
    """'Bram' is a first name on the PII list: under cloud_only the model never sees it."""
    monkeypatch.setenv("PII_REDACTION", mode)
    seen: list[str] = []
    real = llm_client_mod.apply_outbound_pii

    def _spy(text: str, *, provider: str, allow_local: bool = True) -> str:
        seen.append(provider)
        return real(text, provider=provider, allow_local=allow_local)

    monkeypatch.setattr(llm_client_mod, "apply_outbound_pii", _spy)
    sdk.reply = lambda kwargs: _answer([_fix("Bramm", 1, "Bram", 0), _fix("ROI", 1, "Roy", 0)])
    session = _session((("prospect", "Ik sprak Bramm over de ROI."),))

    result = await enrich_report(session, _config(), terms=TERMS)

    assert seen == ["openrouter"]
    candidates_line = sdk.sent().split("\n")[1]
    if mode == "off":
        assert candidates_line == "candidates for segment 0: ROI; Roy; Bram"
        assert [c.target for c in result.term_corrections] == ["Bram", "Roy"]
        assert result.term_corrections_pii_limited == 0
    else:
        assert candidates_line == "candidates for segment 0: ROI; Roy; [NAAM]"
        # The redacted candidate cannot be a target; the rest of the segment still is.
        assert [c.target for c in result.term_corrections] == ["Roy"]
        assert result.term_corrections_pii_limited == 1


async def test_a_name_the_strip_removed_from_the_segment_is_not_corrected(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The count of 'Bram' changed under the strip: the model's occurrence cannot be mapped."""
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    sdk.reply = lambda kwargs: _answer([_fix("Bram", 1, "ROI", 0)])
    session = _session((("prospect", "Bram zegt dat de ROI goed is."),))

    result = await enrich_report(session, _config(), terms=TERMS)

    assert sdk.sent().startswith("segment 0 (Prospect): [NAAM] zegt")
    assert result.term_corrections == []
    assert result.term_corrections_pii_limited == 1


# ---------------------------------------------------------------------------
# The report: on disk, redaction, old reports, the dashboard event, delivery
# ---------------------------------------------------------------------------


async def test_corrections_are_written_next_to_the_original_transcript(sdk: _Sdk, reports_dir: Path) -> None:
    sdk.reply = lambda kwargs: _answer([_fix("Zentrix", 1, "Sentrix", 4)])
    written = generator.write_report(_session())

    generator.rewrite_with_enrichment(written, await enrich_report(_session(), _config(), terms=TERMS))

    data = json.loads(written.path.read_text(encoding="utf-8"))
    assert data["term_corrections"] == [
        {"segment_index": 4, "source": "Zentrix", "occurrence": 1, "target": "Sentrix", "reason": "context"}
    ]
    assert data["term_corrections_pii_limited"] == 0
    assert data["full_transcript"][4]["text"] == "Het draait bij Zentrix sinds maart."


def test_term_corrections_are_redacted_on_disk_with_report_redact_pii(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_REDACT_PII", "true")
    enrichment = _answer().model_copy(
        update={
            "term_corrections": [
                TermCorrection(**_fix("Jan", 1, "Bram", 0, reason="Bram stuurt het naar bram@example.com"))
            ]
        }
    )
    written = generator.write_report(_session())

    rewritten = generator.rewrite_with_enrichment(written, enrichment)

    [entry] = json.loads(written.path.read_text(encoding="utf-8"))["term_corrections"]
    assert entry == {
        "segment_index": 0,
        "source": "[NAAM]",
        "occurrence": 1,
        "target": "[NAAM]",
        "reason": "[NAAM] stuurt het naar [EMAIL]",
    }
    assert rewritten.report.term_corrections[0].target == "Bram"


def test_a_report_from_before_the_field_still_loads() -> None:
    old = asdict(generator.build_report(_session()))
    del old["term_corrections"], old["term_corrections_pii_limited"]

    report = generator.CallReport(**old)

    assert report.term_corrections == []
    assert report.term_corrections_pii_limited == 0
    assert json.loads(generator._to_json(report))["term_corrections"] == []


def test_report_enriched_carries_counts_and_no_correction_text() -> None:
    enrichment = _answer([_fix("Zentrix", 1, "Sentrix", 4, reason="product van de klant")]).model_copy(
        update={"term_corrections_pii_limited": 2}
    )

    payload = reports_main._enriched_payload("session-d2", enrichment, JunkVerdict(False, None, 12))

    assert payload["term_correction_count"] == 1
    assert payload["term_corrections_pii_limited"] == 2
    raw = json.dumps(payload)
    assert "Zentrix" not in raw and "Sentrix" not in raw and "product van de klant" not in raw
    assert "term_corrections" not in payload


class _FakeWs:
    def __init__(self, sent: list[dict[str, Any]]) -> None:
        self._sent = sent

    async def __aenter__(self) -> _FakeWs:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, raw: str) -> None:
        self._sent.append(json.loads(raw))


async def test_the_post_call_path_reads_the_client_list_and_delivers_the_field(
    sdk: _Sdk, reports_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """termenlijst.yaml in the client folder feeds the prefilter; the sinks get the corrections."""
    root = tmp_path / "klanten"
    client = root / "acme"
    client.mkdir(parents=True)
    (client / "klant.yaml").write_text("bedrijf: Acme BV\ntermen: [Teamleader]\n", encoding="utf-8")
    (client / "termenlijst.yaml").write_text("termen: [Sentrix]\n", encoding="utf-8")
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", root)
    sdk.reply = lambda kwargs: _answer([_fix("Zentrix", 1, "Sentrix", 4), _fix("Team Leader", 1, "Teamleader", 0)])
    delivered: list[bytes] = []
    monkeypatch.setattr(
        generator.delivery,
        "deliver_report_in_background",
        lambda payload, filename, config, **kwargs: delivered.append(payload),
    )
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(reports_main.websockets, "connect", lambda url: _FakeWs(events))
    written = generator.write_report(_session())

    await reports_main._finish_report(
        written,
        _session(),
        detector_config=_config(),
        aflevering=None,
        client_slug="acme",
        coaching_url="ws://localhost:8760/ws/coaching",
    )

    assert "candidates for segment 4: Sentrix" in sdk.sent()
    [payload] = delivered
    assert payload == written.path.read_bytes()
    assert [(c["source"], c["target"]) for c in json.loads(payload)["term_corrections"]] == [
        ("Zentrix", "Sentrix"),
        ("Team Leader", "Teamleader"),
    ]
    [event] = events
    assert event["type"] == "report_enriched" and event["term_correction_count"] == 2
