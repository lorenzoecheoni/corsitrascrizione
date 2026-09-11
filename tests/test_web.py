from pathlib import Path
from threading import Event
from uuid import uuid4
import re

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.jobs import JobState
from app.main import create_app
from app.models import AcademyReport
from app.pipeline import AnalysisPipeline
from app.bunny import BunnyVideoMetadata


AUTH = ("team", "team-secret")
SOURCE = "https://iframe.mediadelivery.net/embed/123/00000000-0000-0000-0000-000000000001?token=private"


@pytest.fixture
def client(monkeypatch):
    finished = Event()
    def fake_run(self, source_url, progress_callback, cancellation_event):
        finished.wait(2)
        return AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    monkeypatch.setattr(AnalysisPipeline, "run", fake_run)
    app = create_app(Settings(bunny_library_id=123, bunny_stream_api_key="bunny-secret",
        bunny_cdn_hostname="cdn.example.com", openai_api_key="openai-secret",
        app_password="team-secret", _env_file=None))
    app.state.bunny.get_metadata = lambda video_id: BunnyVideoMetadata(video_id=video_id,
        title="Corso di prova", duration_seconds=3600, status=3, available_resolutions=[240, 720])
    with TestClient(app) as client:
        client.headers["X-CSRF-Token"] = app.state.csrf_token
        yield client
        finished.set()
    app.state.runner.shutdown(wait=True)


def test_create_job_redirects_and_can_be_reopened_without_exposing_source(client):
    preview = client.post("/preview", data={"source_url": SOURCE}, auth=AUTH)
    confirmation = re.search(r'name="confirmation" value="([^"]+)"', preview.text)[1]
    response = client.post("/jobs", data={"confirmation": confirmation}, auth=AUTH, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/jobs/")
    assert client.get(location, auth=AUTH).status_code == 200
    data = client.get("/api" + location, auth=AUTH)
    assert data.status_code == 200
    assert data.json()["state"] in {"queued", "processing", "completed"}
    for secret in ("source_url", "private", "bunny-secret", "openai-secret", "transcript"):
        assert secret not in data.text


def test_invalid_link_is_rejected_before_enqueue(client):
    response = client.post("/preview", data={"source_url": "https://evil.example"}, auth=AUTH)
    assert response.status_code == 422
    assert "evil.example" not in response.text


@pytest.mark.parametrize("method,path", [
    ("get", "/"), ("post", "/jobs"), ("get", "/jobs/unknown"),
    ("get", "/api/jobs/unknown"), ("post", "/jobs/unknown/cancel"),
    ("get", "/jobs/unknown/report.md"), ("get", "/jobs/unknown/report.txt"),
    ("get", "/static/job.js"), ("get", "/static/app.css"),
])
def test_every_application_route_requires_auth(client, method, path):
    response = getattr(client, method)(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


@pytest.mark.parametrize("suffix", ["", "/report.md", "/report.txt", "/cancel"])
def test_missing_jobs_are_404(client, suffix):
    path = f"/jobs/{uuid4()}{suffix}"
    response = (client.post if suffix == "/cancel" else client.get)(path, auth=AUTH)
    assert response.status_code == 404


def test_exports_require_completion_and_queued_job_can_be_cancelled(client):
    job = client.app.state.store.create(SOURCE)
    for extension in ("md", "txt"):
        assert client.get(f"/jobs/{job.id}/report.{extension}", auth=AUTH).status_code == 409
    response = client.post(f"/jobs/{job.id}/cancel", auth=AUTH, follow_redirects=False)
    assert response.status_code == 303
    assert client.get(f"/api/jobs/{job.id}", auth=AUTH).json()["state"] == "cancelled"


def test_completed_job_downloads_and_page_escape_untrusted_content(client):
    report = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    report.title = "<script>alert('unsafe')</script>"
    store = client.app.state.store
    job = store.create(SOURCE)
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    for extension, mime in (("md", "text/markdown"), ("txt", "text/plain")):
        response = client.get(f"/jobs/{job.id}/report.{extension}", auth=AUTH)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(mime)
        assert "attachment" in response.headers["content-disposition"]
        assert report.synopsis in response.text
    page = client.get(f"/jobs/{job.id}", auth=AUTH)
    assert "<script>alert('unsafe')</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "Stampa / Salva PDF" in page.text
