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
OTHER_VIDEO_ID = "66666666-7777-8888-9999-aaaaaaaaaaaa"
CATALOG_URL = "https://video.bunnycdn.com/library/123/videos"


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


def catalog_item(video_id: str, **overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "guid": video_id,
        "title": f"Corso {video_id[:8]}",
        "length": 120.5,
        "status": 4,
        "description": "Descrizione catalogo",
        "dateUploaded": "2026-09-11T10:30:00Z",
        "collectionId": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        "thumbnailFileName": "thumbnail.jpg",
    }
    item.update(overrides)
    return item


def catalog_page(items: list[dict[str, object]], *, total: int, page: int, per_page: int = 100) -> dict[str, object]:
    return {
        "totalItems": total,
        "currentPage": page,
        "itemsPerPage": per_page,
        "items": items,
    }


@respx.mock
def test_list_videos_reads_every_page_once_in_provider_order(settings, monkeypatch) -> None:
    created_clients: list[dict[str, object]] = []
    real_client = httpx.Client

    def observing_client(*args, **kwargs):
        created_clients.append(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("app.bunny.httpx.Client", observing_client)
    first = respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(
        200, json=catalog_page([catalog_item(VIDEO_ID)], total=2, page=1)
    )
    second = respx.get(CATALOG_URL, params={"page": 2, "itemsPerPage": 100}).respond(
        200, json=catalog_page([catalog_item(OTHER_VIDEO_ID)], total=2, page=2)
    )

    result = BunnyClient(settings).list_videos()

    assert [str(video.video_id) for video in result.videos] == [VIDEO_ID, OTHER_VIDEO_ID]
    assert result.total_items == 2
    assert first.call_count == second.call_count == 1
    assert len(respx.calls) == 2
    assert all(call.request.headers["AccessKey"] == "bunny-secret" for call in respx.calls)
    assert created_clients == [{"timeout": 30.0, "follow_redirects": False, "trust_env": False}] * 2


@respx.mock
def test_list_videos_returns_an_empty_catalog_after_its_first_page(settings) -> None:
    route = respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(
        200, json=catalog_page([], total=0, page=1)
    )

    result = BunnyClient(settings).list_videos()

    assert result.videos == []
    assert result.total_items == 0
    assert route.call_count == len(respx.calls) == 1


@respx.mock
@pytest.mark.parametrize("payload", [
    catalog_page([catalog_item(VIDEO_ID)], total=2, page=2),
    catalog_page([catalog_item(VIDEO_ID)], total=2, page=1, per_page=-1),
    catalog_page([catalog_item(VIDEO_ID)], total=-1, page=1),
    catalog_page([catalog_item(VIDEO_ID)], total=0, page=1),
    {"totalItems": 1, "currentPage": 1, "itemsPerPage": 100, "items": "not-a-list"},
])
def test_list_videos_rejects_incoherent_page_counters_and_items(settings, payload) -> None:
    respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(200, json=payload)

    with pytest.raises(BunnyResponseError):
        BunnyClient(settings).list_videos()


@respx.mock
def test_list_videos_rejects_duplicate_ids_across_pages(settings) -> None:
    respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(
        200, json=catalog_page([catalog_item(VIDEO_ID)], total=2, page=1)
    )
    respx.get(CATALOG_URL, params={"page": 2, "itemsPerPage": 100}).respond(
        200, json=catalog_page([catalog_item(VIDEO_ID)], total=2, page=2)
    )

    with pytest.raises(BunnyResponseError) as caught:
        BunnyClient(settings).list_videos()

    assert "11111111" not in str(caught.value)


@respx.mock
def test_list_videos_rejects_catalogues_over_the_operational_limit(settings) -> None:
    route = respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(
        200, json=catalog_page([], total=10_001, page=1)
    )

    with pytest.raises(BunnyResponseError, match="Catalogo Bunny troppo grande; usa la paginazione"):
        BunnyClient(settings).list_videos()

    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize("overrides", [
    {"guid": "not-a-uuid"},
    {"length": -0.01},
    {"length": "NaN"},
    {"dateUploaded": "not-a-date"},
    {"thumbnailFileName": "../secret.jpg"},
])
def test_list_videos_redacts_invalid_provider_item_fields(settings, overrides, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    payload = catalog_page([catalog_item(VIDEO_ID, **overrides)], total=1, page=1)
    respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(200, json=payload)

    with pytest.raises(BunnyResponseError) as caught:
        BunnyClient(settings).list_videos()

    rendered = str(caught.value) + caplog.text
    assert "not-a-uuid" not in rendered
    assert "secret.jpg" not in rendered


@respx.mock
@pytest.mark.parametrize("status, error_type, retryable", [
    (401, BunnyAuthError, False),
    (403, BunnyAuthError, False),
    (429, BunnyRateLimitError, True),
    (500, BunnyServerError, True),
])
def test_list_videos_classifies_http_failures_without_leaking_provider_data(settings, status, error_type, retryable, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(
        status, text="bunny-secret upstream-body ?token=never-echo-this"
    )

    with pytest.raises(error_type) as caught:
        BunnyClient(settings).list_videos()

    assert caught.value.retryable is retryable
    assert not any(secret in str(caught.value) + caplog.text for secret in ["bunny-secret", "upstream-body", "never-echo-this"])


@respx.mock
def test_list_videos_redacts_timeout_responses(settings) -> None:
    respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).mock(
        side_effect=httpx.ReadTimeout("bunny-secret upstream-body")
    )
    with pytest.raises(BunnyTimeoutError) as timeout:
        BunnyClient(settings).list_videos()
    assert "bunny-secret" not in str(timeout.value)


@respx.mock
def test_list_videos_redacts_non_json_responses(settings) -> None:
    respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(200, text="bunny-secret upstream-body")
    with pytest.raises(BunnyResponseError) as non_json:
        BunnyClient(settings).list_videos()
    assert "upstream-body" not in str(non_json.value)


@respx.mock
def test_list_videos_does_not_follow_redirects(settings) -> None:
    route = respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(
        302, headers={"Location": "https://evil.example"}
    )
    with pytest.raises(BunnyResponseError):
        BunnyClient(settings).list_videos()
    assert route.call_count == 1


def test_build_thumbnail_url_uses_only_validated_configured_components(settings) -> None:
    assert BunnyClient(settings).build_thumbnail_url(VIDEO_ID, "cover-image_1.jpg") == (
        f"https://{CDN}/{VIDEO_ID}/cover-image_1.jpg"
    )


@pytest.mark.parametrize("filename", ["", ".cover.jpg", "../cover.jpg", "cover/other.jpg", "cover space.jpg", "café.jpg", "a" * 256])
def test_build_thumbnail_url_rejects_untrusted_filename(settings, filename) -> None:
    with pytest.raises(BunnyUrlError) as caught:
        BunnyClient(settings).build_thumbnail_url(VIDEO_ID, filename)
    if filename:
        assert filename not in str(caught.value)


def test_build_thumbnail_url_reuses_directory_token_without_exposing_key(settings, monkeypatch) -> None:
    settings.bunny_token_auth_key = "token-secret"
    monkeypatch.setattr("app.bunny.time.time", lambda: 1_999_996_400)

    url = BunnyClient(settings).build_thumbnail_url(VIDEO_ID, "thumbnail.jpg")

    assert url == (
        f"https://{CDN}/bcdn_token=HS256-ikLY_L4iDZop_kRgJ8YMBA3raTIjo3sjthRORMkkWbY"
        f"&expires=2000000000&token_path=%2F{VIDEO_ID}%2F/{VIDEO_ID}/thumbnail.jpg"
    )
    assert "token-secret" not in url


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
