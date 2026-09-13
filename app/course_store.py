"""Persistent inventory, review and Academy artifacts stored beside video jobs."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import UUID

from pydantic import Field

from app.course_models import IntermediateCourseReport
from app.inventory import InventoryCourse, MatchProposal
from app.models import ReportModel


class CourseRecord(ReportModel):
    id: str
    foglio: str
    gid: str
    posizione_foglio: int
    riga: int
    titolo: str
    relatori_attesi: list[str]
    materiali: list[str]
    link: str | None = None
    colonna_link: str | None = None
    guid_esplicito: UUID | None = None
    attivo: bool
    sincronizzato_il: datetime
    video_confermati: list[UUID] = Field(default_factory=list)
    proposte: list[MatchProposal] = Field(default_factory=list)
    batch_id: UUID | None = None
    job_ids: list[UUID] = Field(default_factory=list)
    intermediate: IntermediateCourseReport | None = None
    academy_json: dict | None = None
    contract_version: int | None = None
    prompt_version: int | None = None

    @property
    def stato(self) -> str:
        if self.academy_json is not None:
            return "pronto_academy"
        if self.intermediate is not None:
            return "verificato" if self.intermediate.stato in {"verificato", "confermato"} else "da_verificare"
        if self.batch_id is not None:
            return "in_elaborazione"
        if self.video_confermati:
            return "abbinato"
        if self.proposte:
            return "proposta"
        return "non_abbinato"


class CourseStore:
    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self._lock = RLock()
        path = str(database_path)
        if path != ":memory:" and not Path(path).expanduser().parent.is_dir():
            raise OSError("Database directory is unavailable")
        self._connection = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize_schema()

    def close(self) -> None:
        self._connection.close()

    def _initialize_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inventory_courses (
                    id TEXT PRIMARY KEY,
                    source_sheet TEXT NOT NULL,
                    sheet_gid TEXT NOT NULL,
                    tab_position INTEGER NOT NULL,
                    sheet_row INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    expected_speakers_json TEXT NOT NULL,
                    materials_json TEXT NOT NULL,
                    link_value TEXT,
                    link_column TEXT,
                    explicit_guid TEXT,
                    active INTEGER NOT NULL,
                    synced_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS inventory_order_idx
                    ON inventory_courses(active DESC, tab_position, sheet_row);
                CREATE TABLE IF NOT EXISTS course_videos (
                    course_id TEXT NOT NULL REFERENCES inventory_courses(id) ON DELETE CASCADE,
                    video_guid TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    confirmed INTEGER NOT NULL,
                    score REAL,
                    reason TEXT,
                    PRIMARY KEY (course_id, video_guid)
                );
                CREATE TABLE IF NOT EXISTS course_runs (
                    course_id TEXT PRIMARY KEY REFERENCES inventory_courses(id) ON DELETE CASCADE,
                    batch_id TEXT,
                    job_ids_json TEXT NOT NULL DEFAULT '[]',
                    intermediate_json TEXT,
                    academy_json TEXT,
                    contract_version INTEGER,
                    prompt_version INTEGER,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def sync_inventory(self, courses: list[InventoryCourse]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute("UPDATE inventory_courses SET active = 0")
            for course in courses:
                self._connection.execute(
                    """
                    INSERT INTO inventory_courses (
                        id, source_sheet, sheet_gid, tab_position, sheet_row, title,
                        expected_speakers_json, materials_json, link_value, link_column,
                        explicit_guid, active, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        source_sheet=excluded.source_sheet,
                        sheet_gid=excluded.sheet_gid,
                        tab_position=excluded.tab_position,
                        sheet_row=excluded.sheet_row,
                        title=excluded.title,
                        expected_speakers_json=excluded.expected_speakers_json,
                        materials_json=excluded.materials_json,
                        link_value=excluded.link_value,
                        link_column=excluded.link_column,
                        explicit_guid=excluded.explicit_guid,
                        active=1,
                        synced_at=excluded.synced_at
                    """,
                    (
                        course.id, course.foglio, course.gid, course.posizione_foglio,
                        course.riga, course.titolo,
                        json.dumps(course.relatori_attesi, ensure_ascii=False),
                        json.dumps(course.materiali, ensure_ascii=False),
                        course.link, course.colonna_link,
                        str(course.guid_esplicito) if course.guid_esplicito else None,
                        now,
                    ),
                )

    def replace_proposals(self, proposals: list[MatchProposal]) -> None:
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM course_videos WHERE confirmed = 0")
            for proposal in proposals:
                confirmed = self._connection.execute(
                    "SELECT 1 FROM course_videos WHERE course_id = ? AND confirmed = 1 LIMIT 1",
                    (proposal.course_id,),
                ).fetchone()
                if confirmed is not None:
                    continue
                self._connection.execute(
                    """
                    INSERT OR REPLACE INTO course_videos (
                        course_id, video_guid, position, confirmed, score, reason
                    ) VALUES (?, ?, 0, 0, ?, ?)
                    """,
                    (proposal.course_id, str(proposal.video_id), proposal.score, proposal.reason),
                )

    def confirm_videos(self, course_id: str, video_ids: list[UUID]) -> None:
        if not video_ids or len(video_ids) != len(set(video_ids)):
            raise ValueError("Selezionare almeno un video senza duplicati")
        with self._lock, self._connection:
            if self._connection.execute(
                "SELECT 1 FROM inventory_courses WHERE id = ? AND active = 1", (course_id,)
            ).fetchone() is None:
                raise KeyError(course_id)
            self._connection.execute("DELETE FROM course_videos WHERE course_id = ?", (course_id,))
            self._connection.executemany(
                """
                INSERT INTO course_videos (
                    course_id, video_guid, position, confirmed, score, reason
                ) VALUES (?, ?, ?, 1, NULL, 'conferma_utente')
                """,
                [(course_id, str(video_id), position) for position, video_id in enumerate(video_ids, 1)],
            )

    def attach_run(self, course_id: str, batch_id: UUID, job_ids: list[UUID]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO course_runs (course_id, batch_id, job_ids_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(course_id) DO UPDATE SET
                    batch_id=excluded.batch_id,
                    job_ids_json=excluded.job_ids_json,
                    intermediate_json=NULL,
                    academy_json=NULL,
                    contract_version=NULL,
                    prompt_version=NULL,
                    updated_at=excluded.updated_at
                """,
                (course_id, str(batch_id), json.dumps([str(item) for item in job_ids]), now),
            )

    def save_intermediate(self, course_id: str, report: IntermediateCourseReport) -> None:
        self._save_run_value(course_id, "intermediate_json", report.model_dump_json(by_alias=True))

    def confirm_intermediate(self, course_id: str, report: IntermediateCourseReport) -> None:
        if report.stato not in {"verificato", "confermato"}:
            raise ValueError("Il report deve essere verificato")
        if any(item.livello == "critico" for item in report.verifiche_richieste):
            raise ValueError("Le verifiche critiche bloccano la conferma")
        self.save_intermediate(course_id, report)

    def save_academy(
        self, course_id: str, academy_json: dict, contract_version: int, prompt_version: int
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """
                UPDATE course_runs SET academy_json = ?, contract_version = ?,
                    prompt_version = ?, updated_at = ? WHERE course_id = ?
                """,
                (
                    json.dumps(academy_json, ensure_ascii=False), contract_version,
                    prompt_version, now, course_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(course_id)

    def _save_run_value(self, course_id: str, column: str, value: str) -> None:
        if column != "intermediate_json":
            raise ValueError("Colonna non consentita")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"UPDATE course_runs SET {column} = ?, academy_json = NULL, "
                "contract_version = NULL, prompt_version = NULL, updated_at = ? WHERE course_id = ?",
                (value, now, course_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(course_id)

    def list_courses(self) -> list[CourseRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM inventory_courses WHERE active = 1 "
                "ORDER BY tab_position, sheet_row"
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_course(self, course_id: str, *, include_inactive: bool = False) -> CourseRecord:
        query = "SELECT * FROM inventory_courses WHERE id = ?"
        if not include_inactive:
            query += " AND active = 1"
        with self._lock:
            row = self._connection.execute(query, (course_id,)).fetchone()
        if row is None:
            raise KeyError(course_id)
        return self._row_to_record(row)

    def _row_to_record(self, row: sqlite3.Row) -> CourseRecord:
        with self._lock:
            video_rows = self._connection.execute(
                "SELECT * FROM course_videos WHERE course_id = ? ORDER BY confirmed DESC, position, score DESC",
                (row["id"],),
            ).fetchall()
            run = self._connection.execute(
                "SELECT * FROM course_runs WHERE course_id = ?", (row["id"],)
            ).fetchone()
        confirmed = [UUID(item["video_guid"]) for item in video_rows if item["confirmed"]]
        proposals = [MatchProposal(
            course_id=row["id"], video_id=UUID(item["video_guid"]),
            score=item["score"], reason=item["reason"],
        ) for item in video_rows if not item["confirmed"]]
        return CourseRecord(
            id=row["id"], foglio=row["source_sheet"], gid=row["sheet_gid"],
            posizione_foglio=row["tab_position"], riga=row["sheet_row"],
            titolo=row["title"], relatori_attesi=json.loads(row["expected_speakers_json"]),
            materiali=json.loads(row["materials_json"]), link=row["link_value"],
            colonna_link=row["link_column"],
            guid_esplicito=UUID(row["explicit_guid"]) if row["explicit_guid"] else None,
            attivo=bool(row["active"]), sincronizzato_il=datetime.fromisoformat(row["synced_at"]),
            video_confermati=confirmed, proposte=proposals,
            batch_id=UUID(run["batch_id"]) if run and run["batch_id"] else None,
            job_ids=[UUID(value) for value in json.loads(run["job_ids_json"])] if run else [],
            intermediate=(IntermediateCourseReport.model_validate_json(run["intermediate_json"])
                          if run and run["intermediate_json"] else None),
            academy_json=json.loads(run["academy_json"]) if run and run["academy_json"] else None,
            contract_version=run["contract_version"] if run else None,
            prompt_version=run["prompt_version"] if run else None,
        )
