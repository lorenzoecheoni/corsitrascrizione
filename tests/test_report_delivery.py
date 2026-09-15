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
from app.transcription import TranscriptSegment, TranscriptWord, TranscriptionResult
from app.web import router
from report_delivery_support import TranscriptLeakAudit, assert_http_exports, assert_report_delivery
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


def _timed_private_transcription():
    segments = []
    for number in range(2):
        words = [TranscriptWord(
            text=f"spoken{number}{index:04d}",
            start_seconds=number * 1860 + index * .93,
            end_seconds=number * 1860 + index * .93 + .7,
            diarization_label="A", confidence=.99,
        ) for index in range(2000)]
        segments.append(TranscriptSegment(
            start_seconds=number * 1860, end_seconds=(number + 1) * 1860,
            text=" ".join(word.text for word in words), words=words,
            diarization_label="A", source_utterance_id=f"synthetic-{number}",
        ))
    return TranscriptionResult(text="", segments=segments, audio_seconds=3720, provider="assemblyai")


def _timed_short_transcription():
    segments = []
    for number in range(100):
        words = [TranscriptWord(
            text=f"brief{number:03d}{index:02d}",
            start_seconds=number * 37.2 + index * 1.8,
            end_seconds=number * 37.2 + index * 1.8 + .7,
            diarization_label="A", confidence=.99,
        ) for index in range(20)]
        segments.append(TranscriptSegment(
            start_seconds=number * 37.2, end_seconds=(number + 1) * 37.2,
            text=" ".join(word.text for word in words), words=words,
            diarization_label="A", source_utterance_id=f"short-{number}",
        ))
    return TranscriptionResult(text="", segments=segments, audio_seconds=3720, provider="assemblyai")


def _live_privacy_case(tmp_path, monkeypatch, report, *, private="TEST_PRIVATE_PASSWORD"):
    """Real acceptance/store/auth/downloads; replace only remote work."""
    workspace = tmp_path / "media"
    workspace.mkdir()
    settings = Settings(_env_file=None, bunny_library_id=123, bunny_stream_api_key="test-only",
        bunny_cdn_hostname="cdn.example.invalid", openai_api_key="test-only",
        app_password=private, database_path=str(tmp_path / "reports.sqlite3"),
        temp_root=str(workspace), assemblyai_api_key=None)
    services = build_services(settings)
    metadata = BunnyVideoMetadata(video_id=VIDEO_ID, title=report.title, duration_seconds=3720)
    transcription = _timed_private_transcription()
    monkeypatch.setattr(services.bunny, "get_metadata", lambda _: metadata)
    monkeypatch.setattr(services.analyzer, "analyze", lambda *args, **kwargs: report)
    # analyze_fast delegates to analyze, which the live audit wraps at runtime.
    monkeypatch.setattr(services.pipeline, "run", lambda source, progress:
        services.analyzer.analyze_fast(metadata, transcription, [], silence_intervals=[]))
    monkeypatch.setattr(live_bunny, "build_services", lambda _: services)
    monkeypatch.setenv("BUNNY_SAMPLE_VIDEO_URL", f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    downloads = []

    def observe_client(app):
        client = TestClient(app)
        client.event_hooks["response"].append(lambda response: downloads.append(
            (response.status_code, response.headers.get("content-type", ""))))
        return client

    monkeypatch.setattr(live_bunny, "TestClient", observe_client)
    return settings, services, transcription, downloads


def _assert_static_live_rejection(settings, monkeypatch):
    with pytest.raises(pytest.fail.Exception) as caught:
        live_bunny.test_real_bunny_video_produces_valid_report(settings, monkeypatch)
    assert str(caught.value) == "Live analysis failed; inspect only sanitized application diagnostics"
    assert caught.value.pytrace is False


@pytest.mark.parametrize("excerpt", ["whole_segment", "reformatted_window"])
def test_live_audit_rejects_one_segment_or_substantial_excerpt(tmp_path, monkeypatch, caplog, capsys, excerpt):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    settings, services, transcription, downloads = _live_privacy_case(tmp_path, monkeypatch, report)
    if excerpt == "whole_segment":
        report.synopsis = transcription.segments[0].text
    else:
        report.synopsis = "Sintesi: " + ",\n".join(
            word.text.upper() for word in transcription.segments[1].words[700:764])
    _assert_static_live_rejection(settings, monkeypatch)
    assert services.store.list_completed() == []
    assert downloads == []
    captured = capsys.readouterr()
    assert "spoken" not in (captured.out + captured.err + caplog.text).casefold()


@pytest.mark.parametrize("excerpt", [
    "whole_transcript", "normalized_cross_segment", "empty_segments", "chronological_segments",
])
def test_live_audit_rejects_substantial_cross_segment_excerpts(
    tmp_path, monkeypatch, caplog, capsys, excerpt,
):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    settings, services, transcription, downloads = _live_privacy_case(tmp_path, monkeypatch, report)
    transcription.segments = _timed_short_transcription().segments
    words = [word.text for segment in transcription.segments for word in segment.words]
    assert len(transcription.segments) == 100 and len(words) == 2000
    assert all(len(segment.words) == 20 for segment in transcription.segments)
    if excerpt == "whole_transcript":
        report.synopsis = " ".join(words)
    else:
        # Forty consecutive words span three individually sub-threshold segments.
        report.synopsis = "Sintesi: " + ",\n\t".join(word.upper() for word in words[990:1030])
        if excerpt == "empty_segments":
            for index in (51, 50):
                transcription.segments.insert(index, TranscriptSegment(
                    start_seconds=index * 37.2, end_seconds=index * 37.2,
                    text="  \n", words=[], diarization_label="A",
                ))
        elif excerpt == "chronological_segments":
            transcription.segments.reverse()
    _assert_static_live_rejection(settings, monkeypatch)
    assert services.store.list_completed() == []
    assert downloads == []
    captured = capsys.readouterr()
    assert "brief" not in (captured.out + captured.err + caplog.text).casefold()


def test_transcript_audits_do_not_join_unrelated_runs_or_reorder_equal_start_segments():
    segments = _timed_short_transcription().segments[:2]
    segments[1].start_seconds = segments[0].start_seconds
    combined = " ".join(segment.text for segment in segments)
    first, second = TranscriptLeakAudit(), TranscriptLeakAudit()
    for audit, segment in zip((first, second), segments, strict=True):
        audit.observe(TranscriptionResult(text="", segments=[segment], audio_seconds=3720))
        audit.assert_absent(combined)
    # Equal start times preserve the source's supplied order, not reversed order.
    joined = TranscriptLeakAudit()
    joined.observe(TranscriptionResult(text="", segments=segments, audio_seconds=3720))
    with pytest.raises(AssertionError, match="Transient transcript excerpt in report"):
        joined.assert_absent(combined)
    joined.assert_absent(" ".join(segment.text for segment in reversed(segments)))
    assert all(isinstance(window, bytes) and len(window) == 32 for window in joined._windows)


@pytest.mark.parametrize("private", ['PRIVATE_SECRET_"quoted"', "PRIVATE_SECRET_new\nline", 'PRIVATE_SECRET_é🔒\x01'],
                         ids=["quoted", "newline", "unicode_control"])
@pytest.mark.parametrize("location", ["persisted_only", "exported_evidence"])
def test_live_audit_rejects_escaped_secrets_in_decoded_json(
    tmp_path, monkeypatch, caplog, capsys, private, location,
):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    if location == "persisted_only":
        report.uncertainties.append(private)
    else:
        report.speakers[0].evidence[0].note = private
    settings, services, _, downloads = _live_privacy_case(tmp_path, monkeypatch, report, private=private)
    _assert_static_live_rejection(settings, monkeypatch)
    assert services.store.list_completed() == []
    assert downloads == []
    captured = capsys.readouterr()
    output = captured.out + captured.err + caplog.text
    assert "PRIVATE_SECRET_" not in output
    assert private not in output
    assert json.dumps(private)[1:-1] not in output


@pytest.mark.parametrize("mutation", ["whole_segment_as_word", "contradictory_audio_rule"])
def test_live_audit_rejects_noncompact_or_contradictory_boundary_evidence(tmp_path, monkeypatch, mutation):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    settings, services, transcription, downloads = _live_privacy_case(tmp_path, monkeypatch, report)
    if mutation == "whole_segment_as_word":
        report.boundaries[0].words_before[0] = transcription.segments[0].text
    else:
        report.boundaries[0].rule = "long_pause"
    _assert_static_live_rejection(settings, monkeypatch)
    assert services.store.list_completed() == []
    assert downloads == []


@pytest.mark.parametrize("short_segments", [False, True])
def test_live_audit_allows_actual_five_plus_five_boundary_words(tmp_path, monkeypatch, short_segments):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    settings, services, transcription, downloads = _live_privacy_case(tmp_path, monkeypatch, report)
    if short_segments:
        transcription.segments = _timed_short_transcription().segments
    words = [word for segment in transcription.segments for word in segment.words]
    for evidence in report.boundaries:
        evidence.words_before = [word.text for word in words
                                 if word.end_seconds <= evidence.boundary_seconds][-5:]
        evidence.words_after = [word.text for word in words
                                if word.start_seconds >= evidence.boundary_seconds][:5]
    live_bunny.test_real_bunny_video_produces_valid_report(settings, monkeypatch)
    assert services.store.list_completed()[0].report == report
    assert [status for status, _ in downloads] == [200, 200, 200, 200]
    assert {media_type.split(";")[0] for _, media_type in downloads} == {
        "application/json", "text/markdown", "text/plain",
    }


@pytest.mark.parametrize("mutation", ["two_thousand_words", "multiple_words", "long_token",
    "long_side", "control_character", "punctuation_only", "short_without_pause", "rule"])
def test_boundary_compactness_does_not_depend_on_transcript_leak_detection(mutation):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    boundary = report.boundaries[0]
    if mutation == "two_thousand_words":
        boundary.words_before[0] = " ".join(["unrelated"] * 2000)
    elif mutation == "multiple_words":
        boundary.words_before[0] = "three actual words"
    elif mutation == "long_token":
        boundary.words_before[0] = "x" * 256
    elif mutation == "long_side":
        boundary.words_before = ["x" * 64] * 5
    elif mutation == "control_character":
        boundary.words_before[0] = "hidden\x01control"
    elif mutation == "punctuation_only":
        boundary.words_before[0] = "----"
    elif mutation == "short_without_pause":
        boundary.words_before.pop()
    else:
        boundary.rule = "long_pause"
    metadata = BunnyVideoMetadata(video_id=VIDEO_ID, title=report.title, duration_seconds=3720)
    with pytest.raises((AssertionError, ValueError)):
        assert_report_delivery(report, metadata)


@pytest.mark.parametrize("word_count", [0, 1, 4])
def test_compact_boundary_pause_semantics_remain_exportable(word_count):
    report = AcademyReport.model_validate_json(FIXTURE.read_text())
    report.boundaries[0].words_before = report.boundaries[0].words_before[:word_count]
    report.boundaries[0].pause_before = True
    metadata = BunnyVideoMetadata(video_id=VIDEO_ID, title=report.title, duration_seconds=3720)
    bodies = assert_report_delivery(report, metadata)
    assert all("(pausa)" in body for body in bodies.values())
