"""Ephemeral per-model pacing from numeric provider limits only."""

from collections.abc import Callable, Mapping
import math
import re
from threading import Event
import time

from app.retry import check_cancelled


_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?"
                       r"(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?")


def parse_reset_seconds(value: str) -> float | None:
    match = _DURATION.fullmatch(value.strip())
    if match is None or not any(match.groups()):
        return None
    seconds = sum(float(amount) * scale for amount, scale in
                  zip(match.groups(), (3600, 60, 1, .001)) if amount is not None)
    return seconds if math.isfinite(seconds) else None


def _wait(seconds: float, event: Event | None) -> None:
    if event is None:
        time.sleep(seconds)
    else:
        event.wait(seconds)


class ProviderRateGate:
    """Sequential analyzer requests reserve budget until the monotonic reset.

    No response, header mapping, or content survives observation. Unknown or
    incomplete limits disable preventive pacing for that model.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[float, Event | None], None] = _wait) -> None:
        self._clock = clock
        self._wait = wait
        self._limits: dict[str, tuple[int, float]] = {}

    def observe(self, model: str, headers: Mapping[str, str]) -> None:
        # Normalize names only for the two recognized numeric fields.
        remaining = reset = None
        for key, value in headers.items():
            if key.lower() == "x-ratelimit-remaining-project-tokens":
                try:
                    remaining = int(value)
                except ValueError:
                    pass
            elif key.lower() == "x-ratelimit-reset-project-tokens":
                reset = parse_reset_seconds(value)
        if remaining is None or remaining < 0 or reset is None:
            self._limits.pop(model, None)
        else:
            self._limits[model] = (remaining, self._clock() + reset)

    def wait(self, model: str, required_tokens: int, event: Event | None) -> None:
        check_cancelled(event)
        limit = self._limits.get(model)
        if limit is None:
            return
        remaining, deadline = limit
        if remaining < required_tokens:
            while (delay := deadline - self._clock()) > 0:
                self._wait(delay, event)
                check_cancelled(event)
        if deadline <= self._clock():
            self._limits.pop(model, None)
        else:
            self._limits[model] = (remaining - required_tokens, deadline)
