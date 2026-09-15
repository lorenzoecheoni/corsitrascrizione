import json
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.analysis import AnalysisError, OpenAIAnalyzer
from app.bunny import BunnyAuthError, BunnyNotFoundError, BunnyVideoMetadata
from app.config import Settings
from app.media import (
    AudioChunk, FrameCandidate, MediaArtifacts, MediaError, MediaProtectedError,
    SilenceInterval,
)
from app.models import AcademyContent, BoundaryEvidence, Intervention
from app.material_registry import AnalysisInventoryContext, MaterialSourceFailure
from app.materials import DeckPage, MaterialAnalysis, MaterialError, MaterialProcessor
from app.models import ReportMaterial, SlideChange
from app.pipeline import AnalysisPipeline, PipelineCancelled, PipelineError
from app.transcription import (
    TranscriptSegment, TranscriptWord, TranscriptionError, TranscriptionResult,
)


SOURCE = "https://iframe.mediadelivery.net/embed/123/00000000-0000-0000-0000-000000000001?token=private"


@pytest.fixture
def components(tmp_path):
    settings = Settings(bunny_library_id=123, bunny_stream_api_key="secret",
                        bunny_cdn_hostname="cdn.example.com", openai_api_key="secret",
                        app_password="team-secret", _env_file=None)
    data = json.loads(Path("tests/fixtures/report.json").read_text())
    data.pop("cost")
    content = AcademyContent.model_validate(data)
    content.audio_boundary_version = 1
    metadata = BunnyVideoMetadata(video_id=UUID(int=1), title="Corso di prova", duration_seconds=3600,
                                  status=3, available_resolutions=[240, 720])
    content.duration_seconds = 3600
    content.interventions = [Intervention(
        id="i001", start_seconds=0, end_seconds=3600, tipo="intervento",
        relatori=[], titolo="Corso di prova", sintesi="Contenuto verificato.",
        punti_chiave=["Uno", "Due", "Tre"], confidenza=.9,
    )]
    state = SimpleNamespace(settings=settings, metadata=metadata, content=content, calls=[],
                            error=None, cancel_after=None, event=Event(), workspace=None)

    def stage(name):
        state.calls.append(name)
        if state.error and state.error[0] == name:
            raise state.error[1]
        if state.cancel_after == name:
            state.event.set()

    def get_metadata(video_id):
        assert video_id == str(UUID(int=1))
        stage("metadata")
        return metadata

    def hls(metadata, *, cancellation_event):
        stage("hls")
        return "https://cdn.example.com/regenerated-playlist"

    def extract(url, workspace, progress, cancellation_event):
        assert url == "https://cdn.example.com/regenerated-playlist"
        assert cancellation_event is state.event
        state.workspace = workspace
        audio, frame = workspace / "audio.m4a", workspace / "frame.jpg"
        audio.write_bytes(b"private audio")
        frame.write_bytes(b"private image")
        stage("media")
        for seconds in (0, 1800, 900, 7200):
            progress(seconds)
        return MediaArtifacts(
            [AudioChunk(audio, 0, 3600)], [FrameCandidate(frame, 0)],
            1_000_000_000, silence_measured=True,
        )

    def transcribe(chunks, *, cancellation_event):
        assert cancellation_event is state.event
        assert chunks[0].path.exists()
        stage("transcription")
        return TranscriptionResult(
            text="private raw transcript", audio_seconds=3600,
            segments=[TranscriptSegment(
                start_seconds=0, end_seconds=3599, diarization_label="chunk-0:A",
                text="private raw transcript", source_utterance_id="fixture-u1",
                words=[TranscriptWord(
                    text="private", start_seconds=1, end_seconds=2,
                    diarization_label="chunk-0:A", confidence=.9,
                )],
            )],
        )

    def analyze(meta, transcript, frames, *, silence_intervals, cancellation_event, progress_callback,
                speaker_name_hints=()):
        assert cancellation_event is state.event
        assert silence_intervals == []
        assert transcript.text == "private raw transcript"
        assert frames[0].path.exists()
        stage("analysis")
        state.speaker_name_hints = speaker_name_hints
        progress_callback("slides", 2, 5)
        progress_callback("transcript", 7, 10)
        return content

    state.pipeline = AnalysisPipeline(settings, SimpleNamespace(get_metadata=get_metadata, select_hls_url=hls),
                                      SimpleNamespace(extract=extract), SimpleNamespace(transcribe=transcribe),
                                      SimpleNamespace(analyze=analyze), temp_root=tmp_path)
    return state


MATERIAL_SOURCE = "Slide Furio D’Andrea | https://www.assoholding.it/furio.pptx"


def material_context(components, failures=()):
    def context(metadata):
        assert metadata is components.metadata
        assert components.calls == ["metadata"]
        components.calls.append("context")
        return AnalysisInventoryContext(("Furio D'Andrea",), (MATERIAL_SOURCE,), failures)
    components.pipeline.context_provider = context
    components.content.slides = [SlideChange(
        timestamp_seconds=0, title="Decisioni assembleari", confidence="alta",
    )]


def test_pipeline_materials_persist_verified_links_and_cleanup(components, tmp_path, caplog):
    caplog.set_level("INFO")
    material_context(components)
    def fetch(url, workspace, allowed_hosts, cancellation_event):
        assert components.calls[-1] == "analysis"
        assert cancellation_event is components.event
        assert workspace.is_relative_to(components.workspace)
        deck = workspace / "furio.pptx"
        deck.write_bytes(b"PRIVATE-DECK-BYTES")
        return deck
    components.pipeline.material_processor = MaterialProcessor(
        fetcher=fetch,
        extractor=lambda *args, **kwargs: [
            DeckPage(1, "Poteri e responsabilità PRIVATE-DECK-TEXT"),
            DeckPage(2, "Decisioni assembleari"),
        ],
    )
    updates = []
    report = components.pipeline.run(SOURCE, lambda p, m: updates.append(p), components.event)
    assert components.calls.count("context") == 1
    assert components.speaker_name_hints == ("Furio D'Andrea",)
    assert report.materials == [ReportMaterial(
        titolo="Slide · Furio D'Andrea", relatore="Furio D'Andrea",
        url="https://www.assoholding.it/furio.pptx", pagine=2,
    )]
    assert report.slides[0].material_title == "Slide · Furio D'Andrea"
    assert report.slides[0].page == 2
    assert report.material_failures == []
    assert list(tmp_path.iterdir()) == []
    assert updates == sorted(updates) and updates[-1] == 100
    assert any(92 <= value < 98 for value in updates)
    for private in ("PRIVATE-DECK", "Poteri e responsabilità", "SIGNED-SECRET", "PRIVATE",
                    "private raw transcript", "Traceback"):
        assert private not in report.model_dump_json() + caplog.text


@pytest.mark.parametrize("failure_stage", ["fetch", "parse"])
def test_pipeline_material_failure_nonfatal_and_safe(components, tmp_path, caplog, failure_stage):
    caplog.set_level("INFO")
    material_context(components)
    def fail():
        raise MaterialError() from RuntimeError("https://user:password@host/?secret RAW-RESPONSE")
    def fetch(url, workspace, allowed_hosts, cancellation_event):
        (workspace / "private.pptx").write_bytes(b"RAW-RESPONSE")
        if failure_stage == "fetch":
            fail()
        return workspace / "private.pptx"
    def extract(*args, **kwargs):
        fail()
    components.pipeline.material_processor = MaterialProcessor(fetcher=fetch, extractor=extract)
    updates = []
    report = components.pipeline.run(SOURCE, lambda p, m: updates.append(p), components.event)
    assert report.materials == []
    assert report.material_failures == ["MATERIALE_NON_RAGGIUNGIBILE"]
    assert report.slides[0].page is None
    assert updates[-1] == 100 and updates == sorted(updates)
    assert list(tmp_path.iterdir()) == []
    assert any(record.msg.get("phase") == "materials"
               and record.msg.get("error_code") == "MATERIALE_NON_RAGGIUNGIBILE"
               for record in caplog.records if isinstance(record.msg, dict))
    for private in ("RAW-RESPONSE", "password", "SIGNED-SECRET", "Traceback"):
        assert private not in report.model_dump_json() + caplog.text


@pytest.mark.parametrize("signal", ["raise", "event"])
def test_pipeline_cancels_inside_materials_and_cleans(components, tmp_path, signal):
    material_context(components)
    def process(sources, slides, workspace, cancellation_event):
        (workspace / "private.pptx").write_bytes(b"private")
        assert cancellation_event is components.event
        if signal == "raise":
            raise CancelledError()
        cancellation_event.set()
        return MaterialAnalysis((), tuple(slides), ())
    components.pipeline.material_processor = SimpleNamespace(process=process)
    updates = []
    with pytest.raises(PipelineCancelled):
        components.pipeline.run(SOURCE, lambda p, m: updates.append(p), components.event)
    assert 100 not in updates
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure_stage", ["processor", "fetcher", "extractor"])
def test_pipeline_unexpected_material_error_fails_safely(components, tmp_path, caplog, failure_stage):
    caplog.set_level("INFO")
    material_context(components)
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE-DECK-TEXT https://user:password@host/?SIGNED-SECRET")
    def fetch(url, workspace, allowed_hosts, cancellation_event):
        deck = workspace / "private.pptx"
        deck.write_bytes(b"PRIVATE-DECK-TEXT")
        if failure_stage == "fetcher":
            fail()
        return deck
    components.pipeline.material_processor = (
        SimpleNamespace(process=fail) if failure_stage == "processor"
        else MaterialProcessor(fetcher=fetch, extractor=fail)
    )
    updates = []
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda p, m: updates.append(p), components.event)
    assert caught.value.code == "temporary_failure"
    assert 100 not in updates
    assert list(tmp_path.iterdir()) == []
    for private in ("PRIVATE-DECK-TEXT", "password", "SIGNED-SECRET", "Traceback"):
        assert private not in str(caught.value) + caplog.text


def test_pipeline_propagates_only_source_failure_codes(components, tmp_path):
    material_context(components, (MaterialSourceFailure(
        inventory_reference="PRIVATE-INVENTORY-REFERENCE", reason="dichiarazione_ambigua",
    ),))
    components.pipeline.material_processor = SimpleNamespace(process=lambda sources, slides, workspace,
        cancellation_event: MaterialAnalysis((), tuple(slides), ()))
    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert report.material_failures == ["MATERIALE_NON_RAGGIUNGIBILE"]
    assert "PRIVATE-INVENTORY-REFERENCE" not in report.model_dump_json()
    assert list(tmp_path.iterdir()) == []


def test_pipeline_never_promotes_analyzer_material_claims(components):
    components.content.materials = [ReportMaterial(titolo="Unverified", url="https://unverified.test/deck")]
    components.content.material_failures = ["UNTRUSTED-RESPONSE"]
    components.content.slides[0].page = 999
    components.content.slides[0].material_title = "Unverified"
    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert report.materials == [] and report.material_failures == []
    assert all(slide.page is None and slide.material_title is None for slide in report.slides)


@pytest.mark.parametrize("suffix", ["?token=SIGNED-SECRET", "#PRIVATE"])
def test_pipeline_omits_sensitive_material_urls_without_inventing_public_url(components, suffix):
    material_context(components)
    components.pipeline.context_provider = lambda metadata: AnalysisInventoryContext((), (MATERIAL_SOURCE + suffix,))
    material = ReportMaterial(titolo="Slide · Furio D'Andrea",
        url="https://www.assoholding.it/furio.pptx" + suffix, pagine=2)
    def process(sources, slides, workspace, cancellation_event):
        return MaterialAnalysis((material,), tuple(slide.model_copy(update={
            "material_title": material.titolo, "page": 2}) for slide in slides), ())
    components.pipeline.material_processor = SimpleNamespace(process=process)
    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert report.materials == []
    assert report.material_failures == ["MATERIALE_NON_RAGGIUNGIBILE"]
    assert report.slides[0].material_title is None and report.slides[0].page is None
    assert "SIGNED-SECRET" not in report.model_dump_json()
    assert "furio.pptx" not in report.model_dump_json()


def test_pipeline_context_constructor_and_inventory_outage(components):
    from app.inventory import InventoryError
    def unavailable(metadata):
        components.calls.append("context")
        raise InventoryError("PRIVATE-INVENTORY-RESPONSE")
    old = components.pipeline
    pipeline = AnalysisPipeline(old.settings, old.bunny, old.media, old.transcriber, old.analyzer,
        context_provider=unavailable, material_processor=MaterialProcessor(), temp_root=old.temp_root)
    report = pipeline.run(SOURCE, lambda *_: None, components.event)
    assert components.calls.count("context") == 1
    assert report.material_failures == ["MATERIALE_NON_RAGGIUNGIBILE"]
    assert "PRIVATE-INVENTORY-RESPONSE" not in report.model_dump_json()


@pytest.mark.parametrize("mode", ["fetch", "parse", "match"])
def test_internal_worker_typeerror_fails_pipeline_and_cleans(components, tmp_path, monkeypatch, caplog, mode):
    import app.materials as module
    from test_materials import write_test_pptx
    caplog.set_level("INFO")
    material_context(components)
    def worker(stage, source, output, **options):
        if stage == mode:
            raise TypeError("PRIVATE-WORKER-ERROR https://user:secret@host/?token")
        if stage == "fetch":
            write_test_pptx(output, [["Decisioni assembleari"]])
        elif stage == "parse":
            output.write_text('[{"number": 1, "text": "Decisioni assembleari"}]')
        else:
            raise AssertionError("unexpected worker phase")
    monkeypatch.setattr(module, "_run_worker", worker)
    components.pipeline.material_processor = MaterialProcessor()
    updates = []
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda p, m: updates.append(p), components.event)
    assert caught.value.code == "temporary_failure"
    assert 100 not in updates and list(tmp_path.iterdir()) == []
    assert "PRIVATE-WORKER-ERROR" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("mode", ["parse", "match"])
def test_invalid_worker_success_payload_is_internal_failure(components, tmp_path, monkeypatch, mode):
    import app.materials as module
    from test_materials import write_test_pptx
    material_context(components)
    def worker(stage, source, output, **options):
        if stage == "fetch":
            write_test_pptx(output, [["Decisioni assembleari"]])
        elif stage == "parse":
            output.write_text(json.dumps([{"number": 0 if mode == "parse" else 1,
                                           "text": "Decisioni assembleari"}]))
        else:
            output.write_text(json.dumps([["Slide · Furio D'Andrea", 999]]))
    monkeypatch.setattr(module, "_run_worker", worker)
    components.pipeline.material_processor = MaterialProcessor()
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert caught.value.code == "temporary_failure"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mutation", ["scalars", "nested"])
def test_material_processor_cannot_mutate_slide_observations(components, mutation):
    material_context(components)
    components.content.slides[0].visible_content = ["Observed content"]
    original = components.content.slides[0].model_dump()
    def process(sources, slides, workspace, cancellation_event):
        if mutation == "scalars":
            slides[0].title = "PRIVATE-DECK-TEXT"
            slides[0].timestamp_seconds = 777
            slides[0].confidence = "bassa"
        else:
            slides[0].visible_content.append("PRIVATE-DECK-TEXT")
        material = ReportMaterial(titolo="Slide · Furio D'Andrea",
            url="https://www.assoholding.it/furio.pptx", pagine=2)
        slides[0].material_title, slides[0].page = material.titolo, 2
        return MaterialAnalysis((material,), tuple(slides), ())
    components.pipeline.material_processor = SimpleNamespace(process=process)
    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert report.slides[0].model_dump(exclude={"material_title", "page"}) == {
        key: value for key, value in original.items() if key not in {"material_title", "page"}}
    assert components.content.slides[0].model_dump() == original
    assert report.slides[0].page == 2
    assert "PRIVATE-DECK-TEXT" not in report.model_dump_json()


@pytest.mark.parametrize("violation", [
    "absent_source", "outside_url", "workspace_file", "undeclared_file", "page_out_of_range",
    "bool_page", "float_page", "missing_pages", "missing_material", "unknown_title",
    "title_without_page", "page_without_title", "duplicate_title_ref", "invented_public_url",
    "declared_disallowed_host", "duplicate_sensitive_ref", "sensitive_bad_page", "bool_pages",
])
def test_processor_contract_violations_fail_safely(components, tmp_path, caplog, violation):
    caplog.set_level("INFO")
    material_context(components)
    if violation == "invented_public_url":
        components.pipeline.context_provider = lambda metadata: AnalysisInventoryContext((), (MATERIAL_SOURCE + "?token=PRIVATE",))
    if violation in {"declared_disallowed_host", "sensitive_bad_page"}:
        source = ("Slide · Furio D'Andrea | https://outside.test/PRIVATE.pptx"
                  if violation == "declared_disallowed_host" else MATERIAL_SOURCE + "?token=PRIVATE")
        components.pipeline.context_provider = lambda metadata: AnalysisInventoryContext((), (source,))
    if violation == "duplicate_title_ref":
        components.pipeline.context_provider = lambda metadata: AnalysisInventoryContext((), (
            MATERIAL_SOURCE, "Slide · Furio D'Andrea | https://www.assoholding.it/second.pptx"))
    if violation == "duplicate_sensitive_ref":
        components.pipeline.context_provider = lambda metadata: AnalysisInventoryContext((), (
            MATERIAL_SOURCE, MATERIAL_SOURCE + "?token=PRIVATE"))
    def process(sources, slides, workspace, cancellation_event):
        deck = workspace / "PRIVATE-DECK.pptx"
        deck.write_bytes(b"PRIVATE-DECK-TEXT")
        material = ReportMaterial(titolo="Slide · Furio D'Andrea",
            url="https://www.assoholding.it/furio.pptx", pagine=2)
        updates = {
            "absent_source": {"url": None},
            "outside_url": {"url": "https://outside.test/PRIVATE.pptx"},
            "workspace_file": {"url": None, "file": str(deck)},
            "undeclared_file": {"url": None, "file": "/PRIVATE/undeclared.pptx"},
            "missing_pages": {"pagine": None},
            "bool_pages": {"pagine": True},
            "declared_disallowed_host": {"url": "https://outside.test/PRIVATE.pptx"},
            "sensitive_bad_page": {"url": "https://www.assoholding.it/furio.pptx?token=PRIVATE"},
        }
        material = material.model_copy(update=updates.get(violation, {}))
        materials = (material,)
        slide = slides[0].model_copy(update={"material_title": material.titolo, "page": 2})
        if violation in {"page_out_of_range", "sensitive_bad_page"}:
            slide.page = 999
        elif violation == "bool_page":
            slide.page = True
        elif violation == "bool_pages":
            slide.page = 1
        elif violation == "float_page":
            slide.page = 1.5
        elif violation == "missing_material":
            materials = ()
        elif violation == "unknown_title":
            slide.material_title = "PRIVATE-UNKNOWN"
        elif violation == "title_without_page":
            slide.page = None
        elif violation == "page_without_title":
            slide.material_title = None
        elif violation == "duplicate_title_ref":
            materials += (material.model_copy(update={"url": "https://www.assoholding.it/second.pptx"}),)
        elif violation == "duplicate_sensitive_ref":
            materials += (material.model_copy(update={"url": material.url + "?token=PRIVATE"}),)
        return MaterialAnalysis(materials, (slide,), ())
    components.pipeline.material_processor = SimpleNamespace(process=process)
    updates = []
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda p, m: updates.append(p), components.event)
    assert caught.value.code == "temporary_failure"
    assert list(tmp_path.iterdir()) == [] and 100 not in updates
    assert "PRIVATE" not in str(caught.value) + caplog.text


def test_processor_contract_rejects_even_declared_workspace_files(tmp_path):
    from app.pipeline import _verified_material_result
    from app.materials import MaterialInternalError
    source = tmp_path / "private.pptx"
    source.write_bytes(b"private deck")
    material = ReportMaterial(titolo="private", file=str(source), pagine=1)
    context = AnalysisInventoryContext((), (str(source),))
    with pytest.raises(MaterialInternalError, match="^MATERIAL_INTERNAL_ERROR$"):
        _verified_material_result(context, MaterialAnalysis((material,), (), ()), (), tmp_path,
                                  ("www.assoholding.it",))


def test_pipeline_accepts_exact_declared_external_file_and_preserves_source(components, tmp_path):
    from test_materials import write_test_pptx
    source = write_test_pptx(tmp_path / "declared.pptx", [["Decisioni assembleari"]])
    material_context(components)
    components.pipeline.context_provider = lambda metadata: AnalysisInventoryContext((), (str(source),))
    components.pipeline.material_processor = MaterialProcessor()
    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert report.materials == [ReportMaterial(titolo="declared", file=str(source), pagine=1)]
    assert report.slides[0].material_title == "declared" and report.slides[0].page == 1
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("error", [CancelledError(), TypeError("PRIVATE-CONTEXT")])
def test_pipeline_context_cancellation_and_programming_error_are_not_swallowed(components, tmp_path, error):
    def context(metadata):
        components.calls.append("context")
        raise error
    components.pipeline.context_provider = context
    expected = PipelineCancelled if isinstance(error, CancelledError) else PipelineError
    with pytest.raises(expected) as caught:
        components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert components.calls == ["metadata", "context"]
    assert list(tmp_path.iterdir()) == []
    assert "PRIVATE-CONTEXT" not in str(caught.value)


def test_concurrent_material_jobs_keep_context_progress_and_cancellation_isolated(components, tmp_path):
    pipeline = components.pipeline
    pipeline.bunny.get_metadata = lambda video_id: components.metadata.model_copy(update={"video_id": UUID(video_id)})
    pipeline.bunny.select_hls_url = lambda *args, **kwargs: "https://cdn.example.com/video"
    def extract(url, workspace, progress, cancellation_event):
        frame = workspace / "frame.jpg"
        frame.write_bytes(b"private")
        progress(3600)
        return MediaArtifacts([], [FrameCandidate(frame, 0)], 100, silence_measured=True)
    pipeline.media.extract = extract
    pipeline.transcriber.transcribe = lambda *args, **kwargs: TranscriptionResult(
        text="private", audio_seconds=3600, segments=[])
    pipeline.analyzer.analyze = lambda *args, **kwargs: components.content.model_copy(deep=True)
    pipeline.context_provider = lambda metadata: AnalysisInventoryContext((), (
        f"{metadata.video_id} | https://www.assoholding.it/{metadata.video_id}.pptx",))
    barrier = Barrier(2)
    first, second = Event(), Event()
    seen_workspaces = []
    def process(sources, slides, workspace, cancellation_event):
        seen_workspaces.append(workspace)
        (workspace / "private.pptx").write_bytes(b"private")
        barrier.wait(timeout=3)
        if cancellation_event is first:
            cancellation_event.set()
        title, url = sources[0].split(" | ")
        return MaterialAnalysis((ReportMaterial(titolo=title, pagine=1,
            url=url),), tuple(slides), ())
    pipeline.material_processor = SimpleNamespace(process=process)
    first_updates, second_updates = [], []
    with ThreadPoolExecutor(max_workers=2) as executor:
        cancelled = executor.submit(pipeline.run, SOURCE, lambda p, m: first_updates.append(p), first)
        completed = executor.submit(pipeline.run, SOURCE.replace(str(UUID(int=1)), str(UUID(int=2))),
            lambda p, m: second_updates.append(p), second)
        with pytest.raises(PipelineCancelled):
            cancelled.result(timeout=5)
        report = completed.result(timeout=5)
    assert report.materials[0].titolo == str(UUID(int=2))
    assert len(set(seen_workspaces)) == 2
    assert first_updates == sorted(first_updates) and 100 not in first_updates
    assert second_updates == sorted(second_updates) and second_updates[-1] == 100
    assert not second.is_set()
    assert list(tmp_path.iterdir()) == []


def test_pipeline_cleans_media_and_reports_monotonic_stage_progress(components, tmp_path):
    updates = []
    report = components.pipeline.run(SOURCE, lambda p, m: updates.append((p, m)), components.event)
    values = [percent for percent, _ in updates]
    messages = [message for _, message in updates]
    assert components.calls == ["metadata", "hls", "media", "transcription", "analysis"]
    assert report.title == components.content.title
    assert report.bunny_title == "Corso di prova"
    assert report.audio_boundary_version == 1
    assert report.usage.transcription.requests == 0
    assert report.cost.estimated_low_usd == .41
    assert report.cost.estimated_high_usd == .69
    assert values == sorted(values)
    assert all(p in values for p in (5, 10, 27, 45, 50, 72, 75, 98, 100))
    assert "Slide 2/5" in messages
    assert "Interventi 7/10" in messages
    assert list(tmp_path.iterdir()) == []
    assert "private raw transcript" not in report.model_dump_json()
    assert "private raw transcript" not in " ".join(messages)


def test_media_artifacts_positional_constructor_keeps_empty_silence_intervals():
    artifact = MediaArtifacts([], [], 0)

    assert artifact.silence_intervals == []
    assert artifact.silence_measured is False


def test_pipeline_rejects_unmeasured_fallback_before_paid_ai_calls(components):
    measured_extract = components.pipeline.media.extract

    def extract(*args, **kwargs):
        media = measured_extract(*args, **kwargs)
        return MediaArtifacts(
            media.audio_chunks, media.frame_candidates, media.downloaded_bytes,
            media.silence_intervals,
        )

    components.pipeline.media.extract = extract
    components.pipeline.transcriber.transcribe = lambda *_args, **_kwargs: pytest.fail(
        "La trascrizione OpenAI non deve partire senza misura dei silenzi"
    )
    components.pipeline.analyzer.analyze = lambda *_args, **_kwargs: pytest.fail(
        "L'analisi OpenAI non deve partire senza misura dei silenzi"
    )

    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert caught.value.code == "boundaries"
    assert str(caught.value) == "Verifica audio dei confini non riuscita; riprova"


def test_cancellation_after_unmeasured_media_wins_over_boundary_failure(components):
    measured_extract = components.pipeline.media.extract
    components.cancel_after = "media"

    def extract(*args, **kwargs):
        media = measured_extract(*args, **kwargs)
        return MediaArtifacts(
            media.audio_chunks, media.frame_candidates, media.downloaded_bytes,
            media.silence_intervals,
        )

    components.pipeline.media.extract = extract

    with pytest.raises(PipelineCancelled):
        components.pipeline.run(SOURCE, lambda *_: None, components.event)


def test_pipeline_rejects_wordless_fallback_before_paid_analysis(components):
    original_extract = components.pipeline.media.extract

    def extract(*args, **kwargs):
        media = original_extract(*args, **kwargs)
        return MediaArtifacts(
            media.audio_chunks, media.frame_candidates, media.downloaded_bytes,
            media.silence_intervals, silence_measured=True,
        )

    components.pipeline.media.extract = extract
    components.pipeline.transcriber.transcribe = lambda *_args, **_kwargs: TranscriptionResult(
        text="wordless fallback", segments=[], audio_seconds=3600,
    )
    paid_calls = []
    remote = SimpleNamespace(
        responses=SimpleNamespace(with_raw_response=SimpleNamespace(
            parse=lambda **kwargs: paid_calls.append(kwargs),
        )),
    )
    components.pipeline.analyzer = OpenAIAnalyzer(SimpleNamespace(
        with_options=lambda **_kwargs: remote,
    ))

    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert caught.value.code == "boundaries"
    assert str(caught.value) == "Verifica audio dei confini non riuscita; riprova"
    assert paid_calls == []


def test_pipeline_passes_optional_inventory_spelling_hints_to_analysis(components):
    received = []
    components.pipeline.speaker_hint_provider = lambda metadata: ["Furio d'Andrea"]

    def analyze(meta, transcript, frames, *, silence_intervals, cancellation_event, progress_callback,
                speaker_name_hints):
        assert silence_intervals == []
        received.extend(speaker_name_hints)
        return components.content

    components.pipeline.analyzer.analyze = analyze

    components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert received == ["Furio d'Andrea"]


def test_pipeline_uses_direct_global_transcription_and_single_fast_analysis_when_configured(components):
    transcription_started = Event()

    def mp4(metadata):
        assert metadata is components.metadata
        components.calls.append("mp4")
        return "https://cdn.example.com/video/play_240p.mp4"

    def extract_visual(url, workspace, progress, cancellation_event, *, duration_seconds=None):
        assert url == "https://cdn.example.com/video/play_240p.mp4"
        assert duration_seconds == 3600
        assert not cancellation_event.is_set()
        assert transcription_started.wait(1), "trascrizione e scansione slide devono partire in parallelo"
        frame = workspace / "frame.jpg"
        frame.write_bytes(b"private image")
        components.calls.append("visual-media")
        progress(3600)
        return MediaArtifacts(
            [], [FrameCandidate(frame, 0)], 1_000_000_000, silence_measured=True,
        )

    def transcribe_url(url, *, duration_seconds, cancellation_event):
        assert url == "https://cdn.example.com/video/play_240p.mp4"
        assert duration_seconds == 3600
        assert not cancellation_event.is_set()
        components.calls.append("assemblyai")
        transcription_started.set()
        return TranscriptionResult(
            provider="assemblyai", language="it", text="testo globale",
            audio_seconds=3600,
            segments=[TranscriptSegment(
                start_seconds=0, end_seconds=3599, diarization_label="assembly:A",
                text="testo globale", source_utterance_id="assembly-u1",
                words=[TranscriptWord(
                    text="testo", start_seconds=1, end_seconds=2,
                    diarization_label="assembly:A", confidence=.9,
                )],
            )],
        )

    def analyze_fast(meta, transcript, frames, *, silence_intervals, cancellation_event, progress_callback):
        assert transcript.provider == "assemblyai"
        assert silence_intervals == []
        assert cancellation_event is components.event
        components.calls.append("fast-analysis")
        progress_callback("transcript", 1, 1)
        progress_callback("consolidation", 0, 1)
        return components.content

    components.pipeline.bunny.build_mp4_url = mp4
    components.pipeline.bunny.select_hls_url = lambda *_args, **_kwargs: pytest.fail("HLS non previsto")
    components.pipeline.media.extract_visual = extract_visual
    components.pipeline.media.extract = lambda *_args, **_kwargs: pytest.fail("estrazione audio non prevista")
    components.pipeline.fast_transcriber = SimpleNamespace(transcribe_url=transcribe_url)
    components.pipeline.analyzer.analyze_fast = analyze_fast
    components.pipeline.transcriber.transcribe = lambda *_args, **_kwargs: pytest.fail("fallback OpenAI non previsto")
    components.pipeline.analyzer.analyze = lambda *_args, **_kwargs: pytest.fail("analisi a finestre non prevista")

    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert report.title == components.content.title
    assert components.calls[0] == "metadata"
    assert components.calls[-1] == "fast-analysis"
    assert components.calls.count("mp4") == 2
    assert components.calls.count("assemblyai") == 1
    assert components.calls.count("visual-media") == 1
    assert components.calls.index("assemblyai") < components.calls.index("visual-media")


def test_pipeline_passes_measured_silence_and_persists_only_compact_boundary_evidence(
    components, tmp_path,
):
    from app.jobs import JobState, JobStore

    transient = (
        "FULL-TRANSCRIPT-SENTINEL /private/audio-file.m4a "
        "provider body ffmpeg diagnostic"
    )
    original_extract = components.pipeline.media.extract

    def extract(*args, **kwargs):
        media = original_extract(*args, **kwargs)
        return MediaArtifacts(
            media.audio_chunks, media.frame_candidates, media.downloaded_bytes,
            [SilenceInterval(100, 102)], silence_measured=True,
        )

    def analyze(meta, transcript, frames, *, silence_intervals, cancellation_event, progress_callback):
        assert silence_intervals == [SilenceInterval(100, 102)]
        assert transcript.text == transient
        result = components.content.model_copy(deep=True)
        result.interventions = [
            Intervention(
                id="i001", start_seconds=0, end_seconds=101, tipo="intervento",
                relatori=[], titolo="Prima parte", sintesi="Prima parte verificata.",
                punti_chiave=["Uno", "Due", "Tre"], confidenza=.9,
            ),
            Intervention(
                id="i002", start_seconds=101, end_seconds=3600, tipo="intervento",
                relatori=[], titolo="Seconda parte", sintesi="Seconda parte verificata.",
                punti_chiave=["Quattro", "Cinque", "Sei"], confidenza=.9,
            ),
        ]
        result.boundaries = [BoundaryEvidence(
            previous_intervention_id="i001", next_intervention_id="i002",
            boundary_seconds=101, words_before=["prima"], words_after=["seconda"],
            pause_before=True, pause_after=True, rule="long_pause",
        )]
        return result

    components.pipeline.media.extract = extract
    components.pipeline.transcriber.transcribe = lambda *_args, **_kwargs: TranscriptionResult(
        text=transient, audio_seconds=3600,
        segments=[TranscriptSegment(
            start_seconds=0, end_seconds=3599, diarization_label="chunk-0:A",
            text="FULL-TRANSCRIPT-SENTINEL", source_utterance_id="private-u1",
            words=[TranscriptWord(
                text="WORD-ARRAY-SENTINEL", start_seconds=1, end_seconds=2,
                diarization_label="chunk-0:A", confidence=.99,
            )],
        )],
    )
    components.pipeline.analyzer.analyze = analyze

    report = components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert report.boundaries[0].boundary_seconds == 101
    assert report.audio_boundary_version == 1
    serialized = report.model_dump_json()
    assert "private raw transcript" not in serialized
    assert "audio.m4a" not in serialized
    assert "source_utterance_id" not in serialized
    assert '"words"' not in serialized
    store = JobStore(tmp_path / "pipeline-boundaries.sqlite3")
    job = store.create("private-source")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    persisted = (tmp_path / "pipeline-boundaries.sqlite3").read_bytes()
    for forbidden in (
        b"FULL-TRANSCRIPT-SENTINEL", b"WORD-ARRAY-SENTINEL", b"private-u1",
        b'"words"', b"0.99", b"/private/audio-file.m4a", b"provider body",
        b"ffmpeg diagnostic",
    ):
        assert forbidden not in persisted


def test_boundary_failure_has_fixed_text_and_safe_log_code(components, caplog):
    import logging
    sentinel = "PROVIDER-BODY WORD-SENTINEL /private/audio-file.m4a"
    caplog.set_level(logging.INFO, logger="app.events")
    components.pipeline.transcriber.transcribe = lambda *_args, **_kwargs: TranscriptionResult(
        text=sentinel, segments=[], audio_seconds=3600,
    )

    def fail_boundary(*_args, **_kwargs):
        raise AnalysisError(
            "boundaries", stage="boundary", detail_code="alignment_overlap",
        )

    components.pipeline.analyzer.analyze = fail_boundary

    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert caught.value.code == "boundaries"
    assert str(caught.value) == "Verifica audio dei confini non riuscita; riprova"
    assert "boundaries" in caplog.text
    assert "'phase': 'boundary'" in caplog.text
    assert "'detail_code': 'alignment_overlap'" in caplog.text
    assert sentinel not in str(caught.value) + caplog.text


@pytest.mark.parametrize("failure", [
    "missing_words", "invalid_words", "silence_end", "no_filter", "no_audio",
])
def test_real_evidence_parsers_map_to_boundaries_and_delete_remote_transcript(
    components, tmp_path, monkeypatch, caplog, failure,
):
    import logging
    import sys
    import traceback
    import httpx
    from app import media as media_module
    from app.assemblyai import AssemblyAITranscriber

    caplog.set_level(logging.INFO, logger="app.events")
    submitted = Event()
    requests = []
    word_failure = failure in {"missing_words", "invalid_words"}

    def handle(request):
        requests.append(request.method)
        if request.method == "POST":
            submitted.set()
            return httpx.Response(200, json={"id": "ephemeral-transcript"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "deleted"})
        if not word_failure:
            return httpx.Response(200, json={"status": "processing"})
        utterance = {"speaker": "A", "start": 0, "end": 1000, "text": "PRIVATE-TRANSCRIPT"}
        if failure == "invalid_words":
            utterance["words"] = [{"text": "PRIVATE-WORD", "start": 900, "end": 100,
                                    "speaker": "A", "confidence": .9}]
        return httpx.Response(200, json={"status": "completed", "audio_duration": 3600,
                                        "utterances": [utterance]})

    real_run = media_module._run_process
    media_cancelled = Event()

    def run_media(args, event, consume):
        assert args.count("-i") == 1
        assert submitted.wait(2)
        if word_failure:
            assert event.wait(2), "The malformed transcript must cancel the media worker"
            media_cancelled.set()
            raise CancelledError()
        diagnostic = {
            "silence_end": "[silencedetect @ 0x1] silence_end: 4 | silence_duration: 2",
            "no_filter": "[AVFilterGraph @ 0x1] No such filter: 'silencedetect'",
            "no_audio": "Stream map '0:a:0' matches no streams.",
        }[failure]
        # Real pipe draining, silencedetect parser, termination and reap.
        script = f"import sys; print({diagnostic!r}, file=sys.stderr, flush=True); sys.exit(1)"
        real_run([sys.executable, "-c", script], event, consume)

    monkeypatch.setattr(media_module, "_run_process", run_media)
    components.pipeline.media = media_module.FFmpegProcessor()
    components.pipeline.bunny.build_mp4_url = lambda _: "https://cdn.example.com/play.mp4"
    transcriber = AssemblyAITranscriber("test-key", transport=httpx.MockTransport(handle),
                                       poll_interval_seconds=.01, timeout_seconds=1)
    components.pipeline.fast_transcriber = transcriber
    try:
        with pytest.raises(PipelineError) as caught:
            components.pipeline.run(SOURCE, lambda *_: None, components.event)
    finally:
        transcriber.close()

    assert caught.value.code == "boundaries"
    assert str(caught.value) == "Verifica audio dei confini non riuscita; riprova"
    assert requests.count("POST") == 1 and requests.count("DELETE") == 1
    assert requests[-1] == "DELETE"
    assert media_cancelled.is_set() is word_failure
    assert list(tmp_path.iterdir()) == []
    assert "'phase': 'boundary'" in caplog.text
    exposed = caplog.text + "".join(traceback.format_exception(caught.value))
    assert all(value not in exposed for value in ("PRIVATE-WORD", "PRIVATE-TRANSCRIPT", "silence_end:", "No such filter:"))


@pytest.mark.parametrize("status", [401, 429, 503])
def test_real_transcription_provider_status_is_not_mapped_to_boundaries(components, status):
    import httpx
    from app.assemblyai import AssemblyAITranscriber

    started = Event()

    def handle(request):
        started.set()
        return httpx.Response(status, json={"error": "PRIVATE-PROVIDER-BODY"})

    def extract_visual(url, workspace, progress, event, *, duration_seconds=None):
        assert duration_seconds == 3600
        assert started.wait(2)
        assert event.wait(2)
        raise CancelledError()

    components.pipeline.media.extract_visual = extract_visual
    components.pipeline.bunny.build_mp4_url = lambda _: "https://cdn.example.com/play.mp4"
    transcriber = AssemblyAITranscriber("test-key", transport=httpx.MockTransport(handle))
    components.pipeline.fast_transcriber = transcriber
    try:
        with pytest.raises(PipelineError) as caught:
            components.pipeline.run(SOURCE, lambda *_: None, components.event)
    finally:
        transcriber.close()
    assert caught.value.code == "transcription"


@pytest.mark.parametrize("missing", ["interventions", "audio_boundary_version"])
def test_pipeline_rejects_report_without_complete_boundary_evidence(components, caplog, missing):
    import logging
    sentinel = "FULL-TRANSCRIPT-SENTINEL provider body /private/audio-file.m4a ffmpeg diagnostic"
    caplog.set_level(logging.INFO, logger="app.events")
    components.content.synopsis = sentinel
    setattr(components.content, missing, [] if missing == "interventions" else None)

    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert caught.value.code == "boundaries"
    assert caught.value.user_message == "Verifica audio dei confini non riuscita; riprova"
    assert sentinel not in str(caught.value) + caplog.text


@pytest.mark.parametrize(
    "failing_stage, expected_code",
    [("media", "media_decode"), ("transcription", "transcription")],
)
def test_fast_parallel_failure_cancels_the_other_stage_and_preserves_root_error(
    components, tmp_path, failing_stage, expected_code,
):
    counterpart_cancelled = Event()
    components.pipeline.bunny.build_mp4_url = lambda _metadata: "https://cdn.example/video.mp4"

    def extract_visual(_url, _workspace, _progress, cancellation_event, *, duration_seconds=None):
        assert duration_seconds == 3600
        if failing_stage == "media":
            raise MediaError("private media failure")
        assert cancellation_event.wait(1)
        counterpart_cancelled.set()
        raise CancelledError()

    def transcribe_url(_url, *, duration_seconds, cancellation_event):
        assert duration_seconds == 3600
        if failing_stage == "transcription":
            raise TranscriptionError("response")
        assert cancellation_event.wait(1)
        counterpart_cancelled.set()
        raise CancelledError()

    components.pipeline.media.extract_visual = extract_visual
    components.pipeline.fast_transcriber = SimpleNamespace(transcribe_url=transcribe_url)

    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None, components.event)

    assert caught.value.code == expected_code
    assert counterpart_cancelled.is_set()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage,error,code", [
    ("metadata", BunnyAuthError("secret upstream"), "bunny_auth"),
    ("metadata", BunnyNotFoundError("secret upstream"), "not_found"),
    ("metadata", RuntimeError("secret upstream"), "temporary_failure"),
    ("media", MediaError("secret upstream"), "media_decode"),
    ("media", MediaProtectedError("secret upstream"), "protected_video"),
    ("transcription", TranscriptionError("response"), "transcription"),
    ("analysis", AnalysisError("response"), "analysis_consolidation"),
])
def test_pipeline_sanitizes_failures_and_cleans_files(components, tmp_path, stage, error, code):
    components.error = stage, error
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda p, m: None, components.event)
    assert caught.value.code == code
    assert "secret" not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


def test_analysis_rate_limit_has_specific_actionable_message(components, tmp_path):
    components.error = "analysis", AnalysisError("rate_limit", status_code=429)
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda p, m: None, components.event)
    assert caught.value.code == "analysis_consolidation_rate_limit"
    assert "consolidamento" in caught.value.user_message.lower()
    assert "riprova" in caught.value.user_message
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage,word", [("visual", "slide"), ("window", "testuale"),
                                       ("consolidation", "consolidamento")])
@pytest.mark.parametrize("reason,status", [("response", None), ("rate_limit", 429)])
def test_analysis_failure_exposes_safe_stage_in_message_and_logs(components, stage, word, reason, status, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="app.events")
    components.error = "analysis", AnalysisError(reason, stage=stage, status_code=status)
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert word in caught.value.user_message.lower()
    assert f"analysis_{stage}" in caught.value.code
    assert caught.value.code in caplog.text
    assert "private raw transcript" not in caplog.text


@pytest.mark.parametrize("stage", ["metadata", "media", "transcription", "analysis"])
def test_cancellation_between_stages_stops_and_cleans(components, tmp_path, stage):
    components.cancel_after = stage
    with pytest.raises(PipelineCancelled):
        components.pipeline.run(SOURCE, lambda p, m: None, components.event)
    assert components.calls[-1] == stage
    assert list(tmp_path.iterdir()) == []


def test_four_hours_passes_but_one_second_over_never_reads_hls(components):
    components.metadata.duration_seconds = 14_400
    components.pipeline.run(SOURCE, lambda p, m: None, components.event)
    components.calls.clear()
    components.metadata.duration_seconds = 14_401
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda p, m: None, components.event)
    assert caught.value.code == "unsupported_duration"
    assert components.calls == ["metadata"]


def test_invalid_link_never_contacts_bunny(components):
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run("https://evil.example/video", lambda p, m: None)
    assert caught.value.code == "invalid_link"
    assert components.calls == []


def test_metadata_retry_uses_three_attempts_and_cancellation(components, monkeypatch):
    from app.bunny import BunnyServerError
    attempts = []
    def metadata(video_id):
        attempts.append(video_id)
        if len(attempts) < 3:
            raise BunnyServerError("safe")
        return components.metadata
    monkeypatch.setattr(components.event, "wait", lambda delay: False)
    components.pipeline.bunny.get_metadata = metadata
    components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert len(attempts) == 3
    attempts.clear()
    monkeypatch.setattr(components.event, "wait", lambda delay: components.event.set())
    with pytest.raises(PipelineCancelled):
        components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert len(attempts) == 1


@pytest.mark.parametrize("status", [0, 1, 2, 5, 6, 7, 8, 99])
def test_nonready_encoding_never_reads_hls(components, status):
    components.metadata.__dict__["status"] = status
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None)
    assert caught.value.code in {"not_ready", "encoding_failed", "unsupported_media"}
    assert components.calls == ["metadata"]


def test_protected_access_regenerates_once_in_clean_workspace(components, tmp_path):
    components.settings.bunny_token_auth_key = "TEST_ONLY"
    original = components.pipeline.media.extract
    attempts = []
    def extract(url, workspace, progress, event):
        assert list(workspace.iterdir()) == []
        attempts.append(workspace)
        if len(attempts) == 1:
            (workspace / "partial").write_bytes(b"partial")
            raise MediaProtectedError("safe")
        return original(url, workspace, progress, event)
    components.pipeline.media.extract = extract
    components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert components.calls.count("hls") == 2
    assert list(tmp_path.iterdir()) == []


def test_protected_access_fails_explicitly_after_one_renewal(components, tmp_path):
    components.settings.bunny_token_auth_key = "TEST_ONLY"
    components.error = "media", MediaProtectedError("safe")
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert caught.value.code == "protected_video"
    assert components.calls.count("media") == 2
    assert list(tmp_path.iterdir()) == []


def test_metadata_retry_exhaustion_never_reads_hls(components, monkeypatch):
    from app.bunny import BunnyServerError
    components.error = "metadata", BunnyServerError("safe")
    monkeypatch.setattr(components.event, "wait", lambda delay: False)
    with pytest.raises(PipelineError):
        components.pipeline.run(SOURCE, lambda *_: None, components.event)
    assert components.calls == ["metadata", "metadata", "metadata"]


def test_failed_bounded_media_is_cleaned_before_next_queued_job(components, tmp_path):
    import sys
    from app.media import _run_process
    from app.jobs import JobStore, SingleWorkerRunner, JobState
    original = components.pipeline.media.extract
    attempts = []
    def extract(url, workspace, progress, event):
        attempts.append(workspace)
        if len(attempts) == 1:
            (workspace / "partial.m4a").write_bytes(b"partial")
            _run_process([sys.executable, "-c", "import time; time.sleep(5)"], event, lambda *_: None,
                         workspace=workspace, runtime_seconds=.2)
        assert not attempts[0].exists()
        return original(url, workspace, progress, event)
    components.pipeline.media.extract = extract
    def run(source, progress, event):
        components.event = event
        return components.pipeline.run(source, progress, event)
    store = JobStore()
    runner = SingleWorkerRunner(store, run)
    first, second = store.create(SOURCE), store.create(SOURCE)
    try:
        one, two = runner.submit(first.id), runner.submit(second.id)
        one.result(timeout=3)
        two.result(timeout=3)
        assert store.get(first.id).state == JobState.FAILED
        assert store.get(second.id).state == JobState.COMPLETED
        assert list(tmp_path.iterdir()) == []
    finally:
        runner.shutdown(wait=True)


def test_worker_refetch_rejects_video_that_became_unready(components):
    components.metadata.status = 2
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None)
    assert caught.value.code == "not_ready"
    assert components.calls == ["metadata"]


def test_video_without_low_resolution_is_rejected_before_hls(components):
    components.metadata.available_resolutions = [1080, 2160]
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda *_: None)
    assert caught.value.code == "unsupported_media"
    assert components.calls == ["metadata"]
