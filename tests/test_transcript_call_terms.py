"""D3 of transcript-normalisatie-na-asr: per-conversation term list (transcriber side).

Covers the layering rules in ``normalize.with_call_terms``, the config-event
ordering in ``transcriber.__main__._handle_config_event`` (start with config
sets, start without config keeps, call_ended clears), the PII rule (no INFO
log of the list) and the PII modes ``off`` / ``cloud_only`` on the downstream
outbound path. Propagation into both engines is covered in
``test_inference_worker.py`` and ``test_whisper_direct.py``.
"""

from __future__ import annotations

import logging

import pytest

from sales_copilot.core.outbound_policy import apply_outbound_pii
from sales_copilot.modules.transcriber import __main__ as transcriber_main
from sales_copilot.modules.transcriber import normalize as norm

FIXED = norm.NormalizationLists(
    enabled=True,
    terms=("HubSpot",),
    variants={"HupSpot": "HubSpot", "Bergkam": "Bergkamp Fixed"},
)


class TestSanitize:
    def test_keeps_only_non_empty_strings_and_dedups(self) -> None:
        assert norm.sanitize_call_terms(["Bergkamp", " ", 3, None, "Bergkamp", " Wattelaar "]) == (
            "Bergkamp",
            "Wattelaar",
        )

    @pytest.mark.parametrize("raw", [None, "Bergkamp", {"a": 1}, 7])
    def test_non_list_gives_empty(self, raw: object) -> None:
        assert norm.sanitize_call_terms(raw) == ()


class TestWithCallTerms:
    def test_empty_returns_base_unchanged(self) -> None:
        assert norm.with_call_terms(FIXED, ()) is FIXED

    def test_call_term_fuzzy_corrects_near_miss(self) -> None:
        lists = norm.with_call_terms(FIXED, ("Bergkamp",))
        text, _ = norm.normalize("gesprek met Bergkamq vandaag", lists)
        assert text == "gesprek met Bergkamp vandaag"

    def test_call_term_takes_precedence_over_fixed_variant(self) -> None:
        # Fixed list would rewrite "Bergkam"; the participant's own name wins.
        assert norm.normalize("Bergkam belt", FIXED)[0] == "Bergkamp Fixed belt"
        lists = norm.with_call_terms(FIXED, ("Bergkam",))
        assert norm.normalize("Bergkam belt", lists)[0] == "Bergkam belt"

    def test_call_variant_overrides_fixed_variant(self) -> None:
        lists = norm.with_call_terms(FIXED, ("HupSpot -> Hupspot Klant",))
        assert norm.normalize("via HupSpot", lists)[0] == "via Hupspot Klant"

    def test_arrow_entry_repairs_short_form_exactly(self) -> None:
        lists = norm.with_call_terms(FIXED, ("VWA -> VBA",))
        assert norm.normalize("de VWA sheet", lists)[0] == "de VBA sheet"
        assert norm.normalize("de VWA sheet", FIXED)[0] == "de VWA sheet"

    def test_malformed_arrow_entry_is_ignored(self) -> None:
        lists = norm.with_call_terms(FIXED, ("-> VBA", "VWA ->"))
        assert lists.variants == FIXED.variants

    def test_word_equal_to_a_term_is_never_fuzzed_to_another_term(self) -> None:
        lists = norm.with_call_terms(FIXED, ("Marius", "Marious"))
        assert norm.normalize("Marius zegt", lists)[0] == "Marius zegt"

    def test_base_lists_are_not_mutated(self) -> None:
        norm.with_call_terms(FIXED, ("Bergkamp", "VWA -> VBA"))
        assert FIXED.terms == ("HubSpot",)
        assert "VWA" not in FIXED.variants


class TestConfigEventOrdering:
    """start_call(config) -> call_started(no config) -> call_ended."""

    def _harness(self):
        state: dict[str, object] = {"terms": (), "starts": 0, "history": []}

        def on_terms(terms: tuple[str, ...]) -> None:
            state["terms"] = terms
            state["history"].append(terms)  # type: ignore[union-attr]

        async def on_start() -> None:
            state["starts"] = int(state["starts"]) + 1  # type: ignore[call-overload]

        return state, on_terms, on_start

    async def _send(self, payload: dict, on_terms, on_start) -> None:
        await transcriber_main._handle_config_event(payload, lambda: None, on_start, on_terms)

    async def test_start_call_with_config_sets_terms(self) -> None:
        state, on_terms, on_start = self._harness()
        cfg = {"call_terms": ["Bergkamp", "VWA -> VBA"]}
        await self._send({"type": "start_call", "config": cfg}, on_terms, on_start)
        assert state["terms"] == ("Bergkamp", "VWA -> VBA")
        assert state["starts"] == 1

    async def test_call_started_without_config_does_not_wipe(self) -> None:
        state, on_terms, on_start = self._harness()
        await self._send({"type": "start_call", "config": {"call_terms": ["Bergkamp"]}}, on_terms, on_start)
        await self._send({"type": "call_started"}, on_terms, on_start)
        await self._send({"type": "call_started", "config": {}}, on_terms, on_start)
        assert state["terms"] == ("Bergkamp",)
        assert state["history"] == [("Bergkamp",)]
        assert state["starts"] == 3

    async def test_call_started_replay_with_same_config_is_idempotent(self) -> None:
        state, on_terms, on_start = self._harness()
        cfg = {"call_terms": ["Bergkamp"]}
        await self._send({"type": "start_call", "config": cfg}, on_terms, on_start)
        await self._send({"type": "call_started", "config": cfg}, on_terms, on_start)
        assert state["terms"] == ("Bergkamp",)

    async def test_config_without_call_terms_key_means_empty(self) -> None:
        state, on_terms, on_start = self._harness()
        await self._send({"type": "start_call", "config": {"call_terms": ["Bergkamp"]}}, on_terms, on_start)
        await self._send({"type": "start_call", "config": {"preset": "x"}}, on_terms, on_start)
        assert state["terms"] == ()

    async def test_malformed_call_terms_gives_empty_not_crash(self) -> None:
        state, on_terms, on_start = self._harness()
        await self._send({"type": "start_call", "config": {"call_terms": "Bergkamp"}}, on_terms, on_start)
        assert state["terms"] == ()

    @pytest.mark.parametrize("ending", ["call_ended", "end_call"])
    async def test_call_ended_clears_terms(self, ending: str) -> None:
        state, on_terms, on_start = self._harness()
        await self._send({"type": "start_call", "config": {"call_terms": ["Bergkamp"]}}, on_terms, on_start)
        await self._send({"type": ending}, on_terms, on_start)
        assert state["terms"] == ()
        assert state["starts"] == 1

    async def test_unrelated_events_do_not_touch_terms(self) -> None:
        state, on_terms, on_start = self._harness()
        await self._send({"type": "start_call", "config": {"call_terms": ["Bergkamp"]}}, on_terms, on_start)
        await self._send({"type": "swap_speakers"}, on_terms, on_start)
        assert state["terms"] == ("Bergkamp",)

    async def test_call_terms_never_logged_at_info(self, caplog: pytest.LogCaptureFixture) -> None:
        state, on_terms, on_start = self._harness()
        with caplog.at_level(logging.INFO):
            await self._send(
                {"type": "start_call", "config": {"call_terms": ["Zzyzx Deelnemer"]}}, on_terms, on_start
            )
            await self._send({"type": "call_ended"}, on_terms, on_start)
        assert "Zzyzx" not in caplog.text


class TestNoLeakOfOriginalIntoOutbound:
    """The normalized text is the only wording downstream sees; PII modes still apply to it."""

    def _normalized(self) -> tuple[str, list[norm.Replacement]]:
        lists = norm.with_call_terms(FIXED, ("VWA -> VBA",))
        return norm.normalize("de VWA koppeling", lists)

    def test_replacements_carry_original_only_in_the_return_value_not_the_text(self) -> None:
        text, replacements = self._normalized()
        assert text == "de VBA koppeling"
        assert "VWA" not in text
        assert replacements == [("VWA", "VBA", "variant")]

    def test_pii_mode_off_passes_normalized_text_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PII_REDACTION", "off")
        text, _ = self._normalized()
        assert apply_outbound_pii(text, provider="openai") == "de VBA koppeling"

    def test_pii_mode_cloud_only_local_provider_passes_through_and_public_never_sees_original(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PII_REDACTION", "cloud_only")
        monkeypatch.delenv("TRUST_OWN_TENANT", raising=False)
        text, _ = self._normalized()
        assert apply_outbound_pii(text, provider="ollama/gemma") == "de VBA koppeling"
        assert "VWA" not in apply_outbound_pii(text, provider="openai/gpt-4o")
