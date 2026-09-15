"""Curated and inventory-declared material sources.

This module resolves provenance only. Deck downloading and page inspection are
performed later inside the job workspace.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from app.academy_registry import find_registry_person, parse_speaker_identity


GOVERNANCE_GUID = UUID("7f254c4d-fe34-4fd3-a4cf-cda4f447e438")
CURATED_MATERIALS = {
    GOVERNANCE_GUID: (
        "Slide · Furio D'Andrea | https://www.assoholding.it/wp-content/uploads/2026/07/19072026_PP-Avv.-Furio-DAndrea_Webinar-22-luglio-2026.pptx",
        "Slide · Luigi Morra | https://www.assoholding.it/wp-content/uploads/2026/07/Slide-Morra-Conferimenti-1.pptx",
    ),
}

_HTTP_URL = re.compile(r"https?://[^\s|]+", re.IGNORECASE)
_SLIDE_TITLE = re.compile(r"^\s*slide(?:\s*[\u00b7:\-–—]\s*|\s+)(?P<person>.+?)\s*$", re.I)
_SUPPORTED_LOCAL_EXTENSIONS = {".pdf", ".pptx"}


@dataclass(frozen=True)
class AnalysisInventoryContext:
    speaker_hints: tuple[str, ...]
    material_sources: tuple[str, ...]


@dataclass(frozen=True)
class RegistryMaterial:
    """One resolved source with a platform-facing title."""

    title: str
    url: str | None = None
    file: str | None = None

    def __post_init__(self) -> None:
        if (self.url is None) == (self.file is None):
            raise ValueError("un materiale richiede esattamente una sorgente")
        title = self.title.strip()
        if not title:
            raise ValueError("un materiale richiede un titolo")
        object.__setattr__(self, "title", title)

    def as_inventory_source(self) -> str:
        return f"{self.title} | {self.url}" if self.url is not None else str(self.file)


def canonical_material_title(value: str) -> str:
    """Canonicalize known speaker slide labels without changing unknown titles."""
    title = value.strip()
    match = _SLIDE_TITLE.fullmatch(title)
    if match is None:
        return title
    parsed = parse_speaker_identity(match.group("person"))
    person = find_registry_person(parsed.name)
    return f"Slide · {person.nome}" if person is not None else title


def _valid_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        parsed.port  # validates a declared port before normalization
        return (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def _url_key(value: str) -> str:
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None and not (
        parsed.scheme.lower() == "http" and port == 80
        or parsed.scheme.lower() == "https" and port == 443
    ):
        hostname = f"{hostname}:{port}"
    return urlunsplit((parsed.scheme.lower(), hostname, parsed.path, parsed.query, ""))


def _declared_material(
    value: str,
    *,
    file_exists: Callable[[Path], bool],
) -> RegistryMaterial | None:
    source = value.strip()
    if not source:
        return None
    match = _HTTP_URL.search(source)
    if match is not None:
        url = match.group().rstrip(").,")
        if not _valid_http_url(url):
            return None
        title = re.sub(r"[\s|\-]+$", "", source[:match.start()])
        if not title:
            title = Path(urlsplit(url).path).name or url
        return RegistryMaterial(canonical_material_title(title), url=url)

    path = Path(source)
    if path.suffix.lower() not in _SUPPORTED_LOCAL_EXTENSIONS:
        return None
    try:
        exists = file_exists(path)
    except (OSError, ValueError):
        exists = False
    if not exists:
        return None
    return RegistryMaterial(path.stem, file=str(path))


def resolve_material_sources(
    declared_sources: Iterable[str],
    video_id: UUID,
    *,
    url_reachable: Callable[[str], bool] | None = None,
    file_exists: Callable[[Path], bool] = Path.is_file,
) -> list[str]:
    """Return curated sources first, then explicit reachable declarations.

    Curated URLs have already been verified when entered in the local registry.
    Callers may inject a fresh reachability policy; when supplied it is applied
    to every URL and failures are omitted conservatively.
    """
    candidates = (*CURATED_MATERIALS.get(video_id, ()), *declared_sources)
    sources: list[str] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        material = _declared_material(candidate, file_exists=file_exists)
        if material is None:
            continue
        if material.url is not None:
            if url_reachable is not None:
                try:
                    if not url_reachable(material.url):
                        continue
                except Exception:
                    continue
            key = ("url", _url_key(material.url))
        else:
            key = ("file", str(Path(material.file or "").resolve(strict=False)))
        if key in seen:
            continue
        seen.add(key)
        sources.append(material.as_inventory_source())
    return sources


__all__ = [
    "AnalysisInventoryContext", "CURATED_MATERIALS", "GOVERNANCE_GUID",
    "RegistryMaterial", "canonical_material_title", "resolve_material_sources",
]
