"""Explicitly opt-in paid integration test. Never publish live inputs or output."""

import os

import pytest

from app.config import Settings
from app.logging_config import configure_logging
from app.main import build_services
from app.models import AcademyReport


@pytest.fixture
def live_settings():
    required = ("BUNNY_LIBRARY_ID", "BUNNY_STREAM_API_KEY", "BUNNY_CDN_HOSTNAME",
                "OPENAI_API_KEY", "APP_PASSWORD", "BUNNY_SAMPLE_VIDEO_URL")
    if os.environ.get("RUN_LIVE_BUNNY") != "1" or not all(os.environ.get(k) for k in required):
        pytest.skip("Live test disabled or required environment variables missing")
    try:
        settings = Settings(_env_file=None)
        configure_logging(settings)
        return settings
    except Exception:
        raise pytest.fail.Exception("Invalid live configuration (details withheld)", pytrace=False) from None


@pytest.mark.live
def test_real_bunny_video_produces_valid_report(live_settings):
    services = None
    try:
        services = build_services(live_settings)
        report = services.pipeline.run(os.environ["BUNNY_SAMPLE_VIDEO_URL"], lambda percent, message: None)
        validated = AcademyReport.model_validate(report)
        if validated.duration_seconds <= 0:
            raise ValueError
    except Exception:
        raise pytest.fail.Exception(
            "Live analysis failed; inspect only sanitized application diagnostics", pytrace=False,
        ) from None
    finally:
        if services is not None:
            services.runner.shutdown(wait=True)
            if services.assemblyai is not None:
                services.assemblyai.close()
            services.openai.close()
