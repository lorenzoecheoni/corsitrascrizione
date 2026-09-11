import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import Settings
from app.main import create_app


SESSION_COOKIE = "bvr_session"


def settings() -> Settings:
    return Settings(
        bunny_library_id=123,
        bunny_stream_api_key="bunny-secret",
        bunny_cdn_hostname="academy.example.b-cdn.net",
        openai_api_key="openai-secret",
        app_password="team-secret",
    )


@pytest.fixture
def client():
    app = create_app(settings())
    with TestClient(app) as test_client:
        yield test_client
    app.state.runner.shutdown(wait=True)


def login(client: TestClient):
    return client.post("/login", data={
        "username": "team", "password": "team-secret",
        "csrf_token": client.app.state.csrf_token,
    }, follow_redirects=False)


def test_html_without_session_redirects_to_login(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_healthcheck_is_public(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_invalid_login_does_not_issue_a_session_cookie(client: TestClient) -> None:
    response = client.post("/login", data={
        "username": "team", "password": "wrong-password",
        "csrf_token": client.app.state.csrf_token,
    }, follow_redirects=False)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("text/html")
    assert "Credenziali non valide" in response.text
    assert SESSION_COOKIE not in response.headers.get("set-cookie", "")


def test_html_login_issues_session_and_unlocks_home(client: TestClient) -> None:
    response = login(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert f"{SESSION_COOKIE}=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/").status_code == 200


def test_session_tokens_reject_expiry_and_tampering() -> None:
    secret = b"x" * 32
    issue_session = getattr(auth, "issue_session", None)
    session_is_valid = getattr(auth, "session_is_valid", None)

    assert callable(issue_session)
    assert callable(session_is_valid)
    session = issue_session(secret, now=100)

    assert session_is_valid(session, secret, now=43_299)
    assert not session_is_valid(session, secret, now=43_300)
    assert not session_is_valid(f"{session}x", secret, now=101)
    assert not session_is_valid(f"{session}=", secret, now=101)
    assert not session_is_valid("not-a-session", secret, now=101)


def test_api_without_session_returns_unauthorized_json(client: TestClient) -> None:
    response = client.get("/api/jobs/unknown")

    assert response.status_code == 401
    assert response.json() == {"detail": "Richiesta non autorizzata"}


def test_logout_with_csrf_invalidates_the_session_cookie(client: TestClient) -> None:
    login(client)

    response = client.post("/logout", data={"csrf_token": client.app.state.csrf_token},
                           follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert f"{SESSION_COOKIE}=\"\"" in response.headers["set-cookie"]
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_login_marks_cookie_secure_for_forwarded_https(client: TestClient) -> None:
    response = client.post("/login", data={
        "username": "team", "password": "team-secret",
        "csrf_token": client.app.state.csrf_token,
    }, headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)

    assert "Secure" in response.headers["set-cookie"]
