from concurrent.futures import CancelledError
from threading import Event

import pytest

from app.retry import retry_remote


def test_retry_stops_after_three_attempts(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.retry.time.sleep", sleeps.append)
    attempts = 0

    def fails():
        nonlocal attempts
        attempts += 1
        raise TimeoutError("temporary")

    with pytest.raises(TimeoutError):
        retry_remote(fails, retryable=lambda exc: True)
    assert attempts == 3
    assert sleeps == [1.0, 2.0]


def test_retry_returns_success_and_does_not_sleep_afterward(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.retry.time.sleep", sleeps.append)
    responses = iter([TimeoutError(), "done"])

    def call():
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    assert retry_remote(call, retryable=lambda exc: True) == "done"
    assert sleeps == [1.0]


def test_final_error_is_not_retried(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.retry.time.sleep", sleeps.append)
    attempts = []

    def fails():
        attempts.append(1)
        raise ValueError("final")

    with pytest.raises(ValueError):
        retry_remote(fails, retryable=lambda exc: False)
    assert attempts == [1]
    assert sleeps == []


@pytest.mark.parametrize("attempts", [0, -1, 4])
def test_invalid_attempt_budget_never_calls_remote(attempts):
    calls = []
    with pytest.raises(ValueError):
        retry_remote(lambda: calls.append(1), retryable=lambda exc: True, attempts=attempts)
    assert calls == []


def test_cancellation_during_retry_wait_prevents_next_request():
    event = Event()
    calls = []

    def cancel_wait(delay):
        assert delay == 1.0
        event.set()
        return True

    event.wait = cancel_wait

    def fails():
        calls.append(1)
        raise TimeoutError()

    with pytest.raises(CancelledError):
        retry_remote(fails, retryable=lambda exc: True, cancellation_event=event)
    assert calls == [1]
