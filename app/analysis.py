"""Ephemeral bounded analysis; remote content is never logged or persisted."""

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
from app.analysis_chunks import (
    MAX_CONSOLIDATION_CHARS, MAX_WINDOW_CHARS,
    ConsolidatedTextReport, WindowAnalysis, build_consolidation_payload,
    split_transcript_windows,
)
from app.media import FrameCandidate
from app.models import (
    Confidence, GENERIC_SPEAKER_LABEL, IDENTITY_EVIDENCE_KINDS,
    ReportModel, SlideChange, AnalysisResult, ProviderUsage, Nonnegative,
)
from app.prompts import CONSOLIDATION_PROMPT, REPAIR_PROMPT, VISUAL_PROMPT, WINDOW_PROMPT
from app.openai_limits import ProviderRateGate, parse_reset_seconds
from app.retry import check_cancelled, retry_remote
from app.transcription import TranscriptionResult
from app.usage import record_usage


_VISUAL_BATCH_SIZE = 25
_MAX_VISUAL_REPAIR_CHARS = 30_000
AnalysisStage = Literal["visual", "window", "consolidation"]


class ClassifiedFrame(ReportModel):
    timestamp_seconds: Nonnegative
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

    def __init__(
        self, code: str, *, status_code: int | None = None,
        retry_after_seconds: float | None = None,
        stage: AnalysisStage = "consolidation",
    ) -> None:
        if stage not in {"visual", "window", "consolidation"}:
            raise ValueError("Fase di analisi non valida")
        super().__init__(_MESSAGES[code])
        self.stage = stage
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.retryable = code in {"timeout", "rate_limit", "server"}


def _remote_error(exc: Exception, stage: AnalysisStage) -> AnalysisError:
    if isinstance(exc, (APITimeoutError, TimeoutError, httpx.TimeoutException)):
        return AnalysisError("timeout", stage=stage)
    if isinstance(exc, ValidationError):
        return AnalysisError("response", stage=stage)
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        code = ("rate_limit" if status == 429 else "server" if 500 <= status <= 599
                else "auth" if status in {401, 403} else "request")
        retry_after = None
        if status == 429:
            try:
                candidate = float(exc.response.headers.get("retry-after", ""))
                if math.isfinite(candidate) and candidate >= 0:
                    retry_after = candidate
            except (TypeError, ValueError):
                pass
            if retry_after is None:
                retry_after = parse_reset_seconds(exc.response.headers.get("x-ratelimit-reset-project-tokens", ""))
        return AnalysisError(code, stage=stage, status_code=status, retry_after_seconds=retry_after)
    return AnalysisError("transport", stage=stage)


def _analysis_retry_delay(exc: Exception, default_delay: float) -> float:
    if isinstance(exc, AnalysisError) and exc.code == "rate_limit":
        if exc.retry_after_seconds is not None:
            return max(default_delay, exc.retry_after_seconds)
        # Leave enough time for the minute token window to recover. A 1–2s
        # transport backoff only repeats the same 429 after an expensive job.
        return 30 * default_delay
    return default_delay


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


def _content_errors(content: ConsolidatedTextReport, duration: float) -> list[str]:
    errors = []
    if not math.isfinite(content.duration_seconds) or content.duration_seconds != duration:
        errors.append(f"duration_seconds deve essere {duration}.")
    for field in ("title", "synopsis", "detected_language"):
        if not getattr(content, field).strip():
            errors.append(f"{field} deve essere compilato.")
    if content.detected_language.strip().lower() == "und":
        errors.append("Inferire detected_language dal testo; se impossibile dichiarare non determinabile.")
    if not content.speakers:
        errors.append("speakers devono essere compilati.")
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
    return errors


T = TypeVar("T", bound=BaseModel)


class OpenAIAnalyzer:
    def __init__(self, client: OpenAI, *, rate_gate: ProviderRateGate | None = None) -> None:
        self._client = client.with_options(max_retries=0)
        self._rate_gate = rate_gate if rate_gate is not None else ProviderRateGate()

    def _structured(
        self, *, text_format: type[T], instructions: str, payload: str | list,
        prepare: Callable[[dict], None], validate: Callable[[T], list[str]],
        cancellation_event: Event | None, usage: ProviderUsage,
        max_repair_chars: int,
        stage: AnalysisStage,
        model: str = "gpt-5.6-luna",
        max_output_tokens: int = 4000,
    ) -> T:
        attempts = 0
        for repair in range(2):
            def request():
                nonlocal attempts
                check_cancelled(cancellation_event)
                serialized = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
                images = (sum(part.get("type") == "input_image" and part.get("detail") == "low"
                              for message in payload for part in message.get("content", []))
                          if isinstance(payload, list) else 0)
                required_tokens = max(max_output_tokens, math.ceil(len(serialized) / 3)) + 300 * images
                self._rate_gate.wait(model, required_tokens, cancellation_event)
                check_cancelled(cancellation_event)
                attempts += 1
                try:
                    raw = self._client.responses.with_raw_response.parse(
                        model=model, store=False, text_format=text_format,
                        instructions=instructions, input=payload,
                        max_output_tokens=max_output_tokens,
                    )
                    try:
                        self._rate_gate.observe(model, raw.headers)
                        response = raw.parse()
                    finally:
                        del raw
                except CancelledError:
                    raise
                except Exception as exc:
                    record_usage(usage, exc)
                    check_cancelled(cancellation_event)
                    raise _remote_error(exc, stage) from None
                record_usage(usage, response)
                check_cancelled(cancellation_event)
                return response

            response = retry_remote(
                request, retryable=lambda exc: isinstance(exc, AnalysisError) and exc.retryable,
                attempts=3 - attempts, delay_for=_analysis_retry_delay,
                cancellation_event=cancellation_event,
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
                raise AnalysisError("response", stage=stage) from None
            if not errors:
                check_cancelled(cancellation_event)
                return result
            if repair or attempts >= 3:
                raise AnalysisError("response", stage=stage)
            # The repair contains no repeated transcript, images or metadata.
            payload = json.dumps({"errors": errors, "previous_json": data}, ensure_ascii=False)
            if len(payload) > max_repair_chars:
                raise AnalysisError("response", stage=stage) from None
            instructions = REPAIR_PROMPT
        raise AnalysisError("response", stage=stage)  # Defensive; both iterations return or raise.

    def analyze(
        self, metadata: BunnyVideoMetadata, transcription: TranscriptionResult,
        frames: Sequence[FrameCandidate], *, cancellation_event: Event | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> AnalysisResult:
        check_cancelled(cancellation_event)
        usage = ProviderUsage()
        timestamps = [frame.timestamp_seconds for frame in frames]
        if (any(not _valid_time(value, metadata.duration_seconds) for value in timestamps)
                or timestamps != sorted(set(timestamps))):
            raise AnalysisError("frames", stage="visual")
        slides: list[SlideChange] = []
        uncertain_count = 0
        total_batches = math.ceil(len(frames) / _VISUAL_BATCH_SIZE)
        for batch_number, offset in enumerate(range(0, len(frames), _VISUAL_BATCH_SIZE), start=1):
            check_cancelled(cancellation_event)
            batch = frames[offset:offset + _VISUAL_BATCH_SIZE]
            expected = [frame.timestamp_seconds for frame in batch]
            parts = []
            try:
                for frame in batch:
                    check_cancelled(cancellation_event)
                    parts.append({"type": "input_text", "text": f"timestamp_seconds={frame.timestamp_seconds}"})
                    parts.append({"type": "input_image", "detail": "low", "image_url": "data:image/jpeg;base64," +
                                  base64.b64encode(frame.path.read_bytes()).decode("ascii")})
                def validate_batch(result: SlideBatchResult) -> list[str]:
                    if [frame.timestamp_seconds for frame in result.frames] != expected:
                        return [f"Restituire ogni timestamp una volta e in ordine: {expected}."]
                    return []
                classified = self._structured(
                    text_format=SlideBatchResult, instructions=VISUAL_PROMPT,
                    payload=[{"role": "user", "content": parts}], prepare=lambda data: None,
                    validate=validate_batch, cancellation_event=cancellation_event, usage=usage,
                    max_output_tokens=3000, max_repair_chars=_MAX_VISUAL_REPAIR_CHARS,
                    stage="visual",
                )
            except OSError:
                raise AnalysisError("frames", stage="visual") from None
            finally:
                # Drop data URLs immediately, including error/cancellation paths.
                parts.clear()
            for frame in classified.frames:
                if frame.kind == "slide":
                    slides.append(SlideChange(**frame.model_dump(exclude={"kind"})))
                elif frame.kind == "uncertain":
                    uncertain_count += 1
            if progress_callback is not None:
                progress_callback("slides", batch_number, total_batches)
        check_cancelled(cancellation_event)
        slide_data = [slide.model_dump(mode="json") for slide in slides]
        try:
            windows = split_transcript_windows(transcription.segments)
        except ValueError:
            raise AnalysisError("response", stage="window") from None
        analyses: list[WindowAnalysis] = []
        for index, window in enumerate(windows, start=1):
            check_cancelled(cancellation_event)
            analyses.append(self._structured(
                text_format=WindowAnalysis, instructions=WINDOW_PROMPT,
                payload=window.to_payload(), prepare=lambda data: None,
                validate=lambda result: [], cancellation_event=cancellation_event,
                usage=usage, model="gpt-4o-mini", max_output_tokens=2000,
                max_repair_chars=MAX_WINDOW_CHARS,
                stage="window",
            ))
            if progress_callback is not None:
                progress_callback("transcript", index, len(windows))
        check_cancelled(cancellation_event)
        try:
            payload = build_consolidation_payload(metadata, analyses, slides)
        except ValueError:
            raise AnalysisError("response", stage="consolidation") from None
        if progress_callback is not None:
            progress_callback("consolidation", 0, 1)

        result = self._structured(
            text_format=ConsolidatedTextReport, instructions=CONSOLIDATION_PROMPT, payload=payload,
            prepare=_normalize_speakers, validate=lambda content: _content_errors(content, metadata.duration_seconds),
            cancellation_event=cancellation_event, usage=usage, model="gpt-4o-mini",
            max_output_tokens=4000,
            max_repair_chars=MAX_CONSOLIDATION_CHARS,
            stage="consolidation",
        )
        check_cancelled(cancellation_event)
        data = result.model_dump()
        if uncertain_count:
            note = f"{uncertain_count} frame con classificazione incerta esclusi dalle slide."
            if note not in data["uncertainties"]:
                data["uncertainties"].append(note)
        return AnalysisResult(**data, slides=slide_data, usage=usage)
