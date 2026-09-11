"""Read-only Bunny Stream metadata and validated, freshly signed playback URLs.

Input URLs are used only to identify a video. Never log input URLs, playback
URLs, credentials, response bodies, or raw transport/validation exceptions.
"""

import base64
import hashlib
import hmac
import math
import re
import time
from datetime import datetime
from urllib.parse import quote, unquote, urlsplit, urljoin
from threading import Event, Lock
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import Settings
from app.logging_config import log_event
from app.retry import check_cancelled, retry_remote


class BunnyError(Exception):
    """Safe Italian user message, with classification for the shared retry layer."""

    retryable = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BunnyUrlError(BunnyError):
    pass


class BunnyAuthError(BunnyError):
    pass


class BunnyNotFoundError(BunnyError):
    pass


class BunnyResponseError(BunnyError):
    pass


class BunnyCatalogTooLarge(BunnyResponseError):
    """The catalog exceeds the bounded in-memory loading limit."""


class BunnyPlaybackError(BunnyError):
    """Safe access failure eligible for one playback-token regeneration."""


class BunnyReadinessError(BunnyError):
    def __init__(self, code: str):
        self.code = code
        super().__init__({
            "not_ready": "Video ancora in elaborazione su Bunny; attendere la fine della codifica",
            "encoding_failed": "Codifica o caricamento Bunny fallito; verificare il video nella libreria",
            "unsupported_media": "Formato o risoluzioni Bunny non supportati; verificare la codifica HLS",
            "unsupported_duration": "Durata non supportata; il limite è quattro ore",
        }[code])


class BunnyRateLimitError(BunnyError):
    retryable = True


class BunnyServerError(BunnyError):
    retryable = True


class BunnyTimeoutError(BunnyError):
    retryable = True


class BunnyTransportError(BunnyError):
    retryable = True


class BunnyVideoRef(BaseModel):
    library_id: int
    video_id: UUID


class BunnyVideoMetadata(BaseModel):
    video_id: UUID
    title: str
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    captions: list[dict[str, object]] = Field(default_factory=list)
    chapters: list[dict[str, object]] = Field(default_factory=list)
    status: int | None = None
    available_resolutions: list[int] = Field(default_factory=list)
    description: str | None = None

    def require_ready(self) -> None:
        if self.status in {5, 8}:
            raise BunnyReadinessError("encoding_failed")
        if self.status in {0, 1, 2, 6, 7}:
            raise BunnyReadinessError("not_ready")
        if self.status not in {3, 4} or not any(0 < resolution <= 720 for resolution in self.available_resolutions):
            raise BunnyReadinessError("unsupported_media")
        if not 0 < self.duration_seconds <= 14_400:
            raise BunnyReadinessError("unsupported_duration")


class BunnyCatalogVideo(BaseModel):
    """Validated, read-only video data intended for the catalog UI."""

    video_id: UUID
    title: str
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    status: int | None = None
    description: str | None = None
    uploaded_at: datetime | None = None
    collection_id: UUID | None = None
    thumbnail_file_name: str | None = None
    thumbnail_url: str | None = None

    @field_validator("duration_seconds", mode="before")
    @classmethod
    def duration_must_be_a_finite_number(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("duration must be a finite number")
        return value


class BunnyCatalog(BaseModel):
    videos: list[BunnyCatalogVideo]
    total_items: int = Field(ge=0)


def read_metadata(client: "BunnyClient", video_id: str, event: Event | None = None) -> BunnyVideoMetadata:
    metadata = retry_remote(lambda: client.get_metadata(video_id),
        retryable=lambda exc: isinstance(exc, BunnyError) and exc.retryable,
        cancellation_event=event)
    metadata.require_ready()
    return metadata


_UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_THUMBNAIL_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
_EMBED_HOSTS = {"iframe.mediadelivery.net", "player.mediadelivery.net"}
_MAX_CATALOG_ITEMS = 10_000


def _video_uuid(value: str | UUID) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not _UUID_PATTERN.fullmatch(value):
        raise BunnyUrlError("Identificativo video Bunny non valido")
    return UUID(value)


def _cdn_hostname(value: str) -> str:
    # A hostname, not a URL: reject userinfo, ports, paths, and delimiters.
    host = value.lower().removesuffix(".")
    if len(host) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in host.split(".")):
        raise BunnyUrlError("Hostname CDN Bunny non valido")
    return host


def parse_bunny_url(
    url: str, *, expected_library_id: int, cdn_hostname: str
) -> BunnyVideoRef:
    """Accept known embed/direct playlist shapes and discard all query data."""
    configured_host = _cdn_hostname(cdn_hostname)
    if expected_library_id <= 0:
        raise BunnyUrlError("Identificativo libreria Bunny non valido")
    # urlsplit silently strips some control characters; reject before parsing.
    if not isinstance(url, str) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise BunnyUrlError("Il link deve essere un URL HTTPS Bunny valido")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = (parsed.hostname or "").lower().removesuffix(".")
    except ValueError:
        raise BunnyUrlError("Formato URL Bunny non valido") from None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.netloc.endswith(":")
    ):
        raise BunnyUrlError("Il link deve essere un URL HTTPS Bunny valido")

    parts = [unquote(part) for part in parsed.path.split("/")]
    if host in _EMBED_HOSTS and len(parts) == 4 and parts[:2] == ["", "embed"]:
        if not re.fullmatch(r"[0-9]+", parts[2]):
            raise BunnyUrlError("Identificativo libreria Bunny non valido")
        # Comparing strings avoids int conversion limits on untrusted input.
        if parts[2].lstrip("0") != str(expected_library_id):
            raise BunnyUrlError("Il video Bunny appartiene a una libreria diversa")
        video_id = _video_uuid(parts[3])
    elif host == configured_host and len(parts) == 3 and parts[0] == "" and parts[2] == "playlist.m3u8":
        video_id = _video_uuid(parts[1])
    else:
        raise BunnyUrlError("Formato link Bunny non riconosciuto")
    return BunnyVideoRef(library_id=expected_library_id, video_id=video_id)


def build_cdn_token_url(
    *, hostname: str, video_id: str | UUID, key: str, expires: int
) -> str:
    """Create a directory token using the approved HMAC-SHA256 contract.

    The key is the HMAC key, and the message is signature_path + expires.
    The path token segment persists when HLS resolves relative playlists and
    media segments. No IP binding, flags, or extra signing data are used.
    """
    host = _cdn_hostname(hostname)
    path = f"/{_video_uuid(video_id)}/"
    if not key or not isinstance(expires, int) or isinstance(expires, bool) or expires <= 0:
        raise BunnyUrlError("Configurazione token Bunny non valida")
    digest = hmac.digest(key.encode("utf-8"), f"{path}{expires}".encode("utf-8"), hashlib.sha256)
    token = "HS256-" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return (
        f"https://{host}/bcdn_token={token}&expires={expires}"
        f"&token_path={quote(path, safe='')}{path}playlist.m3u8"
    )


class BunnyClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token_lock = Lock()
        self._last_token_expiry = 0

    def _get(self, url: str, *, operation: str, not_found_message: str, unexpected_message: str) -> httpx.Response:
        """Perform one protected GET with the shared safe transport classification."""
        started = time.monotonic()
        try:
            # Do not forward AccessKey through redirects or environment proxies.
            with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
                response = client.get(url, headers={"AccessKey": self._settings.bunny_stream_api_key})
        except httpx.TimeoutException:
            log_event(operation, elapsed_seconds=time.monotonic() - started, attempt=1, error_code="timeout")
            raise BunnyTimeoutError("Bunny non ha risposto entro il tempo previsto") from None
        except httpx.RequestError:
            log_event(operation, elapsed_seconds=time.monotonic() - started, attempt=1, error_code="transport")
            raise BunnyTransportError("Impossibile contattare Bunny; riprova più tardi") from None

        status = response.status_code
        log_event(operation, elapsed_seconds=time.monotonic() - started, attempt=1,
                  status_code=status, error_code="ok" if status == 200 else "remote_response")
        if status in (401, 403):
            raise BunnyAuthError("Accesso a Bunny non autorizzato; verifica la configurazione", status_code=status)
        if status == 404:
            raise BunnyNotFoundError(not_found_message, status_code=status)
        if status == 429:
            raise BunnyRateLimitError("Limite di richieste Bunny raggiunto; riprova più tardi", status_code=status)
        if 500 <= status <= 599:
            raise BunnyServerError("Servizio Bunny temporaneamente non disponibile", status_code=status)
        if status != 200:
            raise BunnyResponseError(unexpected_message, status_code=status)
        return response

    def get_metadata(self, video_id: str | UUID) -> BunnyVideoMetadata:
        """Perform one GET. Errors contain no raw upstream data or credentials."""
        validated_id = _video_uuid(video_id)
        library_id = self._settings.bunny_library_id
        if library_id <= 0:
            raise BunnyUrlError("Identificativo libreria Bunny non valido")
        url = f"https://video.bunnycdn.com/library/{library_id}/videos/{validated_id}"
        response = self._get(
            url,
            operation="metadata",
            not_found_message="Video non trovato nella libreria Bunny",
            unexpected_message="Risposta Bunny inattesa durante la lettura del video",
        )
        status = response.status_code

        try:
            payload = response.json()
            resolutions = payload.get("availableResolutions") or ""
            if not isinstance(resolutions, str):
                raise ValueError
            supported = {240, 360, 480, 720, 1080, 1440, 2160}
            resolutions = sorted({int(item.strip().removesuffix("p")) for item in resolutions.split(",") if item.strip()})
            if not set(resolutions) <= supported:
                raise ValueError
            metadata = BunnyVideoMetadata(
                video_id=payload["guid"],
                title=payload["title"],
                duration_seconds=payload["length"],
                captions=payload.get("captions") or [],
                chapters=payload.get("chapters") or [],
                status=payload.get("status"), available_resolutions=resolutions,
                description=payload.get("description"),
            )
        except (ValueError, KeyError, TypeError, ValidationError):
            raise BunnyResponseError("Metadati Bunny non validi", status_code=status) from None
        if metadata.video_id != validated_id:
            raise BunnyResponseError("I metadati Bunny non corrispondono al video richiesto", status_code=status)
        return metadata

    def list_videos(self, *, max_items: int = 10_000) -> BunnyCatalog:
        """Read every bounded catalog page once, preserving Bunny's provider order."""
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise BunnyResponseError("Limite catalogo Bunny non valido")
        max_items = min(max_items, _MAX_CATALOG_ITEMS)
        library_id = self._settings.bunny_library_id
        if library_id <= 0:
            raise BunnyUrlError("Identificativo libreria Bunny non valido")

        url = f"https://video.bunnycdn.com/library/{library_id}/videos"
        videos: list[BunnyCatalogVideo] = []
        seen_ids: set[UUID] = set()
        expected_total: int | None = None
        page = 1

        while True:
            # Build the query locally so no page parameter can come from untrusted input.
            response = self._get(
                f"{url}?page={page}&itemsPerPage=100",
                operation="catalog",
                not_found_message="Catalogo Bunny non trovato nella libreria",
                unexpected_message="Risposta Bunny inattesa durante la lettura del catalogo",
            )
            try:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError
                total_items = payload["totalItems"]
                current_page = payload["currentPage"]
                items_per_page = payload["itemsPerPage"]
                items = payload["items"]
                if (
                    any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                        for value in (total_items, current_page, items_per_page))
                    or current_page != page
                    or not isinstance(items, list)
                    or len(items) > items_per_page
                ):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise BunnyResponseError("Pagina catalogo Bunny non valida", status_code=response.status_code) from None

            if expected_total is None:
                expected_total = total_items
                if expected_total > max_items:
                    raise BunnyCatalogTooLarge("Catalogo Bunny troppo grande; usa la paginazione", status_code=response.status_code)
            elif total_items != expected_total:
                raise BunnyResponseError("Pagina catalogo Bunny non valida", status_code=response.status_code)

            for item in items:
                try:
                    if not isinstance(item, dict):
                        raise ValueError
                    filename = item.get("thumbnailFileName")
                    video = BunnyCatalogVideo(
                        video_id=item["guid"],
                        title=item["title"],
                        duration_seconds=item["length"],
                        status=item.get("status"),
                        description=item.get("description"),
                        uploaded_at=item.get("dateUploaded"),
                        collection_id=item.get("collectionId"),
                        thumbnail_file_name=filename,
                        thumbnail_url=(self.build_thumbnail_url(item["guid"], filename) if filename is not None else None),
                    )
                except (BunnyUrlError, KeyError, TypeError, ValidationError, ValueError):
                    raise BunnyResponseError("Elementi del catalogo Bunny non validi", status_code=response.status_code) from None
                if video.video_id in seen_ids:
                    raise BunnyResponseError("Pagina catalogo Bunny non valida", status_code=response.status_code)
                seen_ids.add(video.video_id)
                videos.append(video)

            if len(videos) > total_items:
                raise BunnyResponseError("Pagina catalogo Bunny non valida", status_code=response.status_code)
            if len(videos) == total_items:
                return BunnyCatalog(videos=videos, total_items=total_items)
            if not items:
                raise BunnyResponseError("Pagina catalogo Bunny non valida", status_code=response.status_code)
            page += 1

    def select_hls_url(self, metadata: BunnyVideoMetadata, *, cancellation_event: Event | None = None) -> str:
        """Read only the bounded master manifest and choose its lowest variant.

        The media remains a single FFmpeg source read. Resolve relative URIs
        within the same signed video directory; never forward API credentials.
        """
        metadata.require_ready()
        url = self.build_hls_url(metadata.video_id)

        def manifest() -> str:
            check_cancelled(cancellation_event)
            try:
                with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
                    with client.stream("GET", url) as response:
                        if response.status_code in {401, 403}:
                            raise BunnyPlaybackError("Accesso al flusso Bunny negato")
                        if response.status_code == 429 or response.status_code >= 500:
                            raise BunnyServerError("Flusso Bunny temporaneamente non disponibile")
                        if response.status_code != 200:
                            raise BunnyResponseError("Playlist Bunny non disponibile")
                        body = bytearray()
                        for chunk in response.iter_bytes(16384):
                            check_cancelled(cancellation_event)
                            body.extend(chunk)
                            if len(body) > 1_000_000:
                                raise BunnyResponseError("Playlist Bunny troppo grande")
                        return body.decode("utf-8")
            except httpx.TimeoutException:
                raise BunnyTimeoutError("Bunny non ha risposto entro il tempo previsto") from None
            except (httpx.RequestError, UnicodeError):
                raise BunnyResponseError("Playlist Bunny non valida") from None

        playlist = retry_remote(manifest, retryable=lambda exc: isinstance(exc, BunnyError) and exc.retryable,
                                cancellation_event=cancellation_event)
        variants = []
        attributes = None
        base = url.rsplit("/", 1)[0] + "/"
        for line in playlist.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-STREAM-INF:"):
                attributes = line
            elif line and not line.startswith("#") and attributes:
                resolution = re.search(r"(?:[:,])RESOLUTION=\d+x(\d+)(?:,|$)", attributes)
                bandwidth = re.search(r"(?:[:,])BANDWIDTH=(\d+)(?:,|$)", attributes)
                if (resolution and bandwidth and int(resolution[1]) <= 720
                        and int(resolution[1]) in metadata.available_resolutions):
                    candidate = urljoin(base, line)
                    decoded = unquote(line)
                    if (urlsplit(line).scheme or line.startswith("/") or "\\" in decoded
                            or ".." in decoded.split("/") or not candidate.startswith(base)
                            or not urlsplit(candidate).path.endswith(".m3u8")):
                        raise BunnyResponseError("Percorso playlist Bunny non valido")
                    if "AUDIO=" in attributes:
                        raise BunnyReadinessError("unsupported_media")
                    variants.append((int(resolution[1]), int(bandwidth[1]), candidate))
                attributes = None
        if not variants:
            raise BunnyReadinessError("unsupported_media")
        return min(variants)[2]

    def build_hls_url(self, video_id: str | UUID) -> str:
        """Regenerate playback exclusively from configured host, key, and video ID."""
        validated_id = _video_uuid(video_id)
        hostname = _cdn_hostname(self._settings.bunny_cdn_hostname)
        key = self._settings.bunny_token_auth_key
        if key:
            with self._token_lock:
                expires = max(int(time.time()) + 3600, self._last_token_expiry + 1)
                self._last_token_expiry = expires
            return build_cdn_token_url(
                hostname=hostname,
                video_id=validated_id,
                key=key,
                expires=expires,
            )
        return f"https://{hostname}/{validated_id}/playlist.m3u8"

    def build_thumbnail_url(self, video_id: str | UUID, filename: str) -> str:
        """Create a safe thumbnail URL from configured CDN data only."""
        validated_id = _video_uuid(video_id)
        if not isinstance(filename, str) or not _THUMBNAIL_FILENAME.fullmatch(filename) or ".." in filename:
            raise BunnyUrlError("Nome miniatura Bunny non valido")
        hostname = _cdn_hostname(self._settings.bunny_cdn_hostname)
        key = self._settings.bunny_token_auth_key
        if key:
            # Catalog volume must not extend thumbnail TTL or playback expiry.
            expires = int(time.time()) + 3600
            playlist = build_cdn_token_url(
                hostname=hostname,
                video_id=validated_id,
                key=key,
                expires=expires,
            )
            return playlist.removesuffix("playlist.m3u8") + filename
        return f"https://{hostname}/{validated_id}/{filename}"
