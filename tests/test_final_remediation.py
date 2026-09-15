"""Whole-path regressions for the final granular-report review, offline only."""

from copy import deepcopy
from itertools import permutations
from pathlib import Path
import random
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.analysis import OpenAIAnalyzer
from app.analysis_chunks import WindowAnalysis
from app.boundaries import align_intervention_boundaries
from app.bunny import BunnyVideoMetadata
from app.chapters import plan_semantic_timeline
from app.config import Settings
from app.intermediate_models import IntermediateVideoV11
from app.intermediate_report import build_intermediate_report
from app.jobs import JobState, JobStore
from app.material_registry import AnalysisInventoryContext
from app.materials import MaterialAnalysis, MaterialProcessor, _download_https
from app.models import AcademyReport, Evidence, ReportMaterial, SlideChange, SpeakerProfile
from app.pipeline import PipelineError, _verified_material_result
from app.transcription import TranscriptionResult
from app.web import router
from granular_support import governance_report
from test_analysis import FakeClient
from test_chapters import topic_unit
from test_materials import Response, add_test_presentation, dns, network, write_test_pptx
from test_pipeline import SOURCE, components


VIDEO_ID = UUID(int=1)
HOST = "https://www.assoholding.it"
WARNING = "MATERIALE_NON_RAGGIUNGIBILE"


def _saved_exports(report, tmp_path, monkeypatch):
    """Exercise real SQLite restore and routes with source I/O forbidden."""
    database = tmp_path / "exports.sqlite3"
    store = JobStore(database)
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    store._connection.close()
    store = JobStore(database)
    app = FastAPI()
    app.state.store = store
    app.state.settings = Settings(_env_file=None, bunny_library_id=123,
        bunny_stream_api_key="test-only", bunny_cdn_hostname="cdn.example.invalid",
        openai_api_key="test-only", app_password="test-only")
    app.include_router(router)
    try:
        restored = store.get(job.id).report
        assert restored == report
        before = store.get(job.id).model_dump_json()

        def forbidden(*args, **kwargs):
            raise AssertionError("Persisted export must not access source I/O")

        with TestClient(app) as client, monkeypatch.context() as offline:
            offline.setattr("socket.getaddrinfo", forbidden)
            offline.setattr("socket.socket.connect", forbidden)
            for method in ("stat", "is_file", "resolve"):
                offline.setattr(Path, method, forbidden)
            responses = {extension: client.get(f"/jobs/{job.id}/report.{extension}")
                         for extension in ("json", "md", "txt")}
            assert {extension: response.status_code for extension, response in responses.items()} == {
                "json": 200, "md": 200, "txt": 200,
            }
            assert client.get(f"/jobs/{job.id}/report.json").content == responses["json"].content
        assert store.get(job.id).model_dump_json() == before
        return responses
    finally:
        store._connection.close()


def _timeline_report(layout):
    units = [topic_unit(start, end, kind=kind) for kind, start, end in layout]
    plan = plan_semantic_timeline(units, [])
    aligned = align_intervention_boundaries(layout[-1][2], plan.groups, [])
    report = governance_report()
    report.duration_seconds = layout[-1][2]
    report.interventions = aligned.interventions
    report.speech_blocks = aligned.blocks
    report.boundaries = aligned.boundaries
    report.slides, report.materials, report.material_failures = [], [], []
    return report


def _material_pipeline(components, sources, *, corrupt_target=None):
    content = _timeline_report([("intervento", 0, 600)])
    for field in ("duration_seconds", "interventions", "speech_blocks", "boundaries",
                  "analysis_profile", "audio_boundary_version", "speakers"):
        setattr(components.content, field, getattr(content, field))
    components.metadata.duration_seconds = 600
    components.content.slides = [SlideChange(timestamp_seconds=0,
        title="Decisioni assembleari", confidence="alta", material_title="unverified", page=99)]
    components.pipeline.context_provider = lambda _: AnalysisInventoryContext((), tuple(sources))
    requests = []

    def fetch(url, workspace, allowed_hosts, cancellation_event):
        source = write_test_pptx(workspace / "fixture.pptx", [[
            "Decisioni assembleari" if "second" not in url else "Controlli contabili",
        ]])
        if corrupt_target is not None:
            add_test_presentation(source, ["r1"], [("r1", corrupt_target)])
        # Only DNS and HTTP transport are replaced. The HTTPS request builder,
        # response streaming, actual parser subprocess and matcher stay real.
        output = workspace / "downloaded.pptx"
        _download_https(url, output, allowed_hosts, resolver=dns,
            connection_factory=network(iter([Response(chunks=[source.read_bytes()])]), requests))
        return output

    components.pipeline.material_processor = MaterialProcessor(fetcher=fetch)
    return requests


def _document_speaker(components, name):
    components.content.speakers = [SpeakerProfile(
        id="documented-speaker", display_name=name, role="Relatore", confidence="alta",
        evidence=[Evidence(kind="introduzione", timestamp_seconds=0,
                           note=f"Il relatore si presenta come {name}.")],
    )]
    for intervention in components.content.interventions:
        intervention.relatori = [name]
    for block in components.content.speech_blocks:
        block.relatori = [name]


@pytest.mark.parametrize("titles", [("Deck", "Deck"),
    ("Slide Furio D’Andrea", "Slide · Furio D'Andrea"),
    ("Slide · Dottor Morra", "Slide · Luigi Morra")])
@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_pipeline_omits_every_ambiguous_material_before_persistence(
        components, tmp_path, monkeypatch, titles, order):
    sources = [f"{titles[0]} | {HOST}/first.pptx", f"{titles[1]} | {HOST}/second.pptx"]
    requests = _material_pipeline(components, [sources[index] for index in order])
    progress = []
    report = components.pipeline.run(SOURCE, lambda percent, _: progress.append(percent), components.event)
    assert progress[-1] == 100 and progress == sorted(progress)
    assert len(requests) == 4  # both real requests and parses completed
    assert report.materials == []
    assert report.slides[0].material_title is None and report.slides[0].page is None
    assert report.material_failures and set(report.material_failures) == {WARNING}
    assert list(tmp_path.iterdir()) == []
    responses = _saved_exports(report, tmp_path, monkeypatch)
    assert responses["json"].json()["video"][0]["materiali"] == []
    for response in responses.values():
        assert WARNING in response.text
        assert "/first.pptx" not in response.text and "/second.pptx" not in response.text


@pytest.mark.parametrize("suffix,accepted", [
    ("/deck.pptx?", False), ("/folder/../deck.pptx", False),
    ("//deck.pptx", False), ("/deck.pptx", True),
])
def test_fetched_source_must_be_persistable_without_rewriting(
        components, tmp_path, monkeypatch, suffix, accepted):
    url = HOST + suffix
    requests = _material_pipeline(components, [f"Deck | {url}"])
    progress = []
    report = components.pipeline.run(SOURCE, lambda percent, _: progress.append(percent), components.event)
    assert progress[-1] == 100 and len(requests) == 2
    assert list(tmp_path.iterdir()) == []
    if accepted:
        assert [(item.titolo, item.url, item.pagine) for item in report.materials] == [("Deck", url, 1)]
        assert (report.slides[0].material_title, report.slides[0].page) == ("Deck", 1)
        assert not report.material_failures
    else:
        assert report.materials == []
        assert report.material_failures == [WARNING]
        assert report.slides[0].material_title is None and report.slides[0].page is None
    responses = _saved_exports(report, tmp_path, monkeypatch)
    payload = responses["json"].json()["video"][0]
    assert bool(payload["materiali"]) is accepted
    for response in responses.values():
        assert (url in response.text) is accepted


@pytest.mark.parametrize("target", ["https://[malformed", "https://external.example/deck"])
def test_corrupt_pptx_relationship_is_a_nonblocking_pipeline_failure(
        components, tmp_path, monkeypatch, target):
    _material_pipeline(components, [f"Deck | {HOST}/deck.pptx"], corrupt_target=target)
    progress = []
    report = components.pipeline.run(SOURCE, lambda percent, _: progress.append(percent), components.event)
    assert progress[-1] == 100
    assert report.materials == [] and report.material_failures == [WARNING]
    assert report.slides[0].material_title is None and report.slides[0].page is None
    assert list(tmp_path.iterdir()) == []
    for response in _saved_exports(report, tmp_path, monkeypatch).values():
        assert WARNING in response.text and target not in response.text


def test_pptx_internal_value_error_remains_fatal(components, tmp_path, monkeypatch):
    import app.materials as materials

    _material_pipeline(components, [f"Deck | {HOST}/deck.pptx"])

    def broken_order(*args, **kwargs):
        raise ValueError("PRIVATE-INTERNAL-ERROR")

    monkeypatch.setattr(materials, "_pptx_display_order", broken_order)
    # Run the real parser in this process to inject an internal programming bug.
    components.pipeline.material_processor.extractor = lambda path, **_: materials._pptx_pages(path)
    progress = []
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda percent, _: progress.append(percent), components.event)
    assert caught.value.code == "temporary_failure" and "PRIVATE" not in str(caught.value)
    assert 100 not in progress and list(tmp_path.iterdir()) == []


EDGE_LAYOUTS = [
    [("logistica", 0, 30), ("intervento", 30, 630)],
    [("intervento", 0, 600), ("logistica", 600, 630)],
    [("logistica", 0, 30), ("intervento", 30, 630), ("logistica", 630, 660)],
    [("logistica", 0, 30), ("intervento", 30, 630), ("logistica", 630, 660),
     ("intervento", 660, 1260), ("logistica", 1260, 1290)],
    [("intervento", 0, 600), ("logistica", 600, 630), ("intervento", 630, 1230)],
    [("saluti", 0, 30), ("intervento", 30, 630)],
]


@pytest.mark.parametrize("layout", EDGE_LAYOUTS)
def test_planned_edge_logistics_survive_persistence_and_all_exports(layout, tmp_path, monkeypatch):
    report = _timeline_report(layout)
    responses = _saved_exports(report, tmp_path, monkeypatch)
    payload = responses["json"].json()
    video = payload["video"][0]
    assert [item["tipo"] for item in video["interventi"]] == [item[0] for item in layout]
    assert [item["tipo"] for item in video["blocchi_parlato"]] == [
        kind for kind, _, _ in layout if kind != "logistica"
    ]
    for item in video["interventi"]:
        if item["tipo"] == "logistica":
            assert not {"blocco", "capitolo_numero", "capitoli_blocco", "confine_inizio"} & item.keys()
            assert item["accesso"] == "iscritti"
    assert [item["tipo"] for item in video["interventi"] if item["accesso"] == "pubblico"] == ["intervento"]
    boundaries = [check for check in payload["verifiche_richieste"] if check["codice"] == "CONFINE"]
    assert len(boundaries) == len(layout) - 1
    assert [check["intervento"] for check in boundaries] == [
        item["id"] for item in video["interventi"][1:]
    ]
    assert all(len(item.words_before) <= 5 and len(item.words_after) <= 5 for item in report.boundaries)
    assert [item.boundary_seconds for item in report.boundaries] == [end for _, _, end in layout[:-1]]


@pytest.mark.parametrize("layout", EDGE_LAYOUTS[:4])
def test_explicit_edge_pauses_share_the_same_gap_contract(layout):
    report = _timeline_report(layout)
    for item in report.interventions:
        if item.tipo == "logistica":
            item.tipo, item.relatori = "pausa", []
    kinds = {item.id: item.tipo for item in report.interventions}
    for boundary in report.boundaries:
        if kinds[boundary.previous_intervention_id] == "pausa":
            boundary.words_before, boundary.pause_before = [], True
        if kinds[boundary.next_intervention_id] == "pausa":
            boundary.words_after, boundary.pause_after = [], True
    result = build_intermediate_report(report, VIDEO_ID)
    assert sum(item.tipo == "pausa" for item in result.video[0].interventi) == sum(
        kind == "logistica" for kind, _, _ in layout)


def _edge_video_data():
    # Start with a valid exported interior bridge, then add equivalent edge gaps.
    report = _timeline_report(EDGE_LAYOUTS[4])
    video = build_intermediate_report(report, VIDEO_ID).video[0]
    data = video.model_dump()
    for item, original in zip([*data["interventi"], *data["blocchi_parlato"]],
                              [*video.interventi, *video.blocchi_parlato], strict=True):
        item["start_seconds"] = original.start_seconds + 30
        item["end_seconds"] = original.end_seconds + 30
    bridge = data["interventi"][1]
    leading = dict(bridge, id="v1-i004", start_seconds=0, end_seconds=30)
    trailing = dict(bridge, id="v1-i005", start_seconds=1260, end_seconds=1290)
    data["interventi"] = [leading, *data["interventi"], trailing]
    data["durata_secondi"] = 1290
    return data


@pytest.mark.parametrize("mutation", ["leading_hole", "leading_overlap", "trailing_hole",
    "trailing_overlap", "duplicate", "inside_block", "orphan", "hidden_leading_hole",
    "hidden_trailing_hole"])
def test_edge_bridge_validation_rejects_holes_overlaps_and_orphans(mutation):
    data = _edge_video_data()
    if mutation == "leading_hole":
        data["interventi"][0]["end_seconds"] = 29
    elif mutation == "leading_overlap":
        data["interventi"][0]["end_seconds"] = 31
    elif mutation == "trailing_hole":
        data["interventi"][-1]["start_seconds"] = 1261
    elif mutation == "trailing_overlap":
        data["interventi"][-1]["start_seconds"] = 1259
    elif mutation == "duplicate":
        data["interventi"].append(dict(data["interventi"][0], id="v1-i006"))
    elif mutation in {"inside_block", "orphan"}:
        data["interventi"].append(dict(data["interventi"][0], id="v1-i006",
            start_seconds=60 if mutation == "inside_block" else 1300,
            end_seconds=70 if mutation == "inside_block" else 1310))
    elif mutation == "hidden_leading_hole":
        data["interventi"][0]["start_seconds"] = 1
    elif mutation == "hidden_trailing_hole":
        data["interventi"][-1]["end_seconds"] = 1289
    with pytest.raises(ValidationError):
        IntermediateVideoV11.model_validate(data)


def test_seeded_bridge_order_never_changes_coverage_or_consumes_a_bridge_twice():
    data = _edge_video_data()
    rng = random.Random(20260915)
    for _ in range(30):
        shuffled = deepcopy(data)
        rng.shuffle(shuffled["interventi"])
        rng.shuffle(shuffled["blocchi_parlato"])
        result = IntermediateVideoV11.model_validate(shuffled)
        assert sorted((item.start_seconds, item.end_seconds) for item in result.interventi) == [
            (0, 30), (30, 630), (630, 660), (660, 1260), (1260, 1290),
        ]


def _analyze_name(name, hints):
    source = topic_unit(0, 600, speaker=name).segments[0]
    evidence = [{"kind": "introduzione", "timestamp_seconds": 0,
                 "note": f"Il relatore si presenta come {name}."}]

    def respond(options):
        import json
        if options["text_format"] is WindowAnalysis:
            payload = json.loads(options["input"])
            return type("Response", (), {"output_parsed": {
                "detected_language": "it", "synopsis_notes": ["Governance"], "uncertainties": [],
                "speakers": [{"diarization_labels": ["A"], "display_name": name,
                    "role": "Relatore", "confidence": "alta", "evidence": evidence}],
                "interventions": [{"segment_indexes": list(range(len(payload["segments"]))),
                    "tipo": "intervento", "diarization_labels": ["A"], "titolo": "Governance",
                    "sintesi": "Poteri societari e controlli.", "punti_chiave": ["Poteri", "Soci", "Controlli"],
                    "confidenza": .95}],
            }})()
        return type("Response", (), {"output_parsed": {
            "title": "Governance", "duration_seconds": 600, "detected_language": "it",
            "synopsis": "Poteri e controlli.", "uncertainties": [], "speakers": [{
                "id": "speaker-a", "display_name": name, "role": "Relatore",
                "confidence": "alta", "evidence": evidence,
            }],
        }})()

    return OpenAIAnalyzer(FakeClient(respond, respond)).analyze(
        BunnyVideoMetadata(video_id=VIDEO_ID, title="Governance", duration_seconds=600),
        TranscriptionResult(text=source.text, segments=[source], audio_seconds=600), [],
        silence_intervals=[], speaker_name_hints=hints,
    )


@pytest.mark.parametrize("name,hints,expected,slug", [
    ("Fabio D'Andrea", ["Furio D'Andrea"], "Fabio D'Andrea", None),
    ("Antonia Sibilia", ["Antonio Sibilia"], "Antonia Sibilia", None),
    ("Luis Morra", ["Luigi Morra"], "Luis Morra", None),
    ("Fulvio D'Andrea", ["Furio d'Andrea"], "Furio D'Andrea", "furio-dandrea"),
    ("  FÚRIO D ’ ANDREA  ", [], "Furio D'Andrea", "furio-dandrea"),
    ("élena   ròssi", ["Elena Rossi"], "Elena Rossi", None),
    ("D. Andrea", ["D'Andrea"], "D. Andrea", None),
])
def test_analyzer_preserves_evidenced_identity_through_sqlite_and_exports(
        name, hints, expected, slug, tmp_path, monkeypatch):
    result = _analyze_name(name, hints)
    assert [speaker.display_name for speaker in result.speakers] == [expected]
    assert result.interventions[0].relatori == result.speech_blocks[0].relatori == [expected]
    report = AcademyReport(**result.model_dump(exclude={"usage"}), cost=governance_report().cost)
    responses = _saved_exports(report, tmp_path, monkeypatch)
    people = responses["json"].json()["relatori"]
    assert [(person["nome"], person.get("slug")) for person in people] == [(expected, slug)]
    for response in responses.values():
        assert expected in response.text


def test_seeded_near_miss_speakers_never_merge_with_registry_hints():
    cases = [("Fabio D'Andrea", "Furio D'Andrea"), ("Antonia Sibilia", "Antonio Sibilia"),
             ("Luis Morra", "Luigi Morra"), ("Fabia D'Andrea", "Furio D'Andrea"),
             ("Antonino Sibilia", "Antonio Sibilia"), ("Luisa Morra", "Luigi Morra")]
    rng = random.Random(15092026)
    for _ in range(4):
        rng.shuffle(cases)
        for name, canonical in cases:
            hints = [canonical, "Elena Rossi", "Mario Bianchi"]
            rng.shuffle(hints)
            result = _analyze_name(name, hints)
            assert result.speakers[0].display_name == name
            assert result.interventions[0].relatori == [name]


def test_material_collision_gate_considers_all_original_results_in_every_order(tmp_path):
    materials = [ReportMaterial(titolo=title, url=HOST + path, pagine=1) for title, path in [
        ("Deck", "/first.pptx"), ("Other", "/second.pptx"), ("Deck", "/second.pptx"),
    ]]
    context = AnalysisInventoryContext((), tuple(f"{item.titolo} | {item.url}" for item in materials))
    for order in permutations(materials):
        verified = _verified_material_result(context, MaterialAnalysis(order, (), ()), (),
            tmp_path, ("www.assoholding.it",))
        assert all(item.titolo != "Deck" for item in verified.materials)
        assert WARNING in verified.failures


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_pipeline_omits_titles_that_collide_only_after_report_reconciliation(
        components, tmp_path, monkeypatch, order):
    sources = [
        f"Slide · Dottor Rossi | {HOST}/first.pptx",
        f"Slide · Elena Rossi | {HOST}/second.pptx",
    ]
    _material_pipeline(components, [sources[index] for index in order])
    _document_speaker(components, "Elena Rossi")

    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert report.materials == []
    assert report.material_failures and set(report.material_failures) == {WARNING}
    assert report.slides[0].material_title is None and report.slides[0].page is None
    responses = _saved_exports(report, tmp_path, monkeypatch)
    assert responses["json"].json()["video"][0]["materiali"] == []


@pytest.mark.parametrize("order", [(0, 1), (1, 0)])
def test_pipeline_omits_normalized_source_with_conflicting_metadata(
        components, tmp_path, monkeypatch, order):
    sources = [
        f"First deck | {HOST}/deck.pptx",
        f"Other deck | {HOST}/%64eck.pptx",
    ]
    _material_pipeline(components, [sources[index] for index in order])

    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert report.materials == []
    assert report.material_failures and set(report.material_failures) == {WARNING}
    assert report.slides[0].material_title is None and report.slides[0].page is None
    responses = _saved_exports(report, tmp_path, monkeypatch)
    assert responses["json"].json()["video"][0]["materiali"] == []


def test_surname_only_material_stays_ambiguous_when_report_documents_another_person(
        components, tmp_path, monkeypatch):
    _material_pipeline(components, [f"Slide · Dottor Morra | {HOST}/deck.pptx"])
    _document_speaker(components, "Luis Morra")

    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert len(report.materials) == 1
    assert report.materials[0].titolo == "Slide · Dottor Morra"
    assert report.materials[0].relatore != "Luigi Morra"
    payload = _saved_exports(report, tmp_path, monkeypatch)["json"].json()
    assert all(person["nome"] != "Luigi Morra" for person in payload["relatori"])
    assert all(person.get("slug") != "luigi-morra" for person in payload["relatori"])
    assert payload["video"][0]["materiali"][0]["titolo"] != "Slide · Luigi Morra"
    assert any(check["codice"] == "ALIAS_RELATORE_AMBIGUO"
               for check in payload["verifiche_richieste"])
