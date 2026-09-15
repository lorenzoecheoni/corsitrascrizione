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
from app.boundaries import BoundaryAlignment
from app.bunny import BunnyVideoMetadata
from app.media import FrameCandidate, SilenceInterval
from app.models import AcademyContent, BoundaryEvidence, Intervention, ProviderUsage
from app.prompts import CONSOLIDATION_PROMPT, REPAIR_PROMPT, WINDOW_PROMPT
from app.transcription import TranscriptWord, TranscriptionResult, TranscriptSegment


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
            segments=[TranscriptSegment(
                start_seconds=0, end_seconds=10, diarization_label="chunk-0:A",
                text="Sono Giulia Bianchi.", source_utterance_id="assembly-u000001",
                words=[TranscriptWord(
                    text="Giulia", start_seconds=1, end_seconds=2,
                    diarization_label="chunk-0:A", confidence=.99,
                )],
            )],
            speaker_mapping={"chunk-0:A": "Relatore 1"}, audio_seconds=90),
        frames=[FrameCandidate(image, 2), FrameCandidate(image, 4), FrameCandidate(image, 6)],
        silence_intervals=[],
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


def window_result(segment_indexes=None, diarization_labels=None):
    return {
        "detected_language": "it",
        "synopsis_notes": ["Pubblicazione Academy"],
        "speakers": [],
        "uncertainties": [],
        "interventions": [{
            "segment_indexes": [0] if segment_indexes is None else segment_indexes,
            "tipo": "intervento",
            "diarization_labels": ["chunk-0:A"] if diarization_labels is None else diarization_labels,
            "titolo": "Pubblicazione Academy",
            "sintesi": "Il relatore illustra il processo di pubblicazione.",
            "punti_chiave": ["Preparazione", "Pubblicazione", "Controllo"],
            "confidenza": 0.9,
        }],
    }


def test_analyzer_plans_every_window_with_all_classified_slides(inputs, content, monkeypatch):
    from app import chapters
    from app.models import SlideChange

    inputs["metadata"].duration_seconds = content["duration_seconds"] = 1100.75
    inputs["frames"] = []
    inputs["transcription"].segments = [TranscriptSegment(
        start_seconds=start, end_seconds=end, diarization_label="chunk-0:A",
        source_utterance_id=f"u-{index}", text=f"Tema {index}",
        words=[TranscriptWord(text=f"Tema-{index}.", start_seconds=start, end_seconds=end,
                              diarization_label="chunk-0:A", confidence=.99)],
    ) for index, (start, end) in enumerate([(0, 540), (541, 1080), (1081, 1100.7)])]
    slides = [SlideChange(timestamp_seconds=time, title=title, confidence="alta")
              for time, title in [(0, "Premesse"), (541, "Controlli"), (1099, "Fine")]]
    monkeypatch.setattr(OpenAIAnalyzer, "_classify_slides", lambda *args, **kwargs: (slides, 0))
    seen = []
    original = chapters.plan_semantic_timeline

    def observe(groups, slides):
        seen.append((groups, slides))
        return original(groups, slides)

    # Observe the real planner, keeping planning and audio alignment active.
    monkeypatch.setattr(chapters, "plan_semantic_timeline", observe)
    monkeypatch.setattr("app.analysis_chunks.plan_semantic_timeline", observe, raising=False)

    def respond(call):
        if call["text_format"] is ConsolidatedTextReport:
            return SimpleNamespace(output_parsed=content)
        payload = json.loads(call["input"])
        data = window_result()
        data["interventions"] = [dict(window_result()["interventions"][0],
                                      segment_indexes=[item["segment_index"]],
                                      titolo=f"Tema {item['source_utterance_id']}",
                                      confine_motivo="cambio_tema") for item in payload["segments"]]
        data["previous_continuity"] = "separate" if payload.get("previous_context") else None
        return SimpleNamespace(output_parsed=data)

    client = FakeClient(*([respond] * 5))
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert len(seen) == 1
    assert [segment.source_utterance_id for group in seen[0][0] for segment in group.segments] == [
        "u-0", "u-1", "u-2",
    ]
    assert [slide.timestamp_seconds for slide in seen[0][1]] == [0, 541, 1099]
    assert result.analysis_profile == 2
    assert result.duration_seconds == 1100.75
    assert [(block.id, block.start_seconds, block.end_seconds) for block in result.speech_blocks] == [
        ("b001", 0, 1100),
    ]
    assert [(chapter.block_id, chapter.chapter_number, chapter.chapters_in_block,
             chapter.start_seconds, chapter.end_seconds) for chapter in result.interventions] == [
        ("b001", 1, 2, 0, 540), ("b001", 2, 2, 540, 1100),
    ]
    assert result.interventions[1].boundary_origin.motivo_editoriale == "slide_e_tema"
    assert result.interventions[1].boundary_origin.slide_indizio_seconds == 541
    assert "source_utterance_id" not in result.model_dump_json()
    repeated = OpenAIAnalyzer(FakeClient(*([respond] * 5))).analyze(**inputs)
    assert repeated.model_dump() == result.model_dump()


@pytest.mark.parametrize("invalid_fields", [
    {"punti_chiave": ["PRIVATE-POINT", "PRIVATE-POINT", "Terzo"]},
    {"confine_motivo": "slide_e_tema", "slide_indizio_seconds": 79},
    {"confine_motivo": "cambio_tema", "slide_indizio_seconds": 2},
    {"confine_motivo": "slide_e_tema", "slide_indizio_seconds": None},
])
def test_invalid_theme_evidence_regenerates_original_window_before_planning(
    inputs, content, invalid_fields, caplog,
):
    invalid = window_result()
    invalid["interventions"][0].update(invalid_fields)
    client = FakeClient(visual("slide", "camera_change", "uncertain"), invalid,
                        window_result(), content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    windows = [call for call in client.calls if call["text_format"] is WindowAnalysis]
    assert len(windows) == 2
    assert windows[1]["input"] == windows[0]["input"]
    assert windows[1]["instructions"] == WINDOW_PROMPT
    assert result.interventions[0].punti_chiave == ["Preparazione", "Pubblicazione", "Controllo"]
    assert "PRIVATE-POINT" not in result.model_dump_json() + caplog.text


def test_valid_window_slide_reference_is_accepted_without_retry(inputs, content):
    data = window_result()
    data["interventions"][0].update(confine_motivo="slide_e_tema", slide_indizio_seconds=2)
    client = FakeClient(visual("slide", "camera_change", "uncertain"), data, content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert len(client.calls) == 3
    assert json.loads(client.calls[1]["input"])["slide_hints"] == [
        {"timestamp_seconds": 2, "title": "Frame 0"},
    ]
    assert result.interventions[0].boundary_origin.slide_indizio_seconds == 2


def test_window_provider_schema_requires_editorial_fields_without_wire_defaults():
    from openai.lib._pydantic import to_strict_json_schema

    schema = to_strict_json_schema(WindowAnalysis)["$defs"]["WindowInterventionDraft"]
    assert {"confine_motivo", "slide_indizio_seconds"} <= set(schema["required"])
    assert "default" not in schema["properties"]["confine_motivo"]
    assert "default" not in schema["properties"]["slide_indizio_seconds"]


def test_classified_slide_omitted_by_payload_cap_cannot_be_referenced(inputs, content, monkeypatch):
    from app.models import SlideChange

    slides = [SlideChange(timestamp_seconds=index, title=f"Tema {index}", confidence="alta")
              for index in range(10)]
    monkeypatch.setattr(OpenAIAnalyzer, "_classify_slides", lambda *args, **kwargs: (slides, 0))
    invalid = window_result()
    invalid["interventions"][0].update(confine_motivo="slide_e_tema", slide_indizio_seconds=9)
    valid = window_result()
    valid["interventions"][0].update(confine_motivo="slide_e_tema", slide_indizio_seconds=7)
    client = FakeClient(invalid, valid, content)
    result = OpenAIAnalyzer(client).analyze(**inputs)
    assert [hint["timestamp_seconds"] for hint in json.loads(client.calls[0]["input"])["slide_hints"]] == list(range(8))
    assert client.calls[1]["input"] == client.calls[0]["input"]
    assert len(client.calls) == 3
    assert [slide.timestamp_seconds for slide in result.slides] == list(range(10))
    assert result.interventions[0].start_seconds == 0


@pytest.mark.parametrize("invalid_fields", [
    {"punti_chiave": ["PRIVATE-DUPLICATE"] * 3},
    {"confine_motivo": "slide_e_tema", "slide_indizio_seconds": 2},
    {"confine_motivo": "slide_e_tema", "slide_indizio_seconds": float("nan")},
    {"confine_motivo": "slide_e_tema", "slide_indizio_seconds": -1},
])
def test_invalid_theme_evidence_exhausts_window_retries_safely(inputs, invalid_fields, caplog):
    inputs["frames"] = []
    invalid = window_result()
    invalid["interventions"][0].update(invalid_fields)
    client = FakeClient(invalid, invalid, invalid)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.stage == "boundary"
    assert caught.value.detail_code == "window_contract"
    assert len(client.calls) == 3
    assert all(call["input"] == client.calls[0]["input"] for call in client.calls)
    assert all(call["instructions"] == WINDOW_PROMPT for call in client.calls)
    assert "PRIVATE-DUPLICATE" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("corruption", ["blocks", "block_id", "origin", "number", "duplicate_block",
                                        "block_overrun", "duplicate_chapter", "gap", "overlap"])
def test_result_with_slides_rejects_invalid_chapter_block_provenance(inputs, content, corruption):
    result = OpenAIAnalyzer(FakeClient(visual("slide", "camera_change", "uncertain"),
                                      window_result(), content)).analyze(**inputs)
    alignment = BoundaryAlignment(interventions=copy.deepcopy(result.interventions),
                                  boundaries=copy.deepcopy(result.boundaries),
                                  blocks=copy.deepcopy(result.speech_blocks))
    assert alignment.blocks
    if corruption == "blocks":
        alignment.blocks.clear()
    elif corruption == "block_id":
        alignment.interventions[0].block_id = "missing"
    elif corruption == "origin":
        alignment.interventions[0].boundary_origin = None
    elif corruption == "number":
        alignment.interventions[0].chapter_number = 2
    elif corruption == "duplicate_block":
        alignment.blocks.append(copy.deepcopy(alignment.blocks[0]))
    elif corruption == "block_overrun":
        alignment.blocks[0].end_seconds = 91
    elif corruption == "duplicate_chapter":
        alignment.interventions.append(copy.deepcopy(alignment.interventions[0]))
    elif corruption == "gap":
        alignment.interventions[0].start_seconds = 1
    else:
        alignment.interventions[0].end_seconds = 91
    with pytest.raises(AnalysisError, match="Verifica audio"):
        OpenAIAnalyzer._result_with_slides(ConsolidatedTextReport(**content), [], alignment,
                                         0, ProviderUsage(), None)


def test_window_prompt_requires_complete_semantic_groups_without_model_boundaries():
    requirement = """Raggruppa tutte le utterance che completano la stessa frase, esempio,
spiegazione, risposta o linea di ragionamento. Non creare mai un confine nel
mezzo di questi elementi. Se interviene il moderatore, assegna le sue
utterance a un segmento autonomo saluti, domande o cambio_relatore: non
accodarle all'intervento precedente. I timestamp finali sono calcolati
localmente dall'audio: non scegliere mai il secondo finale di un confine."""

    assert requirement in WINDOW_PROMPT
    assert "CONFINE" not in WINDOW_PROMPT
    assert "timestamp del silenzio" not in WINDOW_PROMPT.lower()
    assert "piccole unità tematiche omogenee" in WINDOW_PROMPT
    assert "quando anche il parlato chiude un" in WINDOW_PROMPT
    assert "Il cambio di slide da solo non giustifica un taglio" in WINDOW_PROMPT
    assert "non ripetere o inventare" in WINDOW_PROMPT


@pytest.mark.parametrize("limit", ["seconds", "characters"])
@pytest.mark.parametrize("continuation", [
    ("Quindi per completare la frase dobbiamo", "aggiungere il secondo termine."),
    ("Consideriamo questo esempio. Primo passaggio concluso.", "Ora il secondo passaggio dello stesso esempio."),
    ("Per rispondere alla domanda, partiamo dalle deleghe.", "La risposta si completa valutando i controlli."),
])
def test_window_seam_continuation_is_decided_with_previous_context(
    inputs, content, limit, continuation, caplog,
):
    before, after = continuation
    end, start, duration = (599, 600, 620) if limit == "seconds" else (9, 10, 30)
    texts = [before, after] if limit == "seconds" else ["Premessa. " * 700 + before, after + " Spiegazione." * 580]
    inputs["metadata"].duration_seconds = content["duration_seconds"] = duration
    inputs["frames"] = []

    def word_evidence(text, left, right):
        terms = text.split()
        return [TranscriptWord(
            text=term,
            start_seconds=left + (right - left) * index / len(terms),
            end_seconds=left + (right - left) * (index + 1) / len(terms),
            diarization_label="assembly:A", confidence=.9,
        ) for index, term in enumerate(terms)]

    inputs["transcription"].segments = [TranscriptSegment(
        start_seconds=left, end_seconds=right, diarization_label="assembly:A",
        text=text, source_utterance_id=f"distinct-{index}",
        words=word_evidence(text, left, right),
    ) for index, (left, right, text) in enumerate([(0, end, texts[0]), (start, duration - 1, texts[1])])]

    payloads = []

    def respond(call):
        if call["text_format"] is ConsolidatedTextReport:
            return SimpleNamespace(output_parsed=content)
        payload = json.loads(call["input"])
        data = window_result([item["segment_index"] for item in payload["segments"]], ["assembly:A"])
        if payload["start_seconds"]:
            context = payload.get("previous_context")
            assert context is not None
            assert payloads[-1]["segments"][-1]["text"].endswith(context["segments"][-1]["text"])
            data["previous_continuity"] = "continue"
        payloads.append(payload)
        return SimpleNamespace(output_parsed=data, usage=SimpleNamespace(input_tokens=11, output_tokens=3))

    client = FakeClient(*([respond] * 6))
    result = OpenAIAnalyzer(client).analyze_fast(**inputs)

    assert len(result.interventions) == 1
    assert (result.interventions[0].start_seconds, result.interventions[0].end_seconds) == (0, duration)
    assert result.boundaries == []
    assert getattr(result, "audio_boundary_version", None) == 1
    assert len(payloads) >= 2
    assert " ".join(item["text"] for payload in payloads for item in payload["segments"]) == " ".join(texts)
    assert len(client.calls) == len(payloads) + 1  # Windows and consolidation; no seam request.
    assert result.usage.requests == len(payloads) + 1
    assert result.usage.input_tokens == 11 * len(payloads)
    assert all(len(call["input"]) <= 12_000 for call in client.calls[:-1])
    assert all("PRIVATE-WORD-EVIDENCE" not in call["input"] for call in client.calls)
    assert before not in caplog.text and "PRIVATE-WORD-EVIDENCE" not in caplog.text


@pytest.mark.parametrize("decision", [None, "unresolved"])
def test_window_seam_unresolved_fails_before_context_free_repair(inputs, content, decision):
    inputs["frames"] = []
    inputs["metadata"].duration_seconds = 620
    first = inputs["transcription"].segments[0]
    inputs["transcription"].segments.append(first.model_copy(update={
        "start_seconds": 600, "end_seconds": 610, "source_utterance_id": "distinct-u2",
        "words": [first.words[0].model_copy(update={"start_seconds": 600, "end_seconds": 610})],
    }))
    second = window_result()
    second["previous_continuity"] = decision
    client = FakeClient(window_result(), second, second, second)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "boundaries"
    assert caught.value.stage == "boundary"
    assert len(client.calls) == 2


def test_window_seam_cancellation_drops_context_without_extra_call(inputs, content):
    inputs["frames"] = []
    inputs["metadata"].duration_seconds = 620
    first = inputs["transcription"].segments[0]
    inputs["transcription"].segments.append(first.model_copy(update={
        "start_seconds": 600, "end_seconds": 610, "source_utterance_id": "distinct-u2",
        "words": [first.words[0].model_copy(update={"start_seconds": 600, "end_seconds": 610})],
    }))
    event = Event()
    client = FakeClient(window_result())
    with pytest.raises(CancelledError):
        OpenAIAnalyzer(client).analyze(**inputs, cancellation_event=event,
                                      progress_callback=lambda *_: event.set())
    assert len(client.calls) == 1


def test_analyzer_aligns_semantic_groups_to_measured_silence_and_returns_compact_evidence(
    inputs, content,
):
    inputs["metadata"].duration_seconds = 20
    content["duration_seconds"] = 20
    inputs["frames"] = []
    inputs["silence_intervals"] = [SilenceInterval(8, 12)]
    inputs["transcription"] = TranscriptionResult(
        provider="assemblyai", language="it", text="FULL-TRANSCRIPT-SENTINEL",
        audio_seconds=20,
        segments=[
            TranscriptSegment(
                start_seconds=1, end_seconds=8, diarization_label="assembly:A",
                text="Prima spiegazione completa.", source_utterance_id="u1",
                words=[TranscriptWord(
                    text="prima", start_seconds=1, end_seconds=8,
                    diarization_label="assembly:A", confidence=.99,
                )],
            ),
            TranscriptSegment(
                start_seconds=12, end_seconds=19, diarization_label="assembly:B",
                text="Seconda risposta completa.", source_utterance_id="u2",
                words=[TranscriptWord(
                    text="seconda", start_seconds=12, end_seconds=19,
                    diarization_label="assembly:B", confidence=.98,
                )],
            ),
        ],
    )
    mapped = window_result([0], ["assembly:A"])
    mapped["interventions"].append({
        **window_result([1], ["assembly:B"])["interventions"][0],
        "titolo": "Seconda risposta",
    })

    result = OpenAIAnalyzer(FakeClient(mapped, content)).analyze_fast(**inputs)

    assert [(item.start_seconds, item.end_seconds) for item in result.interventions] == [
        (0, 9), (9, 11), (11, 20),
    ]
    assert [item.boundary_seconds for item in result.boundaries] == [9, 11]
    assert all(item.rule == "long_pause" for item in result.boundaries)
    serialized = result.model_dump_json()
    assert "FULL-TRANSCRIPT-SENTINEL" not in serialized
    assert "assembly-u" not in serialized
    assert '"confidence":0.99' not in serialized


def test_analyzer_fails_closed_when_word_boundary_evidence_is_missing(inputs, content):
    inputs["frames"] = []
    inputs["transcription"].segments[0].words = []

    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(FakeClient(window_result(), content)).analyze(**inputs)

    assert caught.value.code == "boundaries"
    assert caught.value.stage == "boundary"
    assert str(caught.value) == "Verifica audio dei confini non riuscita; riprova"


def test_ai_pause_on_words_is_rejected_after_bounded_schema_retries(inputs, content):
    inputs["frames"] = []
    invalid = window_result()
    invalid["interventions"][0].update(tipo="pausa", punti_chiave=[])
    client = FakeClient(invalid, invalid, invalid, content)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "boundaries"
    assert str(caught.value) == "Verifica audio dei confini non riuscita; riprova"
    assert len(client.calls) == 3


def test_analyzer_fails_closed_when_silence_collection_is_unavailable(inputs, content):
    inputs["frames"] = []
    inputs["silence_intervals"] = None

    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(FakeClient(window_result(), content)).analyze(**inputs)

    assert caught.value.code == "boundaries"
    assert str(caught.value) == "Verifica audio dei confini non riuscita; riprova"


def test_boundary_analyzer_fails_closed_after_incomplete_semantic_group_repair(inputs, content):
    inputs["frames"] = []
    incomplete = window_result(segment_indexes=[0, 0])

    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(FakeClient(incomplete, incomplete, incomplete)).analyze(**inputs)

    assert caught.value.code == "boundaries"
    assert caught.value.stage == "boundary"
    assert str(caught.value) == "Verifica audio dei confini non riuscita; riprova"


def test_boundary_analyzer_maps_schema_invalid_partition_after_retries(inputs, content):
    inputs["frames"] = []
    invalid = window_result()
    invalid["interventions"][0]["segment_indexes"] = ["not-an-index"]

    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(FakeClient(invalid, invalid, invalid)).analyze(**inputs)

    assert caught.value.code == "boundaries"
    assert caught.value.stage == "boundary"
    assert str(caught.value) == "Verifica audio dei confini non riuscita; riprova"


def test_boundary_analyzer_rejects_nonadjacent_materializer_output(inputs, content, monkeypatch):
    inputs["frames"] = []
    malformed = BoundaryAlignment(
        interventions=[
            Intervention(
                id="i001", start_seconds=0, end_seconds=40, tipo="intervento",
                relatori=[], titolo="Prima", sintesi="Prima parte.",
                punti_chiave=["Uno", "Due", "Tre"], confidenza=.9,
            ),
            Intervention(
                id="i002", start_seconds=41, end_seconds=90, tipo="intervento",
                relatori=[], titolo="Seconda", sintesi="Seconda parte.",
                punti_chiave=["Quattro", "Cinque", "Sei"], confidenza=.9,
            ),
        ],
        boundaries=[BoundaryEvidence(
            previous_intervention_id="i001", next_intervention_id="i002",
            boundary_seconds=40, words_before=["prima"], words_after=["seconda"],
            pause_before=True, pause_after=True, rule="no_pause",
        )],
    )
    monkeypatch.setattr("app.analysis.materialize_interventions", lambda *args: malformed)

    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(FakeClient(window_result(), content)).analyze(**inputs)

    assert caught.value.code == "boundaries"
    assert caught.value.stage == "boundary"


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


def test_supported_window_identity_is_attached_to_intervention(inputs, content):
    inputs["frames"] = []
    mapped = window_result()
    mapped["speakers"] = [{
        "diarization_labels": ["chunk-0:A"],
        "display_name": "Giulia Bianchi",
        "role": "Presentatrice",
        "confidence": "alta",
        "evidence": [{
            "kind": "introduzione", "timestamp_seconds": 0,
            "note": "Sono Giulia Bianchi e presento la sessione.",
        }],
    }]

    result = OpenAIAnalyzer(FakeClient(mapped, content)).analyze(**inputs)

    assert result.interventions[0].relatori == ["Giulia Bianchi"]
    assert result.interventions[0].start_seconds == 0
    assert result.interventions[-1].end_seconds == 90


def test_inventory_hints_canonicalize_spelling_but_provider_letters_stay_generic(inputs, content):
    inputs["frames"] = []
    evidence = [{
        "kind": "introduzione", "timestamp_seconds": 0,
        "note": "Il relatore viene introdotto oralmente.",
    }]
    mapped = window_result()
    mapped["speakers"] = [
        {"diarization_labels": ["chunk-0:A"], "display_name": "Fulvio D'Andrea",
         "role": "Relatore", "confidence": "alta", "evidence": evidence},
        {"diarization_labels": ["assembly:F"], "display_name": "F",
         "role": "Relatore", "confidence": "alta", "evidence": evidence},
    ]
    content["speakers"] = [
        {"id": "furio", "display_name": "Fulvio D'Andrea", "role": "Relatore",
         "confidence": "alta", "evidence": evidence},
        {"id": "provider-f", "display_name": "F", "role": "Relatore",
         "confidence": "alta", "evidence": evidence},
    ]

    result = OpenAIAnalyzer(FakeClient(mapped, content)).analyze(
        **inputs, speaker_name_hints=["Furio d'Andrea"],
    )

    assert result.speakers[0].display_name == "Furio d'Andrea"
    assert result.speakers[1].display_name == "Relatore 1"
    assert result.interventions[0].relatori == ["Furio d'Andrea"]


def test_long_transcript_is_mapped_in_bounded_windows_before_small_final_call(inputs, content, caplog, capsys):
    inputs["metadata"].duration_seconds = 5760
    inputs["transcription"] = TranscriptionResult(
        text="private full transcript", audio_seconds=5760,
        segments=[TranscriptSegment(
            start_seconds=i * 60, end_seconds=(i + 1) * 60,
                diarization_label=f"chunk-{i // 10}:A", source_utterance_id=f"u{i}",
                text=f"Segmento distinto {i}: pubblicazione Academy.",
                words=[TranscriptWord(
                    text=word, start_seconds=i * 60 + 1 + word_index,
                    end_seconds=i * 60 + 1.4 + word_index,
                    diarization_label=f"chunk-{i // 10}:A", confidence=.9,
                ) for word_index, word in enumerate(
                    f"Segmento distinto {i}: pubblicazione Academy.".split()
                )],
        )
                  for i in range(96)],
    )
    inputs["frames"] = []
    content["duration_seconds"] = 5760
    def respond(call):
        if call["text_format"] is WindowAnalysis:
            payload = json.loads(call["input"])
            segments = payload["segments"]
            data = window_result(
                [segment["segment_index"] for segment in segments],
                list(dict.fromkeys(segment["diarization_label"] for segment in segments)),
            )
            if payload.get("previous_context"):
                data["previous_continuity"] = "separate"
        else:
            data = content
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


def test_long_provider_segment_is_split_before_remote_analysis(inputs, content, caplog, capsys):
    inputs["frames"] = []
    inputs["metadata"].duration_seconds = 601
    content["duration_seconds"] = 601
    inputs["transcription"].segments = [TranscriptSegment(
        start_seconds=0, end_seconds=601, text="PRIVATE SEGMENT", diarization_label="A",
        source_utterance_id="long-u1", words=[TranscriptWord(
            text="evidenza", start_seconds=1, end_seconds=600,
            diarization_label="A", confidence=.9,
        )])]
    client = FakeClient(
        window_result(diarization_labels=["A"]),
        window_result(diarization_labels=["A"]),
        content,
    )

    result = OpenAIAnalyzer(client).analyze(**inputs)

    assert result.duration_seconds == 601
    assert "PRIVATE" not in caplog.text + capsys.readouterr().out
    assert len(client.calls) == 3


def test_only_slides_feed_final_report_and_every_call_disables_storage(inputs, content):
    inputs["transcription"].segments[0].source_utterance_id = "assembly-u000001"
    inputs["transcription"].segments[0].words = [TranscriptWord(
        text="Sono Giulia Bianchi.", start_seconds=0, end_seconds=10,
        diarization_label="chunk-0:A", confidence=.97,
    )]
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
        "segment_index": 0,
        "start_seconds": 0.0,
        "end_seconds": 10.0,
        "diarization_label": "chunk-0:A",
        "text": "Sono Giulia Bianchi.",
        "source_utterance_id": "assembly-u000001",
    }]
    assert "PRIVATE-WORD-EVIDENCE" not in client.calls[1]["input"]
    assert [item["timestamp_seconds"] for item in payload["slides"]] == [2]
    assert "Visible 1" not in client.calls[-1]["input"]
    assert "Visible 2" not in client.calls[-1]["input"]
    assert [slide.timestamp_seconds for slide in result.slides] == [2]
    assert "1 frame con classificazione incerta esclusi dalle slide." in result.uncertainties
    assert "cost" not in result.model_dump()
    images = [part for part in client.calls[0]["input"][0]["content"] if part["type"] == "input_image"]
    assert all(part["detail"] == "low" and part["image_url"].startswith("data:image/jpeg;base64,")
               for part in images)


def test_fast_analysis_uses_complete_window_path_for_globally_diarized_transcript(inputs, content):
    client = FakeClient(
        visual("slide", "camera_change", "uncertain"), window_result(), content
    )
    progress = []

    result = OpenAIAnalyzer(client).analyze_fast(
        **inputs, progress_callback=lambda *event: progress.append(event),
    )

    assert [call["text_format"] for call in client.calls] == [
        SlideBatchResult, WindowAnalysis, ConsolidatedTextReport,
    ]
    assert [call["model"] for call in client.calls] == [
        "gpt-5.6-luna", "gpt-4o-mini", "gpt-4o-mini",
    ]
    assert all(call["store"] is False for call in client.calls)
    window_payload = json.loads(client.calls[1]["input"])
    assert window_payload["segments"][0]["diarization_label"] == "chunk-0:A"
    assert window_payload["segments"][0]["segment_index"] == 0
    assert "transcription" not in json.loads(client.calls[-1]["input"])
    assert "data:image" not in client.calls[-1]["input"]
    assert [slide.timestamp_seconds for slide in result.slides] == [2]
    assert result.interventions[0].start_seconds == 0
    assert result.interventions[-1].end_seconds == 90
    assert result.usage.requests == 3
    assert progress == [("slides", 1, 1), ("transcript", 1, 1), ("consolidation", 0, 1)]


def test_fast_analysis_normalizes_unsupported_name_after_sdk_wire_validation(inputs, content):
    inputs["frames"] = []
    content["speakers"] = [{
        "id": "provider-name",
        "display_name": "Nome suggerito dal fornitore",
        "role": "Relatore",
        "confidence": "alta",
        "evidence": [],
    }]

    def sdk_validates_before_returning(call):
        data = window_result() if call["text_format"] is WindowAnalysis else content
        return SimpleNamespace(output_parsed=call["text_format"].model_validate(data))

    result = OpenAIAnalyzer(
        FakeClient(sdk_validates_before_returning, sdk_validates_before_returning)
    ).analyze_fast(**inputs)

    assert result.speakers[0].display_name == "Relatore 1"
    assert result.speakers[0].role is None
    assert any("non supportata" in note for note in result.uncertainties)


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
    inputs["transcription"].segments[0].words = [TranscriptWord(
        text="PRIVATE-WORD-EVIDENCE", start_seconds=0, end_seconds=1,
        diarization_label="chunk-0:A", confidence=.97,
    )]
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
    assert "PRIVATE-WORD-EVIDENCE" not in client.calls[2]["input"]
    assert len(client.calls[2]["input"]) <= 30_000
    assert all(call["max_output_tokens"] == 4000 for call in client.calls[1:])


def test_oversized_final_repair_is_rejected_before_second_remote_call(inputs, content, caplog, capsys):
    inputs["frames"] = []
    bad = copy.deepcopy(content)
    private_text = "PRIVATE_REPAIR_CONTENT " * 1500
    bad.update(synopsis=private_text, duration_seconds=91)
    parsed = ConsolidatedTextReport.model_validate(bad)
    client = FakeClient(window_result(), parsed, content)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
    assert caught.value.__suppress_context__
    assert len(client.calls) == 2
    assert client.calls[1]["text_format"] is ConsolidatedTextReport
    assert len(client.calls[1]["input"]) <= 30_000
    captured = capsys.readouterr()
    assert "PRIVATE_REPAIR_CONTENT" not in str(caught.value) + caplog.text + captured.out + captured.err


@pytest.mark.parametrize("size, should_repair", [(11_400, True), (12_000, False)])
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
    client = FakeClient(window_result(), content, content, content)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response" and len(client.calls) == 4


def test_second_invalid_response_has_fixed_safe_error(inputs, content):
    inputs["frames"] = []
    content["speakers"][0]["evidence"][0]["timestamp_seconds"] = 1000
    client = FakeClient(window_result(), content, content, content)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
    assert "Giulia" not in str(caught.value)
    assert len(client.calls) == 4


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
        result = OpenAIAnalyzer(client, rate_gate=gate).analyze(**inputs)
        if invalid_name:
            assert result.speakers[1].display_name == "Relatore 1"
            assert result.speakers[1].role is None
            assert "SECRET INVENTED NAME" not in result.model_dump_json()
        else:
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
    client = FakeClient(bad, bad, bad)
    with pytest.raises(AnalysisError) as caught:
        OpenAIAnalyzer(client).analyze(**inputs)
    assert caught.value.code == "response"
    assert len(client.calls) == 3


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


def test_output_limit_incomplete_window_retries_once_with_double_budget(inputs, content):
    inputs["frames"] = []
    incomplete = lambda kwargs: SimpleNamespace(
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        output_parsed=None,
        usage=SimpleNamespace(input_tokens=20, output_tokens=2000),
    )
    client = FakeClient(incomplete, window_result(), content)

    result = OpenAIAnalyzer(client).analyze(**inputs)

    assert result.title == "Pubblicazione Academy"
    assert [call["max_output_tokens"] for call in client.calls] == [2000, 4000, 4000]
    assert result.usage.requests == 3


def test_completed_response_without_parsed_json_retries_only_the_window(inputs, content):
    inputs["frames"] = []
    missing = lambda kwargs: SimpleNamespace(
        status="completed", output_parsed=None,
        usage=SimpleNamespace(input_tokens=20, output_tokens=10),
    )
    client = FakeClient(missing, window_result(), content)

    result = OpenAIAnalyzer(client).analyze(**inputs)

    assert result.title == "Pubblicazione Academy"
    assert [call["max_output_tokens"] for call in client.calls] == [2000, 4000, 4000]


def test_failed_window_repair_gets_one_fresh_final_attempt(inputs, content):
    inputs["frames"] = []
    invalid = window_result(segment_indexes=[0, 0])
    client = FakeClient(invalid, invalid, window_result(), content)

    result = OpenAIAnalyzer(client).analyze(**inputs)

    assert result.title == "Pubblicazione Academy"
    assert client.calls[0]["instructions"] == WINDOW_PROMPT
    assert client.calls[1]["instructions"] == REPAIR_PROMPT
    assert client.calls[2]["instructions"] == WINDOW_PROMPT
    assert [call["max_output_tokens"] for call in client.calls[:3]] == [2000, 2000, 4000]


@pytest.mark.parametrize("prompt", [WINDOW_PROMPT, CONSOLIDATION_PROMPT])
def test_analysis_prompts_require_deterministic_editorial_handoff(prompt: str) -> None:
    lowered = " ".join(prompt.casefold().split())

    assert "20 secondi" in lowered
    assert "inizino dal contenuto" in lowered
    assert "punti_chiave" in prompt and "solo" in lowered and "intervento" in lowered
    assert "non assegnare accessi" in lowered
    assert "non generare verifiche academy" in lowered
