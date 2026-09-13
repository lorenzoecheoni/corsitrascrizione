import copy
from types import SimpleNamespace

import pytest

from app.academy import AcademyGenerationError, AcademyGenerator
from app.course_models import IntermediateCourseReport


def source_report(*, stato="verificato", critical=False):
    return IntermediateCourseReport.model_validate({
        "versione": 1,
        "stato": stato,
        "corso": {
            "titolo": "Governance delle holding",
            "sinossi_corso": "Il corso esamina regole e strumenti di governance.",
        },
        "relatori": [{
            "nome": "Mario Rossi",
            "ruolo": "Commercialista",
            "confidenza": 0.98,
            "origine_nome": ["audio"],
        }],
        "video": [{
            "chiave": "v1",
            "guid": "cbf23d46-d210-4716-809e-e2c1dbb3f4f1",
            "titolo_bunny": "Governance delle holding",
            "durata_secondi": 1200,
            "ordine": 1,
            "interventi": [
                {
                    "id": "v1-i001", "inizio": "0:00:00", "fine": "0:10:00",
                    "tipo": "intervento", "relatori": ["Mario Rossi"],
                    "titolo": "La governance", "sintesi": "Gli assetti societari.",
                    "punti_chiave": ["Assetti", "Deleghe", "Controlli"],
                    "confidenza": 0.95,
                },
                {
                    "id": "v1-i002", "inizio": "0:10:00", "fine": "0:20:00",
                    "tipo": "intervento", "relatori": ["Mario Rossi"],
                    "titolo": "Gli organi di controllo", "sintesi": "Ruoli e presidi.",
                    "punti_chiave": ["Organi", "Vigilanza", "Responsabilita"],
                    "confidenza": 0.92,
                },
            ],
            "slide": [{
                "inizio": "0:00:05", "titolo": "La governance",
                "testo_principale": "Organi, deleghe e controlli.", "confidenza": 0.95,
            }],
        }],
        "verifiche_richieste": ([{
            "livello": "critico", "codice": "RELATORE_NON_IDENTIFICATO",
            "messaggio": "Identificare il relatore.",
        }] if critical else []),
    })


def valid_academy(*, price=999, guid="cbf23d46-d210-4716-809e-e2c1dbb3f4f1"):
    question = {
        "testo": "Quale elemento sostiene una buona governance?",
        "risposte": [
            {"testo": "Deleghe chiare", "corretta": True},
            {"testo": "Assenza di controlli"},
            {"testo": "Ruoli indefiniti"},
            {"testo": "Decisioni non tracciate"},
        ],
        "spiegazione": "Deleghe chiare rendono responsabilità e controlli verificabili.",
    }
    return {
        "versione": 1,
        "corso": {
            "titolo": "Governance delle holding",
            "sottotitolo": "Organi, deleghe e controlli",
            "area": "Governance",
            "prezzo": price,
            "presentazione": "Primo paragrafo.\n\nSecondo paragrafo.\n\nTerzo paragrafo.",
            "competenze": [
                "Comprendere gli assetti", "Valutare le deleghe",
                "Distinguere gli organi", "Applicare i controlli",
                "Riconoscere i rischi",
            ],
            "profili": ["Commercialisti | Che assistono gruppi societari."],
        },
        "relatori": [{"nome": "Mario Rossi", "ruolo": "Commercialista"}],
        "video": [{
            "chiave": "v1", "sorgente": "bunny", "guid": guid,
            "durata_secondi": 1200, "titolo": "Governance delle holding",
        }],
        "moduli": [{
            "titolo": "Modulo 1 · Governance",
            "sommario": "Gli assetti essenziali.",
            "lezioni": [
                {
                    "titolo": "La governance", "video": "v1",
                    "inizio": "0:00:00", "fine": "0:10:00",
                    "relatori": ["Mario Rossi"],
                    "descrizione": "Gli assetti di governance.\nLe responsabilità degli organi.",
                    "hero": True, "anteprima": True,
                },
                {
                    "titolo": "Gli organi di controllo", "video": "v1",
                    "inizio": "0:10:00", "fine": "0:20:00",
                    "relatori": ["Mario Rossi"],
                    "descrizione": "Gli organi e le deleghe.\nI principali presidi di controllo.",
                },
                {
                    "titolo": "Verifica", "tipo": "quiz",
                    "domande": [copy.deepcopy(question) for _ in range(3)],
                },
            ],
        }],
    }


class FakeClient:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.responses = self
        self.with_raw_response = SimpleNamespace(parse=self.raw_parse)

    def raw_parse(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        response = SimpleNamespace(status="completed", output_parsed=outcome)
        return SimpleNamespace(headers={}, parse=lambda: response)


def test_generator_returns_valid_schema_and_overwrites_price_from_lesson_seconds():
    client = FakeClient(valid_academy(price=999))

    result = AcademyGenerator(client).generate(source_report())

    assert result.corso.prezzo == 97
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["store"] is False
    assert call["model"] == "gpt-4o-mini"
    assert call["text_format"].__name__ == "AcademyDraft"
    assert "Governance delle holding" not in call["instructions"]
    assert "Governance delle holding" in call["input"]


@pytest.mark.parametrize("stato", ["da_verificare"])
def test_generator_blocks_unverified_source_without_remote_request(stato):
    client = FakeClient(valid_academy())

    with pytest.raises(AcademyGenerationError, match="verificato"):
        AcademyGenerator(client).generate(source_report(stato=stato))

    assert client.calls == []


def test_generator_blocks_critical_verification_without_remote_request():
    client = FakeClient(valid_academy())

    with pytest.raises(AcademyGenerationError, match="critiche"):
        AcademyGenerator(client).generate(source_report(stato="da_verificare", critical=True))

    assert client.calls == []


def test_generator_repairs_once_using_only_previous_json_and_machine_errors():
    invalid = valid_academy(guid="00000000-0000-0000-0000-000000000099")
    client = FakeClient(invalid, valid_academy())

    result = AcademyGenerator(client).generate(source_report())

    assert result.video[0].guid == "cbf23d46-d210-4716-809e-e2c1dbb3f4f1"
    assert len(client.calls) == 2
    repair = client.calls[1]
    assert set(__import__("json").loads(repair["input"])) == {"errors", "previous_json"}
    assert "sinossi_corso" not in repair["input"]
    assert "Correggi esclusivamente" in repair["instructions"]


def test_sdk_structured_parse_can_return_an_editorially_invalid_draft_for_repair():
    class ValidatingClient(FakeClient):
        def raw_parse(self, **kwargs):
            self.calls.append(copy.deepcopy(kwargs))
            outcome = self.outcomes.pop(0)
            parsed = kwargs["text_format"].model_validate(outcome)
            response = SimpleNamespace(status="completed", output_parsed=parsed)
            return SimpleNamespace(headers={}, parse=lambda: response)

    invalid = valid_academy()
    invalid["corso"].pop("prezzo")
    invalid["corso"]["area"] = "Area inventata"
    repaired = valid_academy()
    repaired["corso"].pop("prezzo")
    client = ValidatingClient(invalid, repaired)

    result = AcademyGenerator(client).generate(source_report())

    assert result.corso.area == "Governance"
    assert result.corso.prezzo == 97
    assert len(client.calls) == 2


def test_generator_never_makes_a_third_request_and_returns_a_safe_error():
    client = FakeClient(valid_academy(guid="00000000-0000-0000-0000-000000000099"),
                        valid_academy(guid="00000000-0000-0000-0000-000000000098"))

    with pytest.raises(AcademyGenerationError, match="JSON Academy"):
        AcademyGenerator(client).generate(source_report())

    assert len(client.calls) == 2


def test_generator_maps_remote_failure_to_safe_error():
    client = FakeClient(RuntimeError("PRIVATE_PROVIDER_BODY"))

    with pytest.raises(AcademyGenerationError, match="temporaneamente") as captured:
        AcademyGenerator(client).generate(source_report())

    assert "PRIVATE_PROVIDER_BODY" not in str(captured.value)
