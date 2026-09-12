"""Bounded, in-memory inputs for chunked transcript analysis.

This module only shapes transient request payloads.  It never writes or logs
transcript text.
"""

from collections.abc import Sequence
import json
from typing import Annotated

from pydantic import Field, field_validator

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
from app.transcription import TranscriptionResult, TranscriptSegment


MAX_WINDOW_SECONDS = 600
MAX_WINDOW_CHARS = 12_000
MAX_CONSOLIDATION_CHARS = 30_000
MAX_VISUAL_CONTEXT_CHARS = 18_000
MAX_FAST_REPORT_CHARS = 30_000
MAX_FAST_TRANSCRIPT_CONTEXT_CHARS = 20_000
MAX_FAST_VISUAL_CONTEXT_CHARS = 8_000

BoundedText = Annotated[str, Field(max_length=300)]


class TranscriptWindow(ReportModel):
    start_seconds: Nonnegative
    end_seconds: Nonnegative
    segments: list[TranscriptSegment]

    def to_payload(self) -> str:
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False)


class WindowEvidence(Evidence):
    """Evidence returned by a text window, bounded before consolidation."""

    note: BoundedText


class WindowSpeaker(ReportModel):
    diarization_labels: list[str]
    display_name: str | None = None
    role: str | None = None
    confidence: Confidence
    evidence: list[WindowEvidence] = Field(default_factory=list, max_length=4)

    @field_validator("evidence", mode="before")
    @classmethod
    def validate_base_evidence_as_window_evidence(cls, value: object) -> object:
        """Re-validate callers' base Evidence models against the bounded subtype."""
        if not isinstance(value, list):
            return value
        return [item.model_dump() if isinstance(item, Evidence) else item for item in value]


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


class ConsolidationPayloadError(ValueError):
    """Safe local error when mandatory consolidation facts cannot fit."""


def _evidence_payload(evidence: WindowEvidence) -> dict:
    return {
        "kind": evidence.kind,
        "timestamp_seconds": evidence.timestamp_seconds,
        "note": evidence.note,
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
        "role": speaker.role,
        "confidence": speaker.confidence,
        "evidence": evidence,
    }


def _generic_candidate(speaker: WindowSpeaker) -> dict:
    candidate = {
        "diarization_labels": speaker.diarization_labels,
        "confidence": speaker.confidence,
    }
    evidence = [_evidence_payload(item) for item in speaker.evidence
                if item.kind in IDENTITY_EVIDENCE_KINDS and item.note.strip()]
    if speaker.role and evidence:
        candidate.update(role=speaker.role, evidence=evidence)
    return candidate


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
    supported: list[dict] = []
    generic: list[dict] = []
    for analysis in analyses:
        for speaker in analysis.speakers:
            candidate = _supported_candidate(speaker)
            if candidate is None:
                generic.append(_generic_candidate(speaker))
            else:
                supported.append(candidate)

    notes_by_window = [[note for note in analysis.synopsis_notes if note.strip()]
                       for analysis in analyses]
    selected_notes = [notes[:1] for notes in notes_by_window]

    payload = {
        "metadata": {
            "title": metadata.title,
            "duration_seconds": metadata.duration_seconds,
        },
        "detected_languages": list(dict.fromkeys(analysis.detected_language for analysis in analyses)),
        "supported_candidates": supported,
        "slides": [],
        "generic_candidates": [candidate for candidate in generic if candidate.get("evidence")],
        "uncertainties": [],
        "synopsis_notes": [note for notes in selected_notes for note in notes],
    }

    if len(_serialized(payload)) > MAX_CONSOLIDATION_CHARS:
        raise ConsolidationPayloadError("dati obbligatori superano il limite di consolidamento")

    # Complete evidence and one complete note from every nonempty window are
    # indispensable: a prefix can drop the very fact that supports a name/role.
    # Give additional synopsis notes room across windows before visual extras.
    for note_index in range(1, 4):
        for window_index, notes in enumerate(notes_by_window):
            if note_index < len(notes) and _append_if_fits(payload, "synopsis_notes", notes[note_index]):
                selected_notes[window_index].append(notes[note_index])
    payload["synopsis_notes"] = [note for notes in selected_notes for note in notes]

    for slide in sorted(slides, key=lambda item: item.timestamp_seconds):
        _append_slide_if_fits(payload, _slide_payload(slide))
    for candidate in generic:
        if not candidate.get("evidence"):
            _append_if_fits(payload, "generic_candidates", candidate)
    for analysis in analyses:
        for uncertainty in analysis.uncertainties:
            _append_if_fits(payload, "uncertainties", uncertainty)
    serialized = _serialized(payload)
    if len(serialized) > MAX_CONSOLIDATION_CHARS:
        raise AssertionError("Il payload di consolidamento supera il limite")
    return serialized


def _fast_segment_payload(segment: TranscriptSegment) -> dict:
    return {
        "start_seconds": segment.start_seconds,
        "end_seconds": segment.end_seconds,
        "diarization_label": segment.diarization_label,
        "text": segment.text[:600],
    }


def build_fast_report_payload(
    metadata: BunnyVideoMetadata,
    transcription: TranscriptionResult,
    slides: Sequence[SlideChange],
) -> str:
    """Build one bounded report request with identity and whole-video coverage.

    The full transcript is deliberately excluded. Priority is given to the
    opening, the first turn of every detected voice, regular timeline samples,
    and the ending. All detected slide changes remain in the local report; this
    payload includes only enough visual context for naming and synopsis.
    """
    provider_segments = sorted(
        transcription.segments,
        key=lambda segment: (segment.start_seconds, segment.end_seconds),
    )
    if not provider_segments or any(
        segment.start_seconds > metadata.duration_seconds
        or segment.end_seconds > metadata.duration_seconds + 60
        or segment.start_seconds > segment.end_seconds
        or not segment.text.strip()
        for segment in provider_segments
    ):
        raise ConsolidationPayloadError("trascrizione non valida per il report rapido")
    ordered = [
        segment.model_copy(update={"end_seconds": min(segment.end_seconds, metadata.duration_seconds)})
        for segment in provider_segments
    ]

    first_by_label: dict[str, int] = {}
    for index, segment in enumerate(ordered):
        first_by_label.setdefault(segment.diarization_label, index)
    if len(first_by_label) > 64:
        raise ConsolidationPayloadError("numero di voci non rappresentabile nel report rapido")

    payload = {
        "metadata": {
            "title": metadata.title,
            "duration_seconds": metadata.duration_seconds,
        },
        "detected_language": transcription.language,
        "speaker_mapping": {
            label: _bounded_text(transcription.speaker_mapping.get(label, label))
            for label in first_by_label
        },
        "segments": [],
        "slides": [],
    }
    if len(_serialized(payload)) > MAX_FAST_REPORT_CHARS:
        raise ConsolidationPayloadError("dati obbligatori superano il limite del report rapido")

    selected: set[int] = set()

    def append_segment(index: int, *, required: bool = False) -> bool:
        if index in selected:
            return True
        item = _fast_segment_payload(ordered[index])
        payload["segments"].append(item)
        size = len(_serialized({"segments": payload["segments"]}))
        fits = size <= MAX_FAST_TRANSCRIPT_CONTEXT_CHARS and len(_serialized(payload)) <= MAX_FAST_REPORT_CHARS
        if not fits:
            payload["segments"].pop()
            if required:
                raise ConsolidationPayloadError("voci obbligatorie superano il limite del report rapido")
            return False
        selected.add(index)
        return True

    # Every detected voice and the final turn are mandatory; a report must not
    # silently omit a presenter or co-speaker merely to fit the request.
    for index in first_by_label.values():
        append_segment(index, required=True)
    append_segment(len(ordered) - 1, required=True)

    # Preserve the introductions first, then regular coverage across up to four
    # hours. Optional additions stop cleanly once the bounded context is full.
    for index, segment in enumerate(ordered):
        if segment.start_seconds > min(600, metadata.duration_seconds):
            break
        if not append_segment(index):
            break

    sample_count = min(48, len(ordered))
    for sample in range(sample_count):
        target = metadata.duration_seconds * sample / max(1, sample_count - 1)
        index = min(
            range(len(ordered)),
            key=lambda candidate: abs(ordered[candidate].start_seconds - target),
        )
        append_segment(index)

    # Include representative slide text for synthesis and name evidence. The
    # complete chronological slide list is returned locally by the analyzer.
    ordered_slides = sorted(slides, key=lambda slide: slide.timestamp_seconds)
    if ordered_slides:
        slide_indexes = {
            round(sample * (len(ordered_slides) - 1) / max(1, min(47, len(ordered_slides) - 1)))
            for sample in range(min(48, len(ordered_slides)))
        }
        for index in sorted(slide_indexes):
            payload["slides"].append(_slide_payload(ordered_slides[index]))
            visual_size = len(_serialized({"slides": payload["slides"]}))
            if visual_size > MAX_FAST_VISUAL_CONTEXT_CHARS or len(_serialized(payload)) > MAX_FAST_REPORT_CHARS:
                payload["slides"].pop()
                break

    payload["segments"].sort(key=lambda item: (item["start_seconds"], item["end_seconds"]))
    serialized = _serialized(payload)
    if len(serialized) > MAX_FAST_REPORT_CHARS:
        raise AssertionError("Il payload rapido supera il limite")
    return serialized
