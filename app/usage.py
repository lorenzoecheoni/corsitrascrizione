"""Retain numeric provider counters only, including available error usage."""

import math

from app.models import ProviderUsage, UsageEntry


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def record_usage(summary: ProviderUsage, response, *, audio_seconds: float = 0) -> None:
    usage = _field(response, "usage")
    if usage is None:
        usage = _field(_field(response, "body"), "usage")

    def counter(name):
        value = _field(usage, name)
        return value if type(value) is int and value >= 0 else None

    details = _field(usage, "input_token_details")
    audio_tokens = _field(details, "audio_tokens")
    seconds = _field(usage, "seconds")
    summary.entries.append(UsageEntry(
        input_tokens=counter("input_tokens"), output_tokens=counter("output_tokens"),
        audio_input_tokens=audio_tokens if type(audio_tokens) is int and audio_tokens >= 0 else None,
        provider_audio_seconds=seconds if type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0 else None,
        request_audio_seconds=audio_seconds,
    ))
