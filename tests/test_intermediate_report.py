import json
from pathlib import Path
from uuid import UUID

import pytest

from app.intermediate_report import (
    build_intermediate_report,
    registered_slug,
    split_role_organization,
)
from app.models import AcademyReport, Intervention, SpeakerProfile
from app.reporting import correct_speaker_name_mentions


TARGET_GUID = UUID("7f254c4d-fe34-4fd3-a4cf-cda4f447e438")


def report_with_reconciled_speakers() -> AcademyReport:
    data = json.loads(Path("tests/fixtures/report.json").read_text())
    report = AcademyReport.model_validate(data)
    report.speakers = [
        SpeakerProfile(
            id="vincenzo",
            display_name="Vincenzo Manfredi",
            role="Presidente",
            confidence="alta",
            evidence=[{
                "kind": "introduzione",
                "timestamp_seconds": 10,
                "note": "Vincenzo Manfredi apre i lavori.",
            }],
        )
    ]
    report.interventions = [
        Intervention(
            id=f"i{index}",
            start_seconds=(index - 1) * 180,
            end_seconds=index * 180,
            tipo="intervento",
            relatori=[name],
            titolo=f"Intervento di {name}",
            sintesi=f"{name} tratta gli aspetti rilevanti.",
            punti_chiave=["Primo punto", "Secondo punto", "Terzo punto"],
            confidenza=.9,
        )
        for index, name in enumerate([
            "Vincenzo Manfredi", "Gaetano De Vito", "Furio d'Andrea",
            "Antonio Sibilia", "Luigi Morra",
        ], start=1)
    ]
    report.interventions[1].titolo = "Intervento di Fulvio D'Andrea con Gaetano De Vito"
    report.interventions[1].sintesi = "Fulvio D'Andrea e Gaetano De Vito discutono il tema."
    report.interventions[1].punti_chiave[0] = "Fulvio D'Andrea presenta il primo punto"
    report.slides[0].title = "Fulvio D'Andrea in apertura"
    report.slides[0].visible_content = ["Fulvio D'Andrea", "Agenda"]
    report.title = "Webinar con Fulvio D'Andrea"
    report.bunny_title = "Bunny: Fulvio D'Andrea"
    report.synopsis = "Fulvio D'Andrea presenta la sinossi."
    return report


def test_builder_wraps_one_video_and_reconciles_registry_speakers() -> None:
    report = report_with_reconciled_speakers()

    result = build_intermediate_report(report, TARGET_GUID)

    data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert data["versione"] == 1
    assert data["corso"] == {
        "titolo": "Webinar con Furio d'Andrea",
        "sinossi_corso": "Furio d'Andrea presenta la sinossi.",
    }
    assert [(item["chiave"], item["ordine"]) for item in data["video"]] == [("v1", 1)]
    assert [item["nome"] for item in data["relatori"]] == [
        "Vincenzo Manfredi", "Gaetano De Vito", "Furio d'Andrea",
        "Antonio Sibilia", "Luigi Morra",
    ]
    assert {item.get("slug") for item in data["relatori"]} == {
        "vincenzo-manfredi", "gaetano-de-vito", "antonio-sibilia",
        "luigi-morra", None,
    }
    assert [item["id"] for item in data["video"][0]["interventi"]] == [
        "v1-i001", "v1-i002", "v1-i003", "v1-i004", "v1-i005",
    ]
    assert [
        (item["inizio"], item["fine"]) for item in data["video"][0]["interventi"]
    ] == [
        ("0:00:00", "0:03:00"), ("0:03:00", "0:06:00"),
        ("0:06:00", "0:09:00"), ("0:09:00", "0:12:00"),
        ("0:12:00", "0:15:00"),
    ]
    assert "Fulvio" not in json.dumps(data, ensure_ascii=False)


def test_registered_speakers_receive_fixed_slugs_and_timeline_names_are_audio_only() -> None:
    report = report_with_reconciled_speakers()

    result = build_intermediate_report(report, TARGET_GUID)
    speakers = result.model_dump(mode="json", exclude_none=True)["relatori"]

    assert registered_slug("GAETANO de Vito") == "gaetano-de-vito"
    assert registered_slug("Furio d'Andrea") is None
    assert speakers[1]["origine_nome"] == ["audio"]
    assert speakers[1]["confidenza"] == .65


def test_role_split_and_furio_registry_warning_request_missing_qualification() -> None:
    report = report_with_reconciled_speakers()
    report.interventions[1].relatori = ["Gaetano De Vito"]
    report.speakers.append(SpeakerProfile(
        id="gaetano",
        display_name="Gaetano De Vito",
        role="Public Policy and Advocacy Director di Ass Holding",
        confidence="alta",
        evidence=[{
            "kind": "slide", "timestamp_seconds": 90,
            "note": "Gaetano De Vito è presentato in slide.",
        }],
    ))

    result = build_intermediate_report(report, TARGET_GUID)
    data = result.model_dump(mode="json", exclude_none=True)
    gaetano = next(item for item in data["relatori"] if item["nome"] == "Gaetano De Vito")
    furio_warnings = [
        item for item in data["verifiche_richieste"]
        if item["codice"] == "RELATORE_NON_NEL_REGISTRO"
    ]

    assert split_role_organization("Public Policy and Advocacy Director di Ass Holding") == (
        "Public Policy and Advocacy Director", "Assoholding",
    )
    assert gaetano["ruolo"] == "Public Policy and Advocacy Director"
    assert gaetano["organizzazione"] == "Assoholding"
    assert len(furio_warnings) == 1
    assert "Furio d'Andrea" in furio_warnings[0]["messaggio"]
    assert "qualifica" in furio_warnings[0]["messaggio"].casefold()


@pytest.mark.parametrize("organization", ["Ass Holding", "Asso Holding", "Assoholding"])
def test_role_split_accepts_every_explicit_assoholding_spelling(organization: str) -> None:
    assert split_role_organization(f"Direttore di {organization}") == (
        "Direttore", "Assoholding",
    )


def test_role_split_rejects_assholding_without_the_required_space() -> None:
    assert split_role_organization("Direttore di Assholding") == (
        "Direttore di Assholding", None,
    )


def test_furio_is_canonical_when_furio_and_fulvio_are_both_candidate_names() -> None:
    report = report_with_reconciled_speakers()
    report.speakers.append(SpeakerProfile(
        id="fulvio-duplicate",
        display_name="Fulvio D'Andrea",
        confidence="alta",
        evidence=[{
            "kind": "slide", "timestamp_seconds": 90,
            "note": "Fulvio D'Andrea compare nella slide.",
        }],
    ))

    result = build_intermediate_report(report, TARGET_GUID)
    names = [item.nome for item in result.relatori]

    assert correct_speaker_name_mentions(
        "Furio d'Andrea e Fulvio D'Andrea", ["Furio d'Andrea", "Fulvio D'Andrea"]
    ) == "Furio d'Andrea e Furio d'Andrea"
    assert names.count("Furio d'Andrea") == 1
    assert "Fulvio D'Andrea" not in names


def test_builder_filters_generic_formal_speaker_profiles() -> None:
    report = report_with_reconciled_speakers()
    report.speakers.append(SpeakerProfile(
        id="generic",
        display_name="Relatore 3",
        confidence="bassa",
        evidence=[{
            "kind": "inferenza",
            "timestamp_seconds": 90,
            "note": "Voce distinta senza identità verificata.",
        }],
    ))

    result = build_intermediate_report(report, TARGET_GUID)

    assert "Relatore 3" not in [item.nome for item in result.relatori]


def test_builder_renumbers_interventions_in_chronological_stable_order() -> None:
    report = report_with_reconciled_speakers()
    first, second, third, fourth, fifth = report.interventions
    second.start_seconds = 0
    report.interventions = [fourth, second, first, third, fifth]

    result = build_intermediate_report(report, TARGET_GUID)
    interventions = result.video[0].interventi

    assert [item.id for item in interventions] == [
        "v1-i001", "v1-i002", "v1-i003", "v1-i004", "v1-i005",
    ]
    assert [item.relatori for item in interventions] == [
        ["Gaetano De Vito"], ["Vincenzo Manfredi"], ["Furio d'Andrea"],
        ["Antonio Sibilia"], ["Luigi Morra"],
    ]
