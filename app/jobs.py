"""Volatile job records and cooperative execution in a single worker thread.

Restarting the process intentionally loses every job and report. The pipeline
owns cleanup before returning or raising JobCancelled. Progress messages must
be application-authored status text, never upstream diagnostics or content.
"""

from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from datetime import datetime, timezone
from enum import StrEnum
from threading import Event, RLock
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ValidationError

from app.bunny import (
    BunnyAuthError,
    BunnyError,
    BunnyNotFoundError,
    BunnyRateLimitError,
    BunnyTimeoutError,
    BunnyUrlError,
)
from app.models import AcademyReport
from app.logging_config import job_log_context


class JobState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobRecord(BaseModel):
    id: UUID
    source_url: str = Field(exclude=True, repr=False)
    source_title: str = ""
    state: JobState
    progress: int = Field(ge=0, le=100, strict=True)
    message: str
    created_at: datetime
    updated_at: datetime
    report: AcademyReport | None = None
    error: str | None = None


class BatchRecord(BaseModel):
    id: UUID
    job_ids: list[UUID]
    created_at: datetime


_TERMINAL_STATES = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
_TRANSITIONS = {
    JobState.QUEUED: {JobState.PROCESSING, JobState.CANCELLED},
    JobState.PROCESSING: _TERMINAL_STATES | {JobState.PROCESSING},
}


class JobStore:
    """Thread-safe in-memory store; callers never receive mutable stored data."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[UUID, JobRecord] = {}
        self._batches: dict[UUID, BatchRecord] = {}

    @staticmethod
    def _new_record(source_url: str, source_title: str, now: datetime) -> JobRecord:
        return JobRecord(
            id=uuid4(), source_url=source_url, state=JobState.QUEUED,
            source_title=source_title, progress=0, message="In coda",
            created_at=now, updated_at=now,
        )

    def create(self, source_url: str, *, source_title: str = "") -> JobRecord:
        record = self._new_record(source_url, source_title, datetime.now(timezone.utc))
        with self._lock:
            self._records[record.id] = record
            return record.model_copy(deep=True)

    def create_batch(self, items: list[tuple[str, str]]) -> tuple[BatchRecord, list[JobRecord]]:
        """Atomically store a batch and its independently queued jobs."""
        with self._lock:
            now = datetime.now(timezone.utc)
            records = [self._new_record(source_url, source_title, now) for source_url, source_title in items]
            batch = BatchRecord(
                id=uuid4(), job_ids=[record.id for record in records], created_at=now,
            )
            self._records.update({record.id: record for record in records})
            self._batches[batch.id] = batch
            return batch.model_copy(deep=True), [record.model_copy(deep=True) for record in records]

    def get_batch(self, batch_id: UUID) -> BatchRecord:
        with self._lock:
            return self._batches[batch_id].model_copy(deep=True)

    def list_recent(self, limit: int = 20) -> list[JobRecord]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Il limite dei lavori recenti deve essere tra 1 e 100")
        with self._lock:
            recent = sorted(
                self._records.values(), key=lambda record: record.created_at, reverse=True,
            )[:limit]
            return [record.model_copy(deep=True) for record in recent]

    def get(self, job_id: UUID) -> JobRecord:
        with self._lock:
            return self._records[job_id].model_copy(deep=True)

    def update(
        self,
        job_id: UUID,
        *,
        state: JobState | None = None,
        progress: int | None = None,
        message: str | None = None,
        report: AcademyReport | None = None,
        error: str | None = None,
    ) -> JobRecord:
        """Atomically validate a transition; terminal records are immutable.

        Repeated processing updates may keep progress equal, but never lower it.
        Messages and errors passed directly here must be safe application text.
        """
        with self._lock:
            current = self._records[job_id]
            try:
                next_state = current.state if state is None else JobState(state)
            except ValueError:
                raise ValueError("Stato del lavoro non valido") from None
            if next_state not in _TRANSITIONS.get(current.state, set()):
                raise ValueError("Transizione del lavoro non consentita")
            if report is not None and next_state != JobState.COMPLETED:
                raise ValueError("Il report richiede un lavoro completato")
            if error is not None and next_state != JobState.FAILED:
                raise ValueError("L'errore richiede un lavoro fallito")
            next_progress = current.progress if progress is None else progress
            if (
                type(next_progress) is not int
                or not 0 <= next_progress <= 100
                or next_progress < current.progress
            ):
                raise ValueError("Il progresso deve essere tra 0 e 100 e non diminuire")
            if next_state == JobState.COMPLETED:
                next_progress = 100
            # model_dump omits source_url by design; preserve internal fields
            # explicitly and validate before publishing the replacement record.
            data = {name: getattr(current, name) for name in JobRecord.model_fields}
            data.update(
                state=next_state, progress=next_progress,
                message=current.message if message is None else message,
                report=report, error=error, updated_at=datetime.now(timezone.utc),
            )
            try:
                updated = JobRecord.model_validate(data).model_copy(deep=True)
            except ValidationError:
                raise ValueError("Dati del lavoro non validi") from None
            self._records[job_id] = updated
            return updated.model_copy(deep=True)


class JobCancelled(CancelledError):
    """Pipeline cleanup finished after a cooperative cancellation request."""


ProgressCallback = Callable[[int, str], None]
Pipeline = Callable[[str, ProgressCallback, Event], AcademyReport]


def _safe_error(error: Exception) -> str:
    # Never expose str(error): even a known exception may carry input or secrets.
    from app.pipeline import PipelineError

    if isinstance(error, PipelineError):
        return error.user_message
    if isinstance(error, BunnyAuthError):
        return "Accesso a Bunny non autorizzato; verifica la configurazione"
    if isinstance(error, BunnyNotFoundError):
        return "Video non trovato nella libreria Bunny"
    if isinstance(error, BunnyUrlError):
        return "Il link Bunny non è valido"
    if isinstance(error, BunnyRateLimitError):
        return "Limite di richieste Bunny raggiunto; riprova più tardi"
    if isinstance(error, (BunnyTimeoutError, TimeoutError)):
        return "Il servizio non ha risposto entro il tempo previsto; riprova più tardi"
    if isinstance(error, BunnyError):
        return "Impossibile leggere il video da Bunny; riprova più tardi"
    return "Impossibile completare l'elaborazione; riprova più tardi"


class SingleWorkerRunner:
    """Serialize pipelines and coordinate cancellation with their final outcome."""

    def __init__(self, store: JobStore, pipeline: Pipeline) -> None:
        self._store = store
        self._pipeline = pipeline
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._lock = RLock()
        self._events: dict[UUID, Event] = {}
        self._futures: dict[UUID, Future[None]] = {}
        self._closed = False

    def submit(self, job_id: UUID) -> Future[None]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Il worker è stato arrestato")
            record = self._store.get(job_id)
            if record.state != JobState.QUEUED or job_id in self._futures:
                raise ValueError("Il lavoro non può essere accodato nuovamente")
            event = self._events.setdefault(job_id, Event())
            future = self._executor.submit(self._execute, job_id, event)
            self._futures[job_id] = future
            return future

    def cancel(self, job_id: UUID) -> JobRecord:
        with self._lock:
            record = self._store.get(job_id)
            if record.state in _TERMINAL_STATES:
                return record
            self._events.setdefault(job_id, Event()).set()
            future = self._futures.get(job_id)
            if future is None or future.cancel():
                return self._store.update(
                    job_id, state=JobState.CANCELLED, message="Annullato",
                )
            # A running future may still be waiting to enter _execute. It will
            # acknowledge the event there, or after the active pipeline exits.
            return self._store.get(job_id)

    def _execute(self, job_id: UUID, event: Event) -> None:
        with self._lock:
            record = self._store.get(job_id)
            if record.state in _TERMINAL_STATES:
                return
            if event.is_set():
                self._store.update(job_id, state=JobState.CANCELLED, message="Annullato")
                return
            self._store.update(job_id, state=JobState.PROCESSING, message="Elaborazione")

        def progress_callback(progress: int, message: str) -> None:
            self._store.update(job_id, progress=progress, message=message)

        report = None
        error = None
        cancelled = False
        try:
            with job_log_context(job_id):
                report = self._pipeline(record.source_url, progress_callback, event)
            if not isinstance(report, AcademyReport):
                raise ValueError("La pipeline deve restituire un report Academy")
        except CancelledError:
            cancelled = True
        except Exception as exc:
            error = _safe_error(exc)

        # Share this lock with cancel: a completion cannot overwrite an
        # acknowledged cancellation, even if the pipeline returns at that instant.
        with self._lock:
            if self._store.get(job_id).state in _TERMINAL_STATES:
                return
            if cancelled or event.is_set():
                self._store.update(job_id, state=JobState.CANCELLED, message="Annullato")
            elif error is not None:
                self._store.update(job_id, state=JobState.FAILED, message="Elaborazione fallita", error=error)
            else:
                self._store.update(job_id, state=JobState.COMPLETED, message="Completato", report=report)

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting jobs and drain the queue; active cleanup may finish."""
        with self._lock:
            self._closed = True
        # Never join the executor while holding the lock needed by its worker.
        self._executor.shutdown(wait=wait)
