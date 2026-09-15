"""Offline acceptance of the same persisted contract used by paid live tests."""

import json
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.bunny import BunnyVideoMetadata
from app.config import Settings
from app.intermediate_models import IntermediateReportV11
from app.intermediate_report import build_intermediate_report
from app.models import AcademyReport
from app.jobs import JobState, JobStore
from app.main import build_services
from app.web import router
from report_delivery_support import assert_http_exports, assert_report_delivery
import test_live_bunny as live_bunny


FIXTURE = Path(__file__).with_name("fixtures") / "report.json"
VIDEO_ID = UUID("12345678-1234-1234-1234-123456789abc")


def test_committed_fixture_delivers_one_granular_video_with_stable_ids():
    # Losing the stored profile/block metadata must break the shipped example.
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    assert report.analysis_profile == 2
    assert report.audio_boundary_version == 1
    assert [block.id for block in report.speech_blocks] == ["b001"]
    assert report.materials == []
    exported = build_intermediate_report(report, VIDEO_ID)
    raw = exported.model_dump_json(by_alias=True, exclude_none=True)
    assert IntermediateReportV11.model_validate_json(raw) == exported
    assert json.loads(raw)["versione"] == 1
    assert len(exported.video) == 1
    video = exported.video[0]
    assert (video.chiave, video.guid, video.ordine, video.durata_secondi) == (
        "v1", str(VIDEO_ID), 1, 3720,
    )
    assert [block.id for block in video.blocchi_parlato] == ["v1-b001"]
    assert [chapter.id for chapter in video.interventi] == [
        "v1-i001", "v1-i002", "v1-i003", "v1-i004", "v1-i005",
    ]
    assert [(chapter.start_seconds, chapter.end_seconds) for chapter in video.interventi] == [
        (0, 600), (600, 1320), (1320, 2040), (2040, 2880), (2880, 3720),
    ]
    assert [chapter.accesso for chapter in video.interventi] == [
        "pubblico", "iscritti", "iscritti", "iscritti", "iscritti",
    ]
    assert all(chapter.blocco == "v1-b001" for chapter in video.interventi)
    assert [chapter.capitolo_numero for chapter in video.interventi] == [1, 2, 3, 4, 5]
    assert all(chapter.capitoli_blocco == 5 for chapter in video.interventi)
    assert raw == build_intermediate_report(report, VIDEO_ID).model_dump_json(
        by_alias=True, exclude_none=True,
    )


def test_fixture_survives_sqlite_and_all_http_downloads_without_network(tmp_path):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    metadata = BunnyVideoMetadata(video_id=VIDEO_ID, title=report.title, duration_seconds=3720)
    store = JobStore(tmp_path / "delivery.sqlite3")
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    reloaded = JobStore(tmp_path / "delivery.sqlite3").get(job.id)
    assert reloaded.report == report
    bodies = assert_report_delivery(reloaded.report, metadata)
    app = FastAPI()
    app.state.store = store
    app.state.settings = Settings(_env_file=None, bunny_library_id=123,
        bunny_stream_api_key="test-only", bunny_cdn_hostname="cdn.example.invalid",
        openai_api_key="test-only", app_password="test-only")
    app.include_router(router)
    with TestClient(app) as client:
        assert_http_exports(client, job.id, bodies)
    assert store.get(job.id) == reloaded


@pytest.mark.parametrize("mutation", [
    "legacy", "uncovered_block", "past_bunny_end", "duplicate_id", "missing_boundary",
    "missing_origin", "no_eligible_preview", "private_text",
])
def test_live_acceptance_rejects_malformed_or_private_persisted_output(mutation):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    metadata = BunnyVideoMetadata(video_id=VIDEO_ID, title=report.title, duration_seconds=3720)
    if mutation == "legacy":
        report.analysis_profile = 1
    elif mutation == "uncovered_block":
        report.speech_blocks[0].end_seconds = 3700
    elif mutation == "past_bunny_end":
        report.slides[0].timestamp_seconds = 3721
    elif mutation == "duplicate_id":
        report.interventions[1].id = report.interventions[0].id
    elif mutation == "missing_boundary":
        report.boundaries.pop()
    elif mutation == "missing_origin":
        report.interventions[1].boundary_origin = None
    elif mutation == "no_eligible_preview":
        for item in [*report.speech_blocks, *report.interventions]:
            item.tipo = "saluti"
    elif mutation == "private_text":
        report.synopsis = "SYNTHETIC_PRIVATE_TRANSCRIPT"
    with pytest.raises((AssertionError, ValueError)):
        assert_report_delivery(report, metadata, private_values=("SYNTHETIC_PRIVATE_TRANSCRIPT",))


@pytest.mark.parametrize("legacy", [False, True])
def test_live_bunny_acceptance_path_uses_real_store_and_downloads_offline(tmp_path, monkeypatch, legacy):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    if legacy:
        report.analysis_profile = 1
    workspace = tmp_path / "media"
    workspace.mkdir()
    settings = Settings(_env_file=None, bunny_library_id=123, bunny_stream_api_key="test-only",
        bunny_cdn_hostname="cdn.example.invalid", openai_api_key="test-only",
        app_password="test-only", database_path=str(tmp_path / "reports.sqlite3"),
        temp_root=str(workspace), assemblyai_api_key=None)
    services = build_services(settings)
    metadata = BunnyVideoMetadata(video_id=VIDEO_ID, title=report.title, duration_seconds=3720)
    # Only paid/network boundaries are replaced; acceptance, persistence,
    # application authentication and every download route remain real.
    monkeypatch.setattr(services.bunny, "get_metadata", lambda _: metadata)
    monkeypatch.setattr(services.pipeline, "run", lambda source, progress: report)
    monkeypatch.setattr(live_bunny, "build_services", lambda _: services)
    monkeypatch.setenv("BUNNY_SAMPLE_VIDEO_URL", f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    if legacy:
        with pytest.raises(pytest.fail.Exception) as caught:
            live_bunny.test_real_bunny_video_produces_valid_report(settings, monkeypatch)
        assert str(caught.value) == "Live analysis failed; inspect only sanitized application diagnostics"
        assert caught.value.pytrace is False
    else:
        live_bunny.test_real_bunny_video_produces_valid_report(settings, monkeypatch)
        completed = services.store.list_completed()
        assert len(completed) == 1 and completed[0].report == report
