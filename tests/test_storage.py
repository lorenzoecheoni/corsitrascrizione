from pathlib import Path

import pytest

from app.config import Settings
from app.storage import BunnyStorageClient, BunnyStorageError, MAX_UPLOAD_BYTES


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def read(self):
        return b"{}"


class FakeConnection:
    def __init__(self, host, port, timeout=None, *, status=201, capture=None, fail=None):
        capture.update({"host": host, "port": port, "timeout": timeout})
        self._status = status
        self._capture = capture
        self._fail = fail

    def request(self, method, path, body, headers):
        self._capture["method"] = method
        self._capture["path"] = path
        self._capture["headers"] = headers
        self._capture["body"] = body.read()
        if self._fail is not None:
            raise self._fail

    def getresponse(self):
        return FakeResponse(self._status)

    def close(self):
        self._capture["closed"] = True


def make_client(capture, status=201, fail=None, region="de"):
    def factory(host, port, timeout=None):
        return FakeConnection(host, port, timeout, status=status, capture=capture, fail=fail)
    return BunnyStorageClient(
        "academy-decks", "storage-key", "academy-decks.b-cdn.net", region,
        connection_factory=factory,
    )


def test_upload_puts_the_deck_and_returns_the_cdn_url(tmp_path):
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"deck-bytes")
    capture = {}

    url = make_client(capture).upload_file(deck, "video-123/slide-furio-dandrea.pptx")

    assert url == "https://academy-decks.b-cdn.net/video-123/slide-furio-dandrea.pptx"
    assert capture["host"] == "storage.bunnycdn.com"
    assert capture["method"] == "PUT"
    assert capture["path"] == "/academy-decks/video-123/slide-furio-dandrea.pptx"
    assert capture["headers"]["AccessKey"] == "storage-key"
    assert capture["headers"]["Content-Length"] == str(len(b"deck-bytes"))
    assert capture["body"] == b"deck-bytes"
    assert capture["closed"] is True


def test_regional_endpoint_is_used_when_configured(tmp_path):
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"x")
    capture = {}

    make_client(capture, region="uk").upload_file(deck, "video-1/a.pdf")

    assert capture["host"] == "uk.storage.bunnycdn.com"


def test_non_success_status_raises_operational_error(tmp_path):
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"x")

    with pytest.raises(BunnyStorageError):
        make_client({}, status=401).upload_file(deck, "video-1/a.pdf")


def test_network_failure_raises_operational_error(tmp_path):
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"x")

    with pytest.raises(BunnyStorageError):
        make_client({}, fail=OSError("PRIVATE-SOCKET-DETAIL")).upload_file(deck, "v/a.pptx")


@pytest.mark.parametrize("key", ["../escape.pptx", "a//b.pptx", "no-prefix.pptx", "a/bad key.pptx"])
def test_unsafe_keys_are_rejected_before_any_connection(tmp_path, key):
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"x")

    with pytest.raises(ValueError):
        make_client({}).upload_file(deck, key)


def test_oversized_deck_is_rejected_before_any_connection(tmp_path, monkeypatch):
    deck = tmp_path / "big.pptx"
    deck.write_bytes(b"xx")
    capture = {}
    client = make_client(capture)
    monkeypatch.setattr("app.storage.MAX_UPLOAD_BYTES", 1)

    with pytest.raises(BunnyStorageError):
        client.upload_file(deck, "video-1/big.pptx")
    assert "host" not in capture


def test_invalid_configuration_is_rejected():
    with pytest.raises(ValueError):
        BunnyStorageClient("bad zone!", "key", "academy-decks.b-cdn.net")
    with pytest.raises(ValueError):
        BunnyStorageClient("zone", "key", "not a host!")
    with pytest.raises(ValueError):
        BunnyStorageClient("zone", "key", "academy-decks.b-cdn.net", "xx")
    with pytest.raises(ValueError):
        BunnyStorageClient("zone", "  ", "academy-decks.b-cdn.net")


def test_settings_parse_storage_variables_and_blank_disables():
    settings = Settings(
        bunny_library_id=1, bunny_stream_api_key="fake", bunny_cdn_hostname="fake",
        openai_api_key="fake", app_password="fake", _env_file=None,
        bunny_storage_zone="academy-decks", bunny_storage_api_key="  ",
        bunny_storage_pull_hostname="Academy-Decks.B-CDN.net", bunny_storage_region="uk",
    )
    assert settings.bunny_storage_zone == "academy-decks"
    assert settings.bunny_storage_api_key is None
    assert settings.bunny_storage_pull_hostname == "academy-decks.b-cdn.net"
    assert settings.bunny_storage_region == "uk"
    with pytest.raises(ValueError):
        Settings(
            bunny_library_id=1, bunny_stream_api_key="fake", bunny_cdn_hostname="fake",
            openai_api_key="fake", app_password="fake", _env_file=None,
            bunny_storage_region="xx",
        )
