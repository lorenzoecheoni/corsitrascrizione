from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def settings() -> Settings:
    return Settings(
        bunny_library_id=123,
        bunny_stream_api_key="bunny-secret",
        bunny_cdn_hostname="academy.example.b-cdn.net",
        openai_api_key="openai-secret",
        app_password="team-secret",
    )


def test_home_requires_basic_auth() -> None:
    response = TestClient(create_app(settings())).get("/")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


def test_home_accepts_team_password() -> None:
    response = TestClient(create_app(settings())).get(
        "/", auth=("team", "team-secret")
    )
    assert response.status_code == 200
    assert "Bunny Video Report" in response.text


def test_healthcheck_is_public() -> None:
    response = TestClient(create_app(settings())).get("/healthz")
    assert response.json() == {"status": "ok"}
