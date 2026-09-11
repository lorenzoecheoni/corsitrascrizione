import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.analysis import AnalysisError
from app.bunny import BunnyAuthError, BunnyNotFoundError, BunnyVideoMetadata
from app.config import Settings
from app.media import AudioChunk, FrameCandidate, MediaArtifacts, MediaError, MediaProtectedError
from app.models import AcademyContent
from app.pipeline import AnalysisPipeline, PipelineCancelled, PipelineError
from app.transcription import TranscriptionError, TranscriptionResult


SOURCE = "https://iframe.mediadelivery.net/embed/123/00000000-0000-0000-0000-000000000001?token=private"


@pytest.fixture
def components(tmp_path):
    settings = Settings(bunny_library_id=123, bunny_stream_api_key="secret",
                        bunny_cdn_hostname="cdn.example.com", openai_api_key="secret",
                        app_password="team-secret", _env_file=None)
    data = json.loads(Path("tests/fixtures/report.json").read_text())
    data.pop("cost")
    content = AcademyContent.model_validate(data)
    metadata = BunnyVideoMetadata(video_id=UUID(int=1), title="Corso di prova", duration_seconds=3600)
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

    def hls(video_id):
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
        return MediaArtifacts([AudioChunk(audio, 0, 3600)], [FrameCandidate(frame, 0)], 1_000_000_000)

    def transcribe(chunks, *, cancellation_event):
        assert cancellation_event is state.event
        assert chunks[0].path.exists()
        stage("transcription")
        return TranscriptionResult(text="private raw transcript", segments=[], audio_seconds=3600)

    def analyze(meta, transcript, frames, *, cancellation_event):
        assert cancellation_event is state.event
        assert transcript.text == "private raw transcript"
        assert frames[0].path.exists()
        stage("analysis")
        return content

    state.pipeline = AnalysisPipeline(settings, SimpleNamespace(get_metadata=get_metadata, build_hls_url=hls),
                                      SimpleNamespace(extract=extract), SimpleNamespace(transcribe=transcribe),
                                      SimpleNamespace(analyze=analyze), temp_root=tmp_path)
    return state


def test_pipeline_cleans_media_and_reports_monotonic_stage_progress(components, tmp_path):
    values = []
    report = components.pipeline.run(SOURCE, lambda p, m: values.append(p), components.event)
    assert components.calls == ["metadata", "hls", "media", "transcription", "analysis"]
    assert report.title == components.content.title
    assert report.cost.estimated_low_usd == .41
    assert report.cost.estimated_high_usd == .69
    assert values == sorted(values)
    assert all(p in values for p in (5, 10, 27, 45, 50, 72, 75, 85, 88, 98, 100))
    assert list(tmp_path.iterdir()) == []
    assert "private raw transcript" not in report.model_dump_json()


@pytest.mark.parametrize("stage,error,code", [
    ("metadata", BunnyAuthError("secret upstream"), "bunny_auth"),
    ("metadata", BunnyNotFoundError("secret upstream"), "not_found"),
    ("metadata", RuntimeError("secret upstream"), "temporary_failure"),
    ("media", MediaError("secret upstream"), "media_decode"),
    ("media", MediaProtectedError("secret upstream"), "protected_video"),
    ("transcription", TranscriptionError("response"), "transcription"),
    ("analysis", AnalysisError("response"), "analysis"),
])
def test_pipeline_sanitizes_failures_and_cleans_files(components, tmp_path, stage, error, code):
    components.error = stage, error
    with pytest.raises(PipelineError) as caught:
        components.pipeline.run(SOURCE, lambda p, m: None, components.event)
    assert caught.value.code == code
    assert "secret" not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


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
