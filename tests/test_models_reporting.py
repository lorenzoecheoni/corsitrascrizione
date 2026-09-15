import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app import academy_registry, reporting
from app.costs import estimate_cost
from app.models import AcademyReport, Intervention, SpeakerProfile
from app.reporting import (
    build_video_report_payload,
    format_timestamp,
    render_markdown,
    render_text,
)


def load_report() -> AcademyReport:
    data = json.loads(Path("tests/fixtures/report.json").read_text())
    return AcademyReport.model_validate(data)


def test_report_models_retain_blocks_chapter_origin_and_material_page():
    from app.models import ChapterBoundaryOrigin, ReportMaterial, SpeechBlock

    origin = ChapterBoundaryOrigin(
        motivo_editoriale="slide_e_tema",
        regola_audio="short_pause",
        slide_indizio_seconds=1369,
    )
    chapter = Intervention(
        id="i001", start_seconds=1369, end_seconds=1931, tipo="intervento",
        relatori=["Furio D’Andrea"], titolo="Governance delle holding",
        sintesi="Il capitolo illustra poteri, assemblea e direttive.",
        punti_chiave=["Poteri", "Assemblea", "Direttive"], confidenza=.9,
    ).model_copy(update={
        "block_id": "b003", "chapter_number": 2, "chapters_in_block": 4,
        "boundary_origin": origin,
    })
    block = SpeechBlock(
        id="b003", start_seconds=862, end_seconds=3135, tipo="intervento",
        relatori=["Furio D’Andrea"], titolo="Governance delle holding",
        sinossi="Il blocco tratta poteri, assemblea e direttive.",
    )
    material = ReportMaterial(
        titolo="Slide · Furio D’Andrea", relatore="Furio D’Andrea",
        url="https://www.assoholding.it/materiali/furio.pptx", pagine=18,
    )
    report = load_report().model_copy(update={
        "interventions": [chapter], "speech_blocks": [block], "materials": [material],
        "analysis_profile": 2,
    })

    assert report.interventions[0].boundary_origin == origin
    assert report.speech_blocks[0].id == "b003"
    assert report.materials[0].pagine == 18


def test_report_fixture_covers_requested_text_report() -> None:
    report = load_report()
    assert len(report.speakers) == 3
    assert report.synopsis
    assert report.slides[0].timestamp_seconds == 95
    assert set(report.model_dump()) == {
        "title", "duration_seconds", "detected_language", "synopsis",
        "speakers", "slides", "uncertainties", "interventions", "boundaries",
        "speech_blocks", "materials", "material_failures", "analysis_profile",
        "audio_boundary_version", "cost", "bunny_title", "usage",
    }
    assert report.interventions == []
    assert report.boundaries == []
    assert report.speech_blocks == []
    assert report.materials == []
    assert report.material_failures == []
    assert report.analysis_profile == 1
    assert report.audio_boundary_version is None


def _boundary_data():
    return dict(
        previous_intervention_id="i001", next_intervention_id="i002", boundary_seconds=14,
        words_before=["la", "governance", "si", "chiude", "qui"],
        words_after=["passiamo", "ora", "al", "tema", "fiscale"],
        pause_before=True, pause_after=True, rule="long_pause",
    )


def test_boundary_contract_is_shared_by_analysis_and_report():
    from app.models import AcademyContent, AnalysisResult, BoundaryEvidence

    evidence = BoundaryEvidence(**_boundary_data())
    report = load_report().model_copy(update={"boundaries": [evidence]})
    data = report.model_dump(exclude={"cost", "bunny_title", "usage"})
    assert AcademyContent.model_validate(data).boundaries == [evidence]
    assert AnalysisResult.model_validate(data).boundaries == [evidence]
    assert report.model_dump(mode="json")["boundaries"] == [_boundary_data()]


@pytest.mark.parametrize("field,value", [
    ("boundary_seconds", 14.0), ("boundary_seconds", "14"), ("boundary_seconds", True),
    ("boundary_seconds", -1), ("words_before", ["a"] * 6),
    ("words_after", [""]), ("words_before", ["  "]),
    ("next_intervention_id", "i001"), ("previous_intervention_id", ""),
    ("rule", "midpoint"),
])
def test_boundary_evidence_rejects_invalid_contract(field, value):
    from app.models import BoundaryEvidence

    with pytest.raises(ValidationError):
        BoundaryEvidence(**{**_boundary_data(), field: value})


@pytest.mark.parametrize("count", [0, 1, 4, 5])
def test_boundary_evidence_allows_zero_to_five_verbatim_words(count):
    from app.models import BoundaryEvidence

    evidence = BoundaryEvidence(**{**_boundary_data(), "words_before": ["Così,"] * count})
    assert evidence.words_before == ["Così,"] * count


@pytest.mark.parametrize(
    "words_field,flag_field",
    [("words_before", "pause_before"), ("words_after", "pause_after")],
)
@pytest.mark.parametrize("count", [0, 1, 4])
def test_boundary_evidence_requires_pause_flag_for_fewer_than_five_words(
    words_field, flag_field, count,
):
    from app.models import BoundaryEvidence

    with pytest.raises(ValidationError):
        BoundaryEvidence(**{
            **_boundary_data(),
            words_field: ["x"] * count,
            flag_field: False,
        })


def test_one_hour_cost_is_in_approved_range() -> None:
    cost = estimate_cost(duration_seconds=3600, downloaded_bytes=450_000_000)
    assert 0.40 <= cost.estimated_low_usd <= cost.estimated_high_usd <= 0.70


def test_markdown_and_text_are_exportable() -> None:
    report = load_report()
    markdown = render_markdown(report)
    plain = render_text(report)
    assert "## Sinossi" in markdown
    assert "## Relatori" in markdown
    assert "## Slide" in markdown
    assert "## Punti chiave" not in markdown
    assert "## Obiettivi formativi" not in markdown
    assert "01:35" in markdown
    assert "## Descrizione estesa" not in markdown
    assert "## Capitoli" not in markdown
    assert "## Interventi" not in markdown
    assert "## Argomenti e parole chiave" not in markdown
    assert "## Punti chiave" not in markdown
    assert "Relatori" in plain
    assert format_timestamp(3661) == "1:01:01"


def test_interventions_are_rendered_without_transcript_text() -> None:
    report = load_report()
    report.interventions = [Intervention(
        id="i001", start_seconds=0, end_seconds=600, tipo="intervento",
        relatori=["Marco Rossi"], titolo="Assetti di governance",
        sintesi="Il relatore descrive gli assetti.",
        punti_chiave=["Organi", "Deleghe", "Controlli"], confidenza=.92,
    )]

    rendered = render_markdown(report)

    assert "## Interventi" in rendered
    assert "00:00–10:00" in rendered
    assert "Assetti di governance" in rendered
    assert "TRASCRIZIONE" not in rendered


def test_exports_merge_duplicate_speakers_and_correct_near_name_mentions() -> None:
    report = load_report()
    report.speakers = [
        SpeakerProfile(
            id="furio-1", display_name="Furio d'Andrea", role="Relatore",
            confidence="alta", evidence=[{
                "kind": "introduzione", "timestamp_seconds": 10,
                "note": "Intervento di Fulvio D'Andrea sulla governance.",
            }],
        ),
        SpeakerProfile(
            id="furio-2", display_name="Furio d'Andrea", role="Relatore",
            confidence="alta", evidence=[{
                "kind": "slide", "timestamp_seconds": 20,
                "note": "Titolo mostrato durante l'intervento.",
            }],
        ),
    ]
    report.interventions = [Intervention(
        id="i001", start_seconds=0, end_seconds=600, tipo="intervento",
        relatori=["Furio d'Andrea"], titolo="Clausole antistallo",
        sintesi="Fulvio D'Andrea illustra gli assetti di governance.",
        punti_chiave=["Organi", "Deleghe", "Controlli"], confidenza=.92,
    )]

    payload = build_video_report_payload(report, UUID("7f254c4d-fe34-4fd3-a4cf-cda4f447e438"))
    markdown = render_markdown(report)

    assert [speaker["nome"] for speaker in payload["relatori"]] == ["Furio D’Andrea"]
    assert len(payload["relatori"][0]["evidenze"]) == 2
    assert "Fulvio" not in json.dumps(payload, ensure_ascii=False)
    assert "Fulvio" not in markdown
    assert "Furio D’Andrea illustra" in payload["interventi"][0]["sintesi"]


def test_registry_canonicalizes_governance_aliases_and_preserves_unrelated_full_names() -> None:
    report = load_report()
    report.speakers = [
        SpeakerProfile(
            id=f"speaker-{index}", display_name=name, confidence="alta",
            evidence=[{"kind": "introduzione", "note": f"Presentazione: {name}."}],
        )
        for index, name in enumerate([
            "  avv.   FURIO D'ANDREA ", "Luigi   Morra", "ANTONIO sibilia",
            "Dottore Gaetano de vito", "prof. vincenzo MANFREDI",
            "Fabio D'Andrea", "Antonia Sibilia", "Luis Morra",
        ])
    ]
    report.interventions = []

    reconciliation = reporting.reconcile_speakers_detailed(report)

    assert [speaker.display_name for speaker in reconciliation.speakers] == [
        "Furio D’Andrea", "Luigi Morra", "Antonio Sibilia", "Gaetano De Vito",
        "Vincenzo Manfredi", "Fabio D'Andrea", "Antonia Sibilia", "Luis Morra",
    ]
    assert reconciliation.canonical_by_key["avv furio d andrea"] == "Furio D’Andrea"
    assert reconciliation.canonical_by_key["dottore gaetano de vito"] == "Gaetano De Vito"
    assert reconciliation.ambiguous_aliases == []


@pytest.mark.parametrize("honorific", [
    "Avvocata", "Dottoressa", "Professore", "Professoressa",
])
def test_every_supported_honorific_is_role_only_not_identity(honorific: str) -> None:
    report = load_report()
    report.speakers = [SpeakerProfile(
        id="antonio", display_name=f"{honorific} Antonio Sibilia",
        confidence="alta",
        evidence=[{"kind": "introduzione", "note": "Presentazione esplicita."}],
    )]
    report.interventions = []

    speaker = reporting.reconcile_speakers_detailed(report).speakers[0]

    assert speaker.display_name == "Antonio Sibilia"
    assert speaker.role == honorific


def test_surname_alias_is_ambiguous_with_two_registry_people(monkeypatch) -> None:
    monkeypatch.setattr(
        academy_registry,
        "REGISTRY_PEOPLE",
        (*academy_registry.REGISTRY_PEOPLE,
         academy_registry.RegistryPerson("Mario Morra", "mario-morra")),
    )
    report = load_report()
    report.speakers = [SpeakerProfile(
        id="luigi", display_name="Luigi Morra", confidence="alta",
        evidence=[{"kind": "introduzione", "note": "Luigi Morra si presenta."}],
    )]
    report.interventions = [Intervention(
        id="i001", start_seconds=0, end_seconds=600, tipo="intervento",
        relatori=["Dottor Morra"], titolo="Governance", sintesi="Governance.",
        punti_chiave=["Organi", "Deleghe", "Controlli"], confidenza=.9,
    )]

    reconciliation = reporting.reconcile_speakers_detailed(report)

    assert [speaker.display_name for speaker in reconciliation.speakers] == [
        "Luigi Morra", "Morra",
    ]
    assert reconciliation.ambiguous_aliases == ["Morra"]


def test_reconciliation_map_covers_block_and_material_speaker_references() -> None:
    from app.models import ReportMaterial, SpeechBlock

    report = load_report()
    report.speakers = []
    report.interventions = []
    report.speech_blocks = [SpeechBlock(
        id="b001", start_seconds=0, end_seconds=600, tipo="intervento",
        relatori=["Furio d'Andrea", "Avvocato Furio D’Andrea"],
        titolo="Governance", sinossi="Poteri e responsabilità.",
    )]
    report.materials = [ReportMaterial(
        titolo="Slide Morra", relatore="Dottor Morra", file="morra.pptx",
    )]

    reconciliation = reporting.reconcile_speakers_detailed(report)

    assert [speaker.display_name for speaker in reconciliation.speakers] == [
        "Furio D’Andrea", "Luigi Morra",
    ]
    assert reporting.rewrite_speaker_references(
        report.speech_blocks[0].relatori, reconciliation.canonical_by_key,
    ) == ["Furio D’Andrea"]
    assert reconciliation.canonical_by_key["dottor morra"] == "Luigi Morra"
    assert reconciliation.speakers[1].role == "Dottor"
    assert reconciliation.speakers[1].origins == ["inventario"]


@pytest.mark.parametrize(("full_names", "alias", "unresolved"), [
    (("Luigi Morra", "Maria Elena Morra"), "Dottor Morra", "Morra"),
    (("Elena Morra", "Maria Elena Morra"), "Dottor Morra", "Morra"),
    (("Maria De Rossi", "Giulia De Rossi"), "Dottor De Rossi", "De Rossi"),
])
def test_surname_suffix_collision_with_multiple_given_names_stays_ambiguous(
    full_names: tuple[str, str], alias: str, unresolved: str,
) -> None:
    report = load_report()
    report.speakers = []
    report.interventions = [
        Intervention(
            id=f"i{index}", start_seconds=index * 600, end_seconds=(index + 1) * 600,
            tipo="intervento", relatori=[name], titolo="Governance",
            sintesi="Governance.", punti_chiave=["Organi", "Deleghe", "Controlli"],
            confidenza=.9,
        )
        for index, name in enumerate((*full_names, alias))
    ]

    reconciliation = reporting.reconcile_speakers_detailed(report)

    assert [speaker.display_name for speaker in reconciliation.speakers] == [
        *full_names, unresolved,
    ]
    assert reconciliation.ambiguous_aliases == [unresolved]


@pytest.mark.parametrize(("longer_name", "honorific_name", "short_name"), [
    ("Maria Elena Morra", "Avvocato Elena Morra", "Elena Morra"),
    ("Giulia Luca Bianchi", "Dottor Luca Bianchi", "Luca Bianchi"),
])
def test_honorific_two_token_full_name_is_not_merged_into_longer_name(
    longer_name: str, honorific_name: str, short_name: str,
) -> None:
    report = load_report()
    report.speakers = []
    report.interventions = [
        Intervention(
            id=f"i{index}", start_seconds=index * 600, end_seconds=(index + 1) * 600,
            tipo="intervento", relatori=[name], titolo="Governance",
            sintesi="Governance.", punti_chiave=["Organi", "Deleghe", "Controlli"],
            confidenza=.9,
        )
        for index, name in enumerate((longer_name, honorific_name))
    ]

    reconciliation = reporting.reconcile_speakers_detailed(report)

    assert [speaker.display_name for speaker in reconciliation.speakers] == [
        longer_name, short_name,
    ]
    assert reconciliation.ambiguous_aliases == []


def test_honorific_compound_surname_alias_stays_ambiguous() -> None:
    report = load_report()
    report.speakers = []
    names = (
        "Avvocato Maria De Rossi", "Avvocata Giulia De Rossi", "Dottor De Rossi",
    )
    report.interventions = [
        Intervention(
            id=f"i{index}", start_seconds=index * 600, end_seconds=(index + 1) * 600,
            tipo="intervento", relatori=[name], titolo="Governance",
            sintesi="Governance.", punti_chiave=["Organi", "Deleghe", "Controlli"],
            confidenza=.9,
        )
        for index, name in enumerate(names)
    ]

    reconciliation = reporting.reconcile_speakers_detailed(report)

    assert [speaker.display_name for speaker in reconciliation.speakers] == [
        "Maria De Rossi", "Giulia De Rossi", "De Rossi",
    ]
    assert reconciliation.ambiguous_aliases == ["De Rossi"]


@pytest.mark.parametrize(("full_name", "bare_surname"), [
    ("Gaetano De Vito", "De Vito"),
    ("Furio D’Andrea", "D’Andrea"),
])
def test_unique_bare_compound_surname_merges_with_registered_full_name(
    full_name: str, bare_surname: str,
) -> None:
    report = load_report()
    report.speakers = []
    report.interventions = [
        Intervention(
            id=f"i{index}", start_seconds=index * 600, end_seconds=(index + 1) * 600,
            tipo="intervento", relatori=[name], titolo="Governance",
            sintesi="Governance.", punti_chiave=["Organi", "Deleghe", "Controlli"],
            confidenza=.9,
        )
        for index, name in enumerate((full_name, bare_surname))
    ]

    reconciliation = reporting.reconcile_speakers_detailed(report)

    assert [speaker.display_name for speaker in reconciliation.speakers] == [full_name]
    assert reconciliation.ambiguous_aliases == []


def test_normalized_alias_matching_is_bounded_accent_insensitive_and_apostrophe_safe() -> None:
    aliases = {
        "dott morra": "Luigi Morra",
        "morra": "Luigi Morra",
        "jose nunez": "José Núñez",
        "fulvio d andrea": "Furio D’Andrea",
    }
    canonical = ["Luigi Morra", "José Núñez", "Furio D’Andrea"]

    assert reporting.correct_speaker_name_mentions(
        "Dott. Morra e Morra", canonical, aliases,
    ) == "Luigi Morra e Luigi Morra"
    assert reporting.correct_speaker_name_mentions(
        "Jose Nunez", canonical, aliases,
    ) == "José Núñez"
    assert reporting.correct_speaker_name_mentions(
        "Fulvio D ' Andrea", canonical, aliases,
    ) == "Furio D’Andrea"
    assert reporting.correct_speaker_name_mentions(
        "Fulvio D ’ Andrea", canonical, aliases,
    ) == "Furio D’Andrea"
    assert reporting.correct_speaker_name_mentions(
        "Morradale preMorra DAndrea", canonical, aliases,
    ) == "Morradale preMorra DAndrea"
    widely_separated = "Fulvio" + " " * 20 + "D'Andrea"
    assert reporting.correct_speaker_name_mentions(
        widely_separated, canonical, aliases,
    ) == widely_separated


def test_narrative_matcher_does_not_corrupt_names_identifiers_or_sentence_boundaries() -> None:
    aliases = {
        "luigi morra": "Luigi Morra",
        "morra": "Luigi Morra",
        "furio d andrea": "Furio D’Andrea",
        "d andrea": "Furio D’Andrea",
    }
    canonical = ["Luigi Morra", "Furio D’Andrea"]

    for text in (
        "Mario Morra", "Fabio D’Andrea", "Morra2", "_Morra", "Luigi. Morra",
    ):
        assert reporting.correct_speaker_name_mentions(
            text, canonical, aliases,
        ) == text


@pytest.mark.parametrize("text", [
    "mario morra", "mArIo MORRA", "fabio d’andrea", "fAbIo D’ANDREA",
    "Morra2", "_Morra", "Morra\u0301",
])
def test_narrative_surname_alias_protection_is_case_and_unicode_independent(
    text: str,
) -> None:
    assert reporting.correct_speaker_name_mentions(
        text,
        ["Luigi Morra", "Furio D’Andrea"],
        {"morra": "Luigi Morra", "d andrea": "Furio D’Andrea"},
    ) == text


@pytest.mark.parametrize("text", [
    "Morra Mario", "morra mario", "mOrRa mArIo",
    "D’Andrea Fabio", "d’andrea fabio", "D’aNdReA fAbIo",
    "Morra Group", "morra group", "mOrRa gRoUp",
    "D’Andrea & Partners", "d’andrea & partners", "D’aNdReA & pArTnErS",
    "Morra Consulting", "D’Andrea associati", "Morra G.",
    "intervento di Morra Mario", "con D’Andrea & Partners",
    "Dott. Morra Mario", "dott. morra group", "Avv. D’Andrea Fabio",
    "D’Andrea e Partners", "morra e associati", "Studio di Morra",
    "Partners & Morra", "pArTnErS & d’AnDrEa",
])
def test_surname_alias_keeps_following_person_or_organization_context(text: str) -> None:
    assert reporting.correct_speaker_name_mentions(
        text,
        ["Luigi Morra", "Furio D’Andrea"],
        {
            "morra": "Luigi Morra", "dott morra": "Luigi Morra",
            "d andrea": "Furio D’Andrea", "avv d andrea": "Furio D’Andrea",
        },
    ) == text


@pytest.mark.parametrize(("text", "expected"), [
    ("Slide · Morra", "Slide · Luigi Morra"),
    ("Dott. Morra", "Luigi Morra"),
    ("Avv. D’Andrea", "Furio D’Andrea"),
    ("Morra. Mario", "Luigi Morra. Mario"),
    ("Morra; Mario", "Luigi Morra; Mario"),
    ("D’Andrea · Partners", "Furio D’Andrea · Partners"),
    ("Morra e D’Andrea", "Luigi Morra e Furio D’Andrea"),
    ("Morra e: Partners", "Luigi Morra e: Partners"),
    ("Studio. Di Morra", "Studio. Di Luigi Morra"),
])
def test_surname_context_does_not_consume_honorifics_or_cross_prose_punctuation(
    text: str, expected: str,
) -> None:
    assert reporting.correct_speaker_name_mentions(
        text,
        ["Luigi Morra", "Furio D’Andrea"],
        {
            "morra": "Luigi Morra", "dott morra": "Luigi Morra",
            "d andrea": "Furio D’Andrea", "avv d andrea": "Furio D’Andrea",
        },
    ) == expected


@pytest.mark.parametrize(("text", "expected"), [
    ("morra", "Luigi Morra"),
    ("MORRA", "Luigi Morra"),
    ("D'Andrea", "Furio D’Andrea"),
    ("d ’ andrea", "Furio D’Andrea"),
    ("intervento di morra", "intervento di Luigi Morra"),
    ("con d ’ andrea", "con Furio D’Andrea"),
])
def test_truly_bare_surname_aliases_still_rewrite(
    text: str, expected: str,
) -> None:
    assert reporting.correct_speaker_name_mentions(
        text,
        ["Luigi Morra", "Furio D’Andrea"],
        {"morra": "Luigi Morra", "d andrea": "Furio D’Andrea"},
    ) == expected


def test_ambiguous_aliases_are_not_rewritten_in_narrative_text() -> None:
    assert reporting.correct_speaker_name_mentions(
        "Dott. Morra e Morra",
        ["Luigi Morra", "Mario Morra", "Morra"],
        {"dott morra": "Morra", "morra": "Morra"},
        ambiguous_aliases=["Morra"],
    ) == "Dott. Morra e Morra"


def test_exports_include_named_intervention_speakers_missing_from_profiles() -> None:
    report = load_report()
    report.speakers = report.speakers[:1]
    report.interventions = [
        Intervention(
            id="i001", start_seconds=0, end_seconds=300, tipo="intervento",
            relatori=["Gaetano De Vito"], titolo="Apertura",
            sintesi="Introduzione alla governance.",
            punti_chiave=["Organi", "Deleghe", "Controlli"], confidenza=.90,
        ),
        Intervention(
            id="i002", start_seconds=300, end_seconds=900, tipo="intervento",
            relatori=["Furio d'Andrea"], titolo="Poteri e responsabilità",
            sintesi="Analisi della governance della holding.",
            punti_chiave=["Soci", "Amministratori", "Statuto"], confidenza=.95,
        ),
    ]

    payload = build_video_report_payload(
        report, UUID("7f254c4d-fe34-4fd3-a4cf-cda4f447e438")
    )
    markdown = render_markdown(report)

    assert [speaker["nome"] for speaker in payload["relatori"]] == [
        "Giulia Bianchi", "Gaetano De Vito", "Furio D’Andrea",
    ]
    assert "- Gaetano De Vito" in markdown
    assert "- Furio D’Andrea" in markdown


def test_markdown_preserves_generic_formal_speaker_profiles() -> None:
    report = load_report()

    rendered = render_markdown(report)

    assert "- Relatore 2 — Relatrice tecnica (confidenza: bassa)" in rendered


def test_speaker_rejects_personal_name_with_inference_only() -> None:
    with pytest.raises(ValidationError, match="evidenza ammessa"):
        SpeakerProfile.model_validate(
            {
                "id": "relatore-3",
                "display_name": "Elena Verdi",
                "confidence": "bassa",
                "evidence": [
                    {
                        "kind": "inferenza",
                        "note": "Voce distinta senza identificazione supportata.",
                    }
                ],
            }
        )


def test_speaker_accepts_generic_label_without_admissible_evidence() -> None:
    speaker = SpeakerProfile.model_validate(
        {
            "id": "relatore-3",
            "display_name": "Relatore 3",
            "confidence": "bassa",
            "evidence": [
                {
                    "kind": "inferenza",
                    "note": "Voce distinta senza identificazione supportata.",
                }
            ],
        }
    )
    assert speaker.display_name == "Relatore 3"


@pytest.mark.parametrize("field,value", [("duration_seconds", -1), ("duration_seconds", float("nan")),
    ("duration_seconds", float("inf"))])
def test_report_rejects_nonfinite_negative_duration(field, value):
    data = load_report().model_dump()
    data[field] = value
    with pytest.raises(ValueError):
        AcademyReport.model_validate(data)


@pytest.mark.parametrize("section,field,value", [("slides", "timestamp_seconds", float("inf"))])
def test_report_rejects_invalid_timestamps(section, field, value):
    data = load_report().model_dump()
    data[section][0][field] = value
    with pytest.raises(ValueError):
        AcademyReport.model_validate(data)

def test_cost_rejects_negative_nan_and_reversed_bounds():
    for changes in ({"estimated_low_usd": -1}, {"analysis_usd": float("nan")},
                    {"estimated_low_usd": 10, "estimated_high_usd": 1}):
        data = load_report().model_dump()
        data["cost"].update(changes)
        with pytest.raises(ValueError):
            AcademyReport.model_validate(data)


def test_original_title_usage_and_refined_cost_are_rendered():
    from app.models import APIUsage, ProviderUsage, UsageEntry
    usage = APIUsage(transcription=ProviderUsage(entries=[UsageEntry(input_tokens=1000, output_tokens=100,
        request_audio_seconds=3600)]), responses=ProviderUsage(entries=[UsageEntry(input_tokens=2000, output_tokens=200)]))
    report = load_report()
    report.bunny_title = "Titolo originale Bunny"
    report.usage = usage
    report.cost = estimate_cost(3600, 0, usage=usage)
    assert report.cost.transcription_usd == .0035
    assert report.cost.analysis_usd == .0006
    for rendered in (render_markdown(report), render_text(report)):
        assert "Titolo originale Bunny" in rendered
        assert "1000" in rendered and "2000" in rendered
        assert "stima applicativa" in rendered
        assert "non sostituisce la fattura" in rendered


def test_assemblyai_cost_uses_one_global_job_with_diarization_and_speaker_identification():
    from app.models import APIUsage, ProviderUsage, UsageEntry

    usage = APIUsage(transcription=ProviderUsage(entries=[UsageEntry(
        provider_audio_seconds=3600, request_audio_seconds=3600,
    )]))

    cost = estimate_cost(
        3600, 0, usage=usage, transcription_provider="assemblyai",
    )

    assert cost.transcription_usd == .25
    assert cost.estimated_low_usd == .29
    assert cost.estimated_high_usd == .43


def test_assemblyai_cost_counts_both_remote_and_local_bunny_media_reads():
    cost = estimate_cost(
        3600, 1_000_000_000, transcription_provider="assemblyai",
    )

    assert cost.bunny_bandwidth_usd == .02
    assert cost.estimated_low_usd == .31
    assert cost.estimated_high_usd == .45
