"""Tests for the klant.yaml schema, loader, and path safety (D1 of klantmap-als-eenheid)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sales_copilot.core import klant_config


def _write_klant_yaml(directory: Path, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "klant.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_klant_yaml_loads(tmp_path: Path) -> None:
    _write_klant_yaml(
        tmp_path / "acme",
        """
        bedrijf: Acme BV
        branche: Retail
        contactpersonen:
          - Jan Jansen
          - Piet Pietersen
        termen:
          - offerte
          - contract
        privacy: tenant
        bewaren_dagen: 30
        aflevering: lokaal
        """,
    )

    config = klant_config.load_klant_config("acme", root=tmp_path)

    assert config is not None
    assert config.bedrijf == "Acme BV"
    assert config.branche == "Retail"
    assert config.contactpersonen == ["Jan Jansen", "Piet Pietersen"]
    assert config.termen == ["offerte", "contract"]
    assert config.privacy == "tenant"
    assert config.bewaren_dagen == 30
    assert config.aflevering == "lokaal"


def test_valid_klant_yaml_with_only_required_field(tmp_path: Path) -> None:
    _write_klant_yaml(tmp_path / "minimal", "bedrijf: Minimal BV\n")

    config = klant_config.load_klant_config("minimal", root=tmp_path)

    assert config is not None
    assert config.bedrijf == "Minimal BV"
    assert config.branche is None
    assert config.contactpersonen == []
    assert config.privacy is None
    assert config.bewaren_dagen is None
    assert config.aflevering is None


def test_missing_required_field_raises(tmp_path: Path) -> None:
    _write_klant_yaml(tmp_path / "no-bedrijf", "branche: Retail\n")

    with pytest.raises(klant_config.KlantConfigError) as exc_info:
        klant_config.load_klant_config("no-bedrijf", root=tmp_path)

    message = str(exc_info.value)
    assert "bedrijf" in message
    assert "no-bedrijf" in message


def test_wrong_type_per_field_raises(tmp_path: Path) -> None:
    _write_klant_yaml(
        tmp_path / "wrong-type",
        """
        bedrijf: Acme BV
        bewaren_dagen: not-a-number
        """,
    )

    with pytest.raises(klant_config.KlantConfigError) as exc_info:
        klant_config.load_klant_config("wrong-type", root=tmp_path)

    message = str(exc_info.value)
    assert "bewaren_dagen" in message


def test_wrong_type_contactpersonen_not_a_list_raises(tmp_path: Path) -> None:
    _write_klant_yaml(
        tmp_path / "wrong-list-type",
        """
        bedrijf: Acme BV
        contactpersonen: Jan Jansen
        """,
    )

    with pytest.raises(klant_config.KlantConfigError) as exc_info:
        klant_config.load_klant_config("wrong-list-type", root=tmp_path)

    assert "contactpersonen" in str(exc_info.value)


def test_aflevering_hubspot_not_supported(tmp_path: Path) -> None:
    _write_klant_yaml(
        tmp_path / "hubspot",
        """
        bedrijf: Acme BV
        aflevering: hubspot
        """,
    )

    with pytest.raises(klant_config.KlantConfigError) as exc_info:
        klant_config.load_klant_config("hubspot", root=tmp_path)

    message = str(exc_info.value)
    assert "aflevering" in message
    assert "niet ondersteund" in message


def test_folder_without_klant_yaml_returns_none(tmp_path: Path) -> None:
    """Backward compat: an existing client folder with only loose dossier files
    (or nothing at all) is not an error -- callers keep treating the folder
    name as the company and loose files as the dossier, unchanged."""
    (tmp_path / "legacy-client").mkdir()
    (tmp_path / "legacy-client" / "notes.txt").write_text("some prep notes", encoding="utf-8")

    config = klant_config.load_klant_config("legacy-client", root=tmp_path)

    assert config is None


def test_slug_with_dotdot_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(klant_config.KlantConfigError):
        klant_config.load_klant_config("../outside", root=tmp_path)

    with pytest.raises(klant_config.KlantConfigError):
        klant_config.resolve_klant_dir("..", root=tmp_path)


def test_slug_escaping_root_via_nested_dotdot_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(klant_config.KlantConfigError):
        klant_config.resolve_klant_dir("foo/../../etc", root=tmp_path)


def test_empty_slug_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(klant_config.KlantConfigError):
        klant_config.resolve_klant_dir("", root=tmp_path)


def test_contactpersonen_over_max_is_rejected(tmp_path: Path) -> None:
    names = "\n".join(f"  - Persoon {i}" for i in range(21))
    _write_klant_yaml(
        tmp_path / "too-many-contacts",
        f"bedrijf: Acme BV\ncontactpersonen:\n{names}\n",
    )

    with pytest.raises(klant_config.KlantConfigError) as exc_info:
        klant_config.load_klant_config("too-many-contacts", root=tmp_path)

    assert "contactpersonen" in str(exc_info.value)


def test_term_over_max_length_is_rejected(tmp_path: Path) -> None:
    long_term = "x" * 65
    _write_klant_yaml(
        tmp_path / "long-term",
        f"bedrijf: Acme BV\ntermen:\n  - {long_term}\n",
    )

    with pytest.raises(klant_config.KlantConfigError) as exc_info:
        klant_config.load_klant_config("long-term", root=tmp_path)

    assert "termen" in str(exc_info.value)


def test_extra_field_is_rejected(tmp_path: Path) -> None:
    _write_klant_yaml(
        tmp_path / "typo-field",
        "bedrijf: Acme BV\nbedrjif: typo\n",
    )

    with pytest.raises(klant_config.KlantConfigError):
        klant_config.load_klant_config("typo-field", root=tmp_path)


def test_default_root_falls_back_to_context_docs_upload_root(tmp_path: Path, monkeypatch) -> None:
    from sales_copilot.core import context_docs

    monkeypatch.setattr(context_docs, "UPLOAD_ROOT", tmp_path)
    _write_klant_yaml(tmp_path / "default-root-client", "bedrijf: Acme BV\n")

    config = klant_config.load_klant_config("default-root-client")

    assert config is not None
    assert config.bedrijf == "Acme BV"
