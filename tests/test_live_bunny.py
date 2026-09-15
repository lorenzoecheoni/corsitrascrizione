"""Explicitly opt-in paid integration test. Never publish live inputs or output."""

import os
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.auth import SESSION_COOKIE, issue_session
from app.bunny import parse_bunny_url
from app.config import Settings
from app.jobs import JobState
from app.logging_config import configure_logging
from app.main import build_services, create_app
from app.models import AcademyReport
from report_delivery_support import assert_http_exports, assert_report_delivery


@pytest.fixture
def live_settings(tmp_path):
    required = ("BUNNY_LIBRARY_ID", "BUNNY_STREAM_API_KEY", "BUNNY_CDN_HOSTNAME",
                "OPENAI_API_KEY", "ASSEMBLYAI_API_KEY", "APP_PASSWORD", "BUNNY_SAMPLE_VIDEO_URL")
    if os.environ.get("RUN_LIVE_BUNNY") != "1" or not all(os.environ.get(k) for k in required):
        pytest.skip("Live test disabled or required environment variables missing")
    try:
        workspace = tmp_path / "media"
        workspace.mkdir()
        settings = Settings(_env_file=None, database_path=str(tmp_path / "reports.sqlite3"),
                            temp_root=str(workspace))
        configure_logging(settings)
        return settings
    except Exception:
        raise pytest.fail.Exception("Invalid live configuration (details withheld)", pytrace=False) from None


@pytest.mark.live
def test_real_bunny_video_produces_valid_report(live_settings, monkeypatch):
    services = None
    try:
        services = build_services(live_settings)
        source = os.environ["BUNNY_SAMPLE_VIDEO_URL"]
        reference = parse_bunny_url(source, expected_library_id=live_settings.bunny_library_id,
                                   cdn_hostname=live_settings.bunny_cdn_hostname)
        # Read the authoritative duration independently of the pipeline result.
        metadata = services.bunny.get_metadata(reference.video_id)
        private_values = [live_settings.openai_api_key, live_settings.assemblyai_api_key,
                          live_settings.bunny_stream_api_key, live_settings.bunny_token_auth_key,
                          live_settings.app_password, live_settings.temp_root]
        analyze = services.analyzer.analyze

        def audit_transient_input(metadata, transcription, *args, **kwargs):
            private_values.append(" ".join(segment.text for segment in transcription.segments))
            return analyze(metadata, transcription, *args, **kwargs)

        monkeypatch.setattr(services.analyzer, "analyze", audit_transient_input)
        report = services.pipeline.run(source, lambda percent, message: None)
        validated = AcademyReport.model_validate_json(report.model_dump_json())
        assert list(Path(live_settings.temp_root).iterdir()) == []
        bodies = assert_report_delivery(validated, metadata, private_values=private_values)

        canonical_source = (
            f"https://iframe.mediadelivery.net/embed/{live_settings.bunny_library_id}/{reference.video_id}"
        )
        job = services.store.create(canonical_source, source_title=metadata.title)
        services.store.update(job.id, state=JobState.PROCESSING)
        services.store.update(job.id, state=JobState.COMPLETED, report=validated)
        # Inspect actual SQLite serialization, not only the in-memory model.
        with sqlite3.connect(live_settings.database_path) as database:
            persisted, = database.execute(
                "SELECT report_json FROM jobs WHERE id = ?", (str(job.id),),
            ).fetchone()
        restored = AcademyReport.model_validate_json(persisted)
        assert restored == validated
        assert assert_report_delivery(restored, metadata, private_values=private_values) == bodies
        monkeypatch.setattr("app.main.build_services", lambda _: services)
        app = create_app(live_settings)
        with TestClient(app) as client:
            client.cookies.set(SESSION_COOKIE, issue_session(app.state.session_key))
            assert_http_exports(client, job.id, bodies)
        assert services.store.get(job.id).report == validated
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
            services.inventory.close()
            services.course_store.close()
