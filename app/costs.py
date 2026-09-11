"""Deterministic initial cost estimates for a processed video."""

from app.models import CostEstimate


# Initial rates to calibrate after observing production usage.
BUNNY_USD_PER_GB = 0.01
TRANSCRIPTION_LOW_USD_PER_HOUR = 0.36
TRANSCRIPTION_HIGH_USD_PER_HOUR = 0.50
ANALYSIS_LOW_USD_PER_HOUR = 0.04
ANALYSIS_HIGH_USD_PER_HOUR = 0.18


def estimate_cost(duration_seconds: float, downloaded_bytes: int) -> CostEstimate:
    """Estimate USD costs from deterministic media usage measurements."""
    hours = duration_seconds / 3600
    bunny = downloaded_bytes / 1_000_000_000 * BUNNY_USD_PER_GB
    transcription_low = hours * TRANSCRIPTION_LOW_USD_PER_HOUR
    transcription_high = hours * TRANSCRIPTION_HIGH_USD_PER_HOUR
    analysis_low = hours * ANALYSIS_LOW_USD_PER_HOUR
    analysis_high = hours * ANALYSIS_HIGH_USD_PER_HOUR
    return CostEstimate(
        estimated_low_usd=round(bunny + transcription_low + analysis_low, 4),
        estimated_high_usd=round(bunny + transcription_high + analysis_high, 4),
        bunny_bandwidth_usd=round(bunny, 4),
        transcription_usd=round((transcription_low + transcription_high) / 2, 4),
        analysis_usd=round((analysis_low + analysis_high) / 2, 4),
        basis="Stima iniziale da durata e byte letti; calibrare con gli usage reali.",
    )
