"""Bounded, in-memory inputs for chunked transcript analysis.

This module only shapes transient request payloads.  It never writes or logs
transcript text.
"""

from collections.abc import Sequence
import json
from typing import Annotated

from pydantic import Field

from app.bunny import BunnyVideoMetadata
from app.models import (
    Confidence,
    Evidence,
    IDENTITY_EVIDENCE_KINDS,
    Nonnegative,
    ReportModel,
    SlideChange,
    SpeakerProfile,
)
from app.transcription import TranscriptSegment


MAX_WINDOW_SECONDS = 600
MAX_WINDOW_CHARS = 12_000
MAX_CONSOLIDATION_CHARS = 30_000
MAX_VISUAL_CONTEXT_CHARS = 18_000

BoundedText = Annotated[str, Field(max_length=300)]


class TranscriptWindow(ReportModel):
    start_seconds: Nonnegative
    end_seconds: Nonnegative
    segments: list[TranscriptSegment]

    def to_payload(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False)


class WindowSpeaker(ReportModel):
    diarization_labels: list[str]
    display_name: str | None = None
    role: str | None = None
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list, max_length=4)


class WindowAnalysis(ReportModel):
    detected_language: str
    synopsis_notes: list[BoundedText] = Field(max_length=4)
    speakers: list[WindowSpeaker] = Field(max_length=8)
    uncertainties: list[BoundedText] = Field(default_factory=list, max_length=6)


class ConsolidatedTextReport(ReportModel):
    title: str
    duration_seconds: Nonnegative
    detected_language: str
    synopsis: str
    speakers: list[SpeakerProfile]
    uncertainties: list[str]


def _window_for(segments: list[TranscriptSegment]) -> TranscriptWindow:
    return TranscriptWindow(
        start_seconds=segments[0].start_seconds,
        end_seconds=max(segment.end_seconds for segment in segments),
        segments=segments,
    )


def _fits_window(segments: list[TranscriptSegment]) -> bool:
    window = _window_for(segments)
    return (
        window.end_seconds - window.start_seconds <= MAX_WINDOW_SECONDS
        and len(window.to_payload()) <= MAX_WINDOW_CHARS
    )


def _split_oversized_segment(segment: TranscriptSegment) -> list[TranscriptSegment]:
    """Split only the text, retaining the source segment's timing and label."""
    pieces: list[TranscriptSegment] = []
    offset = 0
    while offset < len(segment.text):
        low, high = 1, len(segment.text) - offset
        best = 0
        while low <= high:
            size = (low + high) // 2
            piece = segment.model_copy(update={"text": segment.text[offset:offset + size]})
            if _fits_window([piece]):
                best = size
                low = size + 1
            else:
                high = size - 1
        if best == 0:
            raise ValueError("Un segmento non puo essere serializzato entro il limite della finestra")
        pieces.append(segment.model_copy(update={"text": segment.text[offset:offset + best]}))
        offset += best
    return pieces


def split_transcript_windows(segments: Sequence[TranscriptSegment]) -> list[TranscriptWindow]:
    """Return chronological payload-sized windows without retaining transcript data."""
    ordered = sorted(segments, key=lambda segment: (segment.start_seconds, segment.end_seconds))
    windows: list[TranscriptWindow] = []
    current: list[TranscriptSegment] = []

    for segment in ordered:
        if segment.end_seconds - segment.start_seconds > MAX_WINDOW_SECONDS:
            raise ValueError("Un segmento supera la durata massima della finestra")
        pieces = [segment] if _fits_window([segment]) else _split_oversized_segment(segment)
        for piece in pieces:
            candidate = [*current, piece]
            if current and not _fits_window(candidate):
                windows.append(_window_for(current))
                current = [piece]
            else:
                current = candidate
            if not _fits_window(current):
                raise ValueError("Una finestra non puo rispettare i limiti richiesti")

    if current:
        windows.append(_window_for(current))
    return windows


def _bounded_text(value: str | None) -> str | None:
    return value[:300] if value is not None else None


def _serialized(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _append_if_fits(payload: dict, key: str, item: object) -> bool:
    payload[key].append(item)
    if len(_serialized(payload)) <= MAX_CONSOLIDATION_CHARS:
        return True
    payload[key].pop()
    return False


def _append_slide_if_fits(payload: dict, item: dict) -> bool:
    """Keep visual context bounded so later speaker evidence has room."""
    payload["slides"].append(item)
    visual_size = len(_serialized({"slides": payload["slides"]}))
    if visual_size <= MAX_VISUAL_CONTEXT_CHARS and len(_serialized(payload)) <= MAX_CONSOLIDATION_CHARS:
        return True
    payload["slides"].pop()
    return False


def _evidence_payload(evidence: Evidence) -> dict:
    return {
        "kind": evidence.kind,
        "timestamp_seconds": evidence.timestamp_seconds,
        "note": _bounded_text(evidence.note),
    }


def _supported_candidate(speaker: WindowSpeaker) -> dict | None:
    evidence = [
        _evidence_payload(item)
        for item in speaker.evidence
        if item.kind in IDENTITY_EVIDENCE_KINDS and item.note.strip()
    ]
    if not speaker.display_name or not evidence:
        return None
    return {
        "diarization_labels": speaker.diarization_labels,
        "display_name": speaker.display_name,
        "role": _bounded_text(speaker.role),
        "confidence": speaker.confidence,
        "evidence": evidence,
    }


def _generic_candidate(speaker: WindowSpeaker) -> dict:
    return {
        "diarization_labels": speaker.diarization_labels,
        "confidence": speaker.confidence,
    }


def _slide_payload(slide: SlideChange) -> dict:
    return {
        "timestamp_seconds": slide.timestamp_seconds,
        "title": _bounded_text(slide.title),
        "visible_content": [_bounded_text(value) for value in slide.visible_content[:4]],
        "confidence": slide.confidence,
    }


def build_consolidation_payload(
    metadata: BunnyVideoMetadata,
    analyses: Sequence[WindowAnalysis],
    slides: Sequence[SlideChange],
) -> str:
    """Build a deterministic, JSON-only consolidation request within its budget."""
    payload = {
        "metadata": {
            "title": _bounded_text(metadata.title),
            "duration_seconds": metadata.duration_seconds,
        },
        "detected_languages": [],
        "supported_candidates": [],
        "slides": [],
        "generic_candidates": [],
        "uncertainties": [],
        "synopsis_notes": [],
    }

    if len(_serialized(payload)) > MAX_CONSOLIDATION_CHARS:
        raise ValueError("I metadati essenziali superano il limite di consolidamento")

    seen_languages: set[str] = set()
    for analysis in analyses:
        language = _bounded_text(analysis.detected_language)
        if language not in seen_languages:
            seen_languages.add(language)
            _append_if_fits(payload, "detected_languages", language)

    supported: list[dict] = []
    generic: list[dict] = []
    for analysis in analyses:
        for speaker in analysis.speakers:
            candidate = _supported_candidate(speaker)
            if candidate is None:
                generic.append(_generic_candidate(speaker))
            else:
                supported.append(candidate)

    for candidate in supported:
        _append_if_fits(payload, "supported_candidates", candidate)
    for slide in sorted(slides, key=lambda item: item.timestamp_seconds):
        _append_slide_if_fits(payload, _slide_payload(slide))
    for candidate in generic:
        _append_if_fits(payload, "generic_candidates", candidate)
    for analysis in analyses:
        for uncertainty in analysis.uncertainties:
            _append_if_fits(payload, "uncertainties", uncertainty)
    for analysis in analyses:
        for note in analysis.synopsis_notes:
            _append_if_fits(payload, "synopsis_notes", note)

    serialized = _serialized(payload)
    if len(serialized) > MAX_CONSOLIDATION_CHARS:
        raise AssertionError("Il payload di consolidamento supera il limite")
    return serialized
