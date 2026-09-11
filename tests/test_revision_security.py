import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient
import pytest

from app.bunny import BunnyVideoMetadata
from app.config import Settings
from app.main import create_app


def authenticate(client, password):
    response = client.post("/login", data={
        "username": "team", "password": password,
        "csrf_token": client.app.state.csrf_token,
    }, follow_redirects=False)
    assert response.status_code == 303


def test_actual_startup_never_prints_invalid_settings_or_secrets(tmp_path):
    marker = "TEST_ONLY_STARTUP_PASSWORD_84bc"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BUNNY_", "OPENAI_", "APP_"))}
    env.update(APP_PASSWORD=marker, PYTHONPATH=str(Path.cwd()))
    process = subprocess.run([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
                              "--no-access-log"], env=env, cwd=tmp_path,
                             capture_output=True, timeout=10)
    output = process.stdout + process.stderr
    assert process.returncode != 0
    assert marker.encode() not in output, "Startup exposed a configured secret"
    assert b"input_value" not in output, "Startup exposed validation inputs"


@pytest.fixture
def app_client(monkeypatch):
    app = create_app(Settings(bunny_library_id=123, bunny_stream_api_key="TEST_ONLY",
        bunny_cdn_hostname="cdn.example.invalid", openai_api_key="TEST_ONLY",
        app_password="TEST_ONLY", _env_file=None))
    calls = []
    def metadata(video_id):
        calls.append("metadata")
        return BunnyVideoMetadata(video_id=UUID(int=1), title="Original Bunny <title>",
                                  duration_seconds=3600, status=4, available_resolutions=[240, 720])
    app.state.bunny = SimpleNamespace(get_metadata=metadata)
    app.state.runner.submit = lambda job_id: calls.append("enqueue")
    with TestClient(app) as client:
        authenticate(client, "TEST_ONLY")
        yield client, calls
    app.state.runner.shutdown(wait=True)


def token(client):
    page = client.get("/")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match, "Authenticated form must contain a CSRF token"
    return match[1]


@pytest.mark.parametrize("path", ["/preview", "/jobs", "/jobs/00000000-0000-0000-0000-000000000001/cancel"])
def test_cross_site_forms_cannot_trigger_mutations(app_client, path):
    client, calls = app_client
    response = client.post(path, data={"source_url": "https://iframe.mediadelivery.net/embed/123/00000000-0000-0000-0000-000000000001"},
                           headers={"Origin": "https://untrusted.invalid"},
                           follow_redirects=False)
    assert response.status_code == 403
    assert calls == []


def test_preview_requires_confirmation_and_does_not_trust_changed_fields(app_client):
    client, calls = app_client
    csrf = token(client)
    source = "https://iframe.mediadelivery.net/embed/123/00000000-0000-0000-0000-000000000001?token=discard"
    preview = client.post("/preview", data={"source_url": source, "csrf_token": csrf})
    assert preview.status_code == 200
    assert "Original Bunny &lt;title&gt;" in preview.text
    assert "1:00:00" in preview.text and "USD" in preview.text
    assert calls == ["metadata"]
    confirmation = re.search(r'name="confirmation" value="([^"]+)"', preview.text)
    assert confirmation
    response = client.post("/jobs", data={"confirmation": confirmation[1], "csrf_token": csrf,
        "source_url": "https://untrusted.invalid", "title": "altered", "duration_seconds": "1"},
        follow_redirects=False)
    assert response.status_code == 303
    assert calls == ["metadata", "enqueue"]
    from uuid import UUID
    job = client.app.state.store.get(UUID(response.headers["location"].split("/")[-1]))
    assert job.source_url.endswith("/00000000-0000-0000-0000-000000000001")
    assert client.post("/jobs", data={"confirmation": "tampered", "csrf_token": csrf}).status_code == 422


def test_token_does_not_override_untrusted_origin(app_client):
    client, calls = app_client
    response = client.post("/preview", data={"csrf_token": token(client)},
        headers={"Origin": "null"})
    assert response.status_code == 403
    assert calls == []


@pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Site": "cross-site"}])
def test_missing_token_is_rejected_even_without_origin(app_client, headers):
    client, calls = app_client
    response = client.post("/preview", data={"source_url": "unused"}, headers=headers)
    assert response.status_code == 403 and calls == []


def test_confirmation_cannot_be_skipped_with_valid_csrf(app_client):
    client, calls = app_client
    response = client.post("/jobs", data={"csrf_token": token(client), "source_url": "unused"})
    assert response.status_code == 422 and calls == []
