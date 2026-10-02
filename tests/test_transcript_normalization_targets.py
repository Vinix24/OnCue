"""The four measured terms from the 2026-09-26 measurement, repaired end to end.

Each case runs the real ``normalize()`` with the lists as they exist in
production: the fixed list via ``get_default_normalization_lists()`` and, for
the per-conversation cases, ``with_call_terms`` with the entry shape a client
would put under ``termen`` in klant.yaml.
"""

from __future__ import annotations

from sales_copilot.modules.transcriber.normalize import (
    get_default_normalization_lists,
    normalize,
    with_call_terms,
)


def _run(text: str, call_terms: tuple[str, ...] = ()) -> str:
    lists = with_call_terms(get_default_normalization_lists(), call_terms)
    return normalize(text, lists)[0]


class TestFixedList:
    def test_idi_becomes_edi(self) -> None:
        text = "de visie dat die toch naar die IDI gaat uiteindelijk"
        assert _run(text) == "de visie dat die toch naar die EDI gaat uiteindelijk"

    def test_ide_is_left_alone(self) -> None:
        text = "ik open de code in mijn IDE en draai daar de tests"
        assert _run(text) == text

    def test_ide_is_left_alone_with_client_terms(self) -> None:
        text = "die IDE is prima"
        assert _run(text, ("Voorbeeldtech", "Voorbeeldtek -> Voorbeeldtech")) == text


class TestPerConversationList:
    def test_voorbeeldtek_becomes_voorbeeldtech_via_variant(self) -> None:
        text = "ik ben van Voorbeeldtek en wij bouwen dat"
        assert _run(text, ("Voorbeeldtek -> Voorbeeldtech",)) == "ik ben van Voorbeeldtech en wij bouwen dat"

    def test_voorbeeldtek_becomes_voorbeeldtech_via_canonical_term(self) -> None:
        assert _run("ik ben van Voorbeeldtek", ("Voorbeeldtech",)) == "ik ben van Voorbeeldtech"

    def test_marius_bakker_becomes_marius_bakkers_via_two_word_variant(self) -> None:
        text = "opgericht vanuit Marius Bakker, een sales training organisatie"
        result = _run(text, ("Marius Bakker -> Marius Bakkers",))
        assert result == "opgericht vanuit Marius Bakkers, een sales training organisatie"

    def test_marius_bakker_untouched_without_call_term(self) -> None:
        text = "opgericht vanuit Marius Bakker, een sales training organisatie"
        assert _run(text) == text

    def test_nathan_becomes_n8n_only_with_exact_variant(self) -> None:
        text = "de eigen server voor Nathan"
        assert _run(text, ("Nathan -> n8n",)) == "de eigen server voor n8n"

    def test_nathan_untouched_globally(self) -> None:
        text = "ik sprak gisteren met Nathan over de server"
        assert _run(text) == text

    def test_all_call_terms_together(self) -> None:
        terms = ("Voorbeeldtek -> Voorbeeldtech", "Marius Bakker -> Marius Bakkers", "Nathan -> n8n")
        text = "Voorbeeldtek, Marius Bakker, Nathan en de IDI"
        assert _run(text, terms) == "Voorbeeldtech, Marius Bakkers, n8n en de EDI"
