"""Fail-closed logging: only typed, application-authored event fields survive.

Arbitrary SDK strings/arguments can contain transcripts or provider bodies that
cannot be reliably identified with a regex. Replace those records with a safe
code; remote observability is emitted explicitly by application boundaries.
The factory also protects handlers added after application initialization.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import logging
import math
from threading import RLock
from uuid import UUID

from app.config import Settings


_job_id: ContextVar[str | None] = ContextVar("log_job_id", default=None)
_lock = RLock()
_PHASES = {"validation", "metadata", "media", "transcription", "analysis", "report", "http"}
_CODES = {"ok", "cancelled", "invalid_link", "bunny_auth", "not_found", "protected_video",
          "unsupported_duration", "media_decode", "transcription", "analysis", "temporary_failure",
          "timeout", "transport", "remote_response", "invalid_request", "unauthorized",
          "log_suppressed"}
_ROUTES = {"/", "/jobs", "/jobs/{job_id}", "/api/jobs/{job_id}", "/jobs/{job_id}/cancel",
           "/jobs/{job_id}/report.md", "/jobs/{job_id}/report.txt", "/static/{path:path}",
           "/healthz", "unmatched"}
_RECORD_FIELDS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


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
                elif key == "job_id" and type(value) is str:
                    try:
                        if str(UUID(value)) == value:
                            event[key] = value
                    except ValueError:
                        pass
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
        # Extras are appended after the record factory; filter existing handlers
        # as well so structured formatters cannot serialize an unsafe extra.
        for key in set(record.__dict__) - _RECORD_FIELDS:
            del record.__dict__[key]
        record.__dict__.pop("message", None)
        record.__dict__.pop("asctime", None)
        return True


_filter = SafeEventFilter()


def configure_logging(settings: Settings) -> None:
    """Install one sanitizer, merging known secrets across app instances."""
    with _lock:
        _filter.secrets |= frozenset(value for value in (
            settings.app_password, settings.bunny_stream_api_key,
            settings.openai_api_key, settings.bunny_token_auth_key,
        ) if value)
        previous = logging.getLogRecordFactory()
        if not getattr(previous, "_safe_event_factory", False):
            def safe_factory(*args, **kwargs):
                record = previous(*args, **kwargs)
                _filter.filter(record)
                return record
            safe_factory._safe_event_factory = True
            logging.setLogRecordFactory(safe_factory)
        root = logging.getLogger()
        if not root.handlers:
            root.addHandler(logging.StreamHandler())
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)
        loggers = [root] + [item for item in logging.Logger.manager.loggerDict.values()
                            if isinstance(item, logging.Logger)]
        for logger in loggers:
            for handler in logger.handlers:
                if _filter not in handler.filters:
                    handler.addFilter(_filter)


@contextmanager
def job_log_context(job_id: UUID):
    token = _job_id.set(str(job_id))
    try:
        yield
    finally:
        _job_id.reset(token)


def log_event(phase: str, *, elapsed_seconds: float, error_code: str = "ok",
              status_code: int | None = None, attempt: int | None = None,
              route: str | None = None, method: str | None = None) -> None:
    event = {"job_id": _job_id.get(), "phase": phase, "elapsed_seconds": elapsed_seconds,
             "error_code": error_code, "status_code": status_code, "attempt": attempt,
             "route": route, "method": method}
    record = logging.LogRecord("app.events", logging.INFO, __file__, 0, event, (), None)
    _filter.filter(record)
    logging.getLogger("app.events").info(record.msg)
