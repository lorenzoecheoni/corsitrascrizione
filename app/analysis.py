"""Ephemeral two-pass analysis; remote content is never logged or persisted."""

import base64
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError
import json
import math
from threading import Event
from typing import Literal, TypeVar

import httpx
from openai import APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, Field, ValidationError

from app.bunny import BunnyVideoMetadata
from app.media import FrameCandidate
from app.models import (
    AcademyContent, Confidence, GENERIC_SPEAKER_LABEL, IDENTITY_EVIDENCE_KINDS,
    ReportModel, SlideChange,
)
from app.prompts import REPORT_PROMPT, REPAIR_PROMPT, VISUAL_PROMPT
from app.retry import check_cancelled, retry_remote
from app.transcription import TranscriptionResult


class ClassifiedFrame(ReportModel):
    timestamp_seconds: float
    kind: Literal["slide", "camera_change", "uncertain"]
    title: str | None = None
    visible_content: list[str] = Field(default_factory=list, repr=False)
    confidence: Confidence


class SlideBatchResult(ReportModel):
    frames: list[ClassifiedFrame]


_MESSAGES = {
    "response": "Risposta di analisi OpenAI non valida; riprova più tardi",
    "timeout": "OpenAI non ha risposto entro il tempo previsto; riprova più tardi",
    "rate_limit": "Limite di richieste OpenAI raggiunto; riprova più tardi",
    "server": "Servizio OpenAI temporaneamente non disponibile",
    "auth": "Accesso a OpenAI non autorizzato; verifica la configurazione",
    "request": "Richiesta di analisi OpenAI non accettata",
    "transport": "Impossibile completare la richiesta di analisi",
    "frames": "Frame video non validi o non leggibili",
}


class AnalysisError(Exception):
    """Fixed user-facing text, without response bodies or raw upstream errors."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(_MESSAGES[code])
        self.code = code
        self.status_code = status_code
        self.retryable = code in {"timeout", "rate_limit", "server"}


def _remote_error(exc: Exception) -> AnalysisError:
    if isinstance(exc, (APITimeoutError, TimeoutError, httpx.TimeoutException)):
        return AnalysisError("timeout")
    if isinstance(exc, ValidationError):
        return AnalysisError("response")
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        code = ("rate_limit" if status == 429 else "server" if 500 <= status <= 599
                else "auth" if status in {401, 403} else "request")
        return AnalysisError(code, status_code=status)
    return AnalysisError("transport")


def _valid_time(value: float, duration: float) -> bool:
    return math.isfinite(value) and 0 <= value <= duration


def _normalize_speakers(data: dict) -> None:
    """Conservative fallback before our defensive second Pydantic validation.

    Normal SDK parsing already enforces identity evidence. This also protects
    callers whose client returns permissively constructed or modified models.
    """
    speakers = data.get("speakers", [])
    uncertainties = data.setdefault("uncertainties", [])
    reserved = {item.get("display_name") for item in speakers}
    number = 1
    for index, speaker in enumerate(speakers):
        name = speaker.get("display_name", "")
        supported = any(item.get("kind") in IDENTITY_EVIDENCE_KINDS
                        and isinstance(item.get("note"), str) and item["note"].strip()
                        for item in speaker.get("evidence", []))
        if not supported and not GENERIC_SPEAKER_LABEL.fullmatch(name):
            while f"Relatore {number}" in reserved:
                number += 1
            speaker["display_name"] = f"Relatore {number}"
            reserved.add(speaker["display_name"])
            speaker["confidence"] = "bassa"
            uncertainties.append(f"Identità del relatore {index + 1} non supportata da evidenze ammesse.")
        if speaker.get("role") and (not supported or speaker.get("confidence") == "bassa"):
            speaker["role"] = None
            uncertainties.append(f"Ruolo del relatore {index + 1} non determinabile con sufficiente confidenza.")
        elif speaker.get("role") and speaker.get("confidence") == "media":
            uncertainties.append(f"Attribuzione del ruolo del relatore {index + 1} con confidenza media.")
        if not supported and not speaker.get("role"):
            uncertainties.append(f"Nome o ruolo del relatore {index + 1} non determinabile dalle fonti.")
    data["uncertainties"] = list(dict.fromkeys(uncertainties))


def _content_errors(content: AcademyContent, duration: float) -> list[str]:
    errors = []
    if not math.isfinite(content.duration_seconds) or content.duration_seconds != duration:
        errors.append(f"duration_seconds deve essere {duration}.")
    for field in ("title", "synopsis", "extended_description", "detected_language"):
        if not getattr(content, field).strip():
            errors.append(f"{field} deve essere compilato.")
    if content.detected_language.strip().lower() == "und":
        errors.append("Inferire detected_language dal testo; se impossibile dichiarare non determinabile.")
    for field in ("target_audience", "prerequisites", "learning_objectives", "topics", "keywords", "key_takeaways"):
        if not getattr(content, field) or any(not value.strip() for value in getattr(content, field)):
            errors.append(f"{field} deve contenere informazioni o un'assenza esplicita.")
    if not content.speakers or not content.interventions or not content.chapters:
        errors.append("speakers, interventions e chapters devono essere compilati.")
    ids = [speaker.id for speaker in content.speakers]
    if len(set(ids)) != len(ids) or any(not value.strip() for value in ids):
        errors.append("speakers.id devono essere unici e non vuoti.")
    generic_names = [speaker.display_name for speaker in content.speakers
                     if GENERIC_SPEAKER_LABEL.fullmatch(speaker.display_name)]
    if len(set(generic_names)) != len(generic_names):
        errors.append("Le etichette Relatore N devono essere distinte.")
    for index, speaker in enumerate(content.speakers):
        for evidence in speaker.evidence:
            if evidence.timestamp_seconds is not None and not _valid_time(evidence.timestamp_seconds, duration):
                errors.append(f"speakers[{index}].evidence: timestamp fuori durata.")
            if not evidence.note.strip():
                errors.append(f"speakers[{index}].evidence: nota vuota.")
    for field in ("interventions", "chapters"):
        previous_start = previous_end = -1.0
        for index, item in enumerate(getattr(content, field)):
            if (not _valid_time(item.start_seconds, duration) or not _valid_time(item.end_seconds, duration)
                    or item.start_seconds >= item.end_seconds):
                errors.append(f"{field}[{index}]: intervallo non valido entro durata {duration}.")
            if item.start_seconds < previous_start:
                errors.append(f"{field}[{index}]: timestamp non ordinati.")
            if field == "chapters" and item.start_seconds < previous_end:
                errors.append(f"chapters[{index}]: capitoli sovrapposti.")
            if not item.summary.strip() or (field == "chapters" and not item.title.strip()):
                errors.append(f"{field}[{index}]: titolo o descrizione vuoti.")
            if field == "interventions" and (not item.speaker_ids or
                    len(set(item.speaker_ids)) != len(item.speaker_ids) or
                    any(speaker_id not in ids for speaker_id in item.speaker_ids)):
                errors.append(f"interventions[{index}]: speaker_ids mancanti, duplicati o sconosciuti.")
            previous_start, previous_end = item.start_seconds, item.end_seconds
    return errors


T = TypeVar("T", bound=BaseModel)


class OpenAIAnalyzer:
    def __init__(self, client: OpenAI) -> None:
        self._client = client.with_options(max_retries=0)

    def _structured(
        self, *, text_format: type[T], instructions: str, payload: str | list,
        prepare: Callable[[dict], None], validate: Callable[[T], list[str]],
        cancellation_event: Event | None,
    ) -> T:
        attempts = 0
        for repair in range(2):
            def request():
                nonlocal attempts
                check_cancelled(cancellation_event)
                attempts += 1
                try:
                    response = self._client.responses.parse(
                        model="gpt-5.6-luna", store=False, text_format=text_format,
                        instructions=instructions, input=payload,
                    )
                except CancelledError:
                    raise
                except Exception as exc:
                    check_cancelled(cancellation_event)
                    raise _remote_error(exc) from None
                check_cancelled(cancellation_event)
                return response

            response = retry_remote(
                request, retryable=lambda exc: isinstance(exc, AnalysisError) and exc.retryable,
                attempts=3 - attempts, cancellation_event=cancellation_event,
            )
            try:
                if getattr(response, "status", "completed") != "completed":
                    raise ValueError
                parsed = response.output_parsed
                if parsed is None:
                    raise ValueError
                data = parsed.model_dump(mode="json") if isinstance(parsed, BaseModel) else parsed
                # Detach normalization from caller/SDK-owned objects.
                data = json.loads(json.dumps(data))
                prepare(data)
                result = text_format.model_validate(data)
                errors = validate(result)
            except (ValueError, TypeError, AttributeError, KeyError):
                raise AnalysisError("response") from None
            if not errors:
                check_cancelled(cancellation_event)
                return result
            if repair or attempts >= 3:
                raise AnalysisError("response")
            # The repair contains no repeated transcript, images or metadata.
            payload = json.dumps({"errors": errors, "previous_json": data}, ensure_ascii=False)
            instructions = REPAIR_PROMPT
        raise AnalysisError("response")  # Defensive; both iterations return or raise.

    def analyze(
        self, metadata: BunnyVideoMetadata, transcription: TranscriptionResult,
        frames: Sequence[FrameCandidate], *, cancellation_event: Event | None = None,
    ) -> AcademyContent:
        check_cancelled(cancellation_event)
        timestamps = [frame.timestamp_seconds for frame in frames]
        if (any(not _valid_time(value, metadata.duration_seconds) for value in timestamps)
                or timestamps != sorted(set(timestamps))):
            raise AnalysisError("frames")
        slides: list[SlideChange] = []
        uncertain_count = 0
        for offset in range(0, len(frames), 20):
            check_cancelled(cancellation_event)
            batch = frames[offset:offset + 20]
            expected = [frame.timestamp_seconds for frame in batch]
            parts = []
            try:
                for frame in batch:
                    check_cancelled(cancellation_event)
                    parts.append({"type": "input_text", "text": f"timestamp_seconds={frame.timestamp_seconds}"})
                    parts.append({"type": "input_image", "detail": "high", "image_url": "data:image/jpeg;base64," +
                                  base64.b64encode(frame.path.read_bytes()).decode("ascii")})
                def validate_batch(result: SlideBatchResult) -> list[str]:
                    if [frame.timestamp_seconds for frame in result.frames] != expected:
                        return [f"Restituire ogni timestamp una volta e in ordine: {expected}."]
                    return []
                classified = self._structured(
                    text_format=SlideBatchResult, instructions=VISUAL_PROMPT,
                    payload=[{"role": "user", "content": parts}], prepare=lambda data: None,
                    validate=validate_batch, cancellation_event=cancellation_event,
                )
            except OSError:
                raise AnalysisError("frames") from None
            finally:
                # Drop data URLs immediately, including error/cancellation paths.
                parts.clear()
            for frame in classified.frames:
                if frame.kind == "slide":
                    slides.append(SlideChange(**frame.model_dump(exclude={"kind"})))
                elif frame.kind == "uncertain":
                    uncertain_count += 1
        check_cancelled(cancellation_event)
        slide_data = [slide.model_dump(mode="json") for slide in slides]
        payload = json.dumps({"metadata": metadata.model_dump(mode="json"),
                              "transcription": transcription.model_dump(mode="json"),
                              "slides": slide_data}, ensure_ascii=False)

        def prepare_content(data: dict) -> None:
            _normalize_speakers(data)
            # The visual pass owns slide facts; generated additions are discarded.
            data["slides"] = slide_data
            if uncertain_count:
                note = f"{uncertain_count} frame con classificazione incerta esclusi dalle slide."
                if note not in data["uncertainties"]:
                    data["uncertainties"].append(note)

        result = self._structured(
            text_format=AcademyContent, instructions=REPORT_PROMPT, payload=payload,
            prepare=prepare_content, validate=lambda content: _content_errors(content, metadata.duration_seconds),
            cancellation_event=cancellation_event,
        )
        check_cancelled(cancellation_event)
        return result
