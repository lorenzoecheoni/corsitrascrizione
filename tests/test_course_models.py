from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.course_models import (
    AcademyImport,
    IntermediateCourseReport,
    academy_price,
    format_hms,
    parse_hms,
    validate_academy_import,
)
from app.models import Intervention


def intervention(**changes):
    data = {
        "id": "v1-i001",
        "inizio": "0:00:00",
        "fine": "0:10:00",
        "tipo": "intervento",
        "relatori": ["Mario Rossi", "Anna Bianchi"],
        "titolo": "La governance",
        "sintesi": "Analisi dei principali assetti di governance.",
        "punti_chiave": ["Assetti", "Deleghe", "Controlli"],
        "confidenza": 0.91,
    }
    data.update(changes)
    return data


def source_report(**changes):
    data = {
        "versione": 1,
        "stato": "verificato",
        "corso": {
            "titolo": "Governance delle holding",
            "sinossi_corso": "Il corso esamina regole e strumenti di governance.",
            "inventario": {"foglio": "Formazione", "ordine": 2},
        },
        "relatori": [
            {
                "nome": "Mario Rossi",
                "ruolo": "Commercialista",
                "organizzazione": "Studio Rossi",
                "confidenza": 0.98,
                "origine_nome": ["audio", "slide"],
            },
            {
                "nome": "Anna Bianchi",
                "confidenza": 0.9,
                "origine_nome": ["inventario"],
            },
        ],
        "video": [
            {
                "chiave": "v1",
                "guid": "cbf23d46-d210-4716-809e-e2c1dbb3f4f1",
                "titolo_bunny": "Governance delle holding",
                "durata_secondi": 1200,
                "ordine": 1,
                "interventi": [
                    intervention(),
                    intervention(
                        id="v1-i002",
                        inizio="0:10:00",
                        fine="0:20:00",
                        titolo="Gli organi di controllo",
                    ),
                ],
                "slide": [
                    {
                        "inizio": "0:00:05",
                        "titolo": "La governance",
                        "testo_principale": "Organi, deleghe e controlli.",
                        "confidenza": 0.95,
                    }
                ],
            }
        ],
        "verifiche_richieste": [],
    }
    data.update(changes)
    return IntermediateCourseReport.model_validate(data)


def quiz():
    return {
        "titolo": "Verifica di apprendimento",
        "tipo": "quiz",
        "domande": [
            {
                "testo": f"Domanda {number}?",
                "risposte": [
                    {"testo": "Corretta", "corretta": True},
                    {"testo": "Errata A", "corretta": False},
                    {"testo": "Errata B", "corretta": False},
                    {"testo": "Errata C", "corretta": False},
                ],
                "spiegazione": "La risposta deriva dal contenuto del corso.",
            }
            for number in range(1, 4)
        ],
    }


def academy_import(**changes):
    data = {
        "versione": 1,
        "corso": {
            "titolo": "Governance delle holding",
            "sottotitolo": "Organi, deleghe e controlli",
            "area": "Governance",
            "prezzo": 97,
            "presentazione": "Primo paragrafo.\n\nSecondo paragrafo.\n\nTerzo paragrafo.",
            "competenze": [
                "Comprendere gli assetti",
                "Valutare le deleghe",
                "Distinguere gli organi",
                "Applicare i controlli",
                "Riconoscere i rischi",
            ],
            "profili": ["Commercialisti | Che assistono gruppi societari."],
        },
        "relatori": [
            {"nome": "Mario Rossi", "ruolo": "Commercialista", "organizzazione": "Studio Rossi"},
            {"nome": "Anna Bianchi"},
        ],
        "video": [
            {
                "chiave": "v1",
                "sorgente": "bunny",
                "guid": "cbf23d46-d210-4716-809e-e2c1dbb3f4f1",
                "durata_secondi": 1200,
                "titolo": "Governance delle holding",
            }
        ],
        "moduli": [
            {
                "titolo": "Modulo 1 · Governance",
                "sommario": "Gli assetti essenziali.",
                "lezioni": [
                    {
                        "titolo": "La governance",
                        "video": "v1",
                        "inizio": "0:00:00",
                        "fine": "0:10:00",
                        "relatori": ["Mario Rossi", "Anna Bianchi"],
                        "descrizione": "Gli assetti di governance.\nLe responsabilità degli organi.",
                        "hero": True,
                        "anteprima": True,
                    },
                    {
                        "titolo": "Gli organi di controllo",
                        "video": "v1",
                        "inizio": "0:10:00",
                        "fine": "0:20:00",
                        "relatori": ["Mario Rossi"],
                        "descrizione": "Gli organi e le deleghe.\nI principali presidi di controllo.",
                    },
                    quiz(),
                ],
            }
        ],
    }
    data.update(changes)
    return AcademyImport.model_validate(data)


@pytest.mark.parametrize(
    ("seconds", "rendered"),
    [(0, "0:00:00"), (59.4, "0:00:59"), (59.5, "0:01:00"), (3661, "1:01:01")],
)
def test_hms_round_trip_is_exact_to_the_nearest_second(seconds, rendered):
    assert format_hms(seconds) == rendered
    assert parse_hms(rendered) == round(seconds)


@pytest.mark.parametrize("value", ["1:2:03", "00:00", "-1:00:00", "1:60:00", "testo"])
def test_parse_hms_rejects_noncanonical_values(value):
    with pytest.raises(ValueError):
        parse_hms(value)


@pytest.mark.parametrize(
    "kind",
    ["intervento", "saluti", "logistica", "domande", "pausa", "cambio_relatore"],
)
def test_all_intervention_kinds_are_accepted(kind):
    points = ["Uno", "Due", "Tre"] if kind == "intervento" else []
    item = Intervention.model_validate(intervention(tipo=kind, punti_chiave=points))
    assert item.tipo == kind
    assert item.relatori == ["Mario Rossi", "Anna Bianchi"]


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), float("inf")])
def test_intervention_confidence_is_finite_and_bounded(confidence):
    with pytest.raises(ValidationError):
        Intervention.model_validate(intervention(confidenza=confidence))


@pytest.mark.parametrize("points", [[], ["a", "b"], ["a", "b", "c", "d", "e", "f", "g", "h"]])
def test_substantive_intervention_requires_three_to_seven_key_points(points):
    with pytest.raises(ValidationError, match="punti_chiave"):
        Intervention.model_validate(intervention(punti_chiave=points))


def test_generic_speaker_labels_are_omitted_from_intervention():
    item = Intervention.model_validate(
        intervention(relatori=["Mario Rossi", "Relatore 1", "SPEAKER_02"])
    )
    assert item.relatori == ["Mario Rossi"]


def test_intermediate_video_requires_complete_adjacent_coverage():
    data = source_report().model_dump(mode="json", by_alias=True)
    data["video"][0]["interventi"][1]["inizio"] = "0:10:01"
    with pytest.raises(ValidationError, match="continua"):
        IntermediateCourseReport.model_validate(data)


def test_intermediate_rejects_slide_outside_video_and_long_text():
    data = source_report().model_dump(mode="json", by_alias=True)
    data["video"][0]["slide"][0]["inizio"] = "0:20:01"
    with pytest.raises(ValidationError, match="slide"):
        IntermediateCourseReport.model_validate(data)
    data = source_report().model_dump(mode="json", by_alias=True)
    data["video"][0]["slide"][0]["testo_principale"] = "x" * 501
    with pytest.raises(ValidationError):
        IntermediateCourseReport.model_validate(data)


@pytest.mark.parametrize(
    ("seconds", "price"),
    [(1, "97"), (3600, "97"), (3601, "147"), (7200, "147"), (7201, "197"),
     (10800, "197"), (10801, "247"), (14400, "247")],
)
def test_academy_price_boundaries(seconds, price):
    assert academy_price(seconds) == Decimal(price)


@pytest.mark.parametrize("seconds", [0, -1, 14401])
def test_academy_price_rejects_out_of_scope_duration(seconds):
    with pytest.raises(ValueError):
        academy_price(seconds)


def test_academy_contract_accepts_valid_course_and_source():
    final = academy_import()
    assert validate_academy_import(final, source_report()) == []


@pytest.mark.parametrize("area", ["Altro", "Fisco", ""])
def test_academy_contract_rejects_unknown_area(area):
    data = academy_import().model_dump(mode="json", by_alias=True)
    data["corso"]["area"] = area
    with pytest.raises(ValidationError):
        AcademyImport.model_validate(data)


def test_academy_module_requires_two_to_six_video_lessons_and_final_quiz():
    data = academy_import().model_dump(mode="json", by_alias=True)
    data["moduli"][0]["lezioni"] = [data["moduli"][0]["lezioni"][0], quiz()]
    with pytest.raises(ValidationError, match="2 e 6"):
        AcademyImport.model_validate(data)
    data = academy_import().model_dump(mode="json", by_alias=True)
    data["moduli"][0]["lezioni"].reverse()
    with pytest.raises(ValidationError, match="terminare"):
        AcademyImport.model_validate(data)


def test_academy_quiz_requires_three_questions_four_answers_and_one_correct():
    data = academy_import().model_dump(mode="json", by_alias=True)
    data["moduli"][0]["lezioni"][-1]["domande"][0]["risposte"][1]["corretta"] = True
    with pytest.raises(ValidationError, match="una sola risposta corretta"):
        AcademyImport.model_validate(data)


def test_academy_validation_checks_refs_bounds_overlap_speakers_price_and_hero():
    source = source_report()
    final = academy_import()
    data = final.model_dump(mode="json", by_alias=True)
    data["video"][0]["guid"] = "wrong-guid"
    data["moduli"][0]["lezioni"][0]["relatori"] = ["Persona Inventata"]
    data["moduli"][0]["lezioni"][1]["inizio"] = "0:09:59"
    data["moduli"][0]["lezioni"][1]["fine"] = "0:21:00"
    data["moduli"][0]["lezioni"][0]["hero"] = False
    data["corso"]["prezzo"] = 147
    invalid = AcademyImport.model_validate(data)
    errors = validate_academy_import(invalid, source)
    assert any("GUID" in error for error in errors)
    assert any("relatore" in error.lower() for error in errors)
    assert any("sovrapp" in error.lower() for error in errors)
    assert any("durata" in error.lower() for error in errors)
    assert any("hero" in error.lower() for error in errors)
    assert any("prezzo" in error.lower() for error in errors)


def test_academy_contract_rejects_two_heroes():
    data = academy_import().model_dump(mode="json", by_alias=True)
    data["moduli"][0]["lezioni"][1]["hero"] = True
    data["moduli"][0]["lezioni"][1]["anteprima"] = True
    invalid = AcademyImport.model_validate(data)
    assert any("hero" in error.lower() for error in validate_academy_import(invalid, source_report()))
