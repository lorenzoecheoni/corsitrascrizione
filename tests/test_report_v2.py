from uuid import UUID

from app.editorial import EditorialDraft
from app.report_v2 import build_report_v2, slugify

from granular_support import governance_report, valid_editorial_draft


GUID = UUID("00000000-0000-0000-0000-000000000001")


def build(report=None):
    report = report or governance_report()
    draft = EditorialDraft.model_validate(valid_editorial_draft())
    return build_report_v2(report, GUID, draft)


def test_slugify_folds_accents_and_drops_apostrophes():
    assert slugify("Furio D'Andrea") == "furio-dandrea"
    assert slugify("Furio D’Andrea") == "furio-dandrea"
    assert slugify("Slide · Furio D’Andrea") == "slide-furio-dandrea"


def test_build_v2_uses_slugs_and_the_editorial_layer():
    report = build()

    assert report.versione == 2
    assert report.stato == "verificato"
    assert report.corso.codice == f"video-{GUID}"
    assert report.corso.titolo == "Governance"
    assert report.corso.sottotitolo == "Organi, deleghe e controlli nella holding"
    assert report.corso.area == "Governance"
    assert len(report.corso.competenze) == 5
    assert len(report.corso.profili) == 3
    assert len(report.corso.faq) == 2
    assert [speaker.slug for speaker in report.relatori] == ["furio-dandrea", "luigi-morra"]
    video = report.video[0]
    first = video.interventi[0]
    assert first.relatori == ["furio-dandrea"]
    assert (first.inizio, first.inizio_secondi) == ("0:00:00", 0)
    assert (first.fine, first.fine_secondi) == ("0:10:00", 600)
    assert first.titolo == "Lezione 1"
    assert first.titolo_slide == "Deleghe"
    assert first.blocco.id == "v1-b001"
    assert first.blocco.titolo == "La governance della holding"
    assert first.descrizione == "Cosa si vede nella lezione 1."
    assert first.casi == ["Caso 1"]
    assert first.riferimenti == ["art. 2479 c.c."]
    assert video.interventi[3].blocco.titolo == "I controlli"


def test_build_v2_links_slides_to_interventions_and_materials():
    video = build().video[0]

    slides = {slide.titolo: slide for slide in video.slide}
    assert slides["Deleghe"].intervento == "v1-i001"
    assert slides["Deleghe"].inizio_secondi == 599
    assert slides["Deleghe"].materiale == "slide-furio-dandrea"
    assert slides["Deleghe"].pagina == 2
    assert slides["Deleghe"].testo == "Poteri e deleghe"
    assert slides["Chiusura"].intervento == "v1-i006"
    assert slides["Chiusura"].materiale is None
    material = video.materiali[0]
    assert material.id == "slide-furio-dandrea"
    assert material.relatore == "furio-dandrea"
    assert material.interventi == ["v1-i001"]


def test_build_v2_moves_boundaries_to_diagnostics_and_adds_new_warnings():
    report = build()

    codes = [check.codice for check in report.verifiche_richieste]
    assert "CONFINE" not in codes
    assert codes.count("INTERVENTO_SENZA_SLIDE") == 4
    assert "RELATORE_SENZA_MATERIALE" in codes
    assert all(
        check.livello in ("bloccante", "avviso")
        for check in report.verifiche_richieste
    )
    assert len(report.diagnostica.confini) == 5
    assert report.diagnostica.costo_stimato.valuta == "USD"


def test_build_v2_derives_course_code_from_bunny_title_date():
    report = governance_report()
    report.bunny_title = "Webinar del 22/07/2026"

    assert build(report).corso.codice == "webinar-2026-07-22"


def test_build_v2_is_deterministic():
    first = build().model_dump_json(by_alias=True, exclude_none=True)

    assert first == build().model_dump_json(by_alias=True, exclude_none=True)
