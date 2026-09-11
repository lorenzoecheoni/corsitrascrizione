from pathlib import Path
from threading import Event
from uuid import UUID, uuid4
import re

from fastapi.testclient import TestClient
import pytest

from app.bunny import (
    BunnyAuthError,
    BunnyCatalog,
    BunnyCatalogVideo,
    BunnyRateLimitError,
    BunnyServerError,
    BunnyVideoMetadata,
)
from app.config import Settings
from app.jobs import JobState
from app.main import create_app
from app.models import AcademyReport
from app.pipeline import AnalysisPipeline
from app.selection import sign_selection


VIDEO_ID = "00000000-0000-0000-0000-000000000001"
OTHER_VIDEO_ID = "00000000-0000-0000-0000-000000000002"
VIDEO_TITLE = "Corso di prova"
OTHER_VIDEO_TITLE = "Secondo corso"


def extract_hidden(page: str, name: str) -> str:
    return re.search(rf'name="{name}" value="([^"]+)"', page)[1]


def catalog(*videos: BunnyCatalogVideo) -> BunnyCatalog:
    return BunnyCatalog(videos=list(videos), total_items=len(videos))


def catalog_video(video_id: str, title: str, duration: float) -> BunnyCatalogVideo:
    return BunnyCatalogVideo(
        video_id=video_id,
        title=title,
        duration_seconds=duration,
        status=3,
        description="Descrizione catalogo",
        thumbnail_url=f"https://cdn.example.com/{video_id}/thumb.jpg",
    )


@pytest.fixture
def client(monkeypatch):
    finished = Event()

    def fake_run(self, source_url, progress_callback, cancellation_event):
        finished.wait(2)
        return AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())

    monkeypatch.setattr(AnalysisPipeline, "run", fake_run)
    app = create_app(Settings(
        bunny_library_id=123,
        bunny_stream_api_key="bunny-secret",
        bunny_cdn_hostname="cdn.example.com",
        openai_api_key="openai-secret",
        app_password="team-secret",
        _env_file=None,
    ))
    app.state.bunny.list_videos = lambda: catalog()
    app.state.bunny.get_metadata = lambda video_id: BunnyVideoMetadata(
        video_id=video_id,
        title=VIDEO_TITLE if str(video_id) == VIDEO_ID else OTHER_VIDEO_TITLE,
        duration_seconds=3600 if str(video_id) == VIDEO_ID else 1800,
        status=3,
        available_resolutions=[240, 720],
    )
    with TestClient(app) as test_client:
        login = test_client.post("/login", data={
            "username": "team",
            "password": "team-secret",
            "csrf_token": app.state.csrf_token,
        }, follow_redirects=False)
        assert login.status_code == 303
        test_client.headers["X-CSRF-Token"] = app.state.csrf_token
        yield test_client
        finished.set()
    app.state.runner.shutdown(wait=True)


def selection_form(client: TestClient, video_ids: list[str]):
    data = {"csrf_token": client.app.state.csrf_token}
    if video_ids:
        data["video_ids"] = video_ids
    return client.post("/selections/preview", data=data)


def test_dashboard_renders_catalog_totals_and_recent_jobs_without_source_url(client):
    app = client.app
    app.state.bunny.list_videos = lambda: catalog(
        catalog_video(VIDEO_ID, VIDEO_TITLE, 3600),
        catalog_video(OTHER_VIDEO_ID, OTHER_VIDEO_TITLE, 1800),
    )
    app.state.store.create("https://private.example/source", source_title="Lavoro recente")

    response = client.get("/")

    assert response.status_code == 200
    assert VIDEO_TITLE in response.text and OTHER_VIDEO_TITLE in response.text
    assert "2 video" in response.text
    assert "1:30:00" in response.text
    assert "Lavoro recente" in response.text
    assert "private.example" not in response.text
    assert "bunny-secret" not in response.text
    assert 'name="video_ids"' in response.text
    assert 'action="/selections/preview"' in response.text


def test_dashboard_handles_an_empty_catalog(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Nessun video disponibile" in response.text


@pytest.mark.parametrize(("error", "message"), [
    (BunnyAuthError("bunny-secret upstream body"), "Accesso a Bunny non autorizzato"),
    (BunnyRateLimitError("bunny-secret upstream body"), "Limite di richieste Bunny raggiunto"),
    (BunnyServerError("bunny-secret upstream body"), "Servizio Bunny temporaneamente non disponibile"),
])
def test_dashboard_maps_provider_errors_to_safe_messages(client, error, message):
    def unavailable():
        raise error

    client.app.state.bunny.list_videos = unavailable
    response = client.get("/")

    assert response.status_code == 200
    assert message in response.text
    assert "bunny-secret" not in response.text
    assert "upstream body" not in response.text


@pytest.mark.parametrize("video_ids", [[VIDEO_ID], [VIDEO_ID] * 50])
def test_selection_preview_accepts_between_one_and_fifty_unique_canonical_ids(client, video_ids):
    if len(video_ids) == 50:
        video_ids = [str(UUID(int=index + 1)) for index in range(50)]
    response = selection_form(client, video_ids)

    assert response.status_code == 200
    assert extract_hidden(response.text, "confirmation")


def test_selection_preview_rejects_an_empty_selection(client):
    response = selection_form(client, [])

    assert response.status_code == 422


@pytest.mark.parametrize("video_ids", [
    [VIDEO_ID, VIDEO_ID],
    ["not-a-uuid"],
    [str(UUID(int=index + 1)) for index in range(51)],
])
def test_selection_preview_rejects_duplicates_invalid_and_oversized_selections(client, video_ids):
    response = selection_form(client, video_ids)

    assert response.status_code == 422
    assert "Conferma non valida" not in response.text


def test_selected_videos_are_rechecked_then_queued(client):
    checked = []

    def get_metadata(video_id):
        checked.append(str(video_id))
        return BunnyVideoMetadata(
            video_id=video_id,
            title=VIDEO_TITLE if str(video_id) == VIDEO_ID else OTHER_VIDEO_TITLE,
            duration_seconds=3600 if str(video_id) == VIDEO_ID else 1800,
            status=3,
            available_resolutions=[240],
        )

    submitted = []
    client.app.state.bunny.get_metadata = get_metadata
    client.app.state.runner.submit = lambda job_id: submitted.append(job_id)
    preview = selection_form(client, [VIDEO_ID, OTHER_VIDEO_ID])
    token = extract_hidden(preview.text, "confirmation")

    assert preview.status_code == 200
    assert checked == [VIDEO_ID, OTHER_VIDEO_ID]
    assert "1:30:00" in preview.text
    assert "0.6000" in preview.text
    created = client.post("/batches", data={
        "confirmation": token,
        "csrf_token": client.app.state.csrf_token,
    }, follow_redirects=False)

    assert created.status_code == 303
    assert len(submitted) == 2
    assert checked == [VIDEO_ID, OTHER_VIDEO_ID, VIDEO_ID, OTHER_VIDEO_ID]
    batch = client.get(created.headers["location"])
    assert VIDEO_TITLE in batch.text and OTHER_VIDEO_TITLE in batch.text
    for secret in ("bunny-secret", "cdn.example.com", "iframe.mediadelivery.net"):
        assert secret not in batch.text


@pytest.mark.parametrize("confirmation", ["changed", sign_selection([UUID(VIDEO_ID)], b"x" * 32, now=0)])
def test_batch_rejects_altered_or_expired_confirmation(client, confirmation):
    response = client.post("/batches", data={
        "confirmation": confirmation,
        "csrf_token": client.app.state.csrf_token,
    })

    assert response.status_code == 422
    assert "Conferma non valida o scaduta" in response.text


@pytest.mark.parametrize(("path", "data"), [
    ("/selections/preview", {"video_ids": VIDEO_ID}),
    ("/batches", {"confirmation": "changed"}),
])
def test_batch_posts_require_csrf(client, path, data):
    response = client.post(path, data=data, headers={"X-CSRF-Token": ""})

    assert response.status_code == 403


def test_submit_failure_cancels_only_unscheduled_job_and_keeps_batch_visible(client):
    preview = selection_form(client, [VIDEO_ID, OTHER_VIDEO_ID])
    token = extract_hidden(preview.text, "confirmation")
    submitted = []

    def submit(job_id):
        if not submitted:
            submitted.append(job_id)
            raise RuntimeError("bunny-secret upstream response")
        submitted.append(job_id)

    client.app.state.runner.submit = submit
    response = client.post("/batches", data={
        "confirmation": token,
        "csrf_token": client.app.state.csrf_token,
    }, follow_redirects=False)

    assert response.status_code == 303
    assert len(submitted) == 2
    batch = client.get(response.headers["location"])
    assert batch.status_code == 200
    assert "Annullato" in batch.text
    assert "bunny-secret" not in batch.text
    assert "upstream response" not in batch.text


def test_missing_batch_is_404(client):
    assert client.get(f"/batches/{uuid4()}").status_code == 404


def test_exports_require_completion_and_queued_job_can_be_cancelled(client):
    job = client.app.state.store.create("https://private.example/source")
    for extension in ("md", "txt"):
        assert client.get(f"/jobs/{job.id}/report.{extension}").status_code == 409
    response = client.post(f"/jobs/{job.id}/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert client.get(f"/api/jobs/{job.id}").json()["state"] == "cancelled"


def test_completed_job_downloads_and_page_escape_untrusted_content(client):
    report = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    report.title = "<script>alert('unsafe')</script>"
    store = client.app.state.store
    job = store.create("https://private.example/source")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    for extension, mime in (("md", "text/markdown"), ("txt", "text/plain")):
        response = client.get(f"/jobs/{job.id}/report.{extension}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(mime)
        assert "attachment" in response.headers["content-disposition"]
        assert report.synopsis in response.text
    page = client.get(f"/jobs/{job.id}")
    assert "<script>alert('unsafe')</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "Stampa / Salva PDF" in page.text
