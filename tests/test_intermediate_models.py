from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.intermediate_models import IntermediateReportV11


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
            "titolo_bunny": "Governance", "durata_secondi": 60, "ordine": 1,
            "lingua": "italiano", "sinossi": "Sintesi.",
            "interventi": [{
                "id": "v1-i001", "inizio": "0:00:00", "fine": "0:01:00",
                "tipo": "intervento", "relatori": ["Gaetano De Vito"],
                "titolo": "Apertura", "sintesi": "La governance viene introdotta.",
                "punti_chiave": ["Organi", "Deleghe", "Controlli"],
                "accesso": "pubblico", "confidenza": .9,
            }],
            "slide": [], "materiali": [],
        }],
        "verifiche_richieste": [],
    }


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
