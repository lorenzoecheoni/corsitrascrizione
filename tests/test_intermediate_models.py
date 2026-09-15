from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.intermediate_models import (
    IntermediateCostV11,
    IntermediateReportV11,
    IntermediateSpeechBlockV11,
)


def valid_payload():
    return {
        "versione": 1,
        "stato": "verificato",
        "corso": {"titolo": "Governance", "sinossi_corso": "Sintesi."},
        "relatori": [{
            "nome": "Gaetano De Vito", "slug": "gaetano-de-vito",
            "confidenza": .95, "origine_nome": ["audio"],
        }],
        "video": [{
            "chiave": "v1", "guid": "7f254c4d-fe34-4fd3-a4cf-cda4f447e438",
            "titolo_bunny": "Governance", "durata_secondi": 600, "ordine": 1,
            "lingua": "italiano", "sinossi": "Sintesi.",
            "interventi": [{
                "id": "v1-i001", "inizio": "0:00:00", "fine": "0:10:00",
                "tipo": "intervento", "relatori": ["Gaetano De Vito"],
                "titolo": "Apertura", "sintesi": "La governance viene introdotta.",
                "punti_chiave": ["Organi", "Deleghe", "Controlli"],
                "accesso": "pubblico", "confidenza": .9,
                "blocco": "b001", "capitolo_numero": 1, "capitoli_blocco": 1,
                "confine_inizio": {
                    "motivo_editoriale": "inizio_blocco", "regola_audio": "no_pause",
                },
            }],
            "blocchi_parlato": [{
                "id": "b001", "inizio": "0:00:00", "fine": "0:10:00",
                "tipo": "intervento", "relatori": ["Gaetano De Vito"],
                "titolo": "Apertura", "sinossi": "La governance viene introdotta.",
            }],
            "costo_stimato": {
                "valuta": "USD", "minimo": .4, "massimo": .7, "banda_bunny": .01,
                "trascrizione": .3, "analisi": .09, "criterio": "Stima applicativa.",
            },
            "slide": [], "materiali": [],
        }],
        "verifiche_richieste": [],
    }


def test_v11_retains_factual_blocks_and_cost_contract():
    model = IntermediateReportV11.model_validate(valid_payload())

    assert model.video[0].blocchi_parlato == [
        IntermediateSpeechBlockV11(
            id="b001", inizio="0:00:00", fine="0:10:00", tipo="intervento",
            relatori=["Gaetano De Vito"], titolo="Apertura",
            sinossi="La governance viene introdotta.",
        )
    ]
    assert model.video[0].costo_stimato == IntermediateCostV11(
        minimo=.4, massimo=.7, banda_bunny=.01, trascrizione=.3, analisi=.09,
        criterio="Stima applicativa.",
    )


@pytest.mark.parametrize("mutation", [
    lambda data: data["video"][0]["interventi"][0].pop("blocco"),
    lambda data: data["video"][0]["blocchi_parlato"][0].update(fine="0:10:01"),
    lambda data: data["video"][0]["blocchi_parlato"].append(
        deepcopy(data["video"][0]["blocchi_parlato"][0])
    ),
    lambda data: data["video"][0]["interventi"][0].update(fine="0:07:59"),
])
def test_v11_rejects_incomplete_or_incoherent_granular_contract(mutation):
    data = valid_payload()
    mutation(data)

    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_requires_pause_or_logistics_to_explain_a_gap_between_blocks():
    data = valid_payload()
    video = data["video"][0]
    video["durata_secondi"] = 1000
    second_chapter = deepcopy(video["interventi"][0])
    second_chapter.update(
        id="v1-i002", inizio="0:10:01", fine="0:16:40", accesso="iscritti",
        blocco="b002", confine_inizio={
            "motivo_editoriale": "inizio_blocco", "regola_audio": "no_pause",
        },
    )
    second_block = deepcopy(video["blocchi_parlato"][0])
    second_block.update(id="b002", inizio="0:10:01", fine="0:16:40")
    video["interventi"].append(second_chapter)
    video["blocchi_parlato"].append(second_block)

    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


@pytest.mark.parametrize("mutation", [
    lambda video: video.pop("blocchi_parlato"),
    lambda video: video.pop("costo_stimato"),
    lambda video: video.update(blocchi_parlato=[]),
    lambda video: video.update(costo_stimato=None),
])
def test_v11_rejects_an_explicitly_incomplete_granular_contract(mutation):
    data = valid_payload()
    mutation(data["video"][0])

    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_accepts_a_legacy_payload_that_omits_both_granular_fields():
    data = valid_payload()
    data["video"][0].pop("blocchi_parlato")
    data["video"][0].pop("costo_stimato")

    assert IntermediateReportV11.model_validate(data).video[0].blocchi_parlato == []


def _two_block_payload_with_exact_pause():
    data = valid_payload()
    video = data["video"][0]
    video["durata_secondi"] = 1001
    second_chapter = deepcopy(video["interventi"][0])
    second_chapter.update(
        id="v1-i002", inizio="0:10:01", fine="0:16:40", accesso="iscritti",
        blocco="b002", confine_inizio={
            "motivo_editoriale": "inizio_blocco", "regola_audio": "short_pause",
        },
    )
    second_block = deepcopy(video["blocchi_parlato"][0])
    second_block.update(id="b002", inizio="0:10:01", fine="0:16:40")
    pause = deepcopy(video["interventi"][0])
    pause.update(
        id="v1-i003", inizio="0:10:00", fine="0:10:01", tipo="pausa",
        relatori=[], titolo="Pausa", sintesi="Breve pausa.", punti_chiave=[],
        accesso="iscritti",
    )
    for field in ("blocco", "capitolo_numero", "capitoli_blocco", "confine_inizio"):
        pause.pop(field)
    video["interventi"].extend([second_chapter, pause])
    video["blocchi_parlato"].append(second_block)
    return data


def _two_contiguous_blocks_with_extra_pause(pause_start, pause_end):
    data = _two_block_payload_with_exact_pause()
    video = data["video"][0]
    video["interventi"][1].update(inizio="0:10:00")
    video["blocchi_parlato"][1].update(inizio="0:10:00")
    video["interventi"][-1].update(inizio=pause_start, fine=pause_end)
    return data


@pytest.mark.parametrize("pause_start,pause_end", [
    ("0:00:10", "0:00:20"),
    ("0:16:40", "0:16:41"),
])
def test_v11_rejects_a_pause_not_consumed_by_an_interblock_gap(pause_start, pause_end):
    data = _two_contiguous_blocks_with_extra_pause(pause_start, pause_end)

    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_rejects_duplicate_intervention_ids():
    data = _two_block_payload_with_exact_pause()
    data["video"][0]["interventi"][1]["id"] = "v1-i001"

    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_round_trips_exact_contract():
    model = IntermediateReportV11.model_validate(valid_payload())
    assert model.model_dump(mode="json", by_alias=True, exclude_none=True) == valid_payload()


def test_v11_rejects_generic_speaker_name():
    data = valid_payload()
    data["relatori"][0]["nome"] = "Relatore 1"
    data["video"][0]["interventi"][0]["relatori"] = ["Relatore 1"]
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


@pytest.mark.parametrize("version", [1.0, True])
def test_v11_requires_builtin_integer_version(version):
    data = valid_payload()
    data["versione"] = version
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


@pytest.mark.parametrize("identifier", ["v1-i000", "v1-i0000"])
def test_v11_intervention_id_suffix_is_positive(identifier):
    data = valid_payload()
    data["video"][0]["interventi"][0]["id"] = identifier
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_rejects_boolean_slide_timestamp():
    data = valid_payload()
    data["video"][0]["slide"] = [{
        "inizio": True, "titolo": "Slide", "testo_principale": "Test",
        "confidenza": 0.9,
    }]
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_requires_critical_status_equivalence():
    data = valid_payload()
    data["stato"] = "da_verificare"
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)
    data["stato"] = "verificato"
    data["verifiche_richieste"] = [{
        "livello": "critico", "codice": "TEMPI_INCOERENTI", "messaggio": "No",
    }]
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_rejects_verification_code_level_mismatch():
    data = valid_payload()
    data["verifiche_richieste"] = [{
        "livello": "critico", "codice": "INTERVENTO_BREVE", "messaggio": "No",
    }]
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_accepts_confine_only_as_warning():
    data = valid_payload()
    data["verifiche_richieste"] = [{
        "livello": "avviso", "codice": "CONFINE", "video": "v1",
        "intervento": "v1-i001", "campo": "inizio",
        "messaggio": "Confine 0:00:00; verifica audio.",
    }]

    assert IntermediateReportV11.model_validate(data).stato == "verificato"

    data["verifiche_richieste"][0]["livello"] = "critico"
    data["stato"] = "da_verificare"
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_rejects_unknown_intervention_speaker_reference():
    data = valid_payload()
    data["video"][0]["interventi"][0]["relatori"] = ["Missing"]
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


def test_v11_requires_empty_key_points_for_non_intervention():
    data = valid_payload()
    intervention = data["video"][0]["interventi"][0]
    intervention["tipo"] = "pausa"
    intervention["punti_chiave"] = ["unexpected"]
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


@pytest.mark.parametrize("sources", [{"url": "https://example.com", "file": "x.pdf"}, {}])
def test_v11_material_requires_exactly_one_source(sources):
    data = valid_payload()
    material = {"titolo": "Materiale", **sources}
    data["video"][0]["materiali"] = [material]
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)


@pytest.mark.parametrize("start,end", [(10, 20), (0, 5)])
def test_v11_represents_timeline_gaps_and_overlaps(start, end):
    data = valid_payload()
    data["video"][0].pop("costo_stimato")
    data["video"][0].pop("blocchi_parlato")
    first = data["video"][0]["interventi"][0]
    first.update(inizio="0:00:00", fine="0:00:10")
    second = deepcopy(first)
    second.update(id="v1-i002", inizio=f"0:00:{start:02d}", fine=f"0:00:{end:02d}")
    data["video"][0]["interventi"].append(second)
    model = IntermediateReportV11.model_validate(data)
    assert model.video[0].interventi[1].start_seconds == start


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(versione=1.1),
    lambda data: data.update(stato="confermato"),
    lambda data: data["video"].append(deepcopy(data["video"][0])),
    lambda data: data["video"][0]["interventi"][0].update(id="i001"),
    lambda data: data["verifiche_richieste"].append({
        "livello": "avviso", "codice": "CODICE_IGNOTO", "messaggio": "No",
    }),
])
def test_v11_rejects_values_outside_contract(mutation):
    data = valid_payload()
    mutation(data)
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)
