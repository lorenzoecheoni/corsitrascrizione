"""Exercise the proxy configuration actually shipped in the container command."""

import json
from pathlib import Path

from click.core import ParameterSource
from fastapi.testclient import TestClient
import pytest
from uvicorn import Config
from uvicorn.main import main as uvicorn_command

from app.auth import SESSION_COOKIE
from app.config import Settings
from app.main import create_app


@pytest.fixture
def container_proxy(monkeypatch):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    command = json.loads(next(line[4:] for line in dockerfile.read_text().splitlines()
                              if line.startswith("CMD [")))
    assert command[0] == "uvicorn"
    with uvicorn_command.make_context("uvicorn", command[1:]) as context:
        options = context.params
        proxy_source = context.get_parameter_source("proxy_headers")
    app = create_app(Settings(
        bunny_library_id=123, bunny_stream_api_key="bunny-secret",
        bunny_cdn_hostname="cdn.example.com", openai_api_key="openai-secret",
        app_password="team-secret", _env_file=None,
    ))
    config = Config(app, proxy_headers=options["proxy_headers"],
                    forwarded_allow_ips=options["forwarded_allow_ips"],
                    log_config=None, ws="none")
    config.load()
    try:
        yield app, config, proxy_source
    finally:
        app.state.runner.shutdown(wait=True)


def test_container_explicitly_enables_proxy_headers_with_bounded_allowlist(container_proxy):
    _, config, proxy_source = container_proxy
    assert proxy_source is ParameterSource.COMMANDLINE
    assert config.proxy_headers is True
    assert config.forwarded_allow_ips.split(",") == ["127.0.0.1", "100.0.0.0/8"]


@pytest.mark.parametrize("peer", ["127.0.0.1", "100.64.0.42", "100.255.0.1"])
def test_container_accepts_valid_csrf_with_nonstandard_origin(container_proxy, peer):
    app, config, _ = container_proxy
    with TestClient(config.loaded_app, base_url="http://reports.example", client=(peer, 12345)) as browser:
        data = {"username": "team", "password": "team-secret", "csrf_token": app.state.csrf_token}
        headers = {"X-Forwarded-Proto": "https", "Origin": "https://reports.example"}
        response = browser.post("/login", data=data, headers=headers, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert "Secure" in response.headers["set-cookie"]
        headers["Cookie"] = f"{SESSION_COOKIE}={response.cookies[SESSION_COOKIE]}"
        headers["Origin"] = "https://attacker.example"
        assert browser.post("/logout", data=data, headers=headers, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("peer", ["192.0.2.1", "99.255.255.254", "101.0.0.1"])
def test_container_still_requires_csrf_with_spoofed_forwarded_headers(container_proxy, peer):
    app, config, _ = container_proxy
    with TestClient(config.loaded_app, base_url="http://reports.example", client=(peer, 12345)) as browser:
        data = {
            "username": "team", "password": "team-secret", "csrf_token": app.state.csrf_token,
        }
        headers = {"X-Forwarded-Proto": "https", "Origin": "https://reports.example"}
        response = browser.post("/login", data=data, headers=headers, follow_redirects=False)
        assert response.status_code == 303
        headers["Cookie"] = f"{SESSION_COOKIE}={response.cookies[SESSION_COOKIE]}"
        response = browser.post("/logout", data={}, headers=headers, follow_redirects=False)
        assert response.status_code == 403
