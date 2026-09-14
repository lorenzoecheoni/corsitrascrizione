from concurrent.futures import CancelledError
import json
from threading import Event
import traceback

import httpx
import pytest


def word(text: str, start: int, end: int, speaker: str = "A") -> dict:
    return {
        "text": text,
        "start": start,
        "end": end,
        "confidence": 0.97,
        "speaker": speaker,
    }


def test_assemblyai_preserves_ordered_word_timing_in_memory():
    from app.assemblyai import AssemblyAITranscriber

    result = AssemblyAITranscriber._parse_result({
        "status": "completed",
        "audio_duration": 3,
        "language_code": "it",
        "text": "La governance evolve.",
        "utterances": [{
            "speaker": "A",
            "start": 500,
            "end": 2500,
            "text": "La governance evolve.",
            "words": [
                word("La", 500, 700),
                word("governance", 800, 1500),
                word("evolve.", 1600, 2500),
            ],
        }],
    }, 3)
    segment = result.original_segments[0]

    assert segment.source_utterance_id == "assembly-u000001"
    assert [(item.text, item.start_seconds, item.end_seconds) for item in segment.words] == [
        ("La", .5, .7), ("governance", .8, 1.5), ("evolve.", 1.6, 2.5),
    ]


def test_assemblyai_accepts_overlapping_words_and_expands_segment_to_word_evidence():
    from app.assemblyai import AssemblyAITranscriber

    result = AssemblyAITranscriber._parse_result({
        "status": "completed",
        "audio_duration": 3,
        "language_code": "it",
        "text": "La governance evolve.",
        "utterances": [{
            "speaker": "A",
            "start": 500,
            "end": 2500,
            "text": "La governance evolve.",
            "words": [
                word("La", 450, 800),
                word("governance", 750, 1500),
                word("evolve.", 1600, 2550),
            ],
        }],
    }, 3)

    segment = result.original_segments[0]

    assert (segment.start_seconds, segment.end_seconds) == (.45, 2.55)
    assert segment.source_utterance_id == "assembly-u000001"
    assert [(item.text, item.start_seconds, item.end_seconds) for item in segment.words] == [
        ("La", .45, .8), ("governance", .75, 1.5), ("evolve.", 1.6, 2.55),
    ]


def test_assemblyai_rejects_words_beyond_accepted_provider_duration():
    from app.assemblyai import AssemblyAITranscriber
    from app.transcription import TranscriptionError

    with pytest.raises(TranscriptionError) as caught:
        AssemblyAITranscriber._parse_result({
            "status": "completed",
            "audio_duration": 4,
            "language_code": "it",
            "text": "Fuori durata.",
            "utterances": [{
                "speaker": "A",
                "start": 2500,
                "end": 3000,
                "text": "Fuori durata.",
                "words": [word("Fuori", 2500, 2800), word("durata.", 2800, 4100)],
            }],
        }, 3)

    assert caught.value.code == "response"


def test_assemblyai_reconciles_one_second_terminal_provider_rounding_for_alignment_and_report():
    from app.assemblyai import AssemblyAITranscriber
    from app.boundaries import SemanticIntervention, align_intervention_boundaries, has_complete_boundary_evidence
    from app.models import AcademyContent

    result = AssemblyAITranscriber._parse_result({
        "status": "completed",
        "audio_duration": 5790,
        "language_code": "it",
        "text": "Apertura. Conclusione.",
        "utterances": [
            {"speaker": "A", "start": 0, "end": 1000, "text": "Apertura.",
             "words": [word("Apertura.", 0, 1000)]},
            {"speaker": "A", "start": 5_788_000, "end": 5_790_000, "text": "Conclusione.",
             "words": [word("Conclusione.", 5_788_000, 5_789_400)]},
        ],
    }, 5789)
    groups = [
        SemanticIntervention(
            tipo="intervento", relatori=("Relatore 1",), titolo="Apertura", sintesi="",
            punti_chiave=("Uno", "Due", "Tre"), confidenza=.9,
            segments=(result.original_segments[0],),
        ),
        SemanticIntervention(
            tipo="intervento", relatori=("Relatore 1",), titolo="Conclusione", sintesi="",
            punti_chiave=("Uno", "Due", "Tre"), confidenza=.9,
            segments=(result.original_segments[1],),
        ),
    ]

    alignment = align_intervention_boundaries(5789, groups, [])
    report = AcademyContent(
        title="Corso", duration_seconds=5789, detected_language="it", synopsis="",
        speakers=[], slides=[], uncertainties=[], interventions=alignment.interventions,
        boundaries=alignment.boundaries, audio_boundary_version=1,
    )

    assert [(item.start_seconds, item.end_seconds) for item in alignment.interventions] == [
        (0, 1), (1, 5789),
    ]
    assert alignment.interventions[-1].end_seconds == 5789
    assert has_complete_boundary_evidence(report)


def test_assemblyai_rejects_provider_duration_skew_larger_than_one_second():
    from app.assemblyai import AssemblyAITranscriber
    from app.transcription import TranscriptionError

    with pytest.raises(TranscriptionError) as caught:
        AssemblyAITranscriber._parse_result({
            "status": "completed",
            "audio_duration": 5790.01,
            "language_code": "it",
            "text": "Fuori tolleranza.",
            "utterances": [{
                "speaker": "A", "start": 0, "end": 1000, "text": "Fuori tolleranza.",
                "words": [word("Fuori", 0, 500), word("tolleranza.", 500, 1000)],
            }],
        }, 5789)

    assert caught.value.code == "response"


def test_assemblyai_provider_word_variance_remains_usable_for_boundary_alignment():
    from app.assemblyai import AssemblyAITranscriber
    from app.boundaries import SemanticIntervention, align_intervention_boundaries

    result = AssemblyAITranscriber._parse_result({
        "status": "completed",
        "audio_duration": 2.5,
        "language_code": "it",
        "text": "Prima parte. Seconda parte.",
        "utterances": [
            {"speaker": "A", "start": 0, "end": 1000, "text": "Prima parte.",
             "words": [word("Prima", 0, 700), word("parte.", 650, 1050)]},
            {"speaker": "A", "start": 1200, "end": 2000, "text": "Seconda parte.",
             "words": [word("Seconda", 1150, 1500), word("parte.", 1500, 2000)]},
        ],
    }, 2.5)
    groups = [
        SemanticIntervention(
            tipo="intervento", relatori=("Relatore 1",), titolo="Prima", sintesi="",
            punti_chiave=("Uno", "Due", "Tre"), confidenza=.9,
            segments=(result.original_segments[0],),
        ),
        SemanticIntervention(
            tipo="intervento", relatori=("Relatore 1",), titolo="Seconda", sintesi="",
            punti_chiave=("Uno", "Due", "Tre"), confidenza=.9,
            segments=(result.original_segments[1],),
        ),
    ]

    alignment = align_intervention_boundaries(2.5, groups, [])

    assert [(item.start_seconds, item.end_seconds) for item in alignment.interventions] == [(0, 1), (1, 2)]
    assert alignment.boundaries[0].words_before == ["Prima", "parte."]
    assert alignment.boundaries[0].words_after == ["Seconda", "parte."]


@pytest.mark.parametrize("words", [
    None,
    [],
    [word("PRIVATE-WORD-EVIDENCE", 700, 500)],
    [word("PRIVATE-WORD-EVIDENCE", 800, 1000), word("seconda", 500, 700)],
    [word("PRIVATE-WORD-EVIDENCE", -1, 700)],
    [word("PRIVATE-WORD-EVIDENCE", 500, 3_001)],
    [word("PRIVATE-WORD-EVIDENCE", 500, 700, speaker="B")],
    [word("", 500, 700)],
    [{**word("PRIVATE-WORD-EVIDENCE", 500, 700), "confidence": float("nan")}],
    [{**word("PRIVATE-WORD-EVIDENCE", 500, 700), "confidence": 1.01}],
    [{**word("PRIVATE-WORD-EVIDENCE", 500, 700), "confidence": -0.01}],
])
def test_assemblyai_rejects_invalid_word_evidence_safely(words):
    from app.assemblyai import AssemblyAITranscriber
    from app.transcription import TranscriptionError

    payload = {
        "status": "completed",
        "audio_duration": 3,
        "language_code": "it",
        "text": "Testo sicuro.",
        "utterances": [{
            "speaker": "A",
            "start": 500,
            "end": 2500,
            "text": "Testo sicuro.",
        }],
    }
    if words is not None:
        payload["utterances"][0]["words"] = words

    with pytest.raises(TranscriptionError) as caught:
        AssemblyAITranscriber._parse_result(payload, 3)

    assert caught.value.code == "response"
    assert "PRIVATE-WORD-EVIDENCE" not in "".join(traceback.format_exception(caught.value))


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
                 "text": "Sono Vincenzo.",
                 "words": [word("Sono Vincenzo.", 1000, 3000, speaker="Vincenzo Manfredi")]},
                {"speaker": "B", "start": 4000, "end": 5000, "text": "Buongiorno.",
                 "words": [word("Buongiorno.", 4000, 5000, speaker="B")]},
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
            {"speaker": "A", "start": 0, "end": 1000, "text": "Apertura.",
             "words": [word("Apertura.", 0, 1000)]},
            {"speaker": "A", "start": 1200, "end": 2200, "text": "Parte centrale.",
             "words": [word("Parte centrale.", 1200, 2200)]},
            {"speaker": "A", "start": 3_599_000, "end": 3_600_000, "text": "Conclusione.",
             "words": [word("Conclusione.", 3_599_000, 3_600_000)]},
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
                "utterances": [{"speaker": "A", "start": 0, "end": 1000, "text": "Contenuto.",
                                "words": [word("Contenuto.", 0, 1000)]}],
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
