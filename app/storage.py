"""Optional Bunny Storage hosting for verified course decks.

When configured, the analysis pipeline uploads each fetched deck to the
operator's storage zone and persists the pull-zone CDN URL in the report, so
the Academy platform imports materials from our own stable host instead of
the original source link.
"""

import http.client
from pathlib import Path
import re
from typing import Callable
import unicodedata

from app.config import parse_material_allowed_hosts


class BunnyStorageError(Exception):
    """Safe operational failure: the deck keeps its original source URL."""


def slugify(value: str) -> str:
    """Deterministic slug: accents folded, apostrophes dropped, lowercase."""
    folded = "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )
    folded = folded.replace("'", "").replace("’", "").lower()
    return "-".join(re.findall(r"[a-z0-9]+", folded))


_REGIONS = frozenset({"de", "ny", "la", "sg", "syd", "uk", "se", "br", "jh"})
_ZONE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_KEY_PART = re.compile(r"[a-z0-9](?:[a-z0-9._~-]{0,118}[a-z0-9])?|[a-z0-9]")
MAX_UPLOAD_BYTES = 62_914_560  # 60 MiB: the Academy import ceiling.


class BunnyStorageClient:
    """Uploads verified decks to a Bunny Storage zone behind its pull zone.

    Stateless across calls: a fresh connection per upload keeps the shared
    instance safe for the single-worker pipeline.
    """

    def __init__(
        self, zone: str, api_key: str, pull_hostname: str, region: str = "de",
        *, timeout: float = 60.0, connection_factory: Callable | None = None,
    ) -> None:
        if not _ZONE.fullmatch(zone):
            raise ValueError("Nome della storage zone non valido")
        hosts = parse_material_allowed_hosts([pull_hostname])
        if len(hosts) != 1:
            raise ValueError("Hostname CDN della pull zone non valido")
        if region not in _REGIONS:
            raise ValueError("Regione Bunny Storage non valida")
        if not api_key or not api_key.strip():
            raise ValueError("Chiave API della storage zone mancante")
        self.zone = zone
        self._api_key = api_key
        self.pull_hostname = hosts[0]
        self.region = region
        self.timeout = timeout
        self._factory = connection_factory or http.client.HTTPSConnection

    @property
    def endpoint(self) -> str:
        if self.region == "de":
            return "storage.bunnycdn.com"
        return f"{self.region}.storage.bunnycdn.com"

    def cdn_url(self, key: str) -> str:
        return f"https://{self.pull_hostname}/{key}"

    def upload_file(self, source: Path, key: str) -> str:
        """Upload one deck under ``key`` and return its public CDN URL."""
        parts = key.split("/")
        if len(parts) < 2 or any(
            not _KEY_PART.fullmatch(part) or part in {".", ".."} for part in parts
        ):
            raise ValueError("Chiave storage non valida")
        try:
            size = source.stat().st_size
        except OSError:
            raise BunnyStorageError() from None
        if size > MAX_UPLOAD_BYTES:
            raise BunnyStorageError()
        connection = self._factory(self.endpoint, 443, timeout=self.timeout)
        try:
            with source.open("rb") as body:
                connection.request("PUT", f"/{self.zone}/{key}", body, {
                    "AccessKey": self._api_key,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                })
                response = connection.getresponse()
                response.read()
            if response.status not in (200, 201):
                raise BunnyStorageError()
        except OSError:
            raise BunnyStorageError() from None
        finally:
            connection.close()
        return self.cdn_url(key)


__all__ = ["BunnyStorageClient", "BunnyStorageError", "MAX_UPLOAD_BYTES", "slugify"]
