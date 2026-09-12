import json
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.bunny import BunnyVideoMetadata
from app.models import SlideChange
from app.transcription import TranscriptSegment


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
