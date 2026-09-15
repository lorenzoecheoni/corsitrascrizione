import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.bunny import BunnyVideoMetadata
from app.models import SlideChange
from app.transcription import TranscriptSegment, TranscriptWord, TranscriptionResult


def _spoken(start, end, label, text, source=""):
    return TranscriptSegment(
        start_seconds=start, end_seconds=end, diarization_label=label, text=text,
        source_utterance_id=source,
        words=[TranscriptWord(text=text, start_seconds=start, end_seconds=end,
                              diarization_label=label, confidence=.9)],
    )


def _draft(indexes, **changes):
    data = {
        "segment_indexes": indexes,
        "tipo": "intervento",
        "diarization_labels": ["assembly:A"],
        "titolo": "Assetti di governance",
        "sintesi": "Il relatore illustra gli assetti di governance.",
        "punti_chiave": ["Assetti", "Deleghe", "Controlli"],
        "confidenza": 0.9,
    }
    data.update(changes)
    return data


def test_window_payload_has_stable_local_segment_indexes():
    from app.analysis_chunks import split_transcript_windows

    segments = [
        TranscriptSegment(start_seconds=0, end_seconds=5, diarization_label="A", text="uno"),
        TranscriptSegment(start_seconds=5, end_seconds=10, diarization_label="B", text="due"),
    ]

    payload = json.loads(split_transcript_windows(segments)[0].to_payload())

    assert [item["segment_index"] for item in payload["segments"]] == [0, 1]


def test_window_payload_preserves_source_utterance_id_without_word_evidence():
    from app.analysis_chunks import split_transcript_windows
    from app.transcription import TranscriptWord

    segment = TranscriptSegment(
        start_seconds=0, end_seconds=5, diarization_label="assembly:A", text="uno",
        source_utterance_id="assembly-u000001",
        words=[TranscriptWord(
            text="PRIVATE-WORD-EVIDENCE", start_seconds=0, end_seconds=1,
            diarization_label="assembly:A", confidence=.97,
        )],
    )

    payload = split_transcript_windows([segment])[0].to_payload()

    assert "PRIVATE-WORD-EVIDENCE" not in payload
    assert json.loads(payload)["segments"][0]["source_utterance_id"] == "assembly-u000001"


def test_materialize_interventions_aligns_measured_pause_and_covers_entire_duration():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows
    from app.media import SilenceInterval

    segments = [
        _spoken(2.2, 8.4, "assembly:A", "uno"),
        _spoken(12.2, 20.2, "assembly:B", "due"),
    ]
    windows = split_transcript_windows(segments)
    analyses = [WindowAnalysis(
        detected_language="it", synopsis_notes=["Governance"], speakers=[], uncertainties=[],
        interventions=[
            _draft([0]),
            _draft([1], diarization_labels=["assembly:B"], titolo="Controlli"),
        ],
    )]

    result = materialize_interventions(
        25, windows, analyses,
        {"assembly:A": "Mario Rossi", "assembly:B": "Anna Bianchi"},
        [SilenceInterval(8.5, 12.1)],
    )

    assert [(item.start_seconds, item.end_seconds, item.tipo) for item in result.interventions] == [
        (0, 9, "intervento"),
        (9, 11, "pausa"),
        (11, 25, "intervento"),
    ]
    assert result.interventions[0].relatori == ["Mario Rossi"]
    assert result.interventions[2].relatori == ["Anna Bianchi"]
    assert [item.id for item in result.interventions] == ["i001", "i002", "i003"]
    assert [(item.previous_intervention_id, item.next_intervention_id) for item in result.boundaries] == [
        ("i001", "i002"), ("i002", "i003"),
    ]


def test_materialize_interventions_supports_joint_speakers_and_omits_generic_names():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    segments = [
        _spoken(0, 60, "assembly:A", "uno"),
        _spoken(60, 120, "assembly:B", "due"),
    ]
    windows = split_transcript_windows(segments)
    analyses = [WindowAnalysis(
        detected_language="it", synopsis_notes=[], speakers=[], uncertainties=[],
        interventions=[_draft([0, 1], diarization_labels=["assembly:A", "assembly:B"])],
    )]

    result = materialize_interventions(
        120, windows, analyses,
        {"assembly:A": "Mario Rossi", "assembly:B": "Relatore 2"},
        [],
    )

    assert len(result.interventions) == 1
    assert result.interventions[0].relatori == ["Mario Rossi"]
    assert (result.interventions[0].start_seconds, result.interventions[0].end_seconds) == (0, 120)


@pytest.mark.parametrize(
    "drafts",
    [
        [_draft([0])],
        [_draft([0, 1]), _draft([1, 2])],
        [_draft([0, 2]), _draft([1])],
        [_draft([2]), _draft([0, 1])],
    ],
)
def test_materialize_interventions_rejects_missing_duplicate_noncontiguous_or_unordered_indexes(drafts):
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    segments = [
        TranscriptSegment(start_seconds=index * 10, end_seconds=(index + 1) * 10,
                          diarization_label="A", text=str(index))
        for index in range(3)
    ]
    windows = split_transcript_windows(segments)
    analyses = [WindowAnalysis(
        detected_language="it", synopsis_notes=[], speakers=[], uncertainties=[],
        interventions=drafts,
    )]

    with pytest.raises(ValueError, match="partizione"):
        materialize_interventions(30, windows, analyses, {}, [])


def test_materialize_interventions_rejects_overlapping_speech():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    segments = [
        _spoken(0, 10.6, "A", "uno"),
        _spoken(9.4, 20, "B", "due"),
    ]
    windows = split_transcript_windows(segments)
    analyses = [WindowAnalysis(
        detected_language="it", synopsis_notes=[], speakers=[], uncertainties=[],
        interventions=[
            _draft([0], diarization_labels=["A"]),
            _draft([1], diarization_labels=["B"], tipo="domande", punti_chiave=[]),
        ],
    )]

    with pytest.raises(ValueError):
        materialize_interventions(20, windows, analyses, {}, [])


def test_materialize_merges_shared_source_utterance_across_adjacent_windows():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    original = _spoken(0, 1300, "assembly:A", "esempio " * 2000, "utterance-1")
    windows = split_transcript_windows([original])
    analyses = [WindowAnalysis(
        detected_language="it", synopsis_notes=[], speakers=[],
        interventions=[_draft([index], titolo=f"Parte {index}") for index in range(len(window.segments))],
    ) for window in windows]

    result = materialize_interventions(1300, windows, analyses, {"assembly:A": "Mario Rossi"}, [])

    assert len(windows) >= 3
    assert len(result.interventions) == 1
    assert (result.interventions[0].start_seconds, result.interventions[0].end_seconds) == (0, 1300)
    assert result.interventions[0].relatori == ["Mario Rossi"]
    assert result.boundaries == []


def test_materialize_allows_semantic_types_inside_one_provider_utterance():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    windows = split_transcript_windows([
        _spoken(0, 5, "assembly:A", "Prima parte", "u1"),
        _spoken(5, 10, "assembly:A", "Seconda parte", "u1"),
    ])
    analyses = [WindowAnalysis(
        detected_language="it", synopsis_notes=[], speakers=[],
        interventions=[
            _draft([0]),
            _draft([1], tipo="cambio_relatore", punti_chiave=[]),
        ],
    )]

    result = materialize_interventions(10, windows, analyses, {}, [])

    assert [item.tipo for item in result.interventions] == [
        "intervento", "cambio_relatore",
    ]
    assert result.boundaries[0].boundary_seconds == 5


def test_materialize_does_not_merge_distinct_groups_only_because_a_source_id_is_shared():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    windows = split_transcript_windows([
        _spoken(0, 5, "assembly:A", "Prima parte", "u1"),
        _spoken(5, 10, "assembly:A", "Seconda parte", "u1"),
        _spoken(10, 12, "assembly:M", "Passiamo oltre", "u2"),
    ])
    analyses = [WindowAnalysis(
        detected_language="it", synopsis_notes=[], speakers=[],
        interventions=[_draft([0]), _draft([1, 2], diarization_labels=[])],
    )]

    result = materialize_interventions(12, windows, analyses, {}, [])

    assert len(result.interventions) == 2
    assert result.boundaries[0].boundary_seconds == 5


def test_materialize_respects_explicit_separation_at_window_seam_inside_provider_utterance():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    windows = split_transcript_windows([
        _spoken(0, 599, "assembly:A", "Prima parte conclusa.", "u1"),
        _spoken(600, 610, "assembly:A", "Nuovo tema.", "u1"),
    ])
    analyses = [
        WindowAnalysis(
            detected_language="it", synopsis_notes=[], speakers=[],
            interventions=[_draft([0])],
        ),
        WindowAnalysis(
            detected_language="it", synopsis_notes=[], speakers=[],
            previous_continuity="separate", interventions=[_draft([0])],
        ),
    ]

    result = materialize_interventions(610, windows, analyses, {}, [])

    assert len(result.interventions) == 2
    assert result.boundaries[0].boundary_seconds == 599


def test_materialize_keeps_conflicting_types_separate_and_marks_low_confidence():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    windows = split_transcript_windows([
        _spoken(0, 599, "assembly:A", "Intervento concluso.", "u1"),
        _spoken(600, 610, "assembly:A", "Passiamo oltre.", "u1"),
    ])
    analyses = [
        WindowAnalysis(
            detected_language="it", synopsis_notes=[], speakers=[],
            interventions=[_draft([0])],
        ),
        WindowAnalysis(
            detected_language="it", synopsis_notes=[], speakers=[],
            previous_continuity="continue",
            interventions=[_draft(
                [0], tipo="cambio_relatore", punti_chiave=[], confidenza=.95,
            )],
        ),
    ]

    result = materialize_interventions(610, windows, analyses, {}, [])

    assert [item.tipo for item in result.interventions] == [
        "intervento", "cambio_relatore",
    ]
    assert result.interventions[1].confidenza == .7
    assert result.boundaries[0].boundary_seconds == 599


def test_materialize_preserves_one_example_grouped_over_three_utterances():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows
    from app.media import SilenceInterval

    windows = split_transcript_windows([
        _spoken(0, 5, "assembly:A", "Premessa", "u1"),
        _spoken(7, 10, "assembly:A", "Esempio", "u2"),
        _spoken(12, 15, "assembly:A", "Conclusione", "u3"),
    ])
    analyses = [WindowAnalysis(detected_language="it", synopsis_notes=[], speakers=[],
                               interventions=[_draft([0, 1, 2])])]
    result = materialize_interventions(15, windows, analyses, {}, [SilenceInterval(5, 7)])
    assert len(result.interventions) == 1
    assert result.boundaries == []


@pytest.mark.parametrize("decision", [None, "unresolved"])
def test_materialize_rejects_unresolved_window_seam_with_distinct_source_ids(decision):
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    windows = split_transcript_windows([
        _spoken(0, 599, "assembly:A", "Per completare l'esempio dobbiamo", "u1"),
        _spoken(600, 610, "assembly:A", "aggiungere il secondo termine.", "u2"),
    ])
    analyses = [WindowAnalysis(detected_language="it", synopsis_notes=[], speakers=[],
                               interventions=[_draft([0])]) for _ in windows]
    if decision is not None:
        analyses[1] = analyses[1].model_copy(update={"previous_continuity": decision})
    with pytest.raises(ValueError, match="partizione"):
        materialize_interventions(610, windows, analyses, {}, [])


@pytest.mark.parametrize("kind,label", [("intervento", "assembly:A"), ("cambio_relatore", "assembly:M")])
def test_materialize_preserves_explicitly_complete_seam_and_moderator(kind, label):
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    windows = split_transcript_windows([
        _spoken(0, 599, "assembly:A", "Esempio concluso.", "u1"),
        _spoken(600, 610, label, "Passiamo al nuovo tema.", "u2"),
    ])
    analyses = [WindowAnalysis(detected_language="it", synopsis_notes=[], speakers=[],
                               interventions=[_draft([0])]),
                WindowAnalysis(detected_language="it", synopsis_notes=[], speakers=[],
                               interventions=[_draft([0], tipo=kind, diarization_labels=[label],
                                                     punti_chiave=["a", "b", "c"] if kind == "intervento" else [])])]
    analyses[1] = analyses[1].model_copy(update={"previous_continuity": "separate"})
    result = materialize_interventions(610, windows, analyses, {}, [])
    assert [item.tipo for item in result.interventions] == ["intervento", kind]
    assert len(result.boundaries) == 1
    assert result.boundaries[0].boundary_seconds == 599


def test_explicit_seam_continuation_can_extend_split_source_with_next_utterance():
    from app.analysis_chunks import WindowAnalysis, materialize_interventions, split_transcript_windows

    windows = split_transcript_windows([
        _spoken(0, 599, "assembly:A", "Premessa lunga", "u1"),
        _spoken(600, 700, "assembly:A", "completa la frase", "u1"),
        _spoken(701, 710, "assembly:A", "e conclude lo stesso esempio.", "u2"),
    ])
    analyses = [WindowAnalysis(detected_language="it", synopsis_notes=[], speakers=[],
                               interventions=[_draft([0])]),
                WindowAnalysis(detected_language="it", synopsis_notes=[], speakers=[],
                               previous_continuity="continue", interventions=[_draft([0, 1])])]
    result = materialize_interventions(710, windows, analyses, {}, [])
    assert len(result.interventions) == 1
    assert result.boundaries == []


def test_previous_context_escaping_is_bounded_and_hints_cannot_drop_it():
    from app.analysis import _window_payload
    from app.analysis_chunks import WindowAnalysis, previous_window_context, split_transcript_windows

    windows = split_transcript_windows([
        _spoken(0, 599, "assembly:A", "\"\\\n" * 5000 + "Conclusione della premessa", "u1"),
        _spoken(600, 610, "assembly:A", "Si conclude lo stesso esempio.", "u2"),
    ])
    previous = windows[-2]
    analysis = WindowAnalysis(detected_language="it", synopsis_notes=[], speakers=[],
                              interventions=[_draft(list(range(len(previous.segments))))])
    context = previous_window_context(previous, analysis)
    payload = _window_payload(windows[-1], ["Nome " * 30] * 20, context)
    assert len(payload) <= 12_000
    assert json.loads(payload)["previous_context"] == context
    assert context["prefix_omitted"] is True
    assert context["segments"][-1]["text"].endswith("Conclusione della premessa")


def test_previous_context_oversized_required_metadata_fails_without_hanging():
    import subprocess
    import sys

    code = """
from app.analysis_chunks import WindowAnalysis, previous_window_context, split_transcript_windows
from app.transcription import TranscriptSegment
segment = TranscriptSegment(start_seconds=0, end_seconds=10, text='parola',
                            diarization_label='A', source_utterance_id='x' * 4000)
window = split_transcript_windows([segment])[0]
analysis = WindowAnalysis(detected_language='it', synopsis_notes=[], speakers=[], interventions=[
    dict(segment_indexes=[0], tipo='saluti', titolo='Saluti', sintesi='Saluti.', confidenza=.9)])
try:
    previous_window_context(window, analysis)
except ValueError:
    pass
else:
    raise AssertionError('Oversized required context must fail closed')
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, timeout=2)


def test_prompts_preserve_every_announced_presenter_moderator_and_speaker_without_voice_mapping():
    from app.prompts import CONSOLIDATION_PROMPT, WINDOW_PROMPT

    assert "ogni persona esplicitamente annunciata" in WINDOW_PROMPT
    assert "diarization_labels=[]" in WINDOW_PROMPT
    assert "Non eliminare persone esplicitamente annunciate" in CONSOLIDATION_PROMPT


def test_four_hours_are_split_without_loss_or_oversized_payloads():
    from app.analysis_chunks import split_transcript_windows

    segments = [
        TranscriptSegment(
            start_seconds=float(second), end_seconds=float(second + 30),
            diarization_label=f"chunk-{second // 600}:A", text="x" * 900,
        )
        for second in range(0, 14_400, 30)
    ]

    windows = split_transcript_windows(segments)

    flattened = [segment for window in windows for segment in window.segments]
    assert flattened == segments
    assert all(window.end_seconds - window.start_seconds <= 600 for window in windows)
    assert all(len(window.to_payload()) <= 12_000 for window in windows)


def test_segment_crossing_a_time_boundary_is_kept_once():
    from app.analysis_chunks import split_transcript_windows

    segments = [
        TranscriptSegment(start_seconds=0, end_seconds=590, diarization_label="chunk-0:A", text="first"),
        TranscriptSegment(start_seconds=590, end_seconds=610, diarization_label="chunk-0:B", text="boundary"),
        TranscriptSegment(start_seconds=610, end_seconds=620, diarization_label="chunk-1:A", text="last"),
    ]

    windows = split_transcript_windows(segments)

    assert [segment for window in windows for segment in window.segments] == segments
    assert all(window.end_seconds - window.start_seconds <= 600 for window in windows)


def test_one_oversized_text_segment_is_split_deterministically_with_original_timing():
    from app.analysis_chunks import MAX_WINDOW_CHARS, split_transcript_windows

    segment = TranscriptSegment(
        start_seconds=15, end_seconds=45, diarization_label="chunk-0:A", text="a" * 50_000,
    )

    windows = split_transcript_windows([segment])
    pieces = [piece for window in windows for piece in window.segments]

    assert len(pieces) > 1
    assert "".join(piece.text for piece in pieces) == segment.text
    assert all((piece.start_seconds, piece.end_seconds) == (15, 45) for piece in pieces)
    assert all(len(window.to_payload()) <= MAX_WINDOW_CHARS for window in windows)


def test_split_segments_preserve_source_utterance_id_without_serializing_words():
    from app.analysis_chunks import MAX_WINDOW_CHARS, split_transcript_windows
    from app.transcription import TranscriptWord

    segment = TranscriptSegment(
        start_seconds=15, end_seconds=45, diarization_label="assembly:A", text="a" * 50_000,
        source_utterance_id="assembly-u000001",
        words=[TranscriptWord(
            text="PRIVATE-WORD-EVIDENCE", start_seconds=15, end_seconds=16,
            diarization_label="assembly:A", confidence=.97,
        )],
    )

    windows = split_transcript_windows([segment])
    pieces = [piece for window in windows for piece in window.segments]

    assert all(piece.source_utterance_id == "assembly-u000001" for piece in pieces)
    assert all("PRIVATE-WORD-EVIDENCE" not in window.to_payload() for window in windows)
    assert all(len(window.to_payload()) <= MAX_WINDOW_CHARS for window in windows)


def test_one_long_provider_utterance_is_split_by_time_without_text_loss():
    from app.analysis_chunks import MAX_WINDOW_SECONDS, split_transcript_windows

    segment = TranscriptSegment(
        start_seconds=120,
        end_seconds=1921,
        diarization_label="assembly:A",
        text="Intervento continuativo " * 900,
    )

    windows = split_transcript_windows([segment])
    pieces = [piece for window in windows for piece in window.segments]

    assert len(pieces) >= 4
    assert "".join(piece.text for piece in pieces) == segment.text
    assert pieces[0].start_seconds == segment.start_seconds
    assert pieces[-1].end_seconds == segment.end_seconds
    assert all(left.end_seconds == right.start_seconds for left, right in zip(pieces, pieces[1:]))
    assert all(piece.end_seconds - piece.start_seconds <= MAX_WINDOW_SECONDS for piece in pieces)
    assert all(window.end_seconds - window.start_seconds <= MAX_WINDOW_SECONDS for window in windows)


def test_consolidation_payload_prioritizes_supported_evidence_and_stays_bounded():
    from app.analysis_chunks import (
        MAX_CONSOLIDATION_CHARS,
        WindowAnalysis,
        WindowSpeaker,
        build_consolidation_payload,
    )
    from app.models import Evidence

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"),
        title="Sessione Academy", duration_seconds=14_400,
    )
    analyses = [
        WindowAnalysis(
            detected_language="it",
            synopsis_notes=[f"nota {index}: " + "x" * 280 for index in range(4)],
            speakers=[
                WindowSpeaker(
                    diarization_labels=["chunk-0:A"], display_name="Giulia Bianchi", role="Moderatrice",
                    confidence="alta", evidence=[
                        Evidence(kind="introduzione", timestamp_seconds=1, note="Mi chiamo Giulia Bianchi."),
                    ],
                ),
                WindowSpeaker(
                    diarization_labels=["chunk-0:B"], display_name="Nome non supportato",
                    confidence="bassa",
                ),
            ],
            uncertainties=["Ruolo non determinabile."],
        )
    ]
    slides = [
        SlideChange(timestamp_seconds=index, title=f"Slide {index}", visible_content=["x" * 300], confidence="alta")
        for index in range(200)
    ]

    payload = build_consolidation_payload(metadata, analyses, slides)
    parsed = json.loads(payload)

    assert len(payload) <= MAX_CONSOLIDATION_CHARS
    assert "transcription" not in payload
    assert parsed["supported_candidates"][0]["display_name"] == "Giulia Bianchi"
    assert parsed["supported_candidates"][0]["evidence"][0]["kind"] == "introduzione"
    assert parsed["generic_candidates"][0]["diarization_labels"] == ["chunk-0:B"]
    assert parsed["slides"][0]["timestamp_seconds"] == 0
    assert parsed["synopsis_notes"]


def test_empty_identity_evidence_does_not_support_a_personal_name():
    from app.analysis_chunks import WindowAnalysis, WindowSpeaker, build_consolidation_payload
    from app.models import Evidence

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"), title="Sessione", duration_seconds=60,
    )
    analyses = [
        WindowAnalysis(
            detected_language="it", synopsis_notes=[],
            speakers=[WindowSpeaker(
                diarization_labels=["chunk-0:A"], display_name="Nome non verificato", confidence="bassa",
                evidence=[Evidence(kind="introduzione", timestamp_seconds=1, note="")],
            )],
        )
    ]

    payload = json.loads(build_consolidation_payload(metadata, analyses, []))

    assert payload["supported_candidates"] == []
    assert payload["generic_candidates"] == [{"diarization_labels": ["chunk-0:A"], "confidence": "bassa"}]


@pytest.mark.parametrize("kind,note,supported", [
    ("introduzione", "Sono la moderatrice della sessione.", True),
    ("inferenza", "Sembra coordinare gli interventi.", False),
    ("introduzione", " ", False),
])
def test_unnamed_speaker_keeps_only_explicit_role_and_evidence(kind, note, supported):
    from app.analysis_chunks import WindowAnalysis, WindowSpeaker, build_consolidation_payload
    from app.models import Evidence

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"), title="Sessione", duration_seconds=600,
    )
    analysis = WindowAnalysis(detected_language="it", synopsis_notes=["Il seminario si apre."],
        speakers=[WindowSpeaker(diarization_labels=["chunk-0:A"], display_name=None,
            role="Moderatrice", confidence="alta",
            evidence=[Evidence(kind=kind, timestamp_seconds=5, note=note)])])
    slides = [SlideChange(timestamp_seconds=index, title="x" * 300,
                          visible_content=["x" * 300] * 4, confidence="alta") for index in range(100)]

    payload = json.loads(build_consolidation_payload(metadata, [analysis], slides))

    assert payload["supported_candidates"] == []
    candidate = payload["generic_candidates"][0]
    assert candidate["diarization_labels"] == ["chunk-0:A"]
    assert candidate.get("display_name") is None
    if supported:
        assert candidate["role"] == "Moderatrice"
        assert candidate["evidence"] == [{"kind": kind, "timestamp_seconds": 5, "note": note}]
    else:
        assert not candidate.get("role")
        assert not candidate.get("evidence")


def test_unnamed_role_evidence_is_mandatory_under_budget_pressure():
    from app.analysis_chunks import ConsolidationPayloadError, WindowAnalysis, WindowSpeaker, build_consolidation_payload
    from app.models import Evidence

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"), title="Sessione", duration_seconds=600,
    )
    analyses = [WindowAnalysis(detected_language="it", synopsis_notes=["Seminario."],
        speakers=[WindowSpeaker(diarization_labels=[f"chunk-{index}:A"], role="Moderatrice",
            confidence="alta", evidence=[Evidence(kind="introduzione", timestamp_seconds=index,
                note="Contesto. " * 25 + "Sono la moderatrice.")])]) for index in range(100)]

    with pytest.raises(ConsolidationPayloadError, match="dati obbligatori"):
        build_consolidation_payload(metadata, analyses, [])


def test_supported_candidates_keep_complete_evidence_and_summary_from_every_window():
    from app.analysis_chunks import WindowAnalysis, WindowSpeaker, build_consolidation_payload
    from app.models import Evidence

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"), title="Sessione", duration_seconds=60,
    )
    speakers = [
        WindowSpeaker(
            diarization_labels=[f"chunk-{index}:A"], display_name=f"Nome supportato {index}",
            confidence="alta", evidence=[Evidence(
                kind="introduzione", timestamp_seconds=float(index),
                note=f"Mi chiamo Nome supportato {index} e presento il caso {index}.",
            )],
        )
        for index in range(100)
    ]
    analyses = [
        WindowAnalysis(detected_language="it", synopsis_notes=[
            f"Finestra {index}: conclusione specifica del caso discusso.",
            "Approfondimento: " + "x" * 280,
        ], speakers=speakers[index:index + 8])
        for index in range(0, len(speakers), 8)
    ]

    slides = [SlideChange(timestamp_seconds=index, title="x" * 300,
                          visible_content=["x" * 300] * 4, confidence="alta") for index in range(100)]
    payload = json.loads(build_consolidation_payload(metadata, analyses, slides))

    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= 30_000
    assert len(payload["supported_candidates"]) == 100
    assert [candidate["display_name"] for candidate in payload["supported_candidates"]] == [
        f"Nome supportato {index}" for index in range(100)
    ]
    assert [candidate["evidence"][0]["timestamp_seconds"] for candidate in payload["supported_candidates"]] == [
        float(index) for index in range(100)
    ]
    assert {candidate["evidence"][0]["kind"] for candidate in payload["supported_candidates"]} == {"introduzione"}
    assert [candidate["evidence"][0]["note"] for candidate in payload["supported_candidates"]] == [
        f"Mi chiamo Nome supportato {index} e presento il caso {index}." for index in range(100)
    ]
    assert all(analysis.synopsis_notes[0] in payload["synopsis_notes"] for analysis in analyses)


def test_indispensable_evidence_is_never_truncated_to_fit():
    from app.analysis_chunks import (
        ConsolidationPayloadError, WindowAnalysis, WindowSpeaker, build_consolidation_payload,
    )
    from app.models import Evidence

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"), title="Sessione", duration_seconds=600,
    )
    speakers = [WindowSpeaker(
        diarization_labels=[f"chunk-{index}:A"], display_name=f"Nome supportato {index}",
        confidence="alta", evidence=[Evidence(kind="introduzione", timestamp_seconds=index,
            note="Contesto. " * 25 + f"Mi chiamo Nome supportato {index}.")],
    ) for index in range(100)]
    analyses = [WindowAnalysis(detected_language="it", synopsis_notes=[f"Conclusione {index}."],
                speakers=speakers[index:index + 8]) for index in range(0, 100, 8)]

    with pytest.raises(ConsolidationPayloadError, match="dati obbligatori"):
        build_consolidation_payload(metadata, analyses, [])


def test_summary_coverage_cannot_be_silently_dropped_when_budget_is_full():
    from app.analysis_chunks import ConsolidationPayloadError, WindowAnalysis, build_consolidation_payload

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"), title="Sessione", duration_seconds=14_400,
    )
    analyses = [WindowAnalysis(detected_language="it", speakers=[],
                 synopsis_notes=[f"Finestra {index}: " + "x" * 280]) for index in range(110)]

    with pytest.raises(ConsolidationPayloadError, match="dati obbligatori"):
        build_consolidation_payload(metadata, analyses, [])


def test_incompressible_supported_facts_raise_a_safe_budget_error():
    from app.analysis_chunks import WindowAnalysis, WindowSpeaker, build_consolidation_payload
    from app.models import Evidence

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"), title="Sessione", duration_seconds=60,
    )
    speakers = [
        WindowSpeaker(
            diarization_labels=[f"chunk-{index}:A"], display_name="N" * 400,
            confidence="alta", evidence=[Evidence(
                kind="introduzione", timestamp_seconds=float(index), note="nota",
            )],
        )
        for index in range(100)
    ]
    analyses = [
        WindowAnalysis(detected_language="it", synopsis_notes=[], speakers=speakers[index:index + 8])
        for index in range(0, len(speakers), 8)
    ]

    with pytest.raises(ValueError, match="dati obbligatori"):
        build_consolidation_payload(metadata, analyses, [])


def test_window_speaker_rejects_evidence_notes_over_300_characters():
    from app.analysis_chunks import WindowSpeaker
    from app.models import Evidence

    with pytest.raises(ValidationError):
        WindowSpeaker(
            diarization_labels=["chunk-0:A"], confidence="alta",
            evidence=[Evidence(kind="introduzione", timestamp_seconds=0, note="x" * 301)],
        )


def test_fast_report_payload_covers_every_voice_opening_timeline_and_ending_within_one_request():
    from app.analysis_chunks import MAX_FAST_REPORT_CHARS, build_fast_report_payload

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"),
        title="Sessione lunga", duration_seconds=14_400,
    )
    labels = [f"assembly:{letter}" for letter in "ABCDEF"]
    segments = [
        TranscriptSegment(
            start_seconds=float(index * 60), end_seconds=float(index * 60 + 50),
            diarization_label=(labels[index % 5] if index != 120 else labels[5]),
            text=("Apertura e presentazione dei relatori." if index == 0 else
                  "Conclusione definitiva della sessione." if index == 239 else
                  f"Intervento numero {index}. " + "x" * 500),
        )
        for index in range(240)
    ]
    transcription = TranscriptionResult(
        provider="assemblyai", language="it", text="TRASCRIZIONE_COMPLETA_DA_NON_INVIARE",
        segments=segments, audio_seconds=14_400,
        speaker_mapping={label: f"Relatore {index + 1}" for index, label in enumerate(labels)},
    )
    slides = [
        SlideChange(timestamp_seconds=index * 60, title=f"Slide {index}",
                    visible_content=["y" * 300] * 4, confidence="alta")
        for index in range(240)
    ]

    payload = build_fast_report_payload(metadata, transcription, slides)
    parsed = json.loads(payload)

    assert len(payload) <= MAX_FAST_REPORT_CHARS
    assert "TRASCRIZIONE_COMPLETA_DA_NON_INVIARE" not in payload
    assert parsed["metadata"] == {"title": "Sessione lunga", "duration_seconds": 14_400}
    assert parsed["detected_language"] == "it"
    assert set(parsed["speaker_mapping"]) == set(labels)
    assert set(item["diarization_label"] for item in parsed["segments"]) == set(labels)
    assert parsed["segments"][0]["text"] == "Apertura e presentazione dei relatori."
    assert any(item["start_seconds"] == 7200 for item in parsed["segments"])
    assert parsed["segments"][-1]["text"] == "Conclusione definitiva della sessione."
    assert parsed["slides"]


def test_fast_report_payload_clamps_small_provider_duration_rounding():
    from app.analysis_chunks import build_fast_report_payload

    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"),
        title="Sessione", duration_seconds=60,
    )
    transcription = TranscriptionResult(
        provider="assemblyai", text="Conclusione.", audio_seconds=60.4,
        segments=[TranscriptSegment(
            start_seconds=59.5, end_seconds=60.4,
            diarization_label="assembly:A", text="Conclusione.",
        )],
    )

    payload = json.loads(build_fast_report_payload(metadata, transcription, []))

    assert payload["segments"] == [{
        "start_seconds": 59.5, "end_seconds": 60,
        "diarization_label": "assembly:A", "text": "Conclusione.",
    }]
