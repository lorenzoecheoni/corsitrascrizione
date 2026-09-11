"""Read-only Bunny Stream metadata and validated, freshly signed playback URLs.

Input URLs are used only to identify a video. Never log input URLs, playback
URLs, credentials, response bodies, or raw transport/validation exceptions.
"""

import base64
import hashlib
import hmac
import re
import time
from urllib.parse import quote, unquote, urlsplit, urljoin
from threading import Event, Lock
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, ValidationError

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


def read_metadata(client: "BunnyClient", video_id: str, event: Event | None = None) -> BunnyVideoMetadata:
    metadata = retry_remote(lambda: client.get_metadata(video_id),
        retryable=lambda exc: isinstance(exc, BunnyError) and exc.retryable,
        cancellation_event=event)
    metadata.require_ready()
    return metadata


_UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_EMBED_HOSTS = {"iframe.mediadelivery.net", "player.mediadelivery.net"}


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

    def get_metadata(self, video_id: str | UUID) -> BunnyVideoMetadata:
        """Perform one GET. Errors contain no raw upstream data or credentials."""
        validated_id = _video_uuid(video_id)
        library_id = self._settings.bunny_library_id
        if library_id <= 0:
            raise BunnyUrlError("Identificativo libreria Bunny non valido")
        url = f"https://video.bunnycdn.com/library/{library_id}/videos/{validated_id}"
        started = time.monotonic()
        try:
            # Do not forward AccessKey through redirects or environment proxies.
            with httpx.Client(timeout=30.0, follow_redirects=False, trust_env=False) as client:
                response = client.get(url, headers={"AccessKey": self._settings.bunny_stream_api_key})
        except httpx.TimeoutException:
            log_event("metadata", elapsed_seconds=time.monotonic() - started, attempt=1, error_code="timeout")
            raise BunnyTimeoutError("Bunny non ha risposto entro il tempo previsto") from None
        except httpx.RequestError:
            log_event("metadata", elapsed_seconds=time.monotonic() - started, attempt=1, error_code="transport")
            raise BunnyTransportError("Impossibile contattare Bunny; riprova più tardi") from None

        status = response.status_code
        log_event("metadata", elapsed_seconds=time.monotonic() - started, attempt=1,
                  status_code=status, error_code="ok" if status == 200 else "remote_response")
        if status in (401, 403):
            raise BunnyAuthError("Accesso a Bunny non autorizzato; verifica la configurazione", status_code=status)
        if status == 404:
            raise BunnyNotFoundError("Video non trovato nella libreria Bunny", status_code=status)
        if status == 429:
            raise BunnyRateLimitError("Limite di richieste Bunny raggiunto; riprova più tardi", status_code=status)
        if 500 <= status <= 599:
            raise BunnyServerError("Servizio Bunny temporaneamente non disponibile", status_code=status)
        if status != 200:
            raise BunnyResponseError("Risposta Bunny inattesa durante la lettura del video", status_code=status)

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
