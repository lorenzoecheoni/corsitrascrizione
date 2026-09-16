"""Curated and inventory-declared material sources.

This module resolves provenance only. Deck downloading and page inspection are
performed later inside the job workspace.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata
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

LibraryMatcher = Callable[[str], "Path | None"]

_LIBRARY_STOPWORDS = frozenset({
    "slide", "slides", "dispensa", "materiale", "materiali", "link",
    "e", "di", "del", "dello", "della", "dei", "degli", "delle", "da", "de",
    "con", "per", "il", "lo", "la", "i", "gli", "le", "un", "una",
    "prima", "seconda", "terza", "parte",
})
_DELETE_APOSTROPHES = str.maketrans("", "", "’'ʼ`")
_LIBRARY_TOKEN = re.compile(r"[a-z0-9]+")


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
        return f"{self.title} | {self.url}" if self.url is not None else f"{self.title} | {self.file}"


def canonical_material_title(value: str) -> str:
    """Canonicalize known speaker slide labels without changing unknown titles."""
    title = value.strip()
    match = _SLIDE_TITLE.fullmatch(title)
    if match is None:
        return title
    parsed = parse_speaker_identity(match.group("person"))
    person = find_registry_person(parsed.name)
    return f"Slide · {person.nome}" if person is not None else title


def scan_library_files(directory: Path) -> tuple[Path, ...]:
    """List candidate deck files directly inside the configured library."""
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return ()
    return tuple(
        entry for entry in entries
        if entry.suffix.lower() in _SUPPORTED_LOCAL_EXTENSIONS
        and not entry.name.startswith(".")
        and entry.is_file()
    )


def _library_tokens(text: str) -> list[str]:
    folded = "".join(
        character for character in unicodedata.normalize("NFKD", text.casefold())
        if not unicodedata.combining(character)
    )
    folded = folded.translate(_DELETE_APOSTROPHES)
    return [token for token in _LIBRARY_TOKEN.findall(folded) if token not in _LIBRARY_STOPWORDS]


def match_library_file(label: str, files: Sequence[Path]) -> Path | None:
    """Return the one library deck named like the label; ambiguous means None.

    File names usually carry only the speaker surname, so any shared name
    token qualifies; ranking prefers more tokens, the compact signature, the
    trailing (surname) token, label numbers, then the shortest file name.
    """
    source = label.strip().strip("|").strip()
    if not source:
        return None
    declared = Path(source)
    if declared.suffix.lower() in _SUPPORTED_LOCAL_EXTENSIONS:
        source = declared.stem
    tokens = _library_tokens(source)
    alpha = [token for token in tokens if not token.isdigit()]
    numbers = [token for token in tokens if token.isdigit()]
    required = [token for token in alpha if len(token) > 1]
    if not required:
        return None
    signature = "".join(alpha)
    scored: list[tuple[tuple[int, int, int, int, int], Path]] = []
    for file in files:
        file_tokens = _library_tokens(file.stem)
        if not file_tokens:
            continue
        hits = sum(1 for token in required if token in file_tokens)
        signature_hit = int(len(alpha) > 1 and signature in "".join(file_tokens))
        if hits == 0 and not signature_hit:
            continue
        surname_hit = int(required[-1] in file_tokens)
        numeric_hits = sum(1 for number in numbers if number in file_tokens)
        scored.append(((hits, signature_hit, surname_hit, numeric_hits, -len(file_tokens)), file))
    if not scored:
        return None
    scored.sort(key=lambda item: (tuple(-score for score in item[0][:4]), -item[0][4], str(item[1])))
    best = scored[0]
    if len(scored) > 1 and scored[1][0] == best[0]:
        return None
    return best[1]


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
    library: LibraryMatcher | None = None,
) -> RegistryMaterial | None:
    source = value.strip()
    if not source:
        return None
    if source.count(" | ") == 1:
        title, right = source.split(" | ", 1)
        if title != title.strip() or not _EXACT_TITLE.fullmatch(title):
            return None
        if _EXACT_HTTP_URL.fullmatch(right):
            if not _valid_http_url(right):
                return None
            return RegistryMaterial(canonical_material_title(title), url=right)
        path = Path(right)
        if path.suffix.lower() not in _SUPPORTED_LOCAL_EXTENSIONS:
            return None
        try:
            exists = file_exists(path)
        except (OSError, ValueError):
            exists = False
        if not exists:
            return None
        return RegistryMaterial(canonical_material_title(title), file=str(path))
    if _EXACT_HTTP_URL.fullmatch(source):
        if not _valid_http_url(source):
            return None
        title = Path(urlsplit(source).path).name or source
        return RegistryMaterial(canonical_material_title(title), url=source)

    path = Path(source)
    if path.suffix.lower() in _SUPPORTED_LOCAL_EXTENSIONS:
        try:
            exists = file_exists(path)
        except (OSError, ValueError):
            exists = False
        if exists:
            return RegistryMaterial(path.stem, file=str(path))
    if library is not None:
        label = source.strip("|").strip()
        matched = library(label)
        if matched is not None:
            return RegistryMaterial(canonical_material_title(label), file=str(matched))
    return None


def material_declaration_failure_reason(
    value: str,
    *,
    file_exists: Callable[[Path], bool] = Path.is_file,
    library: LibraryMatcher | None = None,
) -> Literal["dichiarazione_ambigua", "sorgente_reale_assente"] | None:
    """Classify a rejected declaration without retaining its untrusted text."""
    if _declared_material(value, file_exists=file_exists, library=library) is not None:
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
    library: LibraryMatcher | None = None,
) -> list[str]:
    """Return curated sources first, then explicit source candidates.

    This function performs no network I/O and makes no reachability claim.
    Download policy and promotion to an analyzed material belong to Task 7.
    """
    candidates = (*CURATED_MATERIALS.get(video_id, ()), *declared_sources)
    sources: list[str] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        material = _declared_material(candidate, file_exists=file_exists, library=library)
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
    "LibraryMatcher", "MaterialSourceFailure", "RegistryMaterial",
    "canonical_material_title", "is_curated_material_label",
    "match_library_file", "material_declaration_failure_reason",
    "resolve_material_sources", "scan_library_files",
]
