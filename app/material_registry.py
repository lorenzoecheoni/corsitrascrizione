"""Curated and inventory-declared material sources.

This module resolves provenance only. Deck downloading and page inspection are
performed later inside the job workspace.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal
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

_EXACT_HTTP_URL = re.compile(r"https?://[^\s<>\"'|]+", re.IGNORECASE)
_EXACT_TITLE = re.compile(r"[^\x00-\x1f<>|]+")
_SLIDE_TITLE = re.compile(r"^\s*slide(?:\s*[\u00b7:\-–—]\s*|\s+)(?P<person>.+?)\s*$", re.I)
_SUPPORTED_LOCAL_EXTENSIONS = {".pdf", ".pptx"}


@dataclass(frozen=True)
class MaterialSourceFailure:
    """Safe, application-authored reason why an inventory cell was rejected."""

    inventory_reference: str
    reason: Literal["dichiarazione_ambigua", "sorgente_reale_assente"]
    code: Literal["MATERIALE_NON_RAGGIUNGIBILE"] = "MATERIALE_NON_RAGGIUNGIBILE"


@dataclass(frozen=True)
class AnalysisInventoryContext:
    speaker_hints: tuple[str, ...]
    material_sources: tuple[str, ...]
    material_failures: tuple[MaterialSourceFailure, ...] = ()


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
    if source.count(" | ") == 1:
        title, url = source.split(" | ", 1)
        if (
            title != title.strip()
            or not _EXACT_TITLE.fullmatch(title)
            or not _EXACT_HTTP_URL.fullmatch(url)
        ):
            return None
        if not _valid_http_url(url):
            return None
        return RegistryMaterial(canonical_material_title(title), url=url)
    if _EXACT_HTTP_URL.fullmatch(source):
        if not _valid_http_url(source):
            return None
        title = Path(urlsplit(source).path).name or source
        return RegistryMaterial(canonical_material_title(title), url=source)

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


def material_declaration_failure_reason(
    value: str,
    *,
    file_exists: Callable[[Path], bool] = Path.is_file,
) -> Literal["dichiarazione_ambigua", "sorgente_reale_assente"] | None:
    """Classify a rejected declaration without retaining its untrusted text."""
    if _declared_material(value, file_exists=file_exists) is not None:
        return None
    source = value.strip()
    if " | " in source or re.search(r"https?://", source, re.I) or "<" in source or ">" in source:
        return "dichiarazione_ambigua"
    return "sorgente_reale_assente"


def is_curated_material_label(video_id: UUID, value: str) -> bool:
    """Return whether a bare label names a curated source for this GUID."""
    candidate = canonical_material_title(value)
    return any(
        candidate == entry.split(" | ", 1)[0]
        for entry in CURATED_MATERIALS.get(video_id, ())
    )


def resolve_material_sources(
    declared_sources: Iterable[str],
    video_id: UUID,
    *,
    file_exists: Callable[[Path], bool] = Path.is_file,
) -> list[str]:
    """Return curated sources first, then explicit source candidates.

    This function performs no network I/O and makes no reachability claim.
    Download policy and promotion to an analyzed material belong to Task 7.
    """
    candidates = (*CURATED_MATERIALS.get(video_id, ()), *declared_sources)
    sources: list[str] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        material = _declared_material(candidate, file_exists=file_exists)
        if material is None:
            continue
        if material.url is not None:
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
    "MaterialSourceFailure", "RegistryMaterial", "canonical_material_title",
    "is_curated_material_label", "material_declaration_failure_reason",
    "resolve_material_sources",
]
