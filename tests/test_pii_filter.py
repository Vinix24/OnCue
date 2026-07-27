"""Tests for the PII redaction filter (sales_copilot.core.pii_filter)."""

from __future__ import annotations

from sales_copilot.core.pii_filter import redact_pii
from sales_copilot.core.pii_patterns import NL_VOORNAMEN

# ---------------------------------------------------------------------------
# Bestaande patronen (backward compat — H-1a)
# ---------------------------------------------------------------------------


def test_bsn_is_redacted() -> None:
    text = "Mijn BSN is 123456789"
    clean, hits = redact_pii(text)
    assert "[BSN]" in clean
    assert "123456789" not in clean
    assert hits == 1


def test_telefoon_plus31_format() -> None:
    text = "Je kunt me bereiken op +31 6 12345678"
    clean, hits = redact_pii(text)
    assert "[TELEFOON]" in clean
    assert "12345678" not in clean
    assert hits == 1


def test_telefoon_06_format() -> None:
    text = "Bel me op 06-12345678 na vijf uur"
    clean, hits = redact_pii(text)
    assert "[TELEFOON]" in clean
    assert hits == 1


def test_telefoon_regionaal_format() -> None:
    text = "Kantoor is te bereiken via 020-1234567"
    clean, hits = redact_pii(text)
    assert "[TELEFOON]" in clean
    assert hits == 1


def test_iban_is_redacted() -> None:
    text = "Rekeningnummer NL91ABNA0417164300 voor de betaling"
    clean, hits = redact_pii(text)
    assert "[IBAN]" in clean
    assert "NL91ABNA0417164300" not in clean
    assert hits == 1


def test_email_is_redacted() -> None:
    text = "Stuur het naar test@example.com alsjeblieft"
    clean, hits = redact_pii(text)
    assert "[EMAIL]" in clean
    assert "test@example.com" not in clean
    assert hits == 1


def test_postcode_is_redacted() -> None:
    text = "Woonadres 1234 AB in Amsterdam"
    clean, hits = redact_pii(text)
    assert "[POSTCODE]" in clean
    assert hits == 1


def test_geboortedatum_is_redacted() -> None:
    text = "Kandidaat is geboren op 01-02-1985"
    clean, hits = redact_pii(text)
    assert "[DATUM]" in clean
    assert "01-02-1985" not in clean
    assert hits == 1


def test_geboortedatum_short_year_not_matched() -> None:
    """Two-digit year (e.g. 1/2/85) does not match the pattern — year must be 19xx or 20xx."""
    text = "Geboren op 1/2/85"
    _clean, hits = redact_pii(text)
    assert hits == 0


def test_no_pii_returns_zero_hits() -> None:
    text = "gewoon tekst zonder persoonsgegevens"
    clean, hits = redact_pii(text)
    assert clean == text
    assert hits == 0


def test_multi_pattern_three_hits() -> None:
    text = "Bel 06-12345678, mail test@example.com, postcode 1234 AB"
    clean, hits = redact_pii(text)
    assert hits == 3
    assert "[TELEFOON]" in clean
    assert "[EMAIL]" in clean
    assert "[POSTCODE]" in clean


def test_empty_string() -> None:
    clean, hits = redact_pii("")
    assert clean == ""
    assert hits == 0


def test_bsn_inside_sentence_preserved_context() -> None:
    """Surrounding words are preserved; only the BSN number is replaced."""
    text = "Het BSN-nummer 123456789 staat in het dossier"
    clean, hits = redact_pii(text)
    assert hits == 1
    assert "Het BSN-nummer" in clean
    assert "staat in het dossier" in clean


# ---------------------------------------------------------------------------
# Voornamen (H-1c)
# ---------------------------------------------------------------------------


def test_voornaam_standalone_mannelijk() -> None:
    text = "Jan komt morgen langs voor het gesprek"
    clean, hits = redact_pii(text)
    assert "[NAAM]" in clean
    assert "Jan" not in clean
    assert hits == 1


def test_voornaam_standalone_vrouwelijk() -> None:
    text = "Emma zei dat ze volgende week beschikbaar is"
    clean, hits = redact_pii(text)
    assert "[NAAM]" in clean
    assert "Emma" not in clean
    assert hits == 1


def test_voornaam_in_midden_van_zin() -> None:
    text = "We spraken gisteren met Joost over de functie"
    clean, hits = redact_pii(text)
    assert "[NAAM]" in clean
    assert hits == 1


def test_voornaam_niet_in_langere_woord() -> None:
    """Woordgrens: "Jansen" mag niet matchen op "Jan"."""
    text = "Jansen is een achternaam, geen voornaam"
    _clean, hits = redact_pii(text)
    assert hits == 0


def test_voornaam_geen_match_mei_als_maand() -> None:
    """'Mei' staat bewust niet in NL_VOORNAMEN om maandnamen te sparen."""
    assert "Mei" not in NL_VOORNAMEN
    text = "De vergadering is in mei gepland"
    _clean, hits = redact_pii(text)
    assert hits == 0


# ---------------------------------------------------------------------------
# Achternaam met tussenvoegsel (H-1c)
# ---------------------------------------------------------------------------


def test_achternaam_tussenvoegsel_van_der() -> None:
    text = "Jan van der Berg belt straks terug"
    clean, hits = redact_pii(text)
    assert "[NAAM]" in clean
    assert "Jan van der Berg" not in clean
    assert hits == 1


def test_achternaam_tussenvoegsel_de() -> None:
    text = "Piet de Vries staat klaar voor het interview"
    clean, hits = redact_pii(text)
    assert "[NAAM]" in clean
    assert "Piet de Vries" not in clean
    assert hits == 1


def test_achternaam_tussenvoegsel_van() -> None:
    text = "Vincent van Deth is de contactpersoon"
    clean, hits = redact_pii(text)
    assert "[NAAM]" in clean
    assert hits == 1


def test_achternaam_geen_dubbele_hit_met_voornaam() -> None:
    """Volledige naam als geheel geredact — voornaam-pattern mag niet extra slaan."""
    text = "Emma ten Cate solliciteert vandaag"
    clean, hits = redact_pii(text)
    assert hits == 1
    assert "[NAAM]" in clean


# ---------------------------------------------------------------------------
# Datum-variants (H-1c)
# ---------------------------------------------------------------------------


def test_datum_woord_formaat() -> None:
    text = "Geboren op 5 mei 1985"
    clean, hits = redact_pii(text)
    assert "[DATUM]" in clean
    assert hits == 1


def test_datum_woord_formaat_uitgeschreven() -> None:
    text = "Geboortedatum: 12 januari 2003"
    clean, hits = redact_pii(text)
    assert "[DATUM]" in clean
    assert hits == 1


def test_datum_slash_formaat() -> None:
    text = "datum: 01/02/1990"
    clean, hits = redact_pii(text)
    assert "[DATUM]" in clean
    assert hits == 1


def test_datum_compact_formaat() -> None:
    text = "geboortedatum 05051985 staat in het systeem"
    clean, hits = redact_pii(text)
    assert "[DATUM]" in clean
    assert "05051985" not in clean
    assert hits == 1


def test_datum_compact_niet_bij_9_cijfers() -> None:
    """8-cijferig compact datumpatroon mag niet BSN (9 cijfers) aanraken."""
    text = "BSN 123456789 in dossier"
    clean, hits = redact_pii(text)
    assert "[BSN]" in clean
    assert hits == 1


# ---------------------------------------------------------------------------
# E-mail edge cases (H-1c)
# ---------------------------------------------------------------------------


def test_email_plus_alias() -> None:
    text = "Stuur naar test+work@example.com voor de bevestiging"
    clean, hits = redact_pii(text)
    assert "[EMAIL]" in clean
    assert "test+work@example.com" not in clean
    assert hits == 1


def test_email_subdomain() -> None:
    text = "Reply-to: info@mail.example.com"
    clean, hits = redact_pii(text)
    assert "[EMAIL]" in clean
    assert "info@mail.example.com" not in clean
    assert hits == 1


# ---------------------------------------------------------------------------
# Multi-PII combinaties (H-1c)
# ---------------------------------------------------------------------------


def test_multi_pii_naam_datum_telefoon_email() -> None:
    """Realistische recruitmentsituatie: alle typen PII in één zin."""
    text = (
        "Jan van der Berg geboren op 5 mei 1985, "
        "telefoon 06-12345678, email jan+work@example.com"
    )
    clean, hits = redact_pii(text)
    assert hits == 4
    assert "[NAAM]" in clean
    assert "[DATUM]" in clean
    assert "[TELEFOON]" in clean
    assert "[EMAIL]" in clean
    assert "Jan van der Berg" not in clean
    assert "5 mei 1985" not in clean
    assert "06-12345678" not in clean
    assert "jan+work@example.com" not in clean


def test_multi_pii_bsn_postcode_iban() -> None:
    text = "BSN 123456789, postcode 2500 GH, rekening NL91ABNA0417164300"
    clean, hits = redact_pii(text)
    assert hits == 3
    assert "[BSN]" in clean
    assert "[POSTCODE]" in clean
    assert "[IBAN]" in clean


# ---------------------------------------------------------------------------
# Voornamen-set sanity
# ---------------------------------------------------------------------------


def test_voornamen_set_minimale_grootte() -> None:
    """NL_VOORNAMEN bevat minstens 100 unieke namen."""
    assert len(NL_VOORNAMEN) >= 100


def test_voornamen_set_bevat_kernlijst() -> None:
    kernlijst = {"Jan", "Emma", "Piet", "Linda", "Jeroen", "Sandra", "Thomas", "Iris"}
    assert kernlijst.issubset(NL_VOORNAMEN)
