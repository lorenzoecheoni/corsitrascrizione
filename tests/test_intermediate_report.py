import json
from itertools import permutations
from pathlib import Path
import socket
from uuid import UUID

import pytest

from app import intermediate_report
from app.intermediate_models import (
    IntermediateSlideV11,
    IntermediateSpeakerV11,
    IntermediateVideoV11,
)
from app.intermediate_report import (
    build_intermediate_report,
    parse_material_source,
    registered_slug,
    split_role_organization,
)
from app.models import AcademyReport, BoundaryEvidence, Intervention, SpeakerProfile
from app.reporting import correct_speaker_name_mentions, render_markdown, render_text


TARGET_GUID = UUID("7f254c4d-fe34-4fd3-a4cf-cda4f447e438")

from granular_support import governance_report


def test_intermediate_json_exposes_blocks_chapters_cost_and_one_public_preview():
    report = governance_report()
    before = report.model_dump_json()
    result = build_intermediate_report(report, TARGET_GUID)
    video = result.video[0]
    assert video.durata_secondi == 5789
    assert [block.id for block in video.blocchi_parlato] == ["v1-b001", "v1-b002"]
    assert [item.id for item in video.interventi] == [f"v1-i{index:03d}" for index in range(1, 7)]
    assert [item.blocco for item in video.interventi] == ["v1-b001"] * 3 + ["v1-b002"] * 3
    assert [item.id for item in video.interventi if item.accesso == "pubblico"] == ["v1-i001"]
    assert video.interventi[-1].end_seconds == video.blocchi_parlato[-1].end_seconds == 5789
    assert video.costo_stimato.model_dump() == {
        "valuta": "USD", "minimo": .123456, "massimo": .789012, "banda_bunny": .012345,
        "trascrizione": .234567, "analisi": .345678, "criterio": "Costi persistiti per richiesta.",
    }
    payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert set(payload) == {"versione", "stato", "corso", "relatori", "video", "verifiche_richieste"}
    assert payload["video"][0]["interventi"][1]["confine_inizio"] == {
        "motivo_editoriale": "slide_e_tema", "regola_audio": "short_pause", "slide_indizio": "0:09:59",
    }
    assert video.slide[0].materiale == video.materiali[0].titolo == "Slide · Furio D'Andrea"
    assert video.slide[0].pagina == 2
    assert video.materiali[0].pagine == 18
    assert video.materiali[0].relatore == "Furio D'Andrea"
    assert result.stato == "verificato"
    checks = result.verifiche_richieste
    assert {check.codice for check in checks} >= {"INTERVENTO_LUNGO", "SLIDE_NON_ABBINATA", "MATERIALE_NON_RAGGIUNGIBILE", "CONFINE"}
    boundaries = [check for check in checks if check.codice == "CONFINE"]
    assert [check.intervento for check in boundaries] == [f"v1-i{index:03d}" for index in range(2, 7)]
    assert "termina v1-i001" in boundaries[0].messaggio
    assert "si conclude qui il tema" in boundaries[0].messaggio
    assert all(check.livello == "avviso" for check in checks)
    assert result.model_dump_json(by_alias=True) == build_intermediate_report(report, TARGET_GUID).model_dump_json(by_alias=True)
    assert report.model_dump_json() == before


def test_profile2_export_sorts_blocks_chapters_and_boundary_evidence_stably():
    report = governance_report()
    expected = build_intermediate_report(report, TARGET_GUID).model_dump_json(by_alias=True)
    report.speech_blocks.reverse()
    report.interventions.reverse()
    report.boundaries.reverse()
    assert build_intermediate_report(report, TARGET_GUID).model_dump_json(by_alias=True) == expected


@pytest.mark.parametrize("missing", ["material_title", "page"])
def test_profile2_warns_for_either_missing_slide_link_part(missing):
    report = governance_report()
    setattr(report.slides[0], missing, None)
    checks = build_intermediate_report(report, TARGET_GUID).verifiche_richieste
    assert any(check.codice == "SLIDE_NON_ABBINATA" and check.campo == "slide[0]" for check in checks)


@pytest.mark.parametrize("length,expected", [(1200, False), (1201, True)])
def test_long_intervention_warning_threshold_is_strict(length, expected):
    video = make_video([make_intervention(0, length, relatori=["Luigi Morra"])])
    checks = intermediate_report.build_verifications(video, [], [])
    assert any(check.codice == "INTERVENTO_LUNGO" for check in checks) == expected


@pytest.mark.parametrize("kind", ["saluti", "pausa", "logistica"])
def test_public_preview_skips_nondidactic_segments_and_includes_900_seconds(kind):
    report = make_report([
        make_intervention(0, 600, tipo=kind), make_intervention(600, 1500), make_intervention(1500, 2100),
    ])
    assert intermediate_report.choose_public_intervention(intermediate_report.normalize_interventions(report)) == "v1-i002"


def test_profile2_without_valid_preview_fails_without_inventing_chapters():
    report = governance_report()
    for chapter in report.interventions:
        chapter.tipo = "saluti"
        chapter.punti_chiave = []
    with pytest.raises(ValueError, match="pubblico"):
        build_intermediate_report(report, TARGET_GUID)


def test_profile2_cannot_publish_an_oversized_preview_fallback():
    report = governance_report()
    chapter = report.interventions[0].model_copy(update={"end_seconds": 5789, "chapters_in_block": 1})
    report.interventions = [chapter]
    report.speech_blocks = [report.speech_blocks[0].model_copy(update={"end_seconds": 5789})]
    report.boundaries = []
    with pytest.raises(ValueError, match="pubblico"):
        build_intermediate_report(report, TARGET_GUID)


@pytest.mark.parametrize("defect", ["unknown_reference", "empty_id", "missing_origin"])
def test_profile2_rejects_broken_stored_provenance_before_rewriting_ids(defect):
    report = governance_report()
    if defect == "unknown_reference":
        for chapter in report.interventions[:3]:
            chapter.block_id = "v1-b001"
    elif defect == "empty_id":
        report.speech_blocks[0].id = ""
        for chapter in report.interventions[:3]:
            chapter.block_id = ""
    else:
        report.interventions[1].boundary_origin = None
    with pytest.raises(ValueError):
        build_intermediate_report(report, TARGET_GUID)


def test_profile2_ignores_raw_inventory_and_deduplicates_persisted_material_sources(monkeypatch):
    report = governance_report()
    report.materials.append(report.materials[0].model_copy())
    def forbidden(*args):
        raise AssertionError("Download must not probe materials")
    monkeypatch.setattr("socket.socket.connect", forbidden)
    result = build_intermediate_report(report, TARGET_GUID,
        material_sources=["Unverified | https://8.8.8.8/private?token=SECRET"])
    assert len(result.video[0].materiali) == 1
    assert "SECRET" not in result.model_dump_json()


def test_raw_inventory_material_title_cannot_bypass_ascii_canonicalization():
    report = make_report([make_intervention(0, 600, relatori=["Furio D’Andrea"])])
    material = build_intermediate_report(report, TARGET_GUID,
        material_sources=["Slide · Furio D ’ Andrea | https://example.test/slide.pptx"]).video[0].materiali[0]
    assert material.titolo == "Slide · Furio D'Andrea"


@pytest.mark.parametrize("value", ["Slide · Furio D’Andrea", "missing.pdf", "unrecognized.txt"])
def test_export_material_parser_rejects_bare_non_sources(value):
    assert parse_material_source(value) is None


def test_profile2_markdown_and_text_render_the_same_persisted_contract():
    for rendered in (render_markdown(governance_report()), render_text(governance_report())):
        for text in ("Blocchi parlato", "v1-b001", "v1-i002", "Capitolo 2/3", "slide_e_tema", "short_pause",
                     "0:09:59", "Slide · Furio D'Andrea", "pagina 2", "INTERVENTO_LUNGO", "CONFINE",
                     "SLIDE_NON_ABBINATA", "USD", "0.123456", "0.789012", "pubblico", "iscritti"):
            assert text in rendered


@pytest.mark.parametrize("critical", ["speaker", "timing"])
def test_profile2_only_critical_identity_or_timeline_warnings_require_verification(critical):
    report = governance_report()
    if critical == "speaker":
        report.interventions[0].relatori = []
    else:
        report.duration_seconds = 5790
    result = build_intermediate_report(report, TARGET_GUID)
    assert result.stato == "da_verificare"
    assert {check.codice for check in result.verifiche_richieste if check.livello == "critico"} == {
        "RELATORE_NON_IDENTIFICATO" if critical == "speaker" else "TEMPI_INCOERENTI",
    }


def report_with_boundary_evidence() -> AcademyReport:
    interventions = [
        make_intervention(0, 11, titolo="Uno"),
        make_intervention(11, 22, titolo="Due"),
        make_intervention(22, 33, titolo="Tre"),
        make_intervention(33, 44, titolo="Quattro"),
    ]
    for intervention, stored_id in zip(
        interventions, ["stored-z", "stored-a", "stored-y", "stored-b"], strict=True,
    ):
        intervention.id = stored_id
    report = make_report(interventions, duration=44)
    report.boundaries = [
        BoundaryEvidence(
            previous_intervention_id="stored-z", next_intervention_id="stored-a",
            boundary_seconds=11,
            words_before=["la", "governance", "si", "chiude", "qui"],
            words_after=["passiamo", "ora", "al", "tema", "fiscale"],
            pause_before=False, pause_after=False, rule="no_pause",
        ),
        BoundaryEvidence(
            previous_intervention_id="stored-a", next_intervention_id="stored-y",
            boundary_seconds=22, words_before=["solo", "quattro", "parole", "qui"],
            words_after=[], pause_before=True, pause_after=True, rule="short_pause",
        ),
        BoundaryEvidence(
            previous_intervention_id="stored-y", next_intervention_id="stored-b",
            boundary_seconds=33, words_before=[], words_after=[],
            pause_before=True, pause_after=True, rule="long_pause",
        ),
    ]
    return report


def test_confine_warnings_use_chronological_export_ids_and_exact_audio_evidence():
    report = report_with_boundary_evidence()

    payload = build_intermediate_report(report, TARGET_GUID).model_dump(
        mode="json", by_alias=True, exclude_none=True,
    )
    checks = [
        item for item in payload["verifiche_richieste"] if item["codice"] == "CONFINE"
    ]

    assert [item["intervento"] for item in checks] == ["v1-i002", "v1-i003", "v1-i004"]
    assert [item["campo"] for item in checks] == ["inizio", "inizio", "inizio"]
    assert "Confine 0:00:11; termina v1-i001." in checks[0]["messaggio"]
    assert "Prima: «la governance si chiude qui»." in checks[0]["messaggio"]
    assert "Dopo: «passiamo ora al tema fiscale»." in checks[0]["messaggio"]
    assert "Prima: «solo quattro parole qui» (pausa)." in checks[1]["messaggio"]
    assert "Dopo: (pausa)." in checks[1]["messaggio"]
    assert checks[2]["messaggio"].count("(pausa)") == 2
    assert payload["stato"] == "verificato"


def test_boundary_builder_rejects_incomplete_evidence_and_keeps_distinct_pairs():
    report = report_with_boundary_evidence()
    mapping = {
        intervention.id: f"v1-i{index:03d}"
        for index, intervention in enumerate(report.interventions, start=1)
    }

    checks = intermediate_report.build_boundary_verifications(report, mapping)

    assert len(checks) == 3
    assert all(item.livello == "avviso" for item in checks)
    report.boundaries.pop()
    with pytest.raises(ValueError, match="confini"):
        intermediate_report.build_boundary_verifications(report, mapping)


def test_main_builder_rejects_incomplete_boundaries_before_material_parsing(monkeypatch):
    report = make_report([
        make_intervention(0, 10), make_intervention(10, 20),
    ], duration=20)
    report.boundaries = []

    checked_urls = []

    def record_material_check(source):
        checked_urls.append(source)
        raise AssertionError("Must validate boundaries before parsing sources")

    monkeypatch.setattr(intermediate_report, "parse_material_source", record_material_check)

    with pytest.raises(ValueError, match="confini"):
        build_intermediate_report(
            report,
            TARGET_GUID,
            material_sources=["https://materials.example.test/dispensa.pdf"],
        )
    assert checked_urls == []


def test_builder_uses_aligner_tie_to_previous_second_for_export_duration():
    report = make_report([make_intervention(0, 10)], duration=10)
    report.duration_seconds = 10.5

    result = build_intermediate_report(report, TARGET_GUID)

    assert result.video[0].durata_secondi == 10
    assert all(check.codice != "TEMPI_INCOERENTI" for check in result.verifiche_richieste)


@pytest.mark.parametrize(("words_before", "pause_before", "words_after", "pause_after", "expected"), [
    (["meno", "di", "cinque", "parole"], True,
     ["dopo", "restano", "cinque", "parole", "esatte"], False,
     "Prima: «meno di cinque parole» (pausa). Dopo: «dopo restano cinque parole esatte»."),
    (["prima", "restano", "cinque", "parole", "esatte"], False,
     [], True,
     "Prima: «prima restano cinque parole esatte». Dopo: (pausa)."),
    (["cinque", "parole", "ma", "pausa", "prima"], True,
     ["dopo", "restano", "cinque", "parole", "esatte"], False,
     "Prima: «cinque parole ma pausa prima» (pausa). Dopo: «dopo restano cinque parole esatte»."),
    (["prima", "restano", "cinque", "parole", "esatte"], False,
     ["cinque", "parole", "ma", "pausa", "dopo"], True,
     "Prima: «prima restano cinque parole esatte». Dopo: «cinque parole ma pausa dopo» (pausa)."),
    (["cinque", "parole", "ma", "pausa", "prima"], True,
     ["cinque", "parole", "ma", "pausa", "dopo"], True,
     "Prima: «cinque parole ma pausa prima» (pausa). Dopo: «cinque parole ma pausa dopo» (pausa)."),
])
def test_confine_renders_pause_flags_instead_of_partial_or_zero_word_excerpts(
    words_before, pause_before, words_after, pause_after, expected,
):
    report = make_report([
        make_intervention(0, 10), make_intervention(10, 20),
    ], duration=20)
    report.interventions[0].id = "first"
    report.interventions[1].id = "second"
    report.boundaries = [BoundaryEvidence(
        previous_intervention_id="first", next_intervention_id="second",
        boundary_seconds=10, words_before=words_before, words_after=words_after,
        pause_before=pause_before, pause_after=pause_after, rule="short_pause",
    )]

    check = intermediate_report.build_boundary_verifications(
        report, {"first": "v1-i001", "second": "v1-i002"},
    )[0]

    assert expected in check.messaggio


@pytest.mark.parametrize("formal_names", [
    ["Marco Rossi", "Mario Rossi"], ["Marco Rossi"], [],
])
def test_explicit_similar_speaker_identities_remain_distinct_in_all_exports(formal_names):
    report = make_report([
        make_intervention(0, 120, relatori=["Marco Rossi"], sintesi="Marco Rossi tratta la governance."),
        make_intervention(120, 240, relatori=["Mario Rossi"], sintesi="Mario Rossi tratta i controlli."),
    ])
    report.speakers = [SpeakerProfile(
        id=name, display_name=name, confidence="alta",
        evidence=[{"kind": "introduzione", "note": f"Presentazione di {name}."}],
    ) for name in formal_names]
    saved = report.model_dump()

    result = build_intermediate_report(report, TARGET_GUID)

    assert [speaker.nome for speaker in result.relatori] == ["Marco Rossi", "Mario Rossi"]
    assert [item.relatori for item in result.video[0].interventi] == [["Marco Rossi"], ["Mario Rossi"]]
    for render in (render_markdown, render_text):
        text = render(report)
        assert "Mario Rossi (confidenza:" in text
        assert "Marco Rossi (confidenza:" in text
        assert "Relatori: Marco Rossi;" in text
        assert "Relatori: Mario Rossi;" in text
        assert "Marco Rossi tratta la governance." in text
        assert "Mario Rossi tratta i controlli." in text
    assert report.model_dump() == saved


def test_accent_insensitive_intervention_references_reuse_first_canonical_profile():
    report = make_report([
        make_intervention(0, 120, relatori=["Jose Nunez"]),
        make_intervention(120, 240, relatori=["JOSÉ NÚÑEZ"]),
    ])
    report.speakers = [SpeakerProfile(
        id="jose", display_name="José Núñez", confidence="alta",
        evidence=[{"kind": "slide", "note": "Nome in slide: José Núñez."}],
    )]
    saved = report.model_dump()

    result = build_intermediate_report(report, TARGET_GUID)

    assert [speaker.nome for speaker in result.relatori] == ["José Núñez"]
    assert [item.relatori for item in result.video[0].interventi] == [["José Núñez"], ["José Núñez"]]
    assert report.model_dump() == saved


@pytest.mark.parametrize("summary", [
    "Relatore Gaetano De Vito approfondisce il realizzo controllato.",
    "Relatore Marco Rossi: illustra gli assetti.",
    "Relatore José Núñez presenta i controlli.",
])
def test_provider_summary_normalization_preserves_explicit_personal_names(summary):
    report = make_report([make_intervention(0, 120, sintesi=summary)])

    assert intermediate_report.normalize_interventions(report)[0].sintesi == summary


def test_malformed_optional_material_url_is_rejected_with_nonblocking_warning():
    source = "https://[::1/dispensa.pdf"
    report = make_report([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])

    result = build_intermediate_report(report, TARGET_GUID, material_sources=[source])

    assert result.video[0].materiali == []
    assert result.stato == "verificato"
    assert [check.codice for check in result.verifiche_richieste if check.campo == "materiali:MATERIALE_NON_RAGGIUNGIBILE"] == [
        "MATERIALE_NON_RAGGIUNGIBILE",
    ]


@pytest.mark.parametrize("label", ["Relatore A", "Relatore 12", "Speaker_01", "provider-F", "voce C2"])
def test_supported_provider_label_formats_still_normalize_to_content(label):
    report = make_report([make_intervention(0, 120, sintesi=f"{label}: approfondisce i controlli.")])

    assert intermediate_report.normalize_interventions(report)[0].sintesi == "Tema trattato: i controlli."


def test_timeline_only_normalized_names_preserve_the_first_spelling_and_known_alias():
    report = make_report([
        make_intervention(0, 120, relatori=["Jose Nunez"]),
        make_intervention(120, 240, relatori=["José Núñez", "Fulvio D’Andrea"]),
        make_intervention(240, 360, relatori=["Furio d'Andrea"]),
    ])
    report.speakers = []

    result = build_intermediate_report(report, TARGET_GUID)

    assert [speaker.nome for speaker in result.relatori] == ["Jose Nunez", "Furio D'Andrea"]
    assert [item.relatori for item in result.video[0].interventi] == [
        ["Jose Nunez"], ["Jose Nunez", "Furio D'Andrea"], ["Furio D'Andrea"],
    ]
    assert "Fulvio" not in render_markdown(report)
    assert "Fulvio" not in render_text(report)


def make_intervention(
    start: float,
    end: float,
    *,
    tipo: str = "intervento",
    titolo: str = "Approfondimento",
    sintesi: str = "Approfondimento sul tema.",
    relatori: list[str] | None = None,
    punti_chiave: list[str] | None = None,
    confidenza: float = .9,
) -> Intervention:
    return Intervention(
        id=f"source-{start}-{end}",
        start_seconds=start,
        end_seconds=end,
        tipo=tipo,
        relatori=[] if relatori is None else relatori,
        titolo=titolo,
        sintesi=sintesi,
        punti_chiave=(
            ["Primo punto", "Secondo punto", "Terzo punto"]
            if punti_chiave is None and tipo == "intervento"
            else punti_chiave or []
        ),
        confidenza=confidenza,
    )


def make_report(items: list[Intervention], *, duration: int | None = None) -> AcademyReport:
    report = report_with_reconciled_speakers()
    report.audio_boundary_version = 1
    report.interventions = items
    report.duration_seconds = duration if duration is not None else int(items[-1].end_seconds)
    report.boundaries = [
        BoundaryEvidence(
            previous_intervention_id=previous.id,
            next_intervention_id=following.id,
            boundary_seconds=int(previous.end_seconds),
            words_before=[], words_after=[], pause_before=True, pause_after=True,
            rule="long_pause",
        )
        for previous, following in zip(items, items[1:])
    ]
    return report


def make_video(
    interventions: list[Intervention],
    *,
    duration: int | None = None,
    slide_confidences: list[float] = (),
) -> IntermediateVideoV11:
    normalized = intermediate_report.normalize_interventions(
        make_report(interventions, duration=duration)
    )
    return IntermediateVideoV11(
        chiave="v1",
        guid=str(TARGET_GUID),
        titolo_bunny="Video di prova",
        durata_secondi=duration if duration is not None else int(interventions[-1].end_seconds),
        ordine=1,
        lingua="italiano",
        sinossi="Sinossi",
        interventi=normalized,
        slide=[
            IntermediateSlideV11(
                inizio=index * 10,
                titolo="Slide",
                testo_principale="Contenuto",
                confidenza=confidence,
            )
            for index, confidence in enumerate(slide_confidences)
        ],
        materiali=[],
    )


def report_with_reconciled_speakers() -> AcademyReport:
    data = json.loads(Path("tests/fixtures/report.json").read_text())
    report = AcademyReport.model_validate(data)
    report.audio_boundary_version = 1
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
    report.duration_seconds = 900
    report.boundaries = [
        BoundaryEvidence(
            previous_intervention_id=previous.id,
            next_intervention_id=following.id,
            boundary_seconds=int(previous.end_seconds),
            words_before=[], words_after=[], pause_before=True, pause_after=True,
            rule="long_pause",
        )
        for previous, following in zip(report.interventions, report.interventions[1:])
    ]
    return report


def test_builder_wraps_one_video_and_reconciles_registry_speakers() -> None:
    report = report_with_reconciled_speakers()

    result = build_intermediate_report(report, TARGET_GUID)

    data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert data["versione"] == 1
    assert data["corso"] == {
        "titolo": "Webinar con Furio D'Andrea",
        "sinossi_corso": "Furio D'Andrea presenta la sinossi.",
    }
    assert [(item["chiave"], item["ordine"]) for item in data["video"]] == [("v1", 1)]
    assert [item["nome"] for item in data["relatori"]] == [
        "Vincenzo Manfredi", "Gaetano De Vito", "Furio D'Andrea",
        "Antonio Sibilia", "Luigi Morra",
    ]
    assert {item.get("slug") for item in data["relatori"]} == {
        "vincenzo-manfredi", "gaetano-de-vito", "antonio-sibilia",
        "luigi-morra", "furio-dandrea",
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
    assert registered_slug("Furio d'Andrea") == "furio-dandrea"
    assert speakers[1]["origine_nome"] == ["audio"]
    assert speakers[1]["confidenza"] == .65


def test_role_split_and_furio_registry_entry_avoid_unregistered_warning() -> None:
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
    assert furio_warnings == []


@pytest.mark.parametrize("organization", ["Ass Holding", "Asso Holding", "Assoholding"])
def test_role_split_accepts_every_explicit_assoholding_spelling(organization: str) -> None:
    assert split_role_organization(f"Direttore di {organization}") == (
        "Direttore", "Assoholding",
    )


def test_role_split_canonicalizes_assholding_without_the_required_space() -> None:
    assert split_role_organization("Direttore di Assholding") == (
        "Direttore", "Assoholding",
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
    ) == "Furio D'Andrea e Furio D'Andrea"
    assert names.count("Furio D'Andrea") == 1
    assert "Fulvio D'Andrea" not in names


def test_governance_aliases_collapse_to_registry_people_and_rewrite_references() -> None:
    names = [
        "Avvocato Furio D'Andrea", "Luigi Morra", "Dottor Morra",
        "Antonio Sibilia", "Dott. Sibilia", "Dottore Gaetano de Vito",
        "prof. vincenzo MANFREDI",
    ]
    report = make_report([
        make_intervention(
            index * 120, (index + 1) * 120, relatori=[name],
            titolo=f"Intervento di {name}", sintesi=f"{name} tratta la governance.",
        )
        for index, name in enumerate(names)
    ])
    report.speakers = [
        SpeakerProfile(
            id="furio", display_name="Avv. Furio D'Andrea",
            role="Responsabile legale di ASSOHOLDING", confidence="alta",
            evidence=[{"kind": "slide", "note": "Avv. Furio D'Andrea."}],
        ),
        SpeakerProfile(
            id="luigi", display_name="Luigi Morra", confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Luigi Morra."}],
        ),
        SpeakerProfile(
            id="antonio", display_name="Antonio Sibilia", confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Antonio Sibilia."}],
        ),
    ]

    result = build_intermediate_report(report, TARGET_GUID)

    assert [(person.nome, person.slug) for person in result.relatori] == [
        ("Furio D'Andrea", "furio-dandrea"),
        ("Luigi Morra", "luigi-morra"),
        ("Antonio Sibilia", "antonio-sibilia"),
        ("Gaetano De Vito", "gaetano-de-vito"),
        ("Vincenzo Manfredi", "vincenzo-manfredi"),
    ]
    assert [item.relatori for item in result.video[0].interventi] == [
        ["Furio D'Andrea"], ["Luigi Morra"], ["Luigi Morra"],
        ["Antonio Sibilia"], ["Antonio Sibilia"], ["Gaetano De Vito"],
        ["Vincenzo Manfredi"],
    ]
    assert result.relatori[0].ruolo == "Responsabile legale"
    assert result.relatori[0].organizzazione == "Assoholding"
    assert all(
        title not in name
        for item in result.video[0].interventi
        for name in item.relatori
        for title in ("Avvocato", "Avv.", "Dottor", "Dott.", "Prof.")
    )
    assert all(
        check.codice != "ALIAS_RELATORE_AMBIGUO"
        for check in result.verifiche_richieste
    )


def test_compatible_explicit_role_keeps_its_evidenced_organization() -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Furio d'Andrea"]),
    ])
    report.speakers = [
        SpeakerProfile(
            id="furio-a", display_name="Furio d'Andrea",
            role="Responsabile legale", confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Furio si presenta."}],
        ),
        SpeakerProfile(
            id="furio-b", display_name="Avv. Furio D’Andrea",
            role="Responsabile legale di Asso Holding", confidence="alta",
            evidence=[{"kind": "slide", "note": "Qualifica completa in slide."}],
        ),
    ]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.ruolo == "Responsabile legale"
    assert speaker.organizzazione == "Assoholding"


def test_professional_qualification_and_moderator_role_are_preserved_separately() -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Furio d'Andrea"]),
    ])
    report.speakers = [
        SpeakerProfile(
            id="furio-generic", display_name="Furio d'Andrea",
            role="Relatore", confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Presentazione."}],
        ),
        SpeakerProfile(
            id="furio-video", display_name="Furio D’Andrea",
            role="moderatore", confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Modera il video."}],
        ),
        SpeakerProfile(
            id="furio-professional", display_name="Furio D’Andrea",
            role="Avvocato", confidence="alta",
            evidence=[{"kind": "slide", "note": "Qualifica professionale."}],
        ),
    ]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.ruolo == "Avvocato; moderatore"
    assert speaker.organizzazione is None


def test_honorific_qualification_beats_generic_explicit_role_after_alias_merge() -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Luigi Morra"]),
    ])
    report.speakers = [SpeakerProfile(
        id="luigi", display_name="Dottor Morra", role="Relatore", confidence="alta",
        evidence=[{"kind": "introduzione", "note": "Presentazione."}],
    )]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.nome == "Luigi Morra"
    assert speaker.ruolo == "Dottor"


def test_honorific_qualification_combines_with_explicit_video_role() -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Luigi Morra"]),
    ])
    report.speakers = [SpeakerProfile(
        id="luigi", display_name="Avvocato Luigi Morra", role="moderatore",
        confidence="alta",
        evidence=[{"kind": "introduzione", "note": "Modera il video."}],
    )]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.ruolo == "Avvocato; moderatore"
    assert speaker.organizzazione is None


def test_honorific_qualification_combines_with_video_role_from_separate_evidence() -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Luigi Morra"]),
    ])
    report.speakers = [
        SpeakerProfile(
            id="luigi-qualification", display_name="Dottor Morra", role="Relatore",
            confidence="alta",
            evidence=[{"kind": "slide", "note": "Qualifica esplicita."}],
        ),
        SpeakerProfile(
            id="luigi-video", display_name="Luigi Morra", role="moderatore",
            confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Modera il video."}],
        ),
    ]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.ruolo == "Dottor; moderatore"


def test_organization_qualification_combines_with_compatible_video_role() -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Luigi Morra"]),
    ])
    report.speakers = [
        SpeakerProfile(
            id="luigi-professional", display_name="Luigi Morra",
            role="Avvocato di Assoholding", confidence="alta",
            evidence=[{"kind": "slide", "note": "Qualifica esplicita."}],
        ),
        SpeakerProfile(
            id="luigi-video", display_name="Luigi Morra", role="moderatore",
            confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Modera il video."}],
        ),
    ]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.ruolo == "Avvocato; moderatore"
    assert speaker.organizzazione == "Assoholding"


@pytest.mark.parametrize("role,expected_role,expected_organization", [
    ("Commercialista; revisore legale", "Commercialista; revisore legale", None),
    ("Avvocato; docente universitario", "Avvocato; docente universitario", None),
    (
        "Commercialista; revisore legale di Assoholding",
        "Commercialista; revisore legale", "Assoholding",
    ),
    (
        "Commercialista di Assoholding; revisore legale",
        "Commercialista; revisore legale", "Assoholding",
    ),
    (
        "Commercialista; revisore legale di Assoholding; moderatore",
        "Commercialista; revisore legale; moderatore", "Assoholding",
    ),
    (
        " Commercialista ; revisore legale ; commercialista ; REVISORE LEGALE ; ",
        "Commercialista; revisore legale", None,
    ),
    (
        "revisore legale; Commercialista; revisore legale",
        "revisore legale; Commercialista", None,
    ),
    (
        "Commercialista; moderatore; revisore legale; MODERATORE",
        "Commercialista; revisore legale; moderatore", None,
    ),
    (
        "Relatore; Commercialista; revisore legale",
        "Commercialista; revisore legale", None,
    ),
])
def test_single_source_explicit_qualification_bundle_is_preserved(
    role: str, expected_role: str, expected_organization: str | None,
) -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Luigi Morra"]),
    ])
    report.speakers = [SpeakerProfile(
        id="luigi", display_name="Dottor Morra", role=role, confidence="alta",
        evidence=[{"kind": "slide", "note": "Qualifiche esplicite nella stessa fonte."}],
    )]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.ruolo == expected_role
    assert speaker.organizzazione == expected_organization


@pytest.mark.parametrize("first_role,second_role", [
    ("Commercialista; revisore legale", "Avvocato; docente universitario"),
    ("Avvocato; docente universitario", "Commercialista; revisore legale"),
])
def test_conflicting_source_bundles_keep_the_first_complete_qualification(
    first_role: str, second_role: str,
) -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Luigi Morra"]),
    ])
    report.speakers = [
        SpeakerProfile(
            id=f"luigi-{index}", display_name="Luigi Morra", role=role,
            confidence="alta",
            evidence=[{"kind": "slide", "note": "Qualifiche esplicite nella fonte."}],
        )
        for index, role in enumerate((first_role, second_role))
    ]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.ruolo == first_role


@pytest.mark.parametrize("professional_role,expected_role,expected_organization", [
    ("Commercialista", "Commercialista; moderatore", None),
    ("Commercialista di Assoholding", "Commercialista; moderatore", "Assoholding"),
    (
        "Commercialista; revisore legale",
        "Commercialista; revisore legale; moderatore", None,
    ),
    (
        "Commercialista; revisore legale di Assoholding",
        "Commercialista; revisore legale; moderatore", "Assoholding",
    ),
])
@pytest.mark.parametrize("order", list(permutations(range(3))))
def test_role_specificity_is_independent_from_evidence_order(
    professional_role: str, expected_role: str, expected_organization: str | None,
    order: tuple[int, int, int],
) -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Luigi Morra"]),
    ])
    profiles = [
        SpeakerProfile(
            id="luigi-fallback", display_name="Dottor Morra", role="Relatore",
            confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Titolo onorifico."}],
        ),
        SpeakerProfile(
            id="luigi-video", display_name="Luigi Morra", role="moderatore",
            confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Modera il video."}],
        ),
        SpeakerProfile(
            id="luigi-professional", display_name="Luigi Morra",
            role=professional_role, confidence="alta",
            evidence=[{"kind": "slide", "note": "Qualifica specifica."}],
        ),
    ]
    report.speakers = [profiles[index] for index in order]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.ruolo == expected_role
    assert speaker.organizzazione == expected_organization


def test_honorific_remains_fallback_and_incompatible_specific_role_keeps_first() -> None:
    report = make_report([
        make_intervention(0, 600, relatori=["Dott. Antonio Sibilia"]),
    ])
    report.speakers = [
        SpeakerProfile(
            id="antonio-first", display_name="Dott. Antonio Sibilia",
            role="Amministratore", confidence="alta",
            evidence=[{"kind": "introduzione", "note": "Prima qualifica."}],
        ),
        SpeakerProfile(
            id="antonio-conflict", display_name="Antonio Sibilia",
            role="Sindaco", confidence="alta",
            evidence=[{"kind": "slide", "note": "Seconda qualifica."}],
        ),
    ]

    speaker = build_intermediate_report(report, TARGET_GUID).relatori[0]

    assert speaker.ruolo == "Amministratore"

    report.speakers = []
    fallback = build_intermediate_report(report, TARGET_GUID).relatori[0]
    assert fallback.ruolo == "Dott."


def test_surname_only_alias_stays_unresolved_when_report_contains_a_collision() -> None:
    report = make_report([
        make_intervention(0, 120, relatori=["Luigi Morra"]),
        make_intervention(120, 240, relatori=["Mario Morra"]),
        make_intervention(240, 360, relatori=["Dottor Morra"]),
    ])
    report.speakers = [
        SpeakerProfile(
            id=name, display_name=name, confidence="alta",
            evidence=[{"kind": "introduzione", "note": f"Presentazione di {name}."}],
        )
        for name in ("Luigi Morra", "Mario Morra")
    ]

    result = build_intermediate_report(report, TARGET_GUID)

    assert [speaker.nome for speaker in result.relatori] == [
        "Luigi Morra", "Mario Morra", "Morra",
    ]
    assert result.video[0].interventi[2].relatori == ["Morra"]
    warnings = [
        check for check in result.verifiche_richieste
        if check.codice == "ALIAS_RELATORE_AMBIGUO"
    ]
    assert len(warnings) == 1
    assert "Morra" in warnings[0].messaggio


def test_unresolved_case_and_punctuation_variants_share_one_stable_display() -> None:
    names = [
        "Luigi Morra", "Mario Morra", "Dottor Morra", "Dott. MORRA",
        "Rossi", "ROSSI",
    ]
    report = make_report([
        make_intervention(index * 120, (index + 1) * 120, relatori=[name])
        for index, name in enumerate(names)
    ])
    report.speakers = []

    reconciliation = intermediate_report.reconcile_speakers_detailed(report)
    result = build_intermediate_report(report, TARGET_GUID)

    assert reconciliation.ambiguous_aliases == ["Morra"]
    assert [speaker.nome for speaker in result.relatori] == [
        "Luigi Morra", "Mario Morra", "Morra", "Rossi",
    ]
    assert [item.relatori for item in result.video[0].interventi] == [
        ["Luigi Morra"], ["Mario Morra"], ["Morra"], ["Morra"],
        ["Rossi"], ["Rossi"],
    ]
    assert [
        warning.messaggio for warning in result.verifiche_richieste
        if warning.codice == "ALIAS_RELATORE_AMBIGUO"
    ] == [
        "L'alias relatore Morra corrisponde a più persone: "
        "mantenere l'identità separata fino alla verifica nel Registro."
    ]


def test_dotted_initial_stays_distinct_from_apostrophe_surname_variants() -> None:
    names = [
        "Furio D’Andrea", "Domenico Andrea", "Dott. D. Andrea",
        "DOTT. d .   ANDREA", "D'Andrea", "D ’ Andrea",
    ]
    report = make_report([
        make_intervention(index * 120, (index + 1) * 120, relatori=[name])
        for index, name in enumerate(names)
    ])
    report.speakers = []

    result = build_intermediate_report(report, TARGET_GUID)

    assert [(speaker.nome, speaker.slug) for speaker in result.relatori] == [
        ("Furio D'Andrea", "furio-dandrea"),
        ("Domenico Andrea", None),
        ("D. Andrea", None),
    ]
    assert [item.relatori for item in result.video[0].interventi] == [
        ["Furio D'Andrea"], ["Domenico Andrea"], ["D. Andrea"],
        ["D. Andrea"], ["Furio D'Andrea"], ["Furio D'Andrea"],
    ]
    assert all(
        check.codice != "ALIAS_RELATORE_AMBIGUO" or "D. Andrea" not in check.messaggio
        for check in result.verifiche_richieste
    )
    assert any(
        check.codice == "RELATORE_NON_NEL_REGISTRO"
        and "D. Andrea" in check.messaggio
        for check in result.verifiche_richieste
    )


def test_resolved_material_alias_rewrites_title_and_speaker_with_complete_map() -> None:
    from app.models import ReportMaterial

    report = make_report([
        make_intervention(0, 600, relatori=["Luigi Morra"]),
    ])
    report.speakers = []
    report.materials = [ReportMaterial(
        titolo="Slide · Dott. Morra", relatore="Dott. Morra", file="morra.pptx",
    )]

    material = build_intermediate_report(report, TARGET_GUID).video[0].materiali[0]

    assert material.titolo == "Slide · Luigi Morra"
    assert material.relatore == "Luigi Morra"


def test_granular_export_rewrites_block_chapter_slide_and_material_references() -> None:
    from app.models import ChapterBoundaryOrigin, ReportMaterial, SpeechBlock

    chapter = make_intervention(
        0, 600, relatori=["Avv. Furio d'Andrea", "Furio D’Andrea"],
        titolo="Capitolo di Fulvio D'Andrea",
        sintesi="Fulvio D'Andrea illustra la governance.",
        punti_chiave=[
            "Poteri illustrati da Fulvio D'Andrea",
            "Deleghe secondo Fulvio D'Andrea",
            "Controlli riepilogati da Fulvio D'Andrea",
        ],
    ).model_copy(update={
        "block_id": "b001", "chapter_number": 1, "chapters_in_block": 1,
        "boundary_origin": ChapterBoundaryOrigin(
            motivo_editoriale="inizio_blocco", regola_audio="long_pause",
        ),
    })
    report = make_report([chapter], duration=600)
    report.analysis_profile = 2
    report.speakers = []
    report.speech_blocks = [SpeechBlock(
        id="b001", start_seconds=0, end_seconds=600, tipo="intervento",
        relatori=["Avvocato Furio D'Andrea"],
        titolo="Blocco di Fulvio D'Andrea",
        sinossi="Fulvio D'Andrea tratta gli assetti.",
    )]
    report.materials = [ReportMaterial(
        titolo="Slide · Fulvio D'Andrea", relatore="Avv. Furio D'Andrea",
        file="furio.pptx", pagine=18,
    )]
    report.slides = report.slides[:1]
    report.slides[0].title = "Fulvio D'Andrea in apertura"
    report.slides[0].visible_content = ["Relazione di Fulvio D'Andrea"]

    result = build_intermediate_report(report, TARGET_GUID)
    video = result.video[0]

    assert video.blocchi_parlato[0].relatori == ["Furio D'Andrea"]
    assert video.blocchi_parlato[0].titolo == "Blocco di Furio D'Andrea"
    assert video.blocchi_parlato[0].sinossi == "Furio D'Andrea tratta gli assetti."
    assert video.interventi[0].relatori == ["Furio D'Andrea"]
    assert video.interventi[0].titolo == "Capitolo di Furio D'Andrea"
    assert video.interventi[0].sintesi == "Furio D'Andrea illustra la governance."
    assert video.interventi[0].punti_chiave == [
        "Poteri illustrati da Furio D'Andrea",
        "Deleghe secondo Furio D'Andrea",
        "Controlli riepilogati da Furio D'Andrea",
    ]
    assert video.interventi[0].blocco == "v1-b001"
    assert video.slide[0].titolo == "Furio D'Andrea in apertura"
    assert video.slide[0].testo_principale == "Relazione di Furio D'Andrea"
    assert video.materiali[0].relatore == "Furio D'Andrea"
    assert video.materiali[0].titolo == "Slide · Furio D'Andrea"
    assert "Furio D’Andrea" not in result.model_dump_json()


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

    interventions = intermediate_report.normalize_interventions(report)

    assert [item.id for item in interventions] == [
        "v1-i001", "v1-i002", "v1-i003", "v1-i004", "v1-i005",
    ]
    assert [item.relatori for item in interventions] == [
        ["Gaetano De Vito"], ["Vincenzo Manfredi"], ["Furio d'Andrea"],
        ["Antonio Sibilia"], ["Luigi Morra"],
    ]


@pytest.mark.parametrize(("title", "summary", "speakers", "expected"), [
    ("Ringraziamenti", "Grazie a tutti.", ["Vincenzo Manfredi"], "saluti"),
    ("Passaggio", "Passaggio della parola ad Antonio.", ["Vincenzo Manfredi"], "cambio_relatore"),
    ("Chiarimento", "Risposta sul regime fiscale.", ["Luigi Morra"], "domande"),
    ("Micro-turno", "Precisazione sul valore fiscale.", ["Luigi Morra"], "domande"),
    ("Silenzio", "Nessun parlato.", [], "pausa"),
])
def test_under_twenty_seconds_is_never_an_intervention(
    title: str, summary: str, speakers: list[str], expected: str
) -> None:
    item = make_intervention(0, 9, titolo=title, sintesi=summary, relatori=speakers)

    result = intermediate_report.normalize_interventions(make_report([item]))[0]

    assert result.tipo == expected
    assert result.punti_chiave == []


def test_non_interventions_lose_key_points_without_mutating_the_saved_report() -> None:
    item = make_intervention(
        0, 30, tipo="saluti", punti_chiave=["Dati editoriali residui"],
    )
    report = make_report([item])

    result = intermediate_report.normalize_interventions(report)[0]

    assert result.punti_chiave == []
    assert report.interventions[0].punti_chiave == ["Dati editoriali residui"]


def test_provider_initial_summary_is_rewritten_to_content_first_form() -> None:
    item = make_intervention(
        0, 30, sintesi="F approfondisce il realizzo controllato.",
    )

    result = intermediate_report.normalize_interventions(make_report([item]))[0]

    assert result.sintesi == "Tema trattato: il realizzo controllato."


@pytest.mark.parametrize("label", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
@pytest.mark.parametrize("delimiter", [": ", ". ", " - "])
def test_explicit_single_letter_provider_labels_are_rewritten_without_delimiters(
    label: str, delimiter: str,
) -> None:
    item = make_intervention(
        0, 30, sintesi=f"{label}{delimiter}approfondisce il realizzo controllato.",
    )

    result = intermediate_report.normalize_interventions(make_report([item]))[0]

    assert result.sintesi == "Tema trattato: il realizzo controllato."


@pytest.mark.parametrize("summary", [
    "A questo punto affronta il realizzo controllato.",
    "E approfondisce il realizzo controllato.",
    "A seguito della premessa tratta il realizzo controllato.",
    "E quindi approfondisce il realizzo controllato.",
])
def test_legitimate_italian_a_and_e_openings_are_not_provider_labels(summary: str) -> None:
    item = make_intervention(0, 30, sintesi=summary)

    result = intermediate_report.normalize_interventions(make_report([item]))[0]

    assert result.sintesi == summary


@pytest.mark.parametrize("seconds", [20, 119])
def test_twenty_to_one_hundred_nineteen_seconds_stays_intervention_and_warns(
    seconds: int,
) -> None:
    item = make_intervention(0, seconds, relatori=["Vincenzo Manfredi"])
    report = make_report([item])

    result = build_intermediate_report(report, TARGET_GUID)

    assert result.video[0].interventi[0].tipo == "intervento"
    assert "INTERVENTO_BREVE" in [check.codice for check in result.verifiche_richieste]


def test_one_hundred_twenty_seconds_does_not_warn_as_short_intervention() -> None:
    item = make_intervention(0, 120, relatori=["Vincenzo Manfredi"])

    result = build_intermediate_report(make_report([item]), TARGET_GUID)

    assert all(check.codice != "INTERVENTO_BREVE" for check in result.verifiche_richieste)


def test_access_selects_only_first_intervention_between_eight_and_fifteen_minutes() -> None:
    report = make_report([
        make_intervention(0, 360, relatori=["Vincenzo Manfredi"]),
        make_intervention(360, 900, relatori=["Vincenzo Manfredi"]),
        make_intervention(900, 2100, relatori=["Vincenzo Manfredi"]),
    ])

    result = build_intermediate_report(report, TARGET_GUID).video[0].interventi

    assert [item.accesso for item in result] == ["iscritti", "pubblico", "iscritti"]


def test_access_includes_exactly_fifteen_minutes() -> None:
    report = make_report([
        make_intervention(0, 900, relatori=["Vincenzo Manfredi"]),
        make_intervention(900, 2100, relatori=["Vincenzo Manfredi"]),
        make_intervention(2100, 3000, relatori=["Vincenzo Manfredi"]),
    ])

    result = build_intermediate_report(report, TARGET_GUID).video[0].interventi

    assert [item.accesso for item in result] == ["pubblico", "iscritti", "iscritti"]


def test_access_has_no_fallback_when_all_interventions_are_too_short() -> None:
    report = make_report([
        make_intervention(0, 300, relatori=["Vincenzo Manfredi"]),
        make_intervention(300, 720, relatori=["Vincenzo Manfredi"]),
        make_intervention(720, 960, relatori=["Vincenzo Manfredi"]),
    ])

    result = build_intermediate_report(report, TARGET_GUID).video[0].interventi

    assert [item.accesso for item in result] == ["iscritti", "iscritti", "iscritti"]


def test_choose_public_intervention_returns_none_without_substantive_segments() -> None:
    items = intermediate_report.normalize_interventions(make_report([
        make_intervention(0, 10, tipo="saluti", punti_chiave=[]),
    ]))

    assert intermediate_report.choose_public_intervention(items) is None


def test_missing_speaker_on_spoken_segment_is_critical_verification() -> None:
    video = make_video([make_intervention(0, 30, relatori=[])])

    checks = intermediate_report.build_verifications(video, [], [])

    assert [(check.livello, check.codice, check.intervento) for check in checks] == [
        ("critico", "RELATORE_NON_IDENTIFICATO", "v1-i001"),
        ("avviso", "INTERVENTO_BREVE", "v1-i001"),
    ]


@pytest.mark.parametrize("second_start, second_end, duration", [
    (11, 20, 20),
    (9, 20, 20),
    (10, 21, 20),
])
def test_gap_overlap_or_end_beyond_duration_is_critical_timeline_verification(
    second_start: int, second_end: int, duration: int,
) -> None:
    video = make_video([
        make_intervention(0, 10, relatori=["Vincenzo Manfredi"]),
        make_intervention(second_start, second_end, relatori=["Vincenzo Manfredi"]),
    ], duration=duration)
    # Exercise inconsistent input directly, before legacy endpoint clamping.
    video.interventi[-1].end_seconds = second_end

    checks = intermediate_report.build_verifications(video, [], [])

    assert any(
        check.livello == "critico" and check.codice == "TEMPI_INCOERENTI"
        for check in checks
    )


def test_low_intervention_and_slide_confidence_each_emit_a_warning() -> None:
    video = make_video(
        [make_intervention(0, 120, relatori=["Vincenzo Manfredi"], confidenza=.79)],
        slide_confidences=[.69],
    )

    checks = intermediate_report.build_verifications(video, [], [])

    confidence_checks = [check for check in checks if check.codice == "CONFIDENZA_BASSA"]
    assert [(check.intervento, check.campo) for check in confidence_checks] == [
        ("v1-i001", "confidenza"), (None, "slide[0].confidenza"),
    ]


def test_exact_confidence_thresholds_do_not_emit_warnings() -> None:
    video = make_video(
        [make_intervention(0, 120, relatori=["Vincenzo Manfredi"], confidenza=.8)],
        slide_confidences=[.7],
    )

    checks = intermediate_report.build_verifications(video, [], [])

    assert all(check.codice != "CONFIDENZA_BASSA" for check in checks)


def test_repeated_material_detection_is_deduplicated_by_code_and_context() -> None:
    video = make_video([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])

    checks = intermediate_report.build_verifications(
        video, [], ["dispensa.pdf", "dispensa.pdf"],
    )

    material_checks = [check for check in checks if check.codice == "MATERIALE_NON_RAGGIUNGIBILE"]
    assert len(material_checks) == 1
    assert material_checks[0].campo == "materiali:dispensa.pdf"


@pytest.mark.parametrize("roles", [
    (None, None),
    ("Avvocata", "Commercialista"),
])
def test_unregistered_speakers_keep_distinct_warnings_and_deduplicate_exact_repeats(
    roles: tuple[str | None, str | None],
) -> None:
    video = make_video([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])
    furio = IntermediateSpeakerV11(
        nome="Furio d'Andrea", ruolo=roles[0], confidenza=.65, origine_nome=["audio"],
    )
    marta = IntermediateSpeakerV11(
        nome="Marta Verdi", ruolo=roles[1], confidenza=.65, origine_nome=["audio"],
    )

    checks = intermediate_report.build_verifications(video, [furio, marta, furio], [])

    registry_checks = [check for check in checks if check.codice == "RELATORE_NON_NEL_REGISTRO"]
    assert len(registry_checks) == 2
    assert {check.campo for check in registry_checks} == {
        "relatori:furio d andrea:ruolo" if roles[0] is None else "relatori:furio d andrea:nome",
        "relatori:marta verdi:ruolo" if roles[1] is None else "relatori:marta verdi:nome",
    }


def test_critical_checks_set_report_status_after_verifications_are_deduplicated() -> None:
    report = make_report([make_intervention(0, 30, relatori=[])])

    result = build_intermediate_report(report, TARGET_GUID)

    assert result.stato == "da_verificare"


def test_warnings_without_critical_checks_leave_report_verified() -> None:
    report = make_report([
        make_intervention(0, 30, relatori=["Vincenzo Manfredi"], confidenza=.79),
    ])

    result = build_intermediate_report(report, TARGET_GUID)

    assert result.stato == "verificato"


def test_parse_material_source_extracts_only_a_declared_url_and_title() -> None:
    material = parse_material_source(
        "Slide governance | https://example.test/governance.pdf"
    )

    assert material is not None
    assert material.model_dump(exclude_none=True) == {
        "titolo": "Slide governance",
        "url": "https://example.test/governance.pdf",
        "accesso": "iscritti",
    }


def test_file_material_is_retained_and_warned_without_network_access(tmp_path) -> None:
    report = make_report([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])
    source = tmp_path / "dispensa.pdf"
    source.write_bytes(b"%PDF")

    result = build_intermediate_report(report, TARGET_GUID, material_sources=[str(source)])

    assert result.video[0].materiali[0].model_dump(exclude_none=True) == {
        "titolo": "dispensa",
        "file": str(source),
        "accesso": "iscritti",
    }
    assert any(
        check.codice == "MATERIALE_NON_RAGGIUNGIBILE"
        for check in result.verifiche_richieste
    )


def test_unverified_legacy_material_warns_without_any_network_request(monkeypatch) -> None:
    report = make_report([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])
    attempts = []
    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Legacy metadata conversion must remain offline")
    monkeypatch.setattr(socket.socket, "connect", forbidden)

    result = build_intermediate_report(
        report,
        TARGET_GUID,
        material_sources=["Slide | https://93.184.216.34/unavailable.pdf"],
    )

    assert result.stato == "verificato"
    assert attempts == []
    assert any(
        check.codice == "MATERIALE_NON_RAGGIUNGIBILE"
        for check in result.verifiche_richieste
    )


def test_empty_material_sources_leave_materials_and_slide_links_empty() -> None:
    report = make_report([make_intervention(0, 120, relatori=["Vincenzo Manfredi"])])

    result = build_intermediate_report(report, TARGET_GUID)
    data = result.model_dump(mode="json", exclude_none=True)

    assert data["video"][0]["materiali"] == []
    assert all("materiale" not in slide and "pagina" not in slide for slide in data["video"][0]["slide"])


def test_parse_material_source_rejects_trailing_ambiguous_prose() -> None:
    material = parse_material_source(
        " \n\t https://example.test/materiali/dispensa.pdf).,| \n"
    )

    assert material is None


def test_parse_material_source_rejects_mixed_title_url_separators() -> None:
    material = parse_material_source(
        "Slide | - \t https://example.test/materiali/dispensa.pdf"
    )

    assert material is None
