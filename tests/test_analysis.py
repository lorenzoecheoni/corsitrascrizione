import base64
import copy
import json
from concurrent.futures import CancelledError
from io import BytesIO
from threading import Event
from types import SimpleNamespace
from uuid import UUID

import httpx
from openai import APIStatusError, OpenAI
from PIL import Image
import pytest

from app.analysis import AnalysisError, OpenAIAnalyzer, SlideBatchResult
from app.analysis_chunks import ConsolidatedTextReport, WindowAnalysis
from app.bunny import BunnyVideoMetadata
from app.media import FrameCandidate
from app.models import AcademyContent, ProviderUsage
from app.transcription import TranscriptionResult, TranscriptSegment


@pytest.fixture
def content():
    return {
        "title": "Pubblicazione Academy", "duration_seconds": 90,
        "detected_language": "it", "synopsis": "Come pubblicare contenuti.",
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
        "uncertainties": [],
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
        self.with_raw_response = SimpleNamespace(parse=self.raw_parse)
        self.max_retries = None

    def raw_parse(self, **kwargs):
        response = self.parse(**kwargs)
        return SimpleNamespace(headers=getattr(response, "headers", {}), parse=lambda: response)

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


def window_result():
    return {"detected_language": "it", "synopsis_notes": ["Pubblicazione Academy"],
            "speakers": [], "uncertainties": []}


def test_unnamed_moderator_role_reaches_consolidation_and_generic_report(inputs, content):
    inputs["frames"] = []
    evidence = [{"kind": "introduzione", "timestamp_seconds": 0,
                 "note": "Sono la moderatrice della sessione."}]
    mapped = window_result()
    mapped["speakers"] = [{"diarization_labels": ["chunk-0:A"], "display_name": None,
                           "role": "Moderatrice", "confidence": "alta", "evidence": evidence}]
    content["speakers"] = [{"id": "host", "display_name": "Relatore 1", "role": "Moderatrice",
                            "confidence": "alta", "evidence": evidence}]
    client = FakeClient(mapped, content)

    result = OpenAIAnalyzer(client).analyze(**inputs)

    candidate = json.loads(client.calls[-1]["input"])["generic_candidates"][0]
    assert candidate["role"] == "Moderatrice"
    assert candidate["evidence"] == evidence
    assert result.speakers[0].display_name == "Relatore 1"
    assert result.speakers[0].role == "Moderatrice"


def test_long_transcript_is_mapped_in_bounded_windows_before_small_final_call(inputs, content, caplog, capsys):
    inputs["metadata"].duration_seconds = 5760
    inputs["transcription"] = TranscriptionResult(
        text="private full transcript", audio_seconds=5760,
        segments=[TranscriptSegment(start_seconds=i * 60, end_seconds=(i + 1) * 60,
                                    diarization_label=f"chunk-{i // 10}:A",
                                    text=f"Segmento distinto {i}: pubblicazione Academy.")
                  for i in range(96)],
    )
    inputs["frames"] = []
    content["duration_seconds"] = 5760
    def respond(call):
        data = window_result() if call["text_format"] is WindowAnalysis else content
        if call["text_format"] is ConsolidatedTextReport:
            data = {key: value for key, value in data.items() if key != "slides"}
        return SimpleNamespace(output_parsed=data, usage=SimpleNamespace(input_tokens=10, output_tokens=2))
    client = FakeClient(*([respond] * 20))
    progress = []
    result = OpenAIAnalyzer(client).analyze(**inputs, progress_callback=lambda *args: progress.append(args))
    assert all("private full transcript" not in json.dumps(call["input"])
               for call in client.calls)
    window_calls = [call for call in client.calls if call["text_format"] is WindowAnalysis]
    assert len(window_calls) >= 10
    assert all(len(call["input"]) <= 12_000 for call in window_calls)
    assert [segment["text"] for call in window_calls for segment in json.loads(call["input"])["segments"]] == [
        segment.text for segment in inputs["transcription"].segments]
    final_call = client.calls[-1]
    assert final_call["text_format"] is ConsolidatedTextReport
    assert len(final_call["input"]) <= 30_000
    assert "segments" not in final_call["input"]
    assert "data:image" not in final_call["input"]
    assert [call["max_output_tokens"] for call in window_calls] == [2000] * len(window_calls)
    assert final_call["max_output_tokens"] == 4000
    assert all(call["model"] == "gpt-4o-mini" and call["store"] is False for call in client.calls)
    assert progress == [("transcript", i, 10) for i in range(1, 11)] + [("consolidation", 0, 1)]
    assert result.usage.requests == 11
    assert result.usage.input_tokens == 110
    assert result.usage.output_tokens == 22
    captured = caplog.text + capsys.readouterr().out
    assert "private full transcript" not in captured
    assert all(segment.text not in captured for segment in inputs["transcription"].segments)


def test_window_progress_reports_completed_windows_and_can_cancel(inputs, content):
    inputs["frames"] = []
    event = Event()
    progress = []
    def completed(phase, done, total):
        progress.append((phase, done, total))
        event.set()
    client = FakeClient(window_result(), content)
    with pytest.raises(CancelledError):
        OpenAIAnalyzer(client).analyze(**inputs, cancellation_event=event, progress_callback=completed)
    assert progress == [("transcript", 1, 1)]
    assert len(client.calls) == 1


def test_progress_reports_visual_batches_windows_and_consolidation_start(inputs, content):
    path = inputs["frames"][0].path
    inputs["metadata"].duration_seconds = 400
    content["duration_seconds"] = 400
    inputs["frames"] = [FrameCandidate(path, index * 2) for index in range(51)]
    visual_outcomes = [
        {"frames": [
            {"timestamp_seconds": index * 2, "kind": "camera_change", "confidence": "alta"}
            for index in range(start, min(start + 25, 51))
        ]}
        for start in (0, 25, 50)
    ]
    progress = []
    client = FakeClient(*visual_outcomes, window_result(), content)

    OpenAIAnalyzer(client).analyze(**inputs, progress_callback=lambda *event: progress.append(event))

    assert progress == [
        ("slides", 1, 3),
        ("slides", 2, 3),
        ("slides", 3, 3),
        ("transcript", 1, 1),
        ("consolidation", 0, 1),
    ]


def test_all_visual_slides_survive_bounded_final_context(inputs, content):
    path = inputs["frames"][0].path
    inputs["frames"] = [FrameCandidate(path, i) for i in range(80)]
    slides = [{"timestamp_seconds": i, "kind": "slide", "title": f"Slide {i}",
               "visible_content": ["x" * 1000] * 5, "confidence": "alta"} for i in range(80)]
    client = FakeClient(*[{"frames": slides[i:i + 25]} for i in range(0, 80, 25)],
                        window_result(), content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert len(json.loads(client.calls[-1]["input"])["slides"]) < 80
    assert [slide.timestamp_seconds for slide in result.slides] == list(range(80))
    assert all(slide.visible_content == ["x" * 1000] * 5 for slide in result.slides)


def test_unrepresentable_windows_have_safe_error(inputs, caplog, capsys):
    inputs["frames"] = []
    inputs["transcription"].segments = [TranscriptSegment(
        start_seconds=0, end_seconds=601, text="PRIVATE SEGMENT", diarization_label="A")]
    client = FakeClient()
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
    assert "PRIVATE" not in str(caught.value) + caplog.text + capsys.readouterr().out
    assert not client.calls


def test_only_slides_feed_final_report_and_every_call_disables_storage(inputs, content):
    client = FakeClient(visual("slide", "camera_change", "uncertain"), window_result(), content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert isinstance(result, AcademyContent)
    assert client.max_retries == 0
    assert [call["text_format"] for call in client.calls] == [SlideBatchResult, WindowAnalysis, ConsolidatedTextReport]
    assert all(call["store"] is False for call in client.calls)
    assert [call["model"] for call in client.calls] == ["gpt-5.6-luna", "gpt-4o-mini", "gpt-4o-mini"]
    payload = json.loads(client.calls[-1]["input"])
    assert "transcription" not in payload
    assert json.loads(client.calls[1]["input"])["segments"] == [{
        "start_seconds": 0.0,
        "end_seconds": 10.0,
        "diarization_label": "chunk-0:A",
        "text": "Sono Giulia Bianchi.",
    }]
    assert [item["timestamp_seconds"] for item in payload["slides"]] == [2]
    assert "Visible 1" not in client.calls[-1]["input"]
    assert "Visible 2" not in client.calls[-1]["input"]
    assert [slide.timestamp_seconds for slide in result.slides] == [2]
    assert "1 frame con classificazione incerta esclusi dalle slide." in result.uncertainties
    assert "cost" not in result.model_dump()
    images = [part for part in client.calls[0]["input"][0]["content"] if part["type"] == "input_image"]
    assert all(part["detail"] == "low" and part["image_url"].startswith("data:image/jpeg;base64,")
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
                        returned(window_result(), 30, 3), returned(bad, 200, 20), returned(content, 50, 5))
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.usage.requests == 5
    assert result.usage.input_tokens == 385
    assert result.usage.output_tokens == 39
    assert result.usage.missing_input_requests == 0


def test_final_metadata_is_limited_to_consolidation_contract(inputs, content):
    inputs["metadata"].__dict__["description"] = "Descrizione originale"
    inputs["metadata"].captions = [{"srclang": "it", "label": "Italiano", "version": 1}]
    inputs["metadata"].chapters = [{"title": "Apertura", "start": 0, "end": 10}]
    client = FakeClient(visual("slide", "camera_change", "slide"), window_result(), content)
    OpenAIAnalyzer(client).analyze(**inputs)
    metadata = json.loads(client.calls[-1]["input"])["metadata"]
    assert metadata == {"title": "Academy", "duration_seconds": 90}


def test_visual_batches_never_exceed_twenty_five_low_detail_images(inputs, content):
    path = inputs["frames"][0].path
    inputs["metadata"].duration_seconds = 400
    content["duration_seconds"] = 400
    inputs["frames"] = [FrameCandidate(path, i * 2) for i in range(101)]
    outcomes = [
        {"frames": [
            {"timestamp_seconds": i * 2, "kind": "camera_change", "confidence": "alta"}
            for i in range(start, min(start + 25, 101))
        ]}
        for start in (0, 25, 50, 75, 100)
    ]
    client = FakeClient(*outcomes, window_result(), content)
    OpenAIAnalyzer(client).analyze(**inputs)
    visual_calls = [call for call in client.calls if call["text_format"] is SlideBatchResult]
    counts = [sum(part["type"] == "input_image" for part in call["input"][0]["content"])
              for call in visual_calls]
    assert counts == [25, 25, 25, 25, 1]
    assert all(part["detail"] == "low" for call in visual_calls
               for part in call["input"][0]["content"] if part["type"] == "input_image")
    assert all(call["max_output_tokens"] == 3000 for call in visual_calls)
    assert "data:image" not in client.calls[-1]["input"]


def test_rate_limit_honors_retry_after_before_retrying(inputs, content, monkeypatch):
    inputs["frames"] = []
    sleeps = []
    monkeypatch.setattr("app.retry.time.sleep", sleeps.append)
    response = httpx.Response(
        429, headers={"Retry-After": "12.5"},
        request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    client = FakeClient(APIStatusError("safe", response=response, body={}), window_result(), content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.title == "Pubblicazione Academy"
    assert sleeps == [12.5]


def test_rate_limit_without_retry_after_waits_for_the_token_window(inputs, content, monkeypatch):
    inputs["frames"] = []
    sleeps = []
    monkeypatch.setattr("app.retry.time.sleep", sleeps.append)
    response = httpx.Response(
        429, request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
    )
    error = APIStatusError("safe", response=response, body={"error": {"code": "rate_limit_exceeded"}})
    client = FakeClient(error, error, window_result(), content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.title == "Pubblicazione Academy"
    assert sleeps == [30.0, 60.0]


@pytest.mark.parametrize("headers,delay", [
    ({"x-ratelimit-reset-project-tokens": "1m2s"}, 62),
    ({"retry-after": "bad", "x-ratelimit-reset-project-tokens": "250ms"}, 1),
    ({"retry-after": "12.5", "x-ratelimit-reset-project-tokens": "1m2s"}, 12.5),
])
def test_rate_limit_reset_fallback_and_retry_after_precedence(inputs, content, monkeypatch, headers, delay):
    inputs["frames"] = []
    sleeps = []
    monkeypatch.setattr("app.retry.time.sleep", sleeps.append)
    error = APIStatusError("PRIVATE", response=httpx.Response(
        429, headers=headers, request=httpx.Request("POST", "https://api.openai.com")), body={})
    client = FakeClient(error, window_result(), content)
    OpenAIAnalyzer(client).analyze(**inputs)
    assert sleeps == [delay]


@pytest.mark.parametrize("cancel", [False, True])
def test_raw_headers_pace_next_call_and_cancel_before_sending(inputs, content, cancel):
    from app.openai_limits import ProviderRateGate
    inputs["frames"] = []
    event = Event()
    now, waits = [100], []
    def wait(seconds, ev):
        waits.append(seconds)
        now[0] += seconds
        if cancel:
            ev.set()
    gate = ProviderRateGate(clock=lambda: now[0], wait=wait)
    response = lambda _: SimpleNamespace(output_parsed=window_result(),
        headers={"x-ratelimit-remaining-project-tokens": "3999",
                 "x-ratelimit-reset-project-tokens": "3s", "private": "SECRET"},
        usage=SimpleNamespace(input_tokens=10, output_tokens=2))
    client = FakeClient(response, content)
    analyzer = OpenAIAnalyzer(client, rate_gate=gate)
    if cancel:
        with pytest.raises(CancelledError):
            analyzer.analyze(**inputs, cancellation_event=event)
        assert len(client.calls) == 1
    else:
        result = analyzer.analyze(**inputs, cancellation_event=event)
        assert result.usage.requests == 2
        assert result.usage.input_tokens == 10
    assert waits == [3]


@pytest.mark.parametrize("stage", ["visual", "window", "consolidation"])
def test_failure_preserves_static_analysis_stage(inputs, stage):
    error = APIStatusError("SECRET", response=httpx.Response(
        400, request=httpx.Request("POST", "https://api.openai.com")), body={})
    if stage != "visual":
        inputs["frames"] = []
    client = FakeClient(*([window_result()] if stage == "consolidation" else []), error)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.stage == stage
    assert caught.value.status_code == 400
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("payload,remaining,waits", [
    ("{}", 1999, [3]), ("{}", 2000, []),
    ("x" * 9001, 3000, [3]), ("x" * 9001, 3001, []),
    ([{"role": "user", "content": [{"type": "input_image", "detail": "low",
       "image_url": "data:image/jpeg;base64,a"}]}], 2299, [3]),
    ([{"role": "user", "content": [{"type": "input_image", "detail": "low",
       "image_url": "data:image/jpeg;base64,a"}]}], 2300, []),
])
def test_gate_request_estimate_includes_output_json_and_low_detail_images(payload, remaining, waits):
    from app.openai_limits import ProviderRateGate
    now, observed = [100], []
    def wait(seconds, event):
        observed.append(seconds)
        now[0] += seconds
    gate = ProviderRateGate(clock=lambda: now[0], wait=wait)
    gate.observe("gpt-4o-mini", {"x-ratelimit-remaining-project-tokens": str(remaining),
                               "x-ratelimit-reset-project-tokens": "3s"})
    analyzer = OpenAIAnalyzer(FakeClient(window_result()), rate_gate=gate)
    result = analyzer._structured(
        text_format=WindowAnalysis, instructions="test", payload=payload,
        prepare=lambda data: None, validate=lambda result: [],
        cancellation_event=None, usage=ProviderUsage(), model="gpt-4o-mini",
        max_output_tokens=2000, max_repair_chars=12_000, stage="window")
    assert result.detected_language == "it"
    assert observed == waits


@pytest.mark.parametrize("remaining,expected_waits", [(10_500, []), (10_499, [3])])
def test_visual_pacing_counts_realistic_jpegs_once(remaining, expected_waits):
    from app.openai_limits import ProviderRateGate

    jpeg = BytesIO()
    Image.effect_noise((1280, 720), 100).convert("RGB").save(jpeg, format="JPEG", quality=85)
    assert len(jpeg.getvalue()) > 100_000
    image_url = "data:image/jpeg;base64," + base64.b64encode(jpeg.getvalue()).decode("ascii")
    parts = []
    for index in range(25):
        parts.extend([{"type": "input_text", "text": f"timestamp_seconds={index}"},
                      {"type": "input_image", "detail": "low", "image_url": image_url}])
    observed, now = [], [100]
    def wait(seconds, event):
        observed.append(seconds)
        now[0] += seconds
    gate = ProviderRateGate(clock=lambda: now[0], wait=wait)
    gate.observe("gpt-5.6-luna", {"x-ratelimit-remaining-project-tokens": str(remaining),
                               "x-ratelimit-reset-project-tokens": "3s"})
    client = FakeClient({"frames": []})

    result = OpenAIAnalyzer(client, rate_gate=gate)._structured(
        text_format=SlideBatchResult, instructions="Visual test", payload=[{"role": "user", "content": parts}],
        prepare=lambda _: None, validate=lambda _: [], cancellation_event=None,
        usage=ProviderUsage(), max_output_tokens=3000, max_repair_chars=30_000, stage="visual")

    assert result.frames == []
    assert observed == expected_waits
    assert client.calls[0]["input"][0]["content"][1]["image_url"] == image_url


def test_unsupported_names_and_roles_become_generic_with_uncertainties(inputs, content):
    inputs["frames"] = []
    content["speakers"][1].update(display_name="Nome Inventato", role="CEO", confidence="bassa",
                                   evidence=[{"kind": "inferenza", "note": "Supposizione"}])
    client = FakeClient(window_result(), content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.speakers[0].display_name == "Giulia Bianchi"
    assert result.speakers[1].display_name.startswith("Relatore ")
    assert result.speakers[1].display_name != result.speakers[2].display_name
    assert result.speakers[1].role is None
    assert result.uncertainties


@pytest.mark.parametrize("fault", ["duplicate", "duration", "language", "evidence"])
def test_semantic_errors_get_one_repair_containing_only_previous_json_and_errors(inputs, content, fault):
    inputs["frames"] = []
    bad = copy.deepcopy(content)
    if fault == "duplicate": bad["speakers"][1]["id"] = "host"
    if fault == "duration": bad["duration_seconds"] = 91
    if fault == "language": bad["detected_language"] = "und"
    if fault == "evidence": bad["speakers"][0]["evidence"][0]["timestamp_seconds"] = 91
    client = FakeClient(window_result(), bad, content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.detected_language == "it"
    assert len(client.calls) == 3
    repair = json.loads(client.calls[2]["input"])
    assert set(repair) == {"errors", "previous_json"}
    assert repair["errors"]
    assert "transcription" not in repair
    assert len(client.calls[2]["input"]) <= 30_000
    assert all(call["max_output_tokens"] == 4000 for call in client.calls[1:])


def test_oversized_final_repair_is_rejected_before_second_remote_call(inputs, content, caplog, capsys):
    inputs["frames"] = []
    inputs["transcription"].segments = []
    bad = copy.deepcopy(content)
    private_text = "PRIVATE_REPAIR_CONTENT " * 1500
    bad.update(synopsis=private_text, duration_seconds=91)
    parsed = ConsolidatedTextReport.model_validate(bad)
    client = FakeClient(parsed, content)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
    assert caught.value.__suppress_context__
    assert len(client.calls) == 1
    assert client.calls[0]["text_format"] is ConsolidatedTextReport
    assert len(client.calls[0]["input"]) <= 30_000
    captured = capsys.readouterr()
    assert "PRIVATE_REPAIR_CONTENT" not in str(caught.value) + caplog.text + captured.out + captured.err


@pytest.mark.parametrize("size, should_repair", [(11_800, True), (12_000, False)])
def test_window_repairs_count_serialized_envelope_against_cap(size, should_repair):
    bad = window_result()
    # WindowAnalysis leaves language unbounded; token limits cannot bound its JSON.
    bad["detected_language"] = "x" * size
    client = FakeClient(WindowAnalysis.model_validate(bad), window_result())
    usage = ProviderUsage()
    def request():
        return OpenAIAnalyzer(client)._structured(
            text_format=WindowAnalysis, instructions="Window instructions", payload="{}",
            prepare=lambda data: None,
            validate=lambda result: ["invalid language"] if result.detected_language != "it" else [],
            cancellation_event=None, usage=usage, model="gpt-4o-mini",
            max_output_tokens=2000, max_repair_chars=12_000,
            stage="window",
        )
    if should_repair:
        assert request().detected_language == "it"
        assert len(client.calls) == 2
        assert len(client.calls[-1]["input"]) <= 12_000
    else:
        with pytest.raises(AnalysisError) as caught:
            request()
        assert caught.value.code == "response"
        assert caught.value.__suppress_context__
        assert len(client.calls) == 1
    assert all(call["max_output_tokens"] == 2000 for call in client.calls)
    assert usage.requests == len(client.calls)


def test_nonfinite_schema_failure_is_safe_and_not_semantically_repaired(inputs, content):
    inputs["frames"] = []
    content["duration_seconds"] = float("nan")
    client = FakeClient(window_result(), content)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response" and len(client.calls) == 2


def test_second_invalid_response_has_fixed_safe_error(inputs, content):
    inputs["frames"] = []
    content["speakers"][0]["evidence"][0]["timestamp_seconds"] = 1000
    client = FakeClient(window_result(), content, content, content)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
    assert "Giulia" not in str(caught.value)
    assert len(client.calls) == 3


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
    bad["speakers"][1]["id"] = "host"
    client = FakeClient(window_result(), TimeoutError("SECRET"), bad, TimeoutError("SECRET"), content)
    with pytest.raises(AnalysisError):
        OpenAIAnalyzer(client).analyze(**inputs)
    assert len(client.calls) == 4


@pytest.mark.parametrize("phase", ["before", "visual", "window", "final"])
def test_cancellation_stops_after_blocking_call(inputs, content, phase):
    event = Event()
    if phase == "before": event.set()
    def cancel(kwargs):
        event.set()
        return SimpleNamespace(output_parsed=content if phase == "final" else visual("slide", "slide", "slide"))
    if phase in {"window", "final"}: inputs["frames"] = []
    client = FakeClient(*([window_result()] if phase == "final" else []), cancel, content)
    with pytest.raises(CancelledError):
        OpenAIAnalyzer(client).analyze(**inputs, cancellation_event=event)
    assert len(client.calls) == (0 if phase == "before" else 2 if phase == "final" else 1)


def test_visual_timestamp_mismatch_is_repaired_before_final_pass(inputs, content):
    bad = visual("slide", "camera_change", "uncertain")
    bad["frames"][0]["timestamp_seconds"] = 89
    client = FakeClient(bad, visual("slide", "camera_change", "uncertain"), window_result(), content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.slides[0].timestamp_seconds == 2
    assert len(client.calls) == 4


@pytest.mark.parametrize("invalid_name", [False, True])
def test_actual_sdk_structured_parsing_contract(inputs, content, invalid_name, caplog, capsys):
    from app.openai_limits import ProviderRateGate
    inputs["frames"] = []
    if invalid_name:
        content["speakers"][1].update(display_name="SECRET INVENTED NAME", evidence=[])
    requests = []
    now, waits = [100], []
    def wait(seconds, event):
        waits.append(seconds)
        now[0] += seconds
    gate = ProviderRateGate(clock=lambda: now[0], wait=wait)
    def respond(request):
        requests.append(json.loads(request.content))
        data = window_result() if len(requests) == 1 else content
        return httpx.Response(200, headers={"x-ratelimit-remaining-project-tokens": "0",
            "x-ratelimit-reset-project-tokens": "250ms", "private": "SECRET HEADER"}, json={
            "id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
            "model": "gpt-5.6-luna", "error": None, "incomplete_details": None,
            "instructions": None, "metadata": {}, "parallel_tool_calls": True,
            "tools": [], "tool_choice": "auto", "temperature": 1, "top_p": 1,
            "output": [{"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(data), "annotations": []}]}],
        })
    with OpenAI(api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        if invalid_name:
            with pytest.raises(AnalysisError) as caught:
                OpenAIAnalyzer(client, rate_gate=gate).analyze(**inputs)
            assert caught.value.code == "response"
            assert "SECRET" not in str(caught.value)
            assert caught.value.__suppress_context__
            assert len(requests) == 2
        else:
            result = OpenAIAnalyzer(client, rate_gate=gate).analyze(**inputs)
            assert result.speakers[0].display_name == "Giulia Bianchi"
    assert [request["text"]["format"]["name"] for request in requests] == ["WindowAnalysis", "ConsolidatedTextReport"]
    assert all(request["text"]["format"]["strict"] is True for request in requests)
    assert all(request["store"] is False for request in requests)
    assert [request["max_output_tokens"] for request in requests] == [2000, 4000]
    assert waits == [.25]
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
    client = FakeClient(window_result(), bad, content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert result.speakers[0].display_name == "Giulia Bianchi"
    assert len(client.calls) == 3


def test_incomplete_response_is_never_returned_as_complete_report(inputs, content):
    inputs["frames"] = []
    client = FakeClient(lambda kwargs: SimpleNamespace(status="incomplete", output_parsed=content))
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
