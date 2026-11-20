"""Router warm-up at detector start and the warming-up status texts (en/nl)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path

import pytest
import yaml

from sales_copilot.core import i18n
from sales_copilot.core.config import DetectorConfig
from sales_copilot.modules.detector import __main__ as detector_main
from sales_copilot.modules.detector import router as router_module
from sales_copilot.modules.detector.router import PainPointRouter

CATALOGS = Path(__file__).resolve().parents[1] / "config" / "i18n"


class _FakeSemanticRouter:
    def __call__(self, text: str):
        return None


@pytest.fixture
def router() -> PainPointRouter:
    return PainPointRouter(DetectorConfig.from_env(), "en")


def _slow_builder(monkeypatch: pytest.MonkeyPatch, delay: float = 0.3) -> list[float]:
    calls: list[float] = []

    def build(routes, model):
        calls.append(time.monotonic())
        time.sleep(delay)
        return _FakeSemanticRouter()

    monkeypatch.setattr(router_module, "_build_router", build)
    return calls


def test_warm_up_builds_the_router_once(router: PainPointRouter, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _slow_builder(monkeypatch, 0)
    assert router.warm_up() is True
    router.warm_up()
    assert len(calls) == 1
    assert router._router is not None


def test_module_start_hook_builds_in_background_without_blocking_the_loop(
    router: PainPointRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _slow_builder(monkeypatch, 0.3)

    async def scenario() -> tuple[float, bool]:
        started = time.monotonic()
        task = detector_main._start_router_warmup(router)
        await asyncio.sleep(0.05)  # the loop stays responsive while the build runs
        responsive = time.monotonic() - started < 0.25 and not task.done()
        finished = responsive and await task
        return time.monotonic() - started, finished

    elapsed, ok = asyncio.run(scenario())
    assert ok
    assert len(calls) == 1
    assert elapsed >= 0.25


def test_classify_during_the_build_waits_and_does_not_build_twice(
    router: PainPointRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _slow_builder(monkeypatch, 0.4)
    warm = threading.Thread(target=router.warm_up)
    warm.start()
    while not calls:
        time.sleep(0.005)
    start = time.monotonic()
    router.classify("we zitten uren aan zinloze kwartaalcijfers zonder enige verbinding")
    waited = time.monotonic() - start
    warm.join()
    assert len(calls) == 1
    assert waited >= 0.2  # it waited for the running build


def test_failed_warm_up_logs_once_and_classify_builds_on_first_use(
    router: PainPointRouter, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[int] = []

    def build(routes, model):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("model unavailable")
        return _FakeSemanticRouter()

    monkeypatch.setattr(router_module, "_build_router", build)
    with caplog.at_level(logging.WARNING, logger=router_module.logger.name):
        assert router.warm_up() is False
    assert [r for r in caplog.records if "warm-up failed" in r.getMessage()].__len__() == 1
    assert router._router is None  # failure stays visible, nothing cached

    router.classify("een zin zonder bekende trefwoorden hier")  # fallback build
    assert len(attempts) == 2
    assert router._router is not None


@pytest.fixture
def real_catalogs():
    i18n.set_catalog_dir(CATALOGS)
    yield
    i18n.set_catalog_dir(None)


@pytest.mark.parametrize(("lang", "text"), [("en", "Warming up the AI..."), ("nl", "AI opwarmen...")])
def test_status_warming_up_resolves_per_language(real_catalogs, lang: str, text: str) -> None:
    assert i18n.t("coaching.status_warming_up", lang=lang) == text


def test_status_keys_have_parity_and_sit_in_the_coaching_section() -> None:
    en = yaml.safe_load((CATALOGS / "en.yaml").read_text(encoding="utf-8"))
    nl = yaml.safe_load((CATALOGS / "nl.yaml").read_text(encoding="utf-8"))
    assert set(en["coaching"]) == set(nl["coaching"])
    assert "status_warming_up" in en["coaching"]


def test_transcriber_no_longer_hardcodes_the_dutch_status() -> None:
    source = (Path(__file__).resolve().parents[1] / "src/sales_copilot/modules/transcriber/__main__.py").read_text(
        encoding="utf-8"
    )
    assert "opwarmen" not in source
    assert source.count('t("coaching.status_warming_up")') == 2
