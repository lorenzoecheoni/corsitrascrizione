"""Fail-closed logging: only typed, application-authored event fields survive.

Arbitrary SDK strings/arguments can contain transcripts or provider bodies that
cannot be reliably identified with a regex. Replace those records with a safe
code; remote observability is emitted explicitly by application boundaries.
Standard handlers sanitize the completed record after their filters run, so
late handlers, nested extras and filter-supplied replacement records are covered.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
from functools import wraps
import logging
import math
import re
from threading import RLock
from uuid import UUID

from app.config import Settings


_job_id: ContextVar[str | None] = ContextVar("log_job_id", default=None)
_lock = RLock()
_PHASES = {"validation", "metadata", "media", "transcription", "analysis", "materials", "boundary", "report", "http"}
_CODES = {"ok", "cancelled", "invalid_link", "bunny_auth", "not_found", "protected_video",
          "unsupported_duration", "media_decode", "transcription", "analysis", "temporary_failure",
          "analysis_rate_limit",
          "analysis_visual", "analysis_window", "analysis_consolidation",
          "analysis_visual_rate_limit", "analysis_window_rate_limit", "analysis_consolidation_rate_limit",
          "boundaries",
          "timeout", "transport", "remote_response", "invalid_request", "unauthorized",
          "log_suppressed", "MATERIALE_NON_RAGGIUNGIBILE"}
_DETAIL_CODES = {
    "window_payload", "window_context", "window_contract", "window_incomplete",
    "materialize_length", "materialize_empty", "materialize_indexes",
    "materialize_margin", "materialize_labels", "materialize_source_conflict",
    "alignment_invalid", "alignment_word_evidence", "alignment_source_split",
    "alignment_speech_timing", "alignment_overlap", "alignment_quantization",
    "alignment_incomplete", "materialize_unknown",
}
_ROUTES = {"/", "/jobs", "/jobs/{job_id}", "/api/jobs/{job_id}", "/jobs/{job_id}/cancel",
           "/jobs/{job_id}/report.md", "/jobs/{job_id}/report.txt",
           "/jobs/{job_id}/report.json", "/static/{path:path}",
           "/healthz", "unmatched"}
_RECORD_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
_LOGGER_NAMES = {"root", "app.events", "httpx", "httpcore", "openai", "asyncio",
                 "uvicorn", "uvicorn.error", "uvicorn.access", "external"}
_LEVEL_NAMES = {0: "NOTSET", 10: "DEBUG", 20: "INFO", 30: "WARNING", 40: "ERROR", 50: "CRITICAL"}
_NUMERIC_FIELDS = {"lineno", "created", "msecs", "relativeCreated", "thread", "process"}
_REDACTED = "[REDACTED]"


class SafeEventFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.secrets: frozenset[str] = frozenset()

    def filter(self, record: logging.LogRecord) -> bool:
        event = {}
        # Never format raw msg/args or stringify arbitrary provider objects.
        if type(record.msg) is dict:
            for key, value in record.msg.items():
                if type(value) is str and any(secret in value for secret in self.secrets):
                    continue
                if key == "phase" and type(value) is str and value in _PHASES:
                    event[key] = value
                elif key == "error_code" and type(value) is str and value in _CODES:
                    event[key] = value
                elif key == "detail_code" and type(value) is str and value in _DETAIL_CODES:
                    event[key] = value
                elif key == "job_id" and type(value) is str:
                    try:
                        if str(UUID(value)) == value:
                            event[key] = value
                    except ValueError:
                        pass
                elif key == "remote_type" and type(value) is str \
                        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,60}", value):
                    event[key] = value
                elif key in {"status_code", "attempt"} and type(value) is int:
                    if (key == "status_code" and 100 <= value <= 599) or (key == "attempt" and 1 <= value <= 100):
                        event[key] = value
                elif key == "elapsed_seconds" and type(value) in {int, float}:
                    if math.isfinite(value) and value >= 0:
                        event[key] = round(value, 3)
                elif key == "route" and type(value) is str and value in _ROUTES:
                    event[key] = value
                elif key == "method" and type(value) is str and value in {"GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE", "PATCH"}:
                    event[key] = value
        record.msg = event or {"error_code": "log_suppressed"}
        record.args = ()
        record.exc_info = record.exc_text = record.stack_info = None
        # Keep extra keys available to ordinary %-style formatters, but discard
        # the entire value without recursively rendering provider-owned objects.
        for key in set(record.__dict__) - _RECORD_FIELDS:
            record.__dict__[key] = _REDACTED
        # Metadata can be supplied by factories, logger names or worker names;
        # none of those strings are trusted application event fields.
        name = record.name if type(record.name) is str and record.name in _LOGGER_NAMES else "external"
        record.name = _REDACTED if any(secret in name for secret in self.secrets) else name
        if type(record.levelno) is not int or record.levelno not in _LEVEL_NAMES:
            record.levelno = logging.INFO
        level = _LEVEL_NAMES[record.levelno]
        record.levelname = _REDACTED if any(secret in level for secret in self.secrets) else level
        for key in {"pathname", "filename", "module", "funcName", "threadName", "processName", "taskName"}:
            if key in record.__dict__:
                record.__dict__[key] = _REDACTED
        for key in _NUMERIC_FIELDS:
            value = record.__dict__.get(key)
            if type(value) not in {int, float} or not math.isfinite(value):
                record.__dict__[key] = 0
        record.__dict__.pop("message", None)
        record.__dict__.pop("asctime", None)
        return True


_filter = SafeEventFilter()


def configure_logging(settings: Settings) -> None:
    """Install one sanitizer, merging known secrets across app instances."""
    with _lock:
        _filter.secrets |= frozenset(value for value in (
            settings.app_password, settings.bunny_stream_api_key,
            settings.openai_api_key, settings.assemblyai_api_key,
            settings.bunny_token_auth_key,
        ) if value)
        previous = logging.Handler.filter
        if not getattr(previous, "_safe_event_filter", False):
            @wraps(previous)
            def safe_filter(handler, record):
                result = previous(handler, record)
                if result:
                    # Python 3.12+ permits handler filters to return a new
                    # LogRecord. Sanitize that record, preserving filter order,
                    # rejection and replacement semantics before handle emits.
                    # Each handler receives its own sanitized copy. Mutating
                    # the shared record would change other handlers' filters.
                    safe_record = copy(result if isinstance(result, logging.LogRecord) else record)
                    _filter.filter(safe_record)
                    return safe_record
                return result
            safe_filter._safe_event_filter = True
            logging.Handler.filter = safe_filter
        root = logging.getLogger()
        if not root.handlers:
            root.addHandler(logging.StreamHandler())
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)


@contextmanager
def job_log_context(job_id: UUID):
    token = _job_id.set(str(job_id))
    try:
        yield
    finally:
        _job_id.reset(token)


def log_event(phase: str, *, elapsed_seconds: float, error_code: str = "ok",
              status_code: int | None = None, attempt: int | None = None,
              route: str | None = None, method: str | None = None,
              detail_code: str | None = None, remote_type: str | None = None) -> None:
    event = {"job_id": _job_id.get(), "phase": phase, "elapsed_seconds": elapsed_seconds,
             "error_code": error_code, "status_code": status_code, "attempt": attempt,
             "route": route, "method": method, "detail_code": detail_code,
             "remote_type": remote_type}
    record = logging.LogRecord("app.events", logging.INFO, __file__, 0, event, (), None)
    _filter.filter(record)
    logging.getLogger("app.events").info(record.msg)
