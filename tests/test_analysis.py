import copy
import json
from concurrent.futures import CancelledError
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import httpx
from openai import APIStatusError, OpenAI
from PIL import Image
import pytest

from app.analysis import AnalysisError, OpenAIAnalyzer, SlideBatchResult
from app.bunny import BunnyVideoMetadata
from app.media import FrameCandidate
from app.models import AcademyContent
from app.transcription import TranscriptionResult, TranscriptSegment


@pytest.fixture
def content():
    return {
        "title": "Pubblicazione Academy", "duration_seconds": 90,
        "detected_language": "it", "synopsis": "Come pubblicare contenuti.",
        "extended_description": "Introduzione, dimostrazione e confronto fra due relatori.",
        "target_audience": ["Redattori"], "prerequisites": ["Nessuno dichiarato"],
        "learning_objectives": ["Pubblicare un corso"],
        "speakers": [
            {"id": "host", "display_name": "Giulia Bianchi", "role": "Presentatrice",
             "confidence": "alta", "evidence": [{"kind": "introduzione", "timestamp_seconds": 0,
                                                   "note": "Sono Giulia Bianchi e presento la sessione."}]},
            {"id": "a", "display_name": "Marco Rossi", "role": "Relatore",
             "confidence": "alta", "evidence": [{"kind": "slide", "timestamp_seconds": 2,
                                                   "note": "Marco Rossi, relatore"}]},
            {"id": "b", "display_name": "Relatore 2", "role": None,
             "confidence": "bassa", "evidence": []},
        ],
        "interventions": [
            {"start_seconds": 0, "end_seconds": 10, "speaker_ids": ["host"], "summary": "Introduzione"},
            {"start_seconds": 10, "end_seconds": 90, "speaker_ids": ["a", "b"], "summary": "Confronto"},
        ],
        "chapters": [
            {"start_seconds": 0, "end_seconds": 10, "title": "Apertura", "summary": "Presentazioni"},
            {"start_seconds": 10, "end_seconds": 90, "title": "Corso", "summary": "Pubblicazione"},
        ],
        "slides": [], "topics": ["Pubblicazione"], "keywords": ["Academy"],
        "key_takeaways": ["Revisionare prima di pubblicare"], "uncertainties": [],
    }


@pytest.fixture
def inputs(tmp_path):
    image = tmp_path / "slide.jpg"
    Image.new("RGB", (32, 18), "white").save(image)
    return dict(
        metadata=BunnyVideoMetadata(video_id=UUID("12345678-1234-1234-1234-123456789abc"),
                                    title="Academy", duration_seconds=90),
        transcription=TranscriptionResult(
            text="Sono Giulia Bianchi e presento la sessione. Parliamo di pubblicazione.",
            segments=[TranscriptSegment(start_seconds=0, end_seconds=10,
                                        diarization_label="chunk-0:A", text="Sono Giulia Bianchi.")],
            speaker_mapping={"chunk-0:A": "Relatore 1"}, audio_seconds=90),
        frames=[FrameCandidate(image, 2), FrameCandidate(image, 4), FrameCandidate(image, 6)],
    )


def visual(*kinds):
    return {"frames": [dict(timestamp_seconds=2 + 2 * i, kind=kind, title=f"Frame {i}",
                            visible_content=[f"Visible {i}"], confidence="alta")
                       for i, kind in enumerate(kinds)]}


class FakeClient:
    """Remote boundary double; bypass validation only for corrupt-body tests."""
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.responses = self
        self.max_retries = None

    def with_options(self, *, max_retries):
        self.max_retries = max_retries
        return self

    def parse(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            return outcome(kwargs)
        # Dictionaries intentionally model SDK-permissive/corrupt output.
        return SimpleNamespace(output_parsed=outcome)


def test_only_slides_feed_final_report_and_every_call_disables_storage(inputs, content):
    client = FakeClient(visual("slide", "camera_change", "uncertain"), content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert isinstance(result, AcademyContent)
    assert client.max_retries == 0
    assert [call["text_format"] for call in client.calls] == [SlideBatchResult, AcademyContent]
    assert all(call["store"] is False and call["model"] == "gpt-5.6-luna" for call in client.calls)
    payload = json.loads(client.calls[-1]["input"])
    assert [item["timestamp_seconds"] for item in payload["slides"]] == [2]
    assert "Visible 1" not in client.calls[-1]["input"]
    assert "Visible 2" not in client.calls[-1]["input"]
    assert [slide.timestamp_seconds for slide in result.slides] == [2]
    assert "cost" not in result.model_dump()
    images = [part for part in client.calls[0]["input"][0]["content"] if part["type"] == "input_image"]
    assert all(part["detail"] == "high" and part["image_url"].startswith("data:image/jpeg;base64,")
               for part in images)


def test_usage_counts_batches_retry_error_and_semantic_repair(inputs, content, monkeypatch):
    monkeypatch.setattr("app.retry.time.sleep", lambda _: None)
    failure = APIStatusError("safe", response=httpx.Response(503, request=httpx.Request("POST", "https://api.openai.com")),
                             body={"usage": {"input_tokens": 5, "output_tokens": 1}})
    bad = copy.deepcopy(content)
    bad["detected_language"] = "und"
    def returned(data, input_tokens, output_tokens):
        return lambda _: SimpleNamespace(output_parsed=data, status="completed",
            usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens))
    client = FakeClient(failure, returned(visual("slide", "camera_change", "slide"), 100, 10),
                        returned(bad, 200, 20), returned(content, 50, 5))
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.usage.requests == 4
    assert result.usage.input_tokens == 355
    assert result.usage.output_tokens == 36
    assert result.usage.missing_input_requests == 0


def test_metadata_description_and_existing_evidence_reach_analysis(inputs, content):
    inputs["metadata"].__dict__["description"] = "Descrizione originale"
    inputs["metadata"].captions = [{"srclang": "it", "label": "Italiano", "version": 1}]
    inputs["metadata"].chapters = [{"title": "Apertura", "start": 0, "end": 10}]
    client = FakeClient(visual("slide", "camera_change", "slide"), content)
    OpenAIAnalyzer(client).analyze(**inputs)
    metadata = json.loads(client.calls[-1]["input"])["metadata"]
    assert metadata["description"] == "Descrizione originale"
    assert metadata["captions"][0]["srclang"] == "it"
    assert metadata["chapters"][0]["title"] == "Apertura"


def test_visual_batches_never_exceed_twenty_images(inputs, content):
    path = inputs["frames"][0].path
    inputs["frames"] = [FrameCandidate(path, i * 2) for i in range(41)]
    outcomes = [{"frames": [dict(timestamp_seconds=i * 2, kind="camera_change", confidence="alta")
                             for i in range(start, min(start + 20, 41))]}
                for start in (0, 20, 40)]
    client = FakeClient(*outcomes, content)
    OpenAIAnalyzer(client).analyze(**inputs)
    counts = [sum(part["type"] == "input_image" for part in call["input"][0]["content"])
              for call in client.calls[:-1]]
    assert counts == [20, 20, 1]
    assert "data:image" not in client.calls[-1]["input"]


def test_unsupported_names_and_roles_become_generic_with_uncertainties(inputs, content):
    inputs["frames"] = []
    content["speakers"][1].update(display_name="Nome Inventato", role="CEO", confidence="bassa",
                                   evidence=[{"kind": "inferenza", "note": "Supposizione"}])
    client = FakeClient(content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.speakers[0].display_name == "Giulia Bianchi"
    assert result.speakers[1].display_name.startswith("Relatore ")
    assert result.speakers[1].display_name != result.speakers[2].display_name
    assert result.speakers[1].role is None
    assert result.uncertainties
    assert result.interventions[1].speaker_ids == ["a", "b"]


@pytest.mark.parametrize("fault", ["overlap", "range", "order", "speaker", "duplicate",
                                  "empty_speakers", "empty_section", "duration", "language", "evidence"])
def test_semantic_errors_get_one_repair_containing_only_previous_json_and_errors(inputs, content, fault):
    inputs["frames"] = []
    bad = copy.deepcopy(content)
    if fault == "overlap": bad["chapters"][1]["start_seconds"] = 9
    if fault == "range": bad["interventions"][1]["end_seconds"] = 91
    if fault == "order": bad["interventions"].reverse()
    if fault == "speaker": bad["interventions"][0]["speaker_ids"] = ["missing"]
    if fault == "duplicate": bad["speakers"][1]["id"] = "host"
    if fault == "empty_speakers": bad["interventions"][0]["speaker_ids"] = []
    if fault == "empty_section": bad["learning_objectives"] = []
    if fault == "duration": bad["duration_seconds"] = 91
    if fault == "language": bad["detected_language"] = "und"
    if fault == "evidence": bad["speakers"][0]["evidence"][0]["timestamp_seconds"] = 91
    client = FakeClient(bad, content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.detected_language == "it"
    assert len(client.calls) == 2
    repair = json.loads(client.calls[1]["input"])
    assert set(repair) == {"errors", "previous_json"}
    assert repair["errors"]
    assert "transcription" not in repair


def test_nonfinite_schema_failure_is_safe_and_not_semantically_repaired(inputs, content):
    inputs["frames"] = []
    content["interventions"][0]["start_seconds"] = float("nan")
    client = FakeClient(content)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response" and len(client.calls) == 1


def test_second_invalid_response_has_fixed_safe_error(inputs, content):
    inputs["frames"] = []
    content["chapters"][0]["end_seconds"] = 1000
    client = FakeClient(content, content, content)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
    assert "Giulia" not in str(caught.value)
    assert len(client.calls) == 2


@pytest.mark.parametrize("status,attempts", [(429, 3), (503, 3), (401, 1), (400, 1), (409, 1)])
def test_selective_shared_retry_policy(inputs, status, attempts, monkeypatch):
    inputs["frames"] = []
    monkeypatch.setattr("app.retry.time.sleep", lambda seconds: None)
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    error = APIStatusError("SECRET REMOTE ERROR", response=response, body={"secret": "transcript"})
    client = FakeClient(error, error, error, error)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert "SECRET" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert len(client.calls) == attempts


def test_repair_and_transient_errors_share_three_remote_attempt_budget(inputs, content, monkeypatch):
    inputs["frames"] = []
    monkeypatch.setattr("app.retry.time.sleep", lambda seconds: None)
    bad = copy.deepcopy(content)
    bad["chapters"][0]["end_seconds"] = 100
    client = FakeClient(TimeoutError("SECRET"), bad, TimeoutError("SECRET"), content)
    with pytest.raises(AnalysisError):
        OpenAIAnalyzer(client).analyze(**inputs)
    assert len(client.calls) == 3


@pytest.mark.parametrize("phase", ["before", "visual", "final"])
def test_cancellation_stops_after_blocking_call(inputs, content, phase):
    event = Event()
    if phase == "before": event.set()
    def cancel(kwargs):
        event.set()
        return SimpleNamespace(output_parsed=content if phase == "final" else visual("slide", "slide", "slide"))
    if phase == "final": inputs["frames"] = []
    client = FakeClient(cancel, content)
    with pytest.raises(CancelledError):
        OpenAIAnalyzer(client).analyze(**inputs, cancellation_event=event)
    assert len(client.calls) == (0 if phase == "before" else 1)


def test_visual_timestamp_mismatch_is_repaired_before_final_pass(inputs, content):
    bad = visual("slide", "camera_change", "uncertain")
    bad["frames"][0]["timestamp_seconds"] = 89
    client = FakeClient(bad, visual("slide", "camera_change", "uncertain"), content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.slides[0].timestamp_seconds == 2
    assert len(client.calls) == 3


@pytest.mark.parametrize("invalid_name", [False, True])
def test_actual_sdk_structured_parsing_contract(inputs, content, invalid_name, caplog, capsys):
    inputs["frames"] = []
    if invalid_name:
        content["speakers"][1].update(display_name="SECRET INVENTED NAME", evidence=[])
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
            "model": "gpt-5.6-luna", "error": None, "incomplete_details": None,
            "instructions": None, "metadata": {}, "parallel_tool_calls": True,
            "tools": [], "tool_choice": "auto", "temperature": 1, "top_p": 1,
            "output": [{"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(content), "annotations": []}]}],
        })
    with OpenAI(api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        if invalid_name:
            with pytest.raises(AnalysisError) as caught:
                OpenAIAnalyzer(client).analyze(**inputs)
            assert caught.value.code == "response"
            assert "SECRET" not in str(caught.value)
            assert caught.value.__suppress_context__
            assert len(requests) == 1
        else:
            result = OpenAIAnalyzer(client).analyze(**inputs)
            assert result.speakers[0].display_name == "Giulia Bianchi"
    assert requests[0]["text"]["format"]["name"] == "AcademyContent"
    assert requests[0]["text"]["format"]["strict"] is True
    assert requests[0]["store"] is False
    assert "SECRET" not in caplog.text + capsys.readouterr().out


@pytest.mark.parametrize("bad", [None, [], {}, {"speakers": [None]}])
def test_malformed_outputs_are_sanitized(inputs, bad):
    inputs["frames"] = []
    client = FakeClient(bad)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
    assert len(client.calls) == 1


def test_missing_image_never_exposes_path_or_sends_partial_batch(inputs):
    inputs["frames"][0].path.unlink()
    client = FakeClient()
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "frames"
    assert str(inputs["frames"][0].path) not in str(caught.value)
    assert not client.calls


def test_duplicate_generic_speakers_must_be_repaired(inputs, content):
    inputs["frames"] = []
    bad = copy.deepcopy(content)
    bad["speakers"][0]["display_name"] = "Relatore 2"
    client = FakeClient(bad, content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.speakers[0].display_name == "Giulia Bianchi"
    assert len(client.calls) == 2


def test_incomplete_response_is_never_returned_as_complete_report(inputs, content):
    inputs["frames"] = []
    client = FakeClient(lambda kwargs: SimpleNamespace(status="incomplete", output_parsed=content))
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
