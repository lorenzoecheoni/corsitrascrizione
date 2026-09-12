import json
from uuid import UUID

from app.bunny import BunnyVideoMetadata
from app.models import SlideChange
from app.transcription import TranscriptSegment


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
