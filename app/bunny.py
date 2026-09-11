"""Read-only Bunny Stream metadata and validated, freshly signed playback URLs.

Input URLs are used only to identify a video. Never log input URLs, playback
URLs, credentials, response bodies, or raw transport/validation exceptions.
"""

import base64
import hashlib
import hmac
import re
import time
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.logging_config import log_event


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
            metadata = BunnyVideoMetadata(
                video_id=payload["guid"],
                title=payload["title"],
                duration_seconds=payload["length"],
                captions=payload.get("captions", []),
                chapters=payload.get("chapters", []),
            )
        except (ValueError, KeyError, TypeError, ValidationError):
            raise BunnyResponseError("Metadati Bunny non validi", status_code=status) from None
        if metadata.video_id != validated_id:
            raise BunnyResponseError("I metadati Bunny non corrispondono al video richiesto", status_code=status)
        return metadata

    def build_hls_url(self, video_id: str | UUID) -> str:
        """Regenerate playback exclusively from configured host, key, and video ID."""
        validated_id = _video_uuid(video_id)
        hostname = _cdn_hostname(self._settings.bunny_cdn_hostname)
        key = self._settings.bunny_token_auth_key
        if key:
            return build_cdn_token_url(
                hostname=hostname,
                video_id=validated_id,
                key=key,
                expires=int(time.time()) + 3600,
            )
        return f"https://{hostname}/{validated_id}/playlist.m3u8"
