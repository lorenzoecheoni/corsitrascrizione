"""Offline contract tests; all keys and token vectors are synthetic test data."""

import logging
import traceback
from urllib.parse import urljoin
from uuid import UUID

import httpx
import pytest
import respx

from app.bunny import (
    BunnyAuthError,
    BunnyClient,
    BunnyError,
    BunnyNotFoundError,
    BunnyRateLimitError,
    BunnyResponseError,
    BunnyServerError,
    BunnyTimeoutError,
    BunnyTransportError,
    BunnyUrlError,
    build_cdn_token_url,
    parse_bunny_url,
)
from app.config import Settings


VIDEO_ID = "11111111-2222-3333-4444-555555555555"
CDN = "academy.example.b-cdn.net"
METADATA_URL = f"https://video.bunnycdn.com/library/123/videos/{VIDEO_ID}"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        bunny_library_id=123,
        bunny_stream_api_key="bunny-secret",
        bunny_cdn_hostname=CDN,
        bunny_token_auth_key=None,
        openai_api_key="openai-secret",
        app_password="team-secret",
    )


def parse(url: str):
    return parse_bunny_url(url, expected_library_id=123, cdn_hostname=CDN)


@pytest.mark.parametrize("host", ["iframe.mediadelivery.net", "player.mediadelivery.net"])
def test_parse_embed_link(host: str) -> None:
    ref = parse(f"https://{host}/embed/123/{VIDEO_ID}")
    assert ref.video_id == UUID(VIDEO_ID)
    assert ref.library_id == 123


def test_direct_cdn_query_auth_is_not_retained(settings) -> None:
    ref = parse(f"https://{CDN}/{VIDEO_ID}/playlist.m3u8?token=discard-me&expires=1")
    assert ref.model_dump() == {"library_id": 123, "video_id": UUID(VIDEO_ID)}
    assert BunnyClient(settings).build_hls_url(ref.video_id) == (
        f"https://{CDN}/{VIDEO_ID}/playlist.m3u8"
    )


def test_https_default_port_and_case_insensitive_host_are_accepted() -> None:
    assert parse(f"https://IFRAME.MEDIADELIVERY.NET:443/embed/123/{VIDEO_ID}").video_id == UUID(VIDEO_ID)


@pytest.mark.parametrize("url", [
    f"http://iframe.mediadelivery.net/embed/123/{VIDEO_ID}",
    f"https://evil.example/embed/123/{VIDEO_ID}",
    f"https://iframe.mediadelivery.net.evil.example/embed/123/{VIDEO_ID}",
    f"https://{CDN}.evil.example/{VIDEO_ID}/playlist.m3u8",
    f"https://user:password@iframe.mediadelivery.net/embed/123/{VIDEO_ID}",
    f"https://@iframe.mediadelivery.net/embed/123/{VIDEO_ID}",
    f"https://iframe.mediadelivery.net:80/embed/123/{VIDEO_ID}",
    f"https://iframe.mediadelivery.net:invalid/embed/123/{VIDEO_ID}",
    f"https://iframe.mediadelivery.net:65536/embed/123/{VIDEO_ID}",
    f"https://iframe.mediadelivery.net/embed/999/{VIDEO_ID}",
    "https://iframe.mediadelivery.net/embed/123/not-a-uuid",
    "https://iframe.mediadelivery.net/embed/123/11111111222233334444555555555555",
    f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}/extra",
    f"https://iframe.mediadelivery.net//embed/123/{VIDEO_ID}",
    f"https://{CDN}/{VIDEO_ID}/other.m3u8",
    f"https://{CDN}/%2F{VIDEO_ID}/playlist.m3u8",
    f"https://iframe.mediadelivery.net\n/embed/123/{VIDEO_ID}",
    "https://[broken/embed/123/no-id",
])
def test_rejects_untrusted_or_malformed_urls_without_echoing_input(url: str) -> None:
    with pytest.raises(BunnyUrlError) as caught:
        parse(url + "?token=never-echo-this")
    assert not caught.value.retryable
    assert "never-echo-this" not in str(caught.value)
    assert url not in str(caught.value)


def test_tokenized_url_has_stable_hmac_signature_and_directory_scope() -> None:
    # Known-answer vector from synthetic key "token-secret", never a real credential.
    url = build_cdn_token_url(hostname=CDN, video_id=VIDEO_ID, key="token-secret", expires=2_000_000_000)
    prefix = (
        f"https://{CDN}/bcdn_token=HS256-"
        "ikLY_L4iDZop_kRgJ8YMBA3raTIjo3sjthRORMkkWbY"
        f"&expires=2000000000&token_path=%2F{VIDEO_ID}%2F"
    )
    assert url == f"{prefix}/{VIDEO_ID}/playlist.m3u8"
    assert urljoin(url, "720p/video.m3u8") == f"{prefix}/{VIDEO_ID}/720p/video.m3u8"
    assert urljoin(urljoin(url, "720p/video.m3u8"), "segment.ts") == f"{prefix}/{VIDEO_ID}/720p/segment.ts"


def test_protected_playback_uses_configured_key_and_expiry(settings, monkeypatch) -> None:
    settings.bunny_token_auth_key = "token-secret"
    monkeypatch.setattr("app.bunny.time.time", lambda: 1_999_996_400)
    url = BunnyClient(settings).build_hls_url(UUID(VIDEO_ID))
    assert "bcdn_token=HS256-ikLY_L4iDZop_kRgJ8YMBA3raTIjo3sjthRORMkkWbY&expires=2000000000" in url
    assert "token-secret" not in url


@pytest.mark.parametrize("hostname", ["evil.example/path", "user@evil.example", "https://evil.example", "evil.example?token=x", "evil.example:8443", ""])
def test_playback_rejects_malformed_configured_hostname(hostname, settings) -> None:
    settings.bunny_cdn_hostname = hostname
    with pytest.raises(BunnyUrlError):
        BunnyClient(settings).build_hls_url(VIDEO_ID)
    with pytest.raises(BunnyUrlError):
        build_cdn_token_url(hostname=hostname, video_id=VIDEO_ID, key="token-secret", expires=2_000_000_000)


@respx.mock
def test_get_metadata_is_read_only_and_uses_access_key(settings) -> None:
    route = respx.get(METADATA_URL).respond(200, json={
        "guid": VIDEO_ID, "title": "Corso", "length": 7200, "status": 4,
        "availableResolutions": "240p,480p,720p", "description": "Descrizione originale",
        "captions": [{"srclang": "it", "label": "Italiano"}],
        "chapters": [{"title": "Inizio", "start": 0, "end": 120}],
    })
    metadata = BunnyClient(settings).get_metadata(VIDEO_ID)
    assert metadata.video_id == UUID(VIDEO_ID)
    assert metadata.title == "Corso"
    assert metadata.duration_seconds == 7200
    assert metadata.status == 4
    assert metadata.available_resolutions == [240, 480, 720]
    assert metadata.description == "Descrizione originale"
    assert metadata.captions == [{"srclang": "it", "label": "Italiano"}]
    assert metadata.chapters == [{"title": "Inizio", "start": 0, "end": 120}]
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["AccessKey"] == "bunny-secret"
    assert all(value == 30 for value in request.extensions["timeout"].values())


@respx.mock
def test_missing_optional_metadata_defaults_to_empty_collections(settings) -> None:
    respx.get(METADATA_URL).respond(200, json={"guid": VIDEO_ID, "title": "Corso", "length": 0})
    metadata = BunnyClient(settings).get_metadata(UUID(VIDEO_ID))
    assert metadata.captions == []
    assert metadata.chapters == []
    assert metadata.duration_seconds == 0


@respx.mock
def test_selects_lowest_available_hls_variant_on_configured_cdn(settings):
    from app.bunny import BunnyVideoMetadata
    client = BunnyClient(settings)
    metadata = BunnyVideoMetadata(video_id=VIDEO_ID, title="Corso", duration_seconds=60,
                                  status=4, available_resolutions=[240, 720])
    master = client.build_hls_url(VIDEO_ID)
    respx.get(master).respond(200, text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720\n720p/video.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=600000,RESOLUTION=352x240\n240p/video.m3u8\n")
    url = client.select_hls_url(metadata)
    assert url == master.removesuffix("playlist.m3u8") + "240p/video.m3u8"


@respx.mock
@pytest.mark.parametrize("uri", ["https://untrusted.invalid/video.m3u8", "../../other.m3u8", "/other/video.m3u8"])
def test_hls_selection_rejects_manifest_uri_outside_signed_video_directory(settings, uri):
    from app.bunny import BunnyVideoMetadata
    client = BunnyClient(settings)
    metadata = BunnyVideoMetadata(video_id=VIDEO_ID, title="Corso", duration_seconds=60,
                                  status=4, available_resolutions=[240])
    respx.get(client.build_hls_url(VIDEO_ID)).respond(200, text=f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=600000,RESOLUTION=352x240\n{uri}\n")
    with pytest.raises(BunnyError):
        client.select_hls_url(metadata)


def test_regenerated_token_is_distinct_even_within_same_second(settings, monkeypatch):
    settings.bunny_token_auth_key = "TEST_ONLY_TOKEN"
    monkeypatch.setattr("app.bunny.time.time", lambda: 2_000_000_000)
    client = BunnyClient(settings)
    first, second = client.build_hls_url(VIDEO_ID), client.build_hls_url(VIDEO_ID)
    assert bool(first != second), "Regeneration must issue a fresh token"


@respx.mock
def test_playback_manifest_access_failure_is_safe_and_not_generically_retried(settings):
    from app.bunny import BunnyPlaybackError, BunnyVideoMetadata
    metadata = BunnyVideoMetadata(video_id=VIDEO_ID, title="Corso", duration_seconds=60,
                                  status=3, available_resolutions=[240])
    client = BunnyClient(settings)
    route = respx.get(client.build_hls_url(VIDEO_ID)).respond(403, text="TEST_ONLY_UPSTREAM_BODY")
    with pytest.raises(BunnyPlaybackError) as caught:
        client.select_hls_url(metadata)
    assert route.call_count == 1
    assert "UPSTREAM" not in str(caught.value) and "https://" not in str(caught.value)


@pytest.mark.parametrize("status, error_type, retryable", [
    (400, BunnyResponseError, False),
    (401, BunnyAuthError, False),
    (403, BunnyAuthError, False),
    (404, BunnyNotFoundError, False),
    (429, BunnyRateLimitError, True),
    (500, BunnyServerError, True),
    (503, BunnyServerError, True),
])
@respx.mock
def test_http_errors_are_classified_without_retrying_or_leaking_secrets(settings, status, error_type, retryable, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    route = respx.get(METADATA_URL).respond(status, text="bunny-secret token-secret ?token=never-echo-this")
    with pytest.raises(error_type) as caught:
        BunnyClient(settings).get_metadata(VIDEO_ID)
    assert isinstance(caught.value, BunnyError)
    assert caught.value.retryable is retryable
    assert caught.value.status_code == status
    assert route.call_count == 1
    text = str(caught.value) + repr(caught.value) + caplog.text
    assert "Bunny" in str(caught.value)
    assert not any(secret in text for secret in ["bunny-secret", "token-secret", "never-echo-this"])


@pytest.mark.parametrize("transport_error, expected_error", [
    (httpx.ReadTimeout, BunnyTimeoutError),
    (httpx.ConnectTimeout, BunnyTimeoutError),
    (httpx.ConnectError, BunnyTransportError),
])
@respx.mock
def test_transport_errors_are_redacted_and_retryable(settings, transport_error, expected_error) -> None:
    respx.get(METADATA_URL).mock(side_effect=transport_error("bunny-secret ?token=never-echo-this"))
    with pytest.raises(expected_error) as caught:
        BunnyClient(settings).get_metadata(VIDEO_ID)
    assert caught.value.retryable
    rendered = "".join(traceback.format_exception(caught.value))
    assert "bunny-secret ?token=never-echo-this" not in rendered
    assert "never-echo-this" not in str(caught.value)


@respx.mock
def test_redirects_are_not_followed_with_the_access_key(settings) -> None:
    route = respx.get(METADATA_URL).respond(302, headers={"Location": "https://evil.example"})
    with pytest.raises(BunnyResponseError):
        BunnyClient(settings).get_metadata(VIDEO_ID)
    assert route.call_count == len(respx.calls) == 1


@pytest.mark.parametrize("payload", [
    {},
    {"guid": "aaaaaaaa-2222-3333-4444-555555555555", "title": "Corso", "length": 10},
    {"guid": VIDEO_ID, "title": "Corso", "length": -1},
    {"guid": VIDEO_ID, "title": "Corso", "length": "NaN"},
    {"guid": VIDEO_ID, "title": "Corso", "length": "bunny-secret"},
    {"guid": VIDEO_ID, "title": "Corso", "length": 1, "captions": "token-secret"},
])
@respx.mock
def test_invalid_metadata_is_redacted_and_not_retryable(settings, payload) -> None:
    respx.get(METADATA_URL).respond(200, json=payload)
    with pytest.raises(BunnyResponseError) as caught:
        BunnyClient(settings).get_metadata(VIDEO_ID)
    assert not caught.value.retryable
    assert "bunny-secret" not in str(caught.value)
    assert "token-secret" not in str(caught.value)


@respx.mock
def test_non_json_metadata_is_a_redacted_response_error(settings) -> None:
    respx.get(METADATA_URL).respond(200, text="bunny-secret")
    with pytest.raises(BunnyResponseError):
        BunnyClient(settings).get_metadata(VIDEO_ID)


@respx.mock
def test_invalid_video_id_never_makes_a_network_request(settings) -> None:
    with pytest.raises(BunnyUrlError):
        BunnyClient(settings).get_metadata("../videos?token=never-echo-this")
    assert not respx.calls
    with pytest.raises(BunnyUrlError):
        BunnyClient(settings).build_hls_url("../videos?token=never-echo-this")
