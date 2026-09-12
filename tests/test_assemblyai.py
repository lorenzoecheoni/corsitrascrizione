from concurrent.futures import CancelledError
import json
from threading import Event
import traceback

import httpx
import pytest


def test_single_remote_job_returns_global_diarization_and_deletes_provider_copy():
    from app.assemblyai import AssemblyAITranscriber

    requests: list[tuple[str, str]] = []
    polls = iter([
        {"id": "transcript-safe-id", "status": "processing"},
        {
            "id": "transcript-safe-id",
            "status": "completed",
            "audio_duration": 60,
            "language_code": "it",
            "text": "Sono Vincenzo. Buongiorno.",
            "utterances": [
                {"speaker": "Vincenzo Manfredi", "start": 1000, "end": 3000,
                 "text": "Sono Vincenzo."},
                {"speaker": "B", "start": 4000, "end": 5000, "text": "Buongiorno."},
            ],
        },
    ])

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            payload = json.loads(request.read())
            assert payload == {
                "audio_url": "https://cdn.example/video/play_240p.mp4",
                "speech_models": ["universal-3-5-pro", "universal-2"],
                "language_detection": True,
                "speaker_labels": True,
                "speech_understanding": {
                    "request": {"speaker_identification": {"speaker_type": "name"}}
                },
            }
            return httpx.Response(200, json={"id": "transcript-safe-id"})
        if request.method == "GET":
            return httpx.Response(200, json=next(polls))
        assert request.method == "DELETE"
        return httpx.Response(200, json={"id": "transcript-safe-id", "status": "deleted"})

    transcriber = AssemblyAITranscriber(
        "test-only", transport=httpx.MockTransport(handle), poll_interval_seconds=0,
    )
    try:
        result = transcriber.transcribe_url(
            "https://cdn.example/video/play_240p.mp4", duration_seconds=60,
        )
    finally:
        transcriber.close()

    assert result.provider == "assemblyai"
    assert result.language == "it"
    assert result.text == "Sono Vincenzo. Buongiorno."
    assert [(item.start_seconds, item.end_seconds, item.diarization_label, item.text)
            for item in result.segments] == [
        (1, 3, "assembly:Vincenzo Manfredi", "Sono Vincenzo."),
        (4, 5, "assembly:B", "Buongiorno."),
    ]
    assert result.speaker_mapping == {
        "assembly:Vincenzo Manfredi": "Vincenzo Manfredi",
        "assembly:B": "Relatore 1",
    }
    assert result.audio_seconds == 60
    assert result.usage.requests == 1
    assert result.usage.provider_audio_seconds == 60
    assert requests == [
        ("POST", "/v2/transcript"),
        ("GET", "/v2/transcript/transcript-safe-id"),
        ("GET", "/v2/transcript/transcript-safe-id"),
        ("DELETE", "/v2/transcript/transcript-safe-id"),
    ]


def test_each_provider_utterance_remains_available_for_whole_video_sampling():
    from app.assemblyai import AssemblyAITranscriber

    result = AssemblyAITranscriber._parse_result({
        "status": "completed",
        "audio_duration": 3600,
        "language_code": "it",
        "text": "Apertura. Parte centrale. Conclusione.",
        "utterances": [
            {"speaker": "A", "start": 0, "end": 1000, "text": "Apertura."},
            {"speaker": "A", "start": 1200, "end": 2200, "text": "Parte centrale."},
            {"speaker": "A", "start": 3_599_000, "end": 3_600_000, "text": "Conclusione."},
        ],
    }, 3600)

    assert [segment.text for segment in result.segments] == [
        "Apertura.", "Parte centrale.", "Conclusione.",
    ]


def test_cancellation_while_polling_still_deletes_remote_transcript():
    from app.assemblyai import AssemblyAITranscriber

    event = Event()
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            return httpx.Response(200, json={"id": "transcript-safe-id"})
        if request.method == "GET":
            event.set()
            return httpx.Response(200, json={"id": "transcript-safe-id", "status": "processing"})
        return httpx.Response(200, json={"status": "deleted"})

    transcriber = AssemblyAITranscriber(
        "test-only", transport=httpx.MockTransport(handle), poll_interval_seconds=0,
    )
    try:
        with pytest.raises(CancelledError):
            transcriber.transcribe_url(
                "https://cdn.example/video/play_240p.mp4", duration_seconds=60,
                cancellation_event=event,
            )
    finally:
        transcriber.close()

    assert methods == ["POST", "GET", "DELETE"]


def test_cancellation_after_submit_response_captures_id_before_deleting_remote_transcript():
    from app.assemblyai import AssemblyAITranscriber

    event = Event()
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "POST":
            event.set()
            return httpx.Response(200, json={"id": "transcript-safe-id"})
        assert request.method == "DELETE"
        return httpx.Response(200, json={"status": "completed"})

    transcriber = AssemblyAITranscriber(
        "test-only", transport=httpx.MockTransport(handle), poll_interval_seconds=0,
    )
    try:
        with pytest.raises(CancelledError):
            transcriber.transcribe_url(
                "https://cdn.example/video/play_240p.mp4", duration_seconds=60,
                cancellation_event=event,
            )
    finally:
        transcriber.close()

    assert methods == ["POST", "DELETE"]


def test_provider_failure_is_static_and_remote_diagnostics_are_not_exposed():
    from app.assemblyai import AssemblyAITranscriber
    from app.transcription import TranscriptionError

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "PRIVATE AUDIO AND API DIAGNOSTIC"})

    transcriber = AssemblyAITranscriber(
        "test-only", transport=httpx.MockTransport(handle), poll_interval_seconds=0,
    )
    try:
        with pytest.raises(TranscriptionError) as caught:
            transcriber.transcribe_url(
                "https://cdn.example/private-video.mp4", duration_seconds=60,
            )
    finally:
        transcriber.close()

    formatted = "".join(traceback.format_exception(caught.value))
    assert caught.value.code == "auth"
    assert caught.value.status_code == 401
    assert "PRIVATE AUDIO" not in formatted
    assert "private-video" not in formatted


def test_report_is_not_released_when_remote_deletion_is_rejected():
    from app.assemblyai import AssemblyAITranscriber
    from app.transcription import TranscriptionError

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "transcript-safe-id"})
        if request.method == "GET":
            return httpx.Response(200, json={
                "id": "transcript-safe-id", "status": "completed",
                "audio_duration": 60, "language_code": "it", "text": "Contenuto.",
                "utterances": [{"speaker": "A", "start": 0, "end": 1000, "text": "Contenuto."}],
            })
        return httpx.Response(401, json={"error": "PRIVATE"})

    transcriber = AssemblyAITranscriber(
        "test-only", transport=httpx.MockTransport(handle), poll_interval_seconds=0,
    )
    try:
        with pytest.raises(TranscriptionError) as caught:
            transcriber.transcribe_url(
                "https://cdn.example/video/play_240p.mp4", duration_seconds=60,
            )
    finally:
        transcriber.close()

    assert caught.value.code == "auth"


def test_submit_is_not_retried_when_the_provider_might_have_created_a_job(monkeypatch):
    from app.assemblyai import AssemblyAITranscriber
    from app.transcription import TranscriptionError

    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": "PRIVATE"})

    monkeypatch.setattr("app.retry.time.sleep", lambda _: None)
    transcriber = AssemblyAITranscriber(
        "test-only", transport=httpx.MockTransport(handle), poll_interval_seconds=0,
    )
    try:
        with pytest.raises(TranscriptionError):
            transcriber.transcribe_url(
                "https://cdn.example/video/play_240p.mp4", duration_seconds=60,
            )
    finally:
        transcriber.close()

    assert attempts == 1
