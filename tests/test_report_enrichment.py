"""belapp-junkfilter-rapportschema D1: ``enrich_report``, the LLM step after the call.

Covers the dispatch's required tests and the plan-gate acceptatiepunten that touch D1:

- the ``report`` task in the resolver: 300 s default, fallback to the conversation's route,
  its own ``REPORT_LLM_*`` values, ``TaskModelMissingError``, the start-call poort and the
  start-call health check;
- ``enrich_report`` against a mocked SDK seam: good response, error, timeout, refusal by the
  privacy ceiling (at resolve and at call time), a task without its model, provider
  ``none``, an empty transcript, no result, and a missing LLM stack;
- the text the client receives went through ``apply_outbound_pii`` for the RESOLVED provider
  (PII modes ``off`` and ``cloud_only``);
- the five fields in the written report, and their redaction under ``REPORT_REDACT_PII``
  even when a local model wrote them (acceptatiepunt opus ``_redact_report``);
- not blocking (acceptatiepunten opus + codex): a slow model leaves the event loop running,
  the local copy and ``report_ready`` exist before the enrichment is done, and delivery waits
  for the rewrite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sales_copilot import __main__ as app_main
from sales_copilot.core import context_docs, llm_routing
from sales_copilot.core import llm_client as llm_client_mod
from sales_copilot.core.config import CallConfig, DetectorConfig, InsightConfig, WebSocketConfig
from sales_copilot.core.llm_routing import (
    TASK_DEFAULT_MAX_OUTPUT_TOKENS,
    TASKS,
    TaskModelMissingError,
    active_tasks,
    resolve_llm,
    task_env_keys,
)
from sales_copilot.core.privacy_gate import PrivacyGateError
from sales_copilot.modules.reports import __main__ as reports_main
from sales_copilot.modules.reports import enrichment as enrichment_mod
from sales_copilot.modules.reports import generator
from sales_copilot.modules.reports.enrichment import (
    ReportEnrichment,
    enrich_report,
    wait_for_pending_reports,
)
from sales_copilot.modules.reports.session import SessionData as TrackerSessionData
from sales_copilot.websocket import hub_core

_EMAIL = "jan@example.com"
_PHONE = "06-12345678"
_FAIL_OPEN = ReportEnrichment(gesprek_gevoerd=True, short_summary="", overview="", keywords=[], action_items=[])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No per-task routing, no ceiling flags, no PII overrides, no delivery sinks."""
    for task in TASKS:
        for key in task_env_keys(task):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_TIMEOUT_MS",
        "TRUST_OWN_TENANT",
        "PII_REDACTION",
        "ALLOW_RAW_LLM_PII",
        "REPORT_REDACT_PII",
        "REPORT_DELIVERY_DIR",
        "REPORT_DELIVERY_ENDPOINT",
        "INSIGHT_ENABLED",
        "ENABLE_SUMMARY",
        "ENABLE_SUGGESTIONS",
        "ENABLE_SCRIPT_TRACKING",
        "AUTO_PHASE_DETECTION",
        "DOSSIER_AUTO_SAVE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def _enrichment(**overrides: Any) -> ReportEnrichment:
    fields: dict[str, Any] = {
        "gesprek_gevoerd": True,
        "short_summary": "  Jan wil een demo.  ",
        "overview": "Jan belde over trage offertes.\n",
        "keywords": ["offertes", " ", "CRM"],
        "action_items": ["Verkoper stuurt een demo-link", ""],
    }
    fields.update(overrides)
    return ReportEnrichment(**fields)


_CLEANED = ReportEnrichment(
    gesprek_gevoerd=True,
    short_summary="Jan wil een demo.",
    overview="Jan belde over trage offertes.",
    keywords=["offertes", "CRM"],
    action_items=["Verkoper stuurt een demo-link"],
)


class _Sdk:
    """Stands in for the provider SDK behind ``LLMClient``; the seam itself stays real."""

    def __init__(self) -> None:
        self.built: list[tuple[str, int]] = []
        self.calls: list[dict[str, Any]] = []
        self.reply: Callable[[dict[str, Any]], Any] = lambda kwargs: _enrichment()

    def build_client(self, provider: str, *, timeout_ms: int) -> object:
        self.built.append((provider, timeout_ms))
        return object()

    def build_create(self, client: object, provider: str):
        def _create(**kwargs: Any) -> Any:
            self.calls.append(kwargs)
            return self.reply(kwargs)

        return _create

    def sent_text(self) -> str:
        return self.calls[-1]["messages"][1]["content"]


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> _Sdk:
    fake = _Sdk()
    monkeypatch.setattr(llm_client_mod, "build_client", fake.build_client)
    monkeypatch.setattr(llm_client_mod, "build_create", fake.build_create)
    monkeypatch.setattr(llm_client_mod, "_ensure_ollama_num_ctx_model", lambda base_url, model, num_ctx: model)
    return fake


@pytest.fixture()
def reports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "reports"
    monkeypatch.setattr(generator, "REPORTS_DIR", directory)
    return directory


def _config(**kwargs: Any) -> DetectorConfig:
    base: dict[str, Any] = {"llm_provider": "openrouter", "llm_model": "global-model", "llm_timeout_ms": 7000}
    base.update(kwargs)
    return DetectorConfig(**base)


def _session(transcript: list[Any] | None = None) -> generator.SessionData:
    if transcript is None:
        transcript = [
            generator.TranscriptSegment("self", "Goedemiddag, met de verkoper.", 0, 2000),
            generator.TranscriptSegment("prospect", f"Mail me op {_EMAIL} of bel {_PHONE}.", 2000, 5000),
            generator.TranscriptSegment("unknown", "Prima,   dat doen we.", 5000, 6000),
        ]
    return generator.SessionData(
        session_id="session-d1",
        call_start_ms=0,
        call_end_ms=60000,
        prospect_name=None,
        prospect_company="Acme BV",
        context_docs=[],
        speech_events=[],
        phase_events=[],
        pain_points=[],
        monologues=[],
        transcript=transcript,
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == enrichment_mod.__name__ and record.levelno == logging.WARNING
    ]


# ---------------------------------------------------------------------------
# The report task in the resolver
# ---------------------------------------------------------------------------


def test_report_task_defaults_to_300s_and_the_conversation_route() -> None:
    resolved = resolve_llm("report", _config(llm_timeout_ms=3000))

    assert task_env_keys("report") == ("REPORT_LLM_PROVIDER", "REPORT_LLM_MODEL", "REPORT_LLM_TIMEOUT_MS")
    assert (resolved.task, resolved.provider, resolved.model, resolved.timeout_ms) == (
        "report",
        "openrouter",
        "global-model",
        300_000,
    )


def test_report_task_own_values_win(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "Azure")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    monkeypatch.setenv("REPORT_LLM_TIMEOUT_MS", "1500")

    resolved = resolve_llm("report", _config())

    assert (resolved.provider, resolved.model, resolved.timeout_ms) == ("azure", "report-model", 1500)


def test_report_provider_without_its_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "azure")

    with pytest.raises(TaskModelMissingError, match="REPORT_LLM_MODEL"):
        resolve_llm("report", _config())


def test_active_tasks_carry_report_only_when_the_reports_module_runs() -> None:
    assert "report" in active_tasks(_config(), insight_active=False, report_active=True)
    assert "report" not in active_tasks(_config(), insight_active=False, report_active=False)


def _write_klant_yaml(root: Path, slug: str, content: str) -> None:
    client_dir = root / slug
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "klant.yaml").write_text(content, encoding="utf-8")


def _local_start_call(**extra: Any) -> dict[str, Any]:
    config: dict[str, Any] = {"client_slug": "acme-corp", "llm": {"provider": "ollama", "model": "llama3"}}
    config.update(extra)
    return {"config": config}


def test_start_call_refuses_a_report_provider_above_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

    with pytest.raises(PrivacyGateError) as exc_info:
        hub_core.extract_start_call_config(_local_start_call())

    assert "report" in str(exc_info.value)
    assert "acme-corp" in str(exc_info.value)


def test_start_call_ignores_the_report_provider_when_reports_are_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, free_feature_policy
) -> None:
    monkeypatch.setattr(hub_core, "_feature_policy", free_feature_policy)
    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    _write_klant_yaml(tmp_path, "acme-corp", "bedrijf: Acme Corp B.V.\nprivacy: local\n")

    config = hub_core.extract_start_call_config(_local_start_call(modules={"post_call_report": False}))

    assert config["privacy"] == "local"


async def test_health_check_covers_the_report_task_without_the_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[tuple[str, str]] = []

    async def _record(provider: str, module: str, ws_config: WebSocketConfig) -> None:
        checked.append((provider, module))

    monkeypatch.setattr(app_main, "_health_check_one_provider", _record)
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "azure")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")

    await app_main._health_check_providers(
        CallConfig(enable_detector=False), _config(), InsightConfig(), WebSocketConfig()
    )
    assert checked == [("azure", "reports")]

    checked.clear()
    await app_main._health_check_providers(
        CallConfig(enable_detector=False, enable_reports=False), _config(), InsightConfig(), WebSocketConfig()
    )
    assert checked == []


# ---------------------------------------------------------------------------
# enrich_report: success and every fail-open path
# ---------------------------------------------------------------------------


async def test_good_response_fills_the_five_fields(sdk: _Sdk) -> None:
    result = await enrich_report(_session(), _config())

    assert result == _CLEANED
    assert sdk.built == [("openrouter", 300_000)]
    call = sdk.calls[0]
    assert call["model"] == "global-model"
    assert call["response_model"] is ReportEnrichment
    assert "gesprek_gevoerd" in call["messages"][0]["content"]
    lines = sdk.sent_text().splitlines()
    # 'unknown' (an undiarized segment, session.py) is labelled as such, never as the prospect.
    assert [line.split(": ", 1)[0] for line in lines] == ["Seller", "Prospect", "Unknown"]
    assert lines[0] == "Seller: Goedemiddag, met de verkoper."
    assert lines[2] == "Unknown: Prima, dat doen we."


async def test_a_model_verdict_of_no_conversation_is_kept(sdk: _Sdk) -> None:
    sdk.reply = lambda kwargs: ReportEnrichment(
        gesprek_gevoerd=False, short_summary="", overview="", keywords=[], action_items=[]
    )

    result = await enrich_report(_session(), _config())

    assert result.gesprek_gevoerd is False


async def test_model_error_fails_open_with_a_warning(sdk: _Sdk, caplog: pytest.LogCaptureFixture) -> None:
    def _boom(kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("provider down")

    sdk.reply = _boom
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config())

    assert result == _FAIL_OPEN
    assert any("Report enrichment failed" in message for message in _warnings(caplog))


async def test_timeout_fails_open_with_a_warning(
    sdk: _Sdk, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_LLM_TIMEOUT_MS", "50")
    release = threading.Event()

    def _hang(kwargs: dict[str, Any]) -> Any:
        release.wait(5)
        return _enrichment()

    sdk.reply = _hang
    caplog.set_level(logging.WARNING)
    start = time.monotonic()
    try:
        result = await enrich_report(_session(), _config())
    finally:
        release.set()

    assert time.monotonic() - start < 2
    assert result == _FAIL_OPEN
    assert any("timed out" in message for message in _warnings(caplog))


async def test_refusal_by_the_privacy_ceiling_fails_open_without_a_call(
    sdk: _Sdk, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config(llm_provider="ollama", privacy="local", client_slug="acme-corp"))

    assert result == _FAIL_OPEN
    assert sdk.built == []
    assert sdk.calls == []
    [warning] = _warnings(caplog)
    assert "privacy ceiling" in warning and "acme-corp" in warning


async def test_refusal_at_call_time_also_fails_open(
    sdk: _Sdk, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TRUST_OWN_TENANT disappears between building the client and the call."""
    monkeypatch.setenv("TRUST_OWN_TENANT", "true")
    build = sdk.build_client

    def _build_then_untrust(provider: str, *, timeout_ms: int) -> object:
        monkeypatch.delenv("TRUST_OWN_TENANT")
        return build(provider, timeout_ms=timeout_ms)

    monkeypatch.setattr(llm_client_mod, "build_client", _build_then_untrust)
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config(llm_provider="azure", privacy="tenant"))

    assert result == _FAIL_OPEN
    assert sdk.calls == []
    assert any("privacy ceiling at call time" in message for message in _warnings(caplog))


async def test_report_provider_without_its_model_fails_open(
    sdk: _Sdk, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "azure")
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config())

    assert result == _FAIL_OPEN
    assert sdk.built == []
    [warning] = _warnings(caplog)
    assert "TaskModelMissingError" in warning and "REPORT_LLM_MODEL" in warning


async def test_provider_none_switches_the_step_off_without_a_warning(
    sdk: _Sdk, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "none")
    caplog.set_level(logging.INFO)

    result = await enrich_report(_session(), _config())

    assert result == _FAIL_OPEN
    assert sdk.calls == []
    assert _warnings(caplog) == []
    assert "switched off" in caplog.text


async def test_empty_transcript_needs_no_model(sdk: _Sdk) -> None:
    session = _session([generator.TranscriptSegment("prospect", "   ", 0, 1000)])

    result = await enrich_report(session, _config())

    assert result == _FAIL_OPEN
    assert sdk.built == []


async def test_no_result_from_the_model_fails_open(sdk: _Sdk, caplog: pytest.LogCaptureFixture) -> None:
    sdk.reply = lambda kwargs: None
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config())

    assert result == _FAIL_OPEN
    assert any("returned no result" in message for message in _warnings(caplog))


def test_missing_llm_stack_fails_open_and_the_reports_module_still_imports() -> None:
    """A capture-only install has no instructor/provider SDKs: import works, the step fails open."""
    snippet = (
        "import asyncio, sys\n"
        "for name in ('instructor', 'litellm', 'openai', 'google.genai'):\n"
        "    sys.modules[name] = None\n"
        "import sales_copilot.modules.reports.__main__\n"
        "from sales_copilot.modules.reports import generator\n"
        "from sales_copilot.modules.reports.enrichment import ReportEnrichment, enrich_report\n"
        "segment = generator.TranscriptSegment('prospect', 'Hallo daar', 0, 1000)\n"
        "session = generator.SessionData('s', 0, 1000, None, None, [], [], [], [], [], [segment])\n"
        "result = asyncio.run(enrich_report(session))\n"
        "assert result == ReportEnrichment.fail_open(), result\n"
        "print('OK')\n"
    )

    completed = subprocess.run([sys.executable, "-c", snippet], capture_output=True, text=True, timeout=60)

    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout
    assert "LLM dependencies are not installed" in completed.stderr


# ---------------------------------------------------------------------------
# PII: the text the client receives, decided on the resolved provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "cloud_only"])
async def test_text_to_the_model_went_through_apply_outbound_pii_for_the_resolved_provider(
    mode: str, sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PII_REDACTION", mode)
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("REPORT_LLM_MODEL", "report-model")
    seen: list[str] = []
    real = llm_client_mod.apply_outbound_pii

    def _spy(text: str, *, provider: str, allow_local: bool = True) -> str:
        seen.append(provider)
        return real(text, provider=provider, allow_local=allow_local)

    monkeypatch.setattr(llm_client_mod, "apply_outbound_pii", _spy)

    # The conversation is local (ollama); only the report task goes to a public provider.
    await enrich_report(_session(), _config(llm_provider="ollama", llm_model="llama3"))

    assert seen == ["openrouter"]
    sent = sdk.sent_text()
    if mode == "off":
        assert _EMAIL in sent and _PHONE in sent
    else:
        assert _EMAIL not in sent and _PHONE not in sent
        assert "[EMAIL]" in sent and "[TELEFOON]" in sent


async def test_a_local_report_provider_receives_the_transcript_raw_under_cloud_only(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing leaves the machine, so cloud_only passes it raw -- decided on the report provider."""
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    monkeypatch.setenv("REPORT_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("REPORT_LLM_MODEL", "llama3")

    await enrich_report(_session(), _config(llm_provider="openrouter"))

    assert sdk.built[0][0] == "ollama"
    assert _EMAIL in sdk.sent_text()


# ---------------------------------------------------------------------------
# The written report: fields, atomic rewrite, redaction
# ---------------------------------------------------------------------------


async def test_the_five_fields_are_written_to_the_local_report(sdk: _Sdk, reports_dir: Path) -> None:
    written = generator.write_report(_session())
    first = json.loads(written.path.read_text(encoding="utf-8"))

    rewritten = generator.rewrite_with_enrichment(written, await enrich_report(_session(), _config()))

    on_disk = json.loads(written.path.read_text(encoding="utf-8"))
    assert (first["gesprek_gevoerd"], first["short_summary"], first["keywords"]) == (True, "", [])
    assert rewritten.path == written.path
    assert rewritten.payload == written.path.read_text(encoding="utf-8")
    assert {key: on_disk[key] for key in _CLEANED.model_dump()} == _CLEANED.model_dump()
    assert [p.name for p in reports_dir.iterdir()] == [written.path.name]
    assert stat.S_IMODE(written.path.stat().st_mode) == 0o600


def test_a_failed_rewrite_leaves_the_first_copy_intact_and_no_temp_file(
    reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = generator.write_report(_session())
    before = written.path.read_bytes()

    def _replace_fails(src: str, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(generator.os, "replace", _replace_fails)

    with pytest.raises(OSError, match="disk full"):
        generator.rewrite_with_enrichment(written, _CLEANED)

    assert written.path.read_bytes() == before
    assert [p.name for p in reports_dir.iterdir()] == [written.path.name]


async def test_enrichment_fields_are_redacted_on_disk_even_from_a_local_model(
    sdk: _Sdk, reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptatiepunt opus: a local model receives PII raw, so its output is redacted too."""
    monkeypatch.setenv("REPORT_REDACT_PII", "true")
    monkeypatch.setenv("PII_REDACTION", "cloud_only")
    sdk.reply = lambda kwargs: ReportEnrichment(
        gesprek_gevoerd=True,
        short_summary=f"Demo aangevraagd via {_EMAIL}.",
        overview=f"Terugbellen op {_PHONE}.",
        keywords=[_EMAIL],
        action_items=[f"Bel {_PHONE} terug"],
    )
    written = generator.write_report(_session())

    enrichment = await enrich_report(_session(), _config(llm_provider="ollama", llm_model="llama3"))
    rewritten = generator.rewrite_with_enrichment(written, enrichment)

    assert _EMAIL in sdk.sent_text()  # the local model got the transcript raw
    disk = written.path.read_text(encoding="utf-8")
    assert _EMAIL not in disk and _PHONE not in disk
    data = json.loads(disk)
    assert data["short_summary"] == "Demo aangevraagd via [EMAIL]."
    assert data["overview"] == "Terugbellen op [TELEFOON]."
    assert data["keywords"] == ["[EMAIL]"]
    assert data["action_items"] == ["Bel [TELEFOON] terug"]
    # The in-memory report stays verbatim, like the rest of the report.
    assert rewritten.report.short_summary == f"Demo aangevraagd via {_EMAIL}."


# ---------------------------------------------------------------------------
# Not blocking: the loop, the local copy, report_ready and delivery order
# ---------------------------------------------------------------------------


async def test_a_slow_report_model_does_not_block_the_event_loop(sdk: _Sdk) -> None:
    def _slow(kwargs: dict[str, Any]) -> Any:
        time.sleep(0.4)
        return _enrichment()

    sdk.reply = _slow
    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        result = await enrich_report(_session(), _config())
    finally:
        ticker.cancel()

    assert result == _CLEANED
    # A call on the loop itself would have frozen the ticker for the full 0.4 s.
    assert ticks >= 10


class _FakeTracker:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def start_session(self) -> None:
        return None

    async def end_session(self) -> TrackerSessionData:
        return TrackerSessionData(
            session_id="session-main",
            started_at="2026-09-30T10:00:00+00:00",
            ended_at="2026-09-30T10:01:00+00:00",
            prospect_name=None,
            prospect_company=None,
            context_docs=[],
            transcript=[{"speaker": "prospect", "text": "Ik wil graag een demo.", "start_ms": 0, "end_ms": 2000}],
            pain_points=[],
            talk_time_snapshots=[],
            phase_transitions=[],
            coaching_alerts=[],
            summaries=[],
        )


class _NoConversionStore:
    def note_tier(self, tier: str) -> None:
        return None

    def record_gap_shown(self, session_id: str) -> None:
        return None


class _FakeWs:
    def __init__(self, sent: list[dict[str, Any]]) -> None:
        self._sent = sent

    async def __aenter__(self) -> _FakeWs:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def send(self, raw: str) -> None:
        self._sent.append(json.loads(raw))


async def test_local_copy_and_report_ready_exist_before_the_enrichment_is_done(
    sdk: _Sdk, reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptatiepunten opus + codex: the minutes-long step holds up neither the local copy nor report_ready.

    Delivery waits for the rewrite, so the sink gets the enriched report.
    """
    started = threading.Event()
    release = threading.Event()

    def _held(kwargs: dict[str, Any]) -> Any:
        started.set()
        release.wait(5)
        return _enrichment()

    sdk.reply = _held
    sent: list[dict[str, Any]] = []
    delivered: list[bytes] = []
    monkeypatch.setenv("SHUTDOWN_TOKEN", "test-token")
    monkeypatch.setattr(reports_main, "load_env", lambda: None)
    monkeypatch.setattr(reports_main, "configure_logging", lambda: None)
    monkeypatch.setattr(reports_main, "SessionTracker", _FakeTracker)
    monkeypatch.setattr(reports_main, "SessionStore", lambda: None)
    monkeypatch.setattr(reports_main, "ConversionCounterStore", _NoConversionStore)
    monkeypatch.setattr(reports_main, "get_active_recorder", lambda: None)
    monkeypatch.setattr(reports_main.websockets, "connect", lambda url: _FakeWs(sent))
    monkeypatch.setattr(
        generator.delivery,
        "deliver_report_in_background",
        lambda payload, filename, config, **kwargs: delivered.append(payload),
    )
    stop_event = asyncio.Event()
    stop_event.set()

    try:
        await reports_main.main(stop_event=stop_event, register_signals=False, detector_config=_config())
        assert await asyncio.to_thread(started.wait, 5)

        # The model call is in flight, and still: the local copy exists, report_ready is out,
        # and nothing has been delivered yet.
        [path] = reports_dir.glob("*_report.json")
        first = json.loads(path.read_text(encoding="utf-8"))
        assert (first["gesprek_gevoerd"], first["short_summary"]) == (True, "")
        assert [payload["type"] for payload in sent] == ["report_ready"]
        assert sent[0]["report_path"] == str(path)
        assert delivered == []
    finally:
        release.set()

    await wait_for_pending_reports()

    final = path.read_bytes()
    assert json.loads(final)["short_summary"] == "Jan wil een demo."
    assert delivered == [final]


async def test_failed_enrichment_still_rewrites_and_delivers_with_fail_open_fields(
    sdk: _Sdk, reports_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("provider down")

    sdk.reply = _boom
    delivered: list[bytes] = []
    monkeypatch.setattr(
        generator.delivery,
        "deliver_report_in_background",
        lambda payload, filename, config, **kwargs: delivered.append(payload),
    )
    written = generator.write_report(_session())

    await reports_main._finish_report(
        written, _session(), detector_config=_config(), aflevering=None, client_slug=None
    )

    data = json.loads(written.path.read_text(encoding="utf-8"))
    assert {key: data[key] for key in _FAIL_OPEN.model_dump()} == _FAIL_OPEN.model_dump()
    assert delivered == [written.path.read_bytes()]


async def test_a_failed_rewrite_delivers_the_first_local_copy(
    sdk: _Sdk, reports_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    delivered: list[bytes] = []
    monkeypatch.setattr(
        generator.delivery,
        "deliver_report_in_background",
        lambda payload, filename, config, **kwargs: delivered.append(payload),
    )
    written = generator.write_report(_session())

    def _disk_full(path: Path, report: generator.CallReport) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(generator, "_write_local_copy", _disk_full)
    caplog.set_level(logging.ERROR)

    await reports_main._finish_report(
        written, _session(), detector_config=_config(), aflevering=None, client_slug=None
    )

    assert delivered == [written.payload.encode("utf-8")]
    assert "Could not rewrite the report" in caplog.text


# ---------------------------------------------------------------------------
# The output limit of the report call
# ---------------------------------------------------------------------------


async def test_the_report_call_carries_the_default_output_limit(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPORT_MAX_OUTPUT_TOKENS", raising=False)

    await enrich_report(_session(), _config())

    assert sdk.calls[0]["max_tokens"] == TASK_DEFAULT_MAX_OUTPUT_TOKENS["report"] == 16384


async def test_the_output_limit_follows_the_environment(sdk: _Sdk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPORT_MAX_OUTPUT_TOKENS", "20000")

    await enrich_report(_session(), _config())

    assert sdk.calls[0]["max_tokens"] == 20000


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
async def test_a_bad_output_limit_falls_back_to_the_default_with_a_warning(
    raw: str, sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("REPORT_MAX_OUTPUT_TOKENS", raw)
    caplog.set_level(logging.WARNING)

    await enrich_report(_session(), _config())

    assert sdk.calls[0]["max_tokens"] == 16384
    assert any(
        "REPORT_MAX_OUTPUT_TOKENS" in record.getMessage()
        for record in caplog.records
        if record.name == llm_routing.__name__ and record.levelno == logging.WARNING
    )


def test_a_reasoning_budget_lifts_the_output_limit_above_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    from sales_copilot.core.thinking_policy import THINKING_PROFILES

    monkeypatch.delenv("REPORT_MAX_OUTPUT_TOKENS", raising=False)
    big = THINKING_PROFILES["think-8192"]
    limit = resolve_llm("report", _config()).output_limit(big)
    assert limit >= big.reasoning_budget + 10_000

    monkeypatch.setenv("REPORT_MAX_OUTPUT_TOKENS", "30000")
    resolved = resolve_llm("report", _config())
    assert resolved.output_limit(big) == 30000
    assert resolved.output_limit(THINKING_PROFILES["no-think"]) == 30000

    monkeypatch.setenv("REPORT_MAX_OUTPUT_TOKENS", "4096")
    resolved = resolve_llm("report", _config())
    assert resolved.output_limit(big) == big.reasoning_budget + TASK_DEFAULT_MAX_OUTPUT_TOKENS["report"]
    assert resolved.output_limit(THINKING_PROFILES["no-think"]) == 4096


async def test_an_answer_cut_off_at_the_limit_fails_open_naming_limit_and_key(
    sdk: _Sdk, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from instructor.core.exceptions import IncompleteOutputException

    monkeypatch.setenv("REPORT_MAX_OUTPUT_TOKENS", "777")

    def _cut(kwargs: dict[str, Any]) -> Any:
        raise IncompleteOutputException(last_completion=None)

    sdk.reply = _cut
    caplog.set_level(logging.WARNING)

    result = await enrich_report(_session(), _config())

    assert result == _FAIL_OPEN
    cut = [m for m in _warnings(caplog) if "output limit" in m]
    assert len(cut) == 1
    assert "777" in cut[0] and "REPORT_MAX_OUTPUT_TOKENS" in cut[0]


async def test_a_wrapped_length_stop_is_recognised_as_the_limit(
    sdk: _Sdk, caplog: pytest.LogCaptureFixture
) -> None:
    class _Choice:
        finish_reason = "length"

    class _Completion:
        choices = [_Choice()]

    class _RetryError(Exception):
        last_completion = _Completion()

    def _cut(kwargs: dict[str, Any]) -> Any:
        raise _RetryError("retries exhausted")

    sdk.reply = _cut
    caplog.set_level(logging.WARNING)

    assert await enrich_report(_session(), _config()) == _FAIL_OPEN
    assert any("output limit" in message for message in _warnings(caplog))


async def test_another_failure_is_not_reported_as_the_limit(sdk: _Sdk, caplog: pytest.LogCaptureFixture) -> None:
    def _boom(kwargs: dict[str, Any]) -> Any:
        raise RuntimeError("provider down")

    sdk.reply = _boom
    caplog.set_level(logging.WARNING)

    await enrich_report(_session(), _config())

    assert not any("output limit" in message for message in _warnings(caplog))
