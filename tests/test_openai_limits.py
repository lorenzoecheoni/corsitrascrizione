from concurrent.futures import CancelledError
from threading import Event

import pytest

from app.openai_limits import ProviderRateGate, parse_reset_seconds


@pytest.mark.parametrize(("raw", "seconds"), [
    ("3s", 3.0), ("1m2s", 62.0), ("250ms", .25), ("1h2m3.5s", 3723.5),
    ("bad", None), ("", None), ("-3s", None), ("nan", None),
    ("3s junk", None), ("0s", 0.0),
])
def test_parse_reset_seconds(raw, seconds):
    assert parse_reset_seconds(raw) == seconds


class Clock:
    def __init__(self):
        self.now = 100.0
        self.waits = []

    def wait(self, seconds, event):
        self.waits.append(seconds)
        self.now += seconds

    def gate(self):
        return ProviderRateGate(clock=lambda: self.now, wait=self.wait)


def test_gate_waits_remaining_reset_time_only_for_constrained_model():
    clock = Clock()
    gate = clock.gate()
    gate.observe("model-a", {"x-ratelimit-remaining-project-tokens": "100",
                            "x-ratelimit-reset-project-tokens": "1m2s"})
    clock.now += 2
    gate.wait("model-b", 200, None)
    assert clock.waits == []
    gate.wait("model-a", 200, None)
    assert clock.waits == [60]
    gate.wait("model-a", 200, None)
    assert clock.waits == [60]


def test_gate_reserves_budget_between_calls():
    clock = Clock()
    gate = clock.gate()
    gate.observe("model", {"x-ratelimit-remaining-project-tokens": "100",
                           "x-ratelimit-reset-project-tokens": "3s"})
    gate.wait("model", 70, None)
    assert clock.waits == []
    gate.wait("model", 40, None)
    assert clock.waits == [3]


@pytest.mark.parametrize("headers", [{}, {"secret": "PRIVATE"},
    {"x-ratelimit-remaining-project-tokens": "bad", "x-ratelimit-reset-project-tokens": "3s"},
    {"x-ratelimit-remaining-project-tokens": "0", "x-ratelimit-reset-project-tokens": "bad"},
    {"x-ratelimit-remaining-project-tokens": "-1", "x-ratelimit-reset-project-tokens": "3s"},
])
def test_missing_or_invalid_headers_do_not_delay(headers):
    clock = Clock()
    gate = clock.gate()
    gate.observe("model", headers)
    gate.wait("model", 4000, None)
    assert clock.waits == []


def test_gate_checks_cancellation_even_without_headers():
    event = Event()
    event.set()
    with pytest.raises(CancelledError):
        Clock().gate().wait("model", 1, event)


def test_gate_wait_is_cancellable():
    event = Event()
    gate = ProviderRateGate(clock=lambda: 10, wait=lambda seconds, ev: ev.set())
    gate.observe("model", {"x-ratelimit-remaining-project-tokens": "0",
                           "x-ratelimit-reset-project-tokens": "3s"})
    with pytest.raises(CancelledError):
        gate.wait("model", 1, event)
