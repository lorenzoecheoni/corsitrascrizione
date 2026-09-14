import base64
from concurrent.futures import CancelledError
import inspect
from pathlib import Path
from threading import Event
import traceback
from types import SimpleNamespace

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from openai.resources.audio.transcriptions import Transcriptions
from openai.types.audio import TranscriptionDiarized
import pytest

from app.media import AudioChunk, temporary_workspace
from app.transcription import KnownSpeaker, OpenAITranscriber, TranscriptionError


def response(segments, *, duration=300, usage=None):
    """Complete SDK 2.54.0 diarized JSON shape; deliberately has no language."""
    return TranscriptionDiarized.model_validate({
        "duration": duration, "task": "transcribe",
        "text": " ".join(text for _, _, _, text in segments),
        "segments": [
            {"id": str(i), "type": "transcript.text.segment", "start": start,
             "end": end, "speaker": speaker, "text": text}
            for i, (start, end, speaker, text) in enumerate(segments)
        ],
        "usage": usage,
    })


def test_transcript_word_rejects_zero_duration():
    from app.transcription import TranscriptWord

    with pytest.raises(ValueError, match="Intervallo parola non valido"):
        TranscriptWord(
            text="PRIVATE-WORD-EVIDENCE", start_seconds=1, end_seconds=1,
            diarization_label="assembly:A", confidence=.97,
        )


class FakeClient:
    def __init__(self, responses, hook=None):
        self.responses = iter(responses)
        self.hook = hook
        self.requests = []
        self.files = []
        self.options = []
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))

    def with_options(self, **kwargs):
        inspect.signature(OpenAI.with_options).bind(self, **kwargs)
        self.options.append(kwargs)
        return self

    def create(self, **kwargs):
        inspect.signature(Transcriptions.create).bind(self, **kwargs)
        assert all(file.closed for file in self.files)
        file = kwargs["file"]
        assert not file.closed
        assert file.read() == b"audio fixture"
        self.files.append(file)
        self.requests.append(kwargs)
        if self.hook:
            self.hook()
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def chunks(tmp_path):
    paths = [tmp_path / "first.m4a", tmp_path / "second.m4a"]
    for path in paths:
        path.write_bytes(b"audio fixture")
    return [AudioChunk(paths[0], 0, 300), AudioChunk(paths[1], 300, 20)]


def test_offsets_local_speakers_adjacent_merge_and_originals(chunks):
    first = response([(0, 2, "A", "Buongiorno."), (2, 4, "A", "Benvenuti."),
                      (10, 12, "B", "Grazie.")])
    second = response([(0, 2, "A", "Continuiamo."), (3, 4, "B", "Va bene.")], duration=20)
    client = FakeClient([first, second])
    result = OpenAITranscriber(client).transcribe(chunks)
    assert result.language == "und"
    assert result.audio_seconds == 320
    assert result.text == "Buongiorno. Benvenuti. Grazie. Continuiamo. Va bene."
    assert [(s.start_seconds, s.end_seconds, s.diarization_label, s.text) for s in result.segments] == [
        (0, 4, "chunk-0:A", "Buongiorno. Benvenuti."),
        (10, 12, "chunk-0:B", "Grazie."),
        (300, 302, "chunk-1:A", "Continuiamo."),
        (303, 304, "chunk-1:B", "Va bene."),
    ]
    assert len(result.original_segments) == 5
    assert result.original_segments[0].end_seconds == 2
    assert result.speaker_mapping == {
        "chunk-0:A": "Relatore 1", "chunk-0:B": "Relatore 2",
        "chunk-1:A": "Relatore 3", "chunk-1:B": "Relatore 4",
    }
    assert result.input_tokens is None and result.output_tokens is None
    for request in client.requests:
        assert request["model"] == "gpt-4o-transcribe-diarize"
        assert request["response_format"] == "diarized_json"
        assert request["chunking_strategy"] == "auto"
        assert "known_speaker_names" not in request
        assert "known_speaker_references" not in request
    assert client.options == [{"max_retries": 0}]
    assert all(file.closed for file in client.files)


def test_reference_identifies_speaker_across_chunks_without_losing_originals(chunks, tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"explicit reference")
    speaker = KnownSpeaker(name="docente", path=reference, duration_seconds=4)
    client = FakeClient([
        response([(298, 300, "docente", "Prima parte.")]),
        response([(0, 2, "docente", "Seconda parte."), (3, 4, "A", "Domanda.")], duration=20),
    ])
    result = OpenAITranscriber(client).transcribe(chunks, known_speakers=[speaker])
    assert result.speaker_mapping == {"chunk-0:docente": "docente", "chunk-1:docente": "docente", "chunk-1:A": "Relatore 1"}
    assert [(s.start_seconds, s.end_seconds, s.text) for s in result.segments] == [
        (298, 302, "Prima parte. Seconda parte."), (303, 304, "Domanda."),
    ]
    assert [s.diarization_label for s in result.original_segments] == ["chunk-0:docente", "chunk-1:docente", "chunk-1:A"]
    for request in client.requests:
        assert request["known_speaker_names"] == ["docente"]
        assert request["known_speaker_references"] == ["data:audio/wav;base64," + base64.b64encode(b"explicit reference").decode()]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["first.m4a", "reference.wav", "second.m4a"]


def test_same_text_and_local_label_do_not_prove_identity(chunks):
    client = FakeClient([response([(298, 300, "A", "Grazie.")]), response([(0, 2, "A", "Grazie.")])])
    result = OpenAITranscriber(client).transcribe(chunks)
    assert len(result.segments) == 2
    assert result.speaker_mapping == {"chunk-0:A": "Relatore 1", "chunk-1:A": "Relatore 2"}


def test_long_silence_and_overlapping_turns_are_not_merged(chunks):
    client = FakeClient([response([(0, 4, "A", "Uno."), (3, 5, "A", "Due."), (20, 22, "A", "Tre.")])])
    result = OpenAITranscriber(client).transcribe(chunks[:1])
    assert len(result.segments) == 3


@pytest.mark.parametrize("usage, expected", [
    ([{"type": "tokens", "input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
      {"type": "tokens", "input_tokens": 7, "output_tokens": 3, "total_tokens": 10}], (17, 5)),
    ([{"type": "tokens", "input_tokens": 10, "output_tokens": 2, "total_tokens": 12}, None], (None, None)),
    ([{"type": "duration", "seconds": 300}, {"type": "duration", "seconds": 20}], (None, None)),
])
def test_usage_requires_complete_token_accounting(chunks, usage, expected):
    client = FakeClient([response([], usage=usage[0]), response([], duration=20, usage=usage[1])])
    result = OpenAITranscriber(client).transcribe(chunks)
    assert (result.input_tokens, result.output_tokens) == expected
    assert result.audio_seconds == 320


def test_partial_usage_keeps_returned_counters_and_marks_missing(chunks):
    client = FakeClient([response([], usage={"type": "tokens", "input_tokens": 10,
        "output_tokens": 2, "total_tokens": 12}), response([], duration=20, usage={"type": "duration", "seconds": 20})])
    result = OpenAITranscriber(client).transcribe(chunks)
    assert result.usage.requests == 2
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 2
    assert result.usage.provider_audio_seconds == 20
    assert result.usage.missing_input_requests == 1


@pytest.mark.parametrize("duration", [1.99, 10.01, float("nan"), float("inf")])
def test_invalid_reference_duration_is_rejected_before_request(chunks, duration):
    client = FakeClient([])
    with pytest.raises((ValueError, TranscriptionError)):
        speaker = KnownSpeaker(name="speaker", path=Path("secret.wav"), duration_seconds=duration)
        OpenAITranscriber(client).transcribe(chunks, known_speakers=[speaker])
    assert client.requests == []


@pytest.mark.parametrize("names", [["a", "b", "c", "d", "e"], ["a", "a"]])
def test_reference_limit_and_duplicate_names_are_rejected_before_file_read(chunks, names):
    speakers = [KnownSpeaker(name=name, path=Path("secret.wav"), duration_seconds=2) for name in names]
    client = FakeClient([])
    with pytest.raises(TranscriptionError):
        OpenAITranscriber(client).transcribe(chunks, known_speakers=speakers)
    assert client.requests == []


@pytest.mark.parametrize("status, expected_attempts, retryable", [
    (400, 1, False), (401, 1, False), (403, 1, False), (404, 1, False),
    (408, 1, False), (409, 1, False), (429, 3, True), (500, 3, True), (503, 3, True),
])
def test_only_allowed_statuses_retry_and_errors_are_safe(chunks, monkeypatch, status, expected_attempts, retryable):
    sleeps = []
    monkeypatch.setattr("app.retry.time.sleep", sleeps.append)
    remote = httpx.Response(status, request=httpx.Request("POST", "https://example.test/secret"))
    error = APIStatusError("private transcript secret-key /secret/path", response=remote, body={"sensitive": "audio"})
    client = FakeClient([error] * 3)
    with pytest.raises(TranscriptionError) as caught:
        OpenAITranscriber(client).transcribe(chunks)
    assert len(client.requests) == expected_attempts
    assert sleeps == ([1.0, 2.0] if retryable else [])
    assert caught.value.retryable is retryable
    assert caught.value.status_code == status
    formatted = "".join(traceback.format_exception(caught.value))
    assert "private transcript" not in formatted and "secret-key" not in formatted
    assert "/secret/path" not in formatted and "sensitive" not in str(caught.value.__dict__)
    assert all(file.closed for file in client.files)


@pytest.mark.parametrize("kind, attempts", [("sdk_timeout", 3), ("timeout", 3), ("connection", 1)])
def test_timeout_retries_but_other_transport_failure_does_not(chunks, monkeypatch, kind, attempts):
    monkeypatch.setattr("app.retry.time.sleep", lambda seconds: None)
    request = httpx.Request("POST", "https://example.test")
    error = {"sdk_timeout": APITimeoutError(request=request), "timeout": TimeoutError("secret"),
             "connection": APIConnectionError(request=request, message="secret")}[kind]
    client = FakeClient([error] * 3)
    with pytest.raises(TranscriptionError):
        OpenAITranscriber(client).transcribe(chunks)
    assert len(client.requests) == attempts
    assert all(file.closed for file in client.files)


def test_cancelled_before_chunk_does_not_open_files_or_call_api(chunks):
    event = Event()
    event.set()
    chunks[0].path.unlink()
    client = FakeClient([])
    with pytest.raises(CancelledError):
        OpenAITranscriber(client).transcribe(chunks, cancellation_event=event)
    assert client.requests == []


@pytest.mark.parametrize("remote_fails", [False, True])
def test_cancellation_after_blocking_request_closes_file_and_allows_cleanup(tmp_path, remote_fails):
    event = Event()
    result = TimeoutError("secret") if remote_fails else response([(0, 2, "A", "Testo privato")])
    client = FakeClient([result], hook=event.set)
    with pytest.raises(CancelledError):
        with temporary_workspace(tmp_path) as workspace:
            path = workspace / "audio.m4a"
            path.write_bytes(b"audio fixture")
            OpenAITranscriber(client).transcribe([AudioChunk(path, 0, 300)], cancellation_event=event)
    assert len(client.requests) == 1
    assert all(file.closed for file in client.files)
    assert not workspace.exists()


def test_missing_audio_file_produces_safe_error(chunks):
    chunks[0].path.unlink()
    with pytest.raises(TranscriptionError) as caught:
        OpenAITranscriber(FakeClient([])).transcribe(chunks)
    assert str(chunks[0].path) not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("segments", [[(-1, 2, "A", "secret")], [(2, 1, "A", "secret")]])
def test_invalid_remote_timestamps_fail_safely(chunks, segments):
    with pytest.raises(TranscriptionError) as caught:
        OpenAITranscriber(FakeClient([response(segments)])).transcribe(chunks)
    assert "secret" not in str(caught.value)


def test_out_of_order_remote_timestamps_are_sorted_and_preserved(chunks):
    client = FakeClient([response([
        (188.366, 203.066, "A", "Prima parte."),
        (203.066, 203.266, "@", "Intervento breve."),
        (202.836, 216.036, "A", "Continuazione."),
    ])])

    result = OpenAITranscriber(client).transcribe(chunks[:1])

    assert [(segment.start_seconds, segment.end_seconds, segment.text)
            for segment in result.original_segments] == [
        (188.366, 203.066, "Prima parte."),
        (202.836, 216.036, "Continuazione."),
        (203.066, 203.266, "Intervento breve."),
    ]


def test_real_sdk_serializes_expected_multipart_and_uses_only_three_attempts(chunks, monkeypatch):
    """Exercise installed SDK parsing/options; only HTTP transport is fake."""
    requests = []
    sleeps = []
    monkeypatch.setattr("app.retry.time.sleep", sleeps.append)

    def handle(request):
        requests.append(request)
        body = request.read()
        assert b'name="model"\r\n\r\ngpt-4o-transcribe-diarize' in body
        assert b'name="response_format"\r\n\r\ndiarized_json' in body
        assert b'name="chunking_strategy"\r\n\r\nauto' in body
        assert b"audio fixture" in body
        if len(requests) < 3:
            return httpx.Response(429, json={"error": {"message": "private"}})
        return httpx.Response(200, json=response([(0, 2, "A", "Testo.")]).model_dump())

    with httpx.Client(transport=httpx.MockTransport(handle)) as http_client:
        with OpenAI(api_key="test-only", http_client=http_client) as client:
            result = OpenAITranscriber(client).transcribe(chunks[:1])
    assert len(requests) == 3
    assert sleeps == [1.0, 2.0]
    assert result.text == "Testo."
    assert result.language == "und"


@pytest.mark.parametrize("status, category", [(400, "request"), (401, "auth"), (403, "auth"),
                                            (404, "request"), (429, "rate_limit"), (503, "server")])
def test_safe_error_category_is_available_to_pipeline(chunks, monkeypatch, status, category):
    monkeypatch.setattr("app.retry.time.sleep", lambda seconds: None)
    remote = httpx.Response(status, request=httpx.Request("POST", "https://example.test"))
    error = APIStatusError("private", response=remote, body=None)
    with pytest.raises(TranscriptionError) as caught:
        OpenAITranscriber(FakeClient([error] * 3)).transcribe(chunks)
    assert caught.value.code == category


def test_four_references_include_both_duration_limits_and_avoid_generic_name_collision(chunks):
    speakers = [KnownSpeaker(name=name, path=chunks[0].path, duration_seconds=duration)
                for name, duration in [("Relatore 1", 2), ("due", 10), ("tre", 5), ("quattro", 5)]]
    client = FakeClient([response([(0, 2, "A", "Anonimo."), (2, 3, "Relatore 1", "Noto.")])])
    result = OpenAITranscriber(client).transcribe(chunks[:1], known_speakers=speakers)
    assert result.speaker_mapping == {"chunk-0:A": "Relatore 2", "chunk-0:Relatore 1": "Relatore 1"}
    assert len(client.requests[0]["known_speaker_references"]) == 4


def test_malformed_sdk_response_is_safe_and_not_retried(chunks):
    malformed = TranscriptionDiarized.model_construct(text="private transcript")
    client = FakeClient([malformed])
    with pytest.raises(TranscriptionError) as caught:
        OpenAITranscriber(client).transcribe(chunks)
    assert caught.value.code == "response"
    assert "private transcript" not in "".join(traceback.format_exception(caught.value))
    assert len(client.requests) == 1


def test_empty_speaker_label_is_not_accepted_as_an_identity(chunks):
    with pytest.raises(TranscriptionError) as caught:
        OpenAITranscriber(FakeClient([response([(0, 2, "", "private")])])).transcribe(chunks[:1])
    assert caught.value.code == "response"


@pytest.mark.parametrize("start, duration", [(-1, 300), (float("nan"), 300), (0, 0), (0, float("inf"))])
def test_invalid_chunk_metadata_fails_before_remote_request(chunks, start, duration):
    client = FakeClient([])
    with pytest.raises(TranscriptionError) as caught:
        OpenAITranscriber(client).transcribe([AudioChunk(chunks[0].path, start, duration)])
    assert caught.value.code == "chunks"
    assert client.requests == []


@pytest.mark.parametrize("filename", ["missing.wav", "unsupported.txt"])
def test_unreadable_or_unsupported_reference_fails_safely_before_upload(chunks, tmp_path, filename):
    client = FakeClient([])
    reference = KnownSpeaker(name="explicit", path=tmp_path / filename, duration_seconds=2)
    with pytest.raises(TranscriptionError) as caught:
        OpenAITranscriber(client).transcribe(chunks, known_speakers=[reference])
    assert caught.value.code == "reference"
    assert filename not in "".join(traceback.format_exception(caught.value))
    assert client.requests == []


def test_controlled_overlap_preserves_global_segment_order_and_upload_seconds(chunks):
    overlap = [chunks[0], AudioChunk(chunks[1].path, 295, 20)]
    client = FakeClient([response([(298, 300, "A", "Prima.")]), response([(0, 2, "A", "Seconda.")])])
    result = OpenAITranscriber(client).transcribe(overlap)
    assert [s.start_seconds for s in result.segments] == [295, 298]
    assert [s.start_seconds for s in result.original_segments] == [295, 298]
    assert result.audio_seconds == 320  # Total uploaded audio, including overlap.
    assert result.speaker_mapping["chunk-0:A"] != result.speaker_mapping["chunk-1:A"]
