"""Bounded retries; callers decide which sanitized errors are transient."""

from collections.abc import Callable
from concurrent.futures import CancelledError
from threading import Event
import time
from typing import TypeVar


T = TypeVar("T")


def retry_remote(
    call: Callable[[], T], *, retryable: Callable[[Exception], bool],
    attempts: int = 3, base_delay: float = 1.0,
    cancellation_event: Event | None = None,
) -> T:
    """Try at most three times, waiting 1s then 2s with default settings.

    A running call is never interrupted. Cancellation is observed immediately
    after it returns/raises, before another attempt, and during the backoff.
    """
    if not 1 <= attempts <= 3:
        raise ValueError("Sono consentiti da uno a tre tentativi")
    for attempt in range(attempts):
        check_cancelled(cancellation_event)
        try:
            result = call()
        except Exception as exc:
            check_cancelled(cancellation_event)
            if isinstance(exc, CancelledError) or attempt == attempts - 1 or not retryable(exc):
                raise
            delay = base_delay * (2**attempt)
            if cancellation_event is None:
                time.sleep(delay)
            else:
                cancellation_event.wait(delay)
                check_cancelled(cancellation_event)
        else:
            check_cancelled(cancellation_event)
            return result
    raise AssertionError("Unreachable")


def check_cancelled(event: Event | None) -> None:
    if event is not None and event.is_set():
        raise CancelledError("Elaborazione annullata") from None
