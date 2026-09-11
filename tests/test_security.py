"""Public-repository regression checks: every sensitive value is deliberately fake."""

import logging
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.analysis import AnalysisError
from app.bunny import BunnyAuthError, BunnyClient, BunnyVideoMetadata
from app.config import Settings
from app.main import create_app
from app.media import MediaArtifacts, MediaError
from app.pipeline import AnalysisPipeline, PipelineError
from app.transcription import TranscriptionError, TranscriptionResult


@pytest.fixture
def sentinels():
    values = SimpleNamespace(
        api_key="FAKE_ONLY_BUNNY_API_SENTINEL", openai="FAKE_ONLY_OPENAI_SENTINEL",
        password="FAKE_ONLY_PASSWORD_SENTINEL", token_key="FAKE_ONLY_TOKEN_KEY_SENTINEL",
        token="FAKE_ONLY_QUERY_TOKEN_SENTINEL", transcript="FAKE_ONLY_TRANSCRIPT_SENTINEL",
        image="FAKE_ONLY_IMAGE_PAYLOAD_SENTINEL", body="FAKE_ONLY_PROVIDER_BODY_SENTINEL",
    )
    values.source_url = (
        "https://iframe.mediadelivery.net/embed/123/00000000-0000-0000-0000-000000000001"
        f"?token={values.token}"
    )
    values.signed_url = f"https://cdn.example.test/bcdn_token={values.token}&expires=9999999999/video"
    values.all = list(vars(values).values())
    return values


@pytest.fixture
def settings(sentinels):
    return Settings(bunny_library_id=123, bunny_stream_api_key=sentinels.api_key,
                    bunny_cdn_hostname="cdn.example.test", openai_api_key=sentinels.openai,
                    app_password=sentinels.password, bunny_token_auth_key=sentinels.token_key,
                    _env_file=None)


def assert_private(caplog, sentinels, response=""):
    rendered = "\n".join(record.getMessage() + repr(record.__dict__) for record in caplog.records)
    rendered += caplog.text + response
    for value in sentinels.all:
        assert value not in rendered


def failing_pipeline(settings, sentinels, tmp_path, phase):
    errors = {"metadata": BunnyAuthError, "media": MediaError,
              "transcription": TranscriptionError, "analysis": AnalysisError}

    def stage(name, value):
        def call(*args, **kwargs):
            if phase == name:
                error = errors[name]("response" if name in {"transcription", "analysis"}
                                     else " ".join(sentinels.all))
                error.args = (" ".join(sentinels.all),)
                raise error
            return value
        return call

    return AnalysisPipeline(
        settings,
        SimpleNamespace(get_metadata=stage("metadata", BunnyVideoMetadata(
            video_id=UUID(int=1), title="Fake fixture", duration_seconds=60)),
            build_hls_url=lambda _: sentinels.signed_url),
        SimpleNamespace(extract=stage("media", MediaArtifacts([], [], 0))),
        SimpleNamespace(transcribe=stage("transcription", TranscriptionResult(
            text=sentinels.transcript, segments=[], audio_seconds=60))),
        SimpleNamespace(analyze=stage("analysis", SimpleNamespace(
            model_dump=lambda: {"title": sentinels.transcript}))), temp_root=tmp_path,
    )


@pytest.mark.parametrize("phase,code", [("metadata", "bunny_auth"), ("media", "media_decode"),
    ("transcription", "transcription"), ("analysis", "analysis"), ("report", "temporary_failure")])
def test_stage_errors_never_disclose_secrets(caplog, settings, sentinels, tmp_path, phase, code):
    caplog.set_level(logging.DEBUG)
    pipeline = failing_pipeline(settings, sentinels, tmp_path, phase)
    with pytest.raises(PipelineError) as caught:
        pipeline.run(sentinels.source_url, lambda *_: None)
    assert caught.value.code == code
    assert caught.value.__suppress_context__
    assert_private(caplog, sentinels, str(caught.value))


@pytest.mark.parametrize("phase,code", [("metadata", "bunny_auth"), ("media", "media_decode"),
    ("transcription", "transcription"), ("analysis", "analysis"), ("report", "temporary_failure")])
def test_http_failed_job_has_safe_correlated_event(caplog, settings, sentinels, tmp_path, phase, code):
    caplog.set_level(logging.DEBUG)
    app = create_app(settings)
    pipeline = failing_pipeline(settings, sentinels, tmp_path, phase)
    app.state.runner._pipeline = pipeline.run
    with TestClient(app) as client:
        job = app.state.store.create(sentinels.source_url)
        app.state.runner.submit(job.id).result(timeout=5)
        response = client.get(f"/api/jobs/{job.id}?token={sentinels.token}",
                              auth=("team", sentinels.password))
    app.state.runner.shutdown()
    assert response.status_code == 200
    assert response.json()["state"] == "failed"
    assert_private(caplog, sentinels, response.text)
    events = [record.msg for record in caplog.records if isinstance(record.msg, dict)]
    assert any(event.get("job_id") == str(job.id) and event.get("phase") == phase
               and event.get("error_code") == code for event in events)


def test_log_filter_covers_args_extra_and_tracebacks(caplog, settings, sentinels):
    from app.logging_config import configure_logging
    configure_logging(settings)
    caplog.set_level(logging.DEBUG)
    logger = logging.getLogger("fake.provider")
    logger.warning("AccessKey=%s Authorization: Bearer %s", sentinels.api_key, sentinels.openai)
    logger.warning({"transcript": sentinels.transcript, "image": sentinels.image,
                    "token": sentinels.token, "expires": "9999999999", "token_path": "/fake/private/"})
    logger.warning("%s", {"body": sentinels.body, "url": sentinels.signed_url})
    try:
        raise RuntimeError(" ".join(sentinels.all))
    except RuntimeError:
        logger.exception("provider failed", extra={"provider_body": sentinels.body}, stack_info=True)
    assert_private(caplog, sentinels)
    assert all(record.exc_info is None and record.exc_text is None and record.stack_info is None
               for record in caplog.records)


def test_repeated_app_creation_is_idempotent_and_access_uses_template(caplog, settings, sentinels):
    caplog.set_level(logging.INFO)
    first = create_app(settings)
    factory = logging.getLogRecordFactory()
    handlers = tuple(logging.getLogger().handlers)
    second = create_app(settings)
    assert logging.getLogRecordFactory() is factory
    assert tuple(logging.getLogger().handlers) == handlers
    caplog.clear()
    with TestClient(second) as client:
        response = client.get(f"/api/jobs/{sentinels.body}?token={sentinels.token}",
                              auth=("team", sentinels.password))
    first.state.runner.shutdown()
    second.state.runner.shutdown()
    assert_private(caplog, sentinels, response.text)
    access = [r.msg for r in caplog.records if isinstance(r.msg, dict) and r.msg.get("phase") == "http"]
    assert len(access) == 1
    assert access[0]["route"] == "/api/jobs/{job_id}"
    assert access[0]["status_code"] == 404


def test_unexpected_http_and_validation_errors_are_fixed(caplog, settings, sentinels):
    caplog.set_level(logging.DEBUG)
    app = create_app(settings)

    @app.get("/fake-error")
    def fail():
        raise RuntimeError(" ".join(sentinels.all))

    with TestClient(app, raise_server_exceptions=False) as client:
        responses = [client.get("/fake-error", auth=("team", sentinels.password)),
                     client.post("/jobs", files={"source_url": (sentinels.body, sentinels.image)},
                                 auth=("team", sentinels.password))]
    app.state.runner.shutdown()
    assert [r.status_code for r in responses] == [500, 422]
    assert_private(caplog, sentinels, "".join(r.text for r in responses))


def test_bunny_remote_status_is_logged_without_body(caplog, settings, sentinels, monkeypatch):
    from app.logging_config import configure_logging
    configure_logging(settings)
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda request: httpx.Response(403, text=" ".join(sentinels.all)))
    real_client = httpx.Client
    monkeypatch.setattr("app.bunny.httpx.Client", lambda **kwargs: real_client(transport=transport, **kwargs))
    with pytest.raises(BunnyAuthError) as caught:
        BunnyClient(settings).get_metadata(UUID(int=1))
    assert_private(caplog, sentinels, str(caught.value))
    assert any(isinstance(r.msg, dict) and r.msg.get("status_code") == 403
               and r.msg.get("attempt") == 1 for r in caplog.records)


def test_pipeline_boundary_cannot_accept_arbitrary_public_text(sentinels):
    error = PipelineError("analysis", " ".join(sentinels.all))
    assert error.code == "analysis"
    for value in sentinels.all:
        assert value not in error.user_message


def test_sequential_jobs_keep_separate_context_and_reset_worker(caplog, settings, sentinels, tmp_path):
    from app.logging_config import log_event
    caplog.set_level(logging.INFO)
    app = create_app(settings)
    app.state.runner._pipeline = failing_pipeline(settings, sentinels, tmp_path, "metadata").run
    jobs = [app.state.store.create(sentinels.source_url) for _ in range(2)]
    for job in jobs:
        app.state.runner.submit(job.id).result(timeout=5)
    # Same worker thread after both failures: no stale context may escape.
    app.state.runner._executor.submit(lambda: log_event("report", elapsed_seconds=0)).result(timeout=5)
    app.state.runner.shutdown()
    failures = [r.msg for r in caplog.records if isinstance(r.msg, dict)
                and r.msg.get("error_code") == "bunny_auth"]
    assert [event["job_id"] for event in failures] == [str(job.id) for job in jobs]
    assert "job_id" not in caplog.records[-1].msg
    assert_private(caplog, sentinels)


def test_factory_protects_new_handlers_and_configured_values(caplog, settings, sentinels):
    from io import StringIO
    from app.logging_config import configure_logging
    configure_logging(settings)
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("fake.late.provider")
    logger.addHandler(handler)
    try:
        logger.warning("%s", {"Authorization": sentinels.openai, "AccessKey": sentinels.api_key,
                               "bcdn_token": sentinels.token, "expires": "9999999999",
                               "token_path": "/fake/private/", "url": sentinels.source_url})
        logger.warning({"error_code": sentinels.password, "phase": sentinels.transcript})
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert_private(caplog, sentinels, stream.getvalue())
    assert "9999999999" not in stream.getvalue()
    assert "/fake/private/" not in stream.getvalue()
    assert "log_suppressed" in stream.getvalue()


def test_invalid_source_has_safe_validation_error_in_pipeline_and_http(caplog, settings, sentinels, tmp_path):
    caplog.set_level(logging.INFO)
    app = create_app(settings)
    pipeline = failing_pipeline(settings, sentinels, tmp_path, "metadata")
    invalid_source = f"https://invalid.example.test/{sentinels.transcript}?token={sentinels.token}"
    with pytest.raises(PipelineError) as caught:
        pipeline.run(invalid_source, lambda *_: None)
    assert caught.value.code == "invalid_link"
    with TestClient(app) as client:
        response = client.post("/jobs", data={"source_url": invalid_source},
                               auth=("team", sentinels.password))
    app.state.runner.shutdown()
    assert response.status_code == 422
    assert_private(caplog, sentinels, response.text + str(caught.value))
    assert any(isinstance(r.msg, dict) and r.msg.get("phase") == "validation"
               and r.msg.get("error_code") == "invalid_link" for r in caplog.records)
