from pathlib import Path
from uuid import UUID

import pytest

from app.inventory import inventory_context_for_video, material_sources_for_video
from app.material_registry import (
    match_library_file,
    material_declaration_failure_reason,
    resolve_material_sources,
    scan_library_files,
)
from app.inventory import InventoryCourse


TARGET_ID = UUID("00000000-0000-0000-0000-000000000001")


def inventory_course(title="Corso con materiali", *, materiali):
    return InventoryCourse(
        id="0:2", foglio="Formazione", gid="0", posizione_foglio=0,
        riga=2, titolo=title, relatori_attesi=[], materiali=materiali, link=None,
        colonna_link="D", guid_esplicito=TARGET_ID,
    )


@pytest.fixture
def library_dir(tmp_path):
    directory = tmp_path / "materiali"
    directory.mkdir()
    return directory


def add_deck(directory, name):
    deck = directory / name
    deck.write_bytes(b"%PDF-test" if name.lower().endswith(".pdf") else b"PK-test")
    return deck


def test_scan_lists_only_supported_regular_files(library_dir):
    deck = add_deck(library_dir, "deck.pdf")
    slides = add_deck(library_dir, "SLIDES.PPTX")
    (library_dir / "note.txt").write_text("not a deck")
    (library_dir / ".hidden.pdf").write_bytes(b"%PDF-hidden")
    subdir = library_dir / "sottocartella"
    subdir.mkdir()
    add_deck(subdir, "nested.pdf")

    assert scan_library_files(library_dir) == (slides, deck)


def test_scan_missing_or_file_directory_returns_empty(tmp_path):
    assert scan_library_files(tmp_path / "assente") == ()
    target = tmp_path / "file"
    target.write_text("x")
    assert scan_library_files(target) == ()


@pytest.mark.parametrize("label", [
    "Slide Furio D'Andrea",
    "Slide Furio D’Andrea",
    "Slide Furio d'Andrea",
    "Slide Furio D ’ Andrea",
])
def test_match_folds_apostrophes_and_wordpress_names(library_dir, label):
    deck = add_deck(
        library_dir,
        "19072026_PP-Avv.-Furio-DAndrea_Webinar-22-luglio-2026.pptx",
    )

    assert match_library_file(label, scan_library_files(library_dir)) == deck


def test_match_uses_surname_tokens_ignoring_extra_words(library_dir):
    deck = add_deck(library_dir, "Slide-Morra-Conferimenti-1.pptx")

    assert match_library_file("Slide Luigi Morra", scan_library_files(library_dir)) == deck


def test_match_uses_label_numbers_to_disambiguate_decks(library_dir):
    add_deck(library_dir, "Germani 1.pdf")
    second = add_deck(library_dir, "Germani 2.pdf")

    files = scan_library_files(library_dir)
    assert match_library_file("Slide Germani 2", files) == second


def test_match_accepts_compact_multi_surname_signatures(library_dir):
    deck = add_deck(library_dir, "DeVito_Sibilia.pdf")

    assert match_library_file("Slide De Vito e Sibilia", scan_library_files(library_dir)) == deck


def test_match_supports_topic_labels_without_speaker_names(library_dir):
    deck = add_deck(library_dir, "Assegnazione agevolata.pdf")

    assert match_library_file("Assegnazione agevolata", scan_library_files(library_dir)) == deck


def test_match_prefers_the_closest_file_name(library_dir):
    deck = add_deck(library_dir, "Vedana.pdf")
    add_deck(library_dir, "Vedana webinar marzo 2026.pdf")

    assert match_library_file("Slide Fabrizio Vedana |", scan_library_files(library_dir)) == deck


def test_match_fails_closed_on_ambiguity(library_dir):
    add_deck(library_dir, "Slides Matta.pptx")
    add_deck(library_dir, "Matta dispensa.pdf")

    assert match_library_file("Slide Mirko Matta", scan_library_files(library_dir)) is None


@pytest.mark.parametrize("label", ["", "|", "link sbagliato !!!!", "!!!", "2024"])
def test_match_rejects_labels_without_name_tokens(library_dir, label):
    add_deck(library_dir, "deck.pdf")

    assert match_library_file(label, scan_library_files(library_dir)) is None


def test_match_resolves_a_declared_file_name(library_dir):
    deck = add_deck(library_dir, "dispensa finale.pdf")

    assert match_library_file("dispensa finale.pdf", scan_library_files(library_dir)) == deck


def test_bare_label_resolves_through_the_library_with_its_title(library_dir):
    deck = add_deck(library_dir, "Slide-Morra-Conferimenti-1.pptx")
    course = inventory_course(materiali=["Slide Luigi Morra"])

    sources = material_sources_for_video(
        [course], TARGET_ID, course.titolo,
        library=lambda label: match_library_file(label, scan_library_files(library_dir)),
    )

    assert sources == [f"Slide · Luigi Morra | {deck}"]


def test_registry_person_labels_emit_the_canonical_title(library_dir):
    deck = add_deck(library_dir, "19072026_PP-Avv.-Furio-DAndrea_Webinar.pptx")
    course = inventory_course(materiali=["Slide Furio d'Andrea"])

    sources = material_sources_for_video(
        [course], TARGET_ID, course.titolo,
        library=lambda label: match_library_file(label, scan_library_files(library_dir)),
    )

    assert sources == [f"Slide · Furio D'Andrea | {deck}"]


def test_two_labels_matched_to_the_same_file_are_deduplicated(library_dir):
    deck = add_deck(library_dir, "Morra.pdf")
    course = inventory_course(materiali=["Slide Morra", "Slide Luigi Morra"])

    sources = material_sources_for_video(
        [course], TARGET_ID, course.titolo,
        library=lambda label: match_library_file(label, scan_library_files(library_dir)),
    )

    assert sources == [f"Slide Morra | {deck}"]


def test_library_match_is_not_reported_as_a_missing_source(library_dir):
    deck = add_deck(library_dir, "Morra.pdf")
    matcher = lambda label: match_library_file(label, scan_library_files(library_dir))

    assert material_declaration_failure_reason("Slide Luigi Morra", library=matcher) is None
    assert material_declaration_failure_reason("Slide Sconosciuto", library=matcher) == "sorgente_reale_assente"

    context = inventory_context_for_video(
        [inventory_course(materiali=["Slide Luigi Morra", "Slide Sconosciuto"])],
        TARGET_ID, "Corso con materiali", library=matcher,
    )

    assert context.material_sources == (f"Slide · Luigi Morra | {deck}",)
    assert [failure.reason for failure in context.material_failures] == ["sorgente_reale_assente"]


def test_without_a_library_bare_labels_keep_the_current_behavior(library_dir):
    add_deck(library_dir, "Morra.pdf")
    course = inventory_course(materiali=["Slide Luigi Morra"])

    assert material_sources_for_video([course], TARGET_ID, course.titolo) == []

    context = inventory_context_for_video([course], TARGET_ID, course.titolo)
    assert context.material_sources == ()
    assert [failure.reason for failure in context.material_failures] == ["sorgente_reale_assente"]


def test_library_never_shadows_explicit_urls_or_existing_paths(library_dir, tmp_path):
    add_deck(library_dir, "deck.pdf")
    explicit = tmp_path / "explicit.pptx"
    explicit.write_bytes(b"PK-test")
    matcher = lambda label: match_library_file(label, scan_library_files(library_dir))

    sources = resolve_material_sources(
        ["Slide · Persona | https://materials.example.test/deck.pdf", str(explicit)],
        TARGET_ID, library=matcher,
    )

    assert sources == [
        "Slide · Persona | https://materials.example.test/deck.pdf",
        f"explicit | {explicit}",
    ]


def test_titled_local_declarations_round_trip(library_dir):
    deck = add_deck(library_dir, "governance.pdf")

    sources = resolve_material_sources(
        [f"Dispensa Governance | {deck}"], TARGET_ID,
    )

    assert sources == [f"Dispensa Governance | {deck}"]
