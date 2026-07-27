import time

from sales_copilot.modules.detector.debouncer import PainPointDebouncer


def test_debouncer_blocks_within_cooldown(monkeypatch) -> None:
    debouncer = PainPointDebouncer(cooldown_seconds=45)
    timeline = iter([100.0, 120.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(timeline))

    debouncer.record_trigger("offerteproces")
    assert debouncer.should_trigger("offerteproces") is False


def test_debouncer_allows_after_cooldown(monkeypatch) -> None:
    debouncer = PainPointDebouncer(cooldown_seconds=45)
    timeline = iter([100.0, 146.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(timeline))

    debouncer.record_trigger("offerteproces")
    assert debouncer.should_trigger("offerteproces") is True
