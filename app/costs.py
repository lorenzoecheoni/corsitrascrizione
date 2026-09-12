"""Deterministic initial cost estimates for a processed video."""

from typing import Literal

from app.models import APIUsage, CostEstimate, ProviderUsage


# Initial rates to calibrate after observing production usage.
BUNNY_USD_PER_GB = 0.01
TRANSCRIPTION_LOW_USD_PER_HOUR = 0.36
TRANSCRIPTION_HIGH_USD_PER_HOUR = 0.50
ASSEMBLYAI_USD_PER_HOUR = 0.25
ANALYSIS_LOW_USD_PER_HOUR = 0.04
ANALYSIS_HIGH_USD_PER_HOUR = 0.18


def estimate_cost(
    duration_seconds: float, downloaded_bytes: int, *, usage: APIUsage | None = None,
    transcription_provider: Literal["openai", "assemblyai"] = "openai",
) -> CostEstimate:
    """Estimate USD costs from deterministic media usage measurements."""
    hours = duration_seconds / 3600
    # The fast path has two CDN consumers in parallel: local FFmpeg for slide
    # detection and AssemblyAI for audio transcription.
    bunny_reads = 2 if transcription_provider == "assemblyai" else 1
    bunny = downloaded_bytes / 1_000_000_000 * BUNNY_USD_PER_GB * bunny_reads
    transcription_rate_low, transcription_rate_high = (
        (ASSEMBLYAI_USD_PER_HOUR, ASSEMBLYAI_USD_PER_HOUR)
        if transcription_provider == "assemblyai"
        else (TRANSCRIPTION_LOW_USD_PER_HOUR, TRANSCRIPTION_HIGH_USD_PER_HOUR)
    )
    transcription_low = hours * transcription_rate_low
    transcription_high = hours * transcription_rate_high
    analysis_low = hours * ANALYSIS_LOW_USD_PER_HOUR
    analysis_high = hours * ANALYSIS_HIGH_USD_PER_HOUR
    if usage is not None:
        def refine(provider: ProviderUsage, input_rate, output_rate, low_rate, high_rate, audio=False):
            low = high = 0.0
            for entry in provider.entries:
                known = ((entry.input_tokens or 0) * input_rate + (entry.output_tokens or 0) * output_rate) / 1_000_000
                low += known
                high += known
                if entry.input_tokens is None or entry.output_tokens is None:
                    fallback_seconds = entry.provider_audio_seconds or entry.request_audio_seconds
                    fallback_hours = fallback_seconds / 3600 if audio else hours / provider.requests
                    low += fallback_hours * low_rate
                    high += fallback_hours * high_rate
            return (low, high) if provider.entries else (hours * low_rate, hours * high_rate)
        transcription_low, transcription_high = refine(usage.transcription, 2.5, 10,
            transcription_rate_low, transcription_rate_high, audio=True)
        analysis_low, analysis_high = refine(usage.responses, .2, 1.2,
            ANALYSIS_LOW_USD_PER_HOUR, ANALYSIS_HIGH_USD_PER_HOUR)
    return CostEstimate(
        estimated_low_usd=round(bunny + transcription_low + analysis_low, 4),
        estimated_high_usd=round(bunny + transcription_high + analysis_high, 4),
        bunny_bandwidth_usd=round(bunny, 4),
        transcription_usd=round((transcription_low + transcription_high) / 2, 4),
        analysis_usd=round((analysis_low + analysis_high) / 2, 4),
        basis=("Stima da contatori API disponibili, inclusi tentativi e riparazioni; per contatori mancanti "
               "si aggiunge una stima da durata. Banda da byte stimati del flusso. Non è una fattura."
               if usage is not None else "Stima preventiva da durata e banda stimata; non è una fattura."),
    )
