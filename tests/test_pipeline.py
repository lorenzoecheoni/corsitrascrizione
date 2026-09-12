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
    metadata = BunnyVideoMetadata(video_id=UUID(int=1), title="Corso di prova", duration_seconds=3600,
                                  status=3, available_resolutions=[240, 720])
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

    state.pipeline = AnalysisPipeline(settings, SimpleNamespace(get_metadata=get_metadata, select_hls_url=hls),
                                      SimpleNamespace(extract=extract), SimpleNamespace(transcribe=transcribe),
                                      SimpleNamespace(analyze=analyze), temp_root=tmp_path)
    return state


def test_pipeline_cleans_media_and_reports_monotonic_stage_progress(components, tmp_path):
    values = []
    report = components.pipeline.run(SOURCE, lambda p, m: values.append(p), components.event)
    assert components.calls == ["metadata", "hls", "media", "transcription", "analysis"]
    assert report.title == components.content.title
    assert report.bunny_title == "Corso di prova"
    assert report.usage.transcription.requests == 0
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
