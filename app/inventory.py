"""Read-only-by-default synchronization of the Academy course inventory."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import StringIO
import json
from pathlib import Path
import re
import unicodedata
from collections.abc import Callable, Sequence
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import Field, field_validator

from app.bunny import BunnyCatalogVideo
from app.material_registry import (
    AnalysisInventoryContext,
    MaterialSourceFailure,
    is_curated_material_label,
    material_declaration_failure_reason,
    resolve_material_sources,
)
from app.models import ReportModel


@dataclass(frozen=True)
class InventoryTab:
    title: str
    gid: str


@dataclass(frozen=True)
class CatalogItem:
    video: BunnyCatalogVideo
    sheet_row: int | None = None


@dataclass(frozen=True)
class CatalogGroup:
    key: str
    title: str
    items: list[CatalogItem]


DEFAULT_INVENTORY_TABS = (
    InventoryTab("Formazione", "0"),
    InventoryTab("Corsi premium - Master", "996207322"),
    InventoryTab("Corsi Premium", "1719623483"),
)

_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_PREFIX = re.compile(r"^(?:(?:webinar|corso|modulo)(?:\s+\d+)?\s*[:\-–—·.]?\s*)+", re.I)


class InventoryError(RuntimeError):
    pass


class InventoryWriteUnavailable(InventoryError):
    pass


class InventoryCourse(ReportModel):
    id: str
    foglio: str
    gid: str
    posizione_foglio: int = Field(ge=0, strict=True)
    riga: int = Field(ge=2, strict=True)
    titolo: str
    relatori_attesi: list[str] = Field(default_factory=list)
    materiali: list[str] = Field(default_factory=list)
    material_failures: list[MaterialSourceFailure] = Field(default_factory=list)
    link: str | None = None
    colonna_link: str | None = None
    guid_esplicito: UUID | None = None

    @field_validator("titolo")
    @classmethod
    def nonempty_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Il titolo inventario non può essere vuoto")
        return value


class MatchProposal(ReportModel):
    course_id: str
    video_id: UUID
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str


def _fold(value: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )


def normalize_title(value: str) -> str:
    normalized = _PREFIX.sub("", _fold(value).strip().lower())
    normalized = normalized.replace("&", " ")
    tokens = re.findall(r"[a-z0-9]+", normalized)
    if tokens and tokens[0] in {"il", "lo", "la", "i", "gli", "le", "un", "una"}:
        tokens.pop(0)
    return " ".join(tokens)


def _header(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", _fold(value).strip().lower()))


def _column_letter(index: int) -> str:
    result = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _people(value: str) -> list[str]:
    parts = re.split(r"[,\n;•]+", value)
    return list(dict.fromkeys(part.strip(" -–—\t") for part in parts if part.strip(" -–—\t")))


def _materials(value: str) -> list[str]:
    source = value.strip()
    if not source:
        return []
    if material_declaration_failure_reason(source) is None:
        return [source]
    parts = re.split(r"[\n;]+", value)
    return list(dict.fromkeys(part.strip() for part in parts if part.strip()))


def _find_column(headers: list[str], candidates: set[str]) -> int | None:
    for index, header in enumerate(headers):
        if header in candidates:
            return index
    return None


def _parse_rows(
    tab: InventoryTab, tab_position: int, rows: list[list[str]],
    *, material_rejections: Sequence[tuple[int, int, str]] = (),
) -> list[InventoryCourse]:
    if not rows:
        raise InventoryError("Il foglio inventario non contiene intestazioni valide")
    headers = [_header(value) for value in rows[0]]
    title_index = _find_column(headers, {"webinar", "modulomaster", "corso", "titolo"})
    link_index = _find_column(headers, {"link", "video", "linkvideo", "linkbunny", "bunny"})
    if title_index is None:
        raise InventoryError("Il foglio inventario non contiene la colonna titolo")
    speaker_indexes = [
        index for index, header in enumerate(headers)
        if any(token in header for token in ("relator", "docent", "speaker"))
    ]
    material_indexes = [
        index for index, header in enumerate(headers)
        if any(token in header for token in ("slide", "material", "dispensa"))
    ]
    courses: list[InventoryCourse] = []
    failures_by_row: dict[int, list[MaterialSourceFailure]] = {}
    for sheet_row, column_index, reason in material_rejections:
        failures_by_row.setdefault(sheet_row, []).append(MaterialSourceFailure(
            inventory_reference=f"{tab.title}!{_column_letter(column_index)}{sheet_row}",
            reason=reason,
        ))
    for row_index, row in enumerate(rows[1:], start=2):
        title = row[title_index].strip() if title_index < len(row) else ""
        if not title:
            continue
        link = row[link_index].strip() if link_index is not None and link_index < len(row) else ""
        guid_match = _GUID.search(link)
        speakers = [
            person
            for index in speaker_indexes if index < len(row)
            for person in _people(row[index])
        ]
        materials = [
            item
            for index in material_indexes if index < len(row)
            for item in _materials(row[index])
        ]
        courses.append(InventoryCourse(
            id=f"{tab.gid}:{row_index}",
            foglio=tab.title,
            gid=tab.gid,
            posizione_foglio=tab_position,
            riga=row_index,
            titolo=title,
            relatori_attesi=list(dict.fromkeys(speakers)),
            materiali=list(dict.fromkeys(materials)),
            material_failures=failures_by_row.get(row_index, []),
            link=link or None,
            colonna_link=_column_letter(link_index) if link_index is not None else None,
            guid_esplicito=UUID(guid_match.group()) if guid_match else None,
        ))
    return courses


def _parse_tab(tab: InventoryTab, tab_position: int, source: str) -> list[InventoryCourse]:
    return _parse_rows(tab, tab_position, list(csv.reader(StringIO(source))))


def _cell_text(cell: object) -> str:
    if not isinstance(cell, dict):
        return ""
    value = cell.get("formattedValue", "")
    return value if isinstance(value, str) else str(value)


def _material_cell_text(cell: object) -> tuple[str, str | None]:
    """Keep a cell link only when it identifies one unambiguous source."""
    value = _cell_text(cell).strip()
    if not isinstance(cell, dict):
        return value, None
    links: set[str] = set()
    hyperlink = cell.get("hyperlink")
    if isinstance(hyperlink, str) and hyperlink.strip():
        links.add(hyperlink.strip())
    runs = cell.get("textFormatRuns", [])
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            formatting = run.get("format")
            link = formatting.get("link") if isinstance(formatting, dict) else None
            uri = link.get("uri") if isinstance(link, dict) else None
            if isinstance(uri, str) and uri.strip():
                links.add(uri.strip())
    if len(links) > 1:
        return "", "dichiarazione_ambigua"
    if re.search(r"https?://", value, re.I):
        return value, None
    if not links:
        return value, None
    link = next(iter(links))
    return (f"{value} | {link}" if value else link), None


def _rows_from_grid_sheet(
    sheet: object,
) -> tuple[list[list[str]], list[tuple[int, int, str]]]:
    if not isinstance(sheet, dict):
        raise InventoryError("Google Sheets ha restituito dati inventario non validi")
    cells: dict[tuple[int, int], object] = {}
    max_row = -1
    max_column = -1
    for grid in sheet.get("data", []):
        if not isinstance(grid, dict):
            continue
        start_row = grid.get("startRow", 0)
        start_column = grid.get("startColumn", 0)
        if not isinstance(start_row, int) or not isinstance(start_column, int):
            raise InventoryError("Google Sheets ha restituito dati inventario non validi")
        row_data = grid.get("rowData", [])
        if not isinstance(row_data, list):
            raise InventoryError("Google Sheets ha restituito dati inventario non validi")
        for row_offset, row in enumerate(row_data):
            if not isinstance(row, dict):
                continue
            values = row.get("values", [])
            if not isinstance(values, list):
                continue
            row_index = start_row + row_offset
            max_row = max(max_row, row_index)
            for column_offset, cell in enumerate(values):
                column_index = start_column + column_offset
                cells[(row_index, column_index)] = cell
                max_column = max(max_column, column_index)
    if max_row < 0 or max_column < 0:
        return [], []
    rows = [
        [_cell_text(cells.get((row, column))) for column in range(max_column + 1)]
        for row in range(max_row + 1)
    ]
    material_indexes = [
        index for index, header in enumerate(_header(value) for value in rows[0])
        if any(token in header for token in ("slide", "material", "dispensa"))
    ]
    material_rejections: list[tuple[int, int, str]] = []
    for row_index in range(1, len(rows)):
        for column_index in material_indexes:
            value, rejection = _material_cell_text(
                cells.get((row_index, column_index))
            )
            rows[row_index][column_index] = value
            if rejection is not None:
                material_rejections.append((row_index + 1, column_index, rejection))
    return rows, material_rejections


class InventoryClient:
    def __init__(
        self,
        spreadsheet_id: str,
        tabs: tuple[InventoryTab, ...] = DEFAULT_INVENTORY_TABS,
        *,
        service_account_json: str | None = None,
        token_provider: Callable[[], str] | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._spreadsheet_id = spreadsheet_id
        self._tabs = tabs
        self._service_account_json = service_account_json
        self._token_provider = token_provider
        self._http = http_client or httpx.Client(timeout=httpx.Timeout(20, connect=10))

    def close(self) -> None:
        self._http.close()

    def fetch(self) -> list[InventoryCourse]:
        if self._service_account_json:
            return self._fetch_authenticated()
        courses: list[InventoryCourse] = []
        for position, tab in enumerate(self._tabs):
            try:
                response = self._http.get(
                    f"https://docs.google.com/spreadsheets/d/{self._spreadsheet_id}/export",
                    params={"format": "csv", "gid": tab.gid},
                    follow_redirects=True,
                )
                if response.status_code != 200:
                    raise InventoryError("Google Sheets non è temporaneamente disponibile")
                courses.extend(_parse_tab(tab, position, response.text))
            except InventoryError:
                raise
            except (httpx.HTTPError, UnicodeError, csv.Error):
                raise InventoryError("Impossibile sincronizzare l'inventario") from None
        return courses

    def _fetch_authenticated(self) -> list[InventoryCourse]:
        try:
            token = self._access_token()
        except InventoryWriteUnavailable:
            raise InventoryError("La lettura autenticata del foglio non è configurata correttamente") from None
        fields = (
            "sheets(properties(sheetId,title),data(startRow,startColumn,"
            "rowData(values(formattedValue,hyperlink,textFormatRuns(startIndex,format(link(uri)))))))"
        )
        try:
            response = self._http.get(
                f"https://sheets.googleapis.com/v4/spreadsheets/{self._spreadsheet_id}",
                params={"includeGridData": "true", "fields": fields},
                headers={"Authorization": f"Bearer {token}"},
            )
            if response.status_code != 200:
                raise InventoryError("Google Sheets non è temporaneamente disponibile")
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("sheets"), list):
                raise InventoryError("Google Sheets ha restituito dati inventario non validi")
            sheets = payload["sheets"]
            by_gid = {
                str(properties.get("sheetId")): sheet
                for sheet in sheets if isinstance(sheet, dict)
                for properties in [sheet.get("properties", {})]
                if isinstance(properties, dict)
            }
            by_title = {
                properties.get("title"): sheet
                for sheet in sheets if isinstance(sheet, dict)
                for properties in [sheet.get("properties", {})]
                if isinstance(properties, dict) and isinstance(properties.get("title"), str)
            }
            courses: list[InventoryCourse] = []
            for position, tab in enumerate(self._tabs):
                sheet = by_gid.get(tab.gid) or by_title.get(tab.title)
                if sheet is None:
                    raise InventoryError("Google Sheets non contiene tutti i fogli inventario")
                rows, material_rejections = _rows_from_grid_sheet(sheet)
                courses.extend(_parse_rows(
                    tab, position, rows,
                    material_rejections=material_rejections,
                ))
            return courses
        except InventoryError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            raise InventoryError("Impossibile sincronizzare l'inventario") from None

    def _access_token(self) -> str:
        if self._token_provider is not None:
            token = self._token_provider()
            if not isinstance(token, str) or not token:
                raise InventoryWriteUnavailable("La scrittura sul foglio non è configurata correttamente")
            return token
        if not self._service_account_json:
            raise InventoryWriteUnavailable(
                "La scrittura sul foglio richiede un service account configurato"
            )
        try:
            from google.auth.exceptions import RefreshError, TransportError
            from google.auth.transport.requests import Request
            from google.oauth2.service_account import Credentials
        except (ImportError, AttributeError):
            raise InventoryWriteUnavailable(
                "La scrittura sul foglio non è configurata correttamente"
            ) from None
        try:
            info = json.loads(self._service_account_json)
            credentials = Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            credentials.refresh(Request())
            if not credentials.token:
                raise ValueError
            return credentials.token
        except (RefreshError, TransportError, TypeError, ValueError, KeyError):
            raise InventoryWriteUnavailable(
                "La scrittura sul foglio non è configurata correttamente"
            ) from None

    def update_link(self, course: InventoryCourse, url: str) -> None:
        if course.colonna_link is None or course.gid not in {tab.gid for tab in self._tabs}:
            raise InventoryError("La riga non ha una colonna Link aggiornabile")
        if not re.fullmatch(
            r"https://(?:iframe|player)\.mediadelivery\.net/embed/\d+/"
            r"[0-9a-fA-F-]{36}",
            url,
        ):
            raise InventoryError("Il link Bunny non è valido")
        token = self._access_token()
        range_name = f"'{course.foglio}'!{course.colonna_link}{course.riga}"
        try:
            response = self._http.put(
                "https://sheets.googleapis.com/v4/spreadsheets/"
                f"{self._spreadsheet_id}/values/{quote(range_name, safe='')}",
                params={"valueInputOption": "RAW"},
                headers={"Authorization": f"Bearer {token}"},
                json={"range": range_name, "majorDimension": "ROWS", "values": [[url]]},
            )
            if response.status_code not in {200, 201}:
                raise InventoryError("Google Sheets non ha accettato l'aggiornamento")
        except httpx.HTTPError:
            raise InventoryError("Impossibile aggiornare il link nel foglio") from None


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right).ratio()
    left_tokens, right_tokens = set(left.split()), set(right.split())
    token_score = len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))
    return max(sequence, token_score)


def _lesson_code(value: str) -> str | None:
    folded = _fold(value).lower()
    match = re.search(
        r"(?:lezione\s+(?:master\s+)?|master\s*[-:·]?\s*lezione\s+)(\d+[._]\d+)",
        folded,
    )
    return match.group(1).replace("_", ".") if match else None


def propose_matches(
    courses: list[InventoryCourse],
    videos: list[BunnyCatalogVideo],
    *,
    threshold: float = .88,
    margin: float = .08,
) -> list[MatchProposal]:
    by_id = {video.video_id: video for video in videos}
    proposals: list[MatchProposal] = []
    for course in courses:
        if course.guid_esplicito is not None:
            if course.guid_esplicito in by_id:
                proposals.append(MatchProposal(
                    course_id=course.id,
                    video_id=course.guid_esplicito,
                    score=1,
                    reason="guid_esplicito",
                ))
            continue
        normalized = normalize_title(course.titolo)
        ranked = sorted(
            ((_similarity(normalized, normalize_title(video.title)), video) for video in videos),
            key=lambda item: item[0],
            reverse=True,
        )
        if not ranked:
            continue
        best_score, best_video = ranked[0]
        runner_up = ranked[1][0] if len(ranked) > 1 else 0
        if best_score >= threshold and best_score - runner_up >= margin:
            proposals.append(MatchProposal(
                course_id=course.id,
                video_id=best_video.video_id,
                score=round(best_score, 4),
                reason="titolo_univoco",
            ))
    return proposals


def organize_catalog(
    courses: list[InventoryCourse],
    videos: list[BunnyCatalogVideo],
    *,
    tabs: tuple[InventoryTab, ...] = DEFAULT_INVENTORY_TABS,
    threshold: float = .88,
    margin: float = .08,
) -> list[CatalogGroup]:
    """Order Bunny videos by the inventory without turning rows into courses.

    A video may intentionally appear in more than one sheet tab, but duplicate
    rows inside the same tab collapse to the first position. Provider-order
    videos with no safe match remain available in a separate final group.
    """
    video_by_id = {video.video_id: video for video in videos}
    proposal_by_course = {
        proposal.course_id: proposal.video_id
        for proposal in propose_matches(courses, videos, threshold=threshold, margin=margin)
    }
    videos_by_title: dict[str, list[BunnyCatalogVideo]] = {}
    videos_by_lesson: dict[str, list[BunnyCatalogVideo]] = {}
    for video in videos:
        videos_by_title.setdefault(normalize_title(video.title), []).append(video)
        lesson_code = _lesson_code(video.title)
        if lesson_code is not None:
            videos_by_lesson.setdefault(lesson_code, []).append(video)
    courses_by_tab: dict[str, list[InventoryCourse]] = {tab.gid: [] for tab in tabs}
    for course in courses:
        if course.gid in courses_by_tab:
            courses_by_tab[course.gid].append(course)
    for tab_courses in courses_by_tab.values():
        tab_courses.sort(key=lambda course: (course.riga, course.id))

    matched_ids: set[UUID] = set()
    groups: list[CatalogGroup] = []
    for tab in tabs:
        items: list[CatalogItem] = []
        seen_in_tab: set[UUID] = set()
        for course in courses_by_tab[tab.gid]:
            candidates: list[BunnyCatalogVideo] = []
            if course.guid_esplicito is not None:
                explicit = video_by_id.get(course.guid_esplicito)
                if explicit is not None:
                    candidates = [explicit]
            else:
                candidates = videos_by_title.get(normalize_title(course.titolo), [])
                lesson_code = _lesson_code(course.titolo)
                if not candidates and lesson_code is not None:
                    candidates = videos_by_lesson.get(lesson_code, [])
                if not candidates and course.id in proposal_by_course:
                    proposed = video_by_id.get(proposal_by_course[course.id])
                    if proposed is not None:
                        candidates = [proposed]
            for video in candidates:
                if video.video_id in seen_in_tab:
                    continue
                items.append(CatalogItem(video=video, sheet_row=course.riga))
                seen_in_tab.add(video.video_id)
                matched_ids.add(video.video_id)
        groups.append(CatalogGroup(key=f"sheet-{tab.gid}", title=tab.title, items=items))

    groups.append(CatalogGroup(
        key="unmatched",
        title="Altri video Bunny",
        items=[CatalogItem(video=video) for video in videos if video.video_id not in matched_ids],
    ))
    return groups


def speaker_hints_for_video(
    courses: list[InventoryCourse], video_id: UUID, video_title: str,
) -> list[str]:
    """Return human-entered spelling hints, never evidence that a person spoke."""
    normalized_title = normalize_title(video_title)
    names: list[str] = []
    for course in sorted(courses, key=lambda item: (item.posizione_foglio, item.riga, item.id)):
        matches = (
            course.guid_esplicito == video_id
            if course.guid_esplicito is not None
            else normalize_title(course.titolo) == normalized_title
        )
        if not matches:
            continue
        for name in course.relatori_attesi:
            cleaned = name.strip()
            if cleaned and cleaned not in names:
                names.append(cleaned)
    return names


def _material_cells_for_video(
    courses: Sequence[InventoryCourse], video_id: UUID, title: str,
) -> list[str]:
    normalized_title = normalize_title(title)
    declared_sources: list[str] = []
    for course in sorted(courses, key=lambda item: (item.posizione_foglio, item.riga, item.id)):
        matches = (
            course.guid_esplicito == video_id
            if course.guid_esplicito is not None
            else normalize_title(course.titolo) == normalized_title
        )
        if not matches:
            continue
        for source in course.materiali:
            if source not in declared_sources:
                declared_sources.append(source)
    return declared_sources


def material_sources_for_video(
    courses: Sequence[InventoryCourse], video_id: UUID, title: str,
    *,
    file_exists: Callable[[Path], bool] = Path.is_file,
) -> list[str]:
    """Return explicit candidates associated with one video, without fetching.

    Materials are evidence from the inventory, so title similarity proposals
    are deliberately excluded. Task 7 alone validates remote reachability and
    promotes candidates to analyzed materials.
    """
    return resolve_material_sources(
        _material_cells_for_video(courses, video_id, title), video_id,
        file_exists=file_exists,
    )


def raw_material_sources_for_video(
    courses: Sequence[InventoryCourse], video_id: UUID, title: str,
) -> list[str]:
    """Compatibility boundary for the pre-Task-9 web export path."""
    return _material_cells_for_video(courses, video_id, title)


def inventory_context_for_video(
    courses: Sequence[InventoryCourse], video_id: UUID, title: str,
    *,
    file_exists: Callable[[Path], bool] = Path.is_file,
) -> AnalysisInventoryContext:
    """Keep the once-fetched inventory inputs for analysis in one value."""
    normalized_title = normalize_title(title)
    material_failures: list[MaterialSourceFailure] = []
    for course in sorted(courses, key=lambda item: (item.posizione_foglio, item.riga, item.id)):
        matches = (
            course.guid_esplicito == video_id
            if course.guid_esplicito is not None
            else normalize_title(course.titolo) == normalized_title
        )
        if not matches:
            continue
        for failure in course.material_failures:
            if failure not in material_failures:
                material_failures.append(failure)
        for source in course.materiali:
            reason = material_declaration_failure_reason(
                source, file_exists=file_exists,
            )
            if reason is None or is_curated_material_label(video_id, source):
                continue
            failure = MaterialSourceFailure(
                inventory_reference=f"{course.foglio}!{course.riga}",
                reason=reason,
            )
            if failure not in material_failures:
                material_failures.append(failure)
    return AnalysisInventoryContext(
        speaker_hints=tuple(speaker_hints_for_video(list(courses), video_id, title)),
        material_sources=tuple(material_sources_for_video(
            courses, video_id, title,
            file_exists=file_exists,
        )),
        material_failures=tuple(material_failures),
    )
