"""Ephemeral bounded analysis; remote content is never logged or persisted."""

import base64
from collections.abc import Callable, Sequence
from concurrent.futures import CancelledError
from difflib import SequenceMatcher
import json
import math
import re
from threading import Event
from typing import Literal, TypeVar
import unicodedata

import httpx
from openai import APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, Field, ValidationError

from app.bunny import BunnyVideoMetadata
from app.analysis_chunks import (
    MAX_CONSOLIDATION_CHARS, MAX_WINDOW_CHARS, MAX_SLIDE_HINT_CHARS,
    ConsolidatedTextReport, WindowAnalysis, build_consolidation_payload,
    materialize_interventions, previous_window_context, shares_seam_utterance,
    split_transcript_windows,
)
from app.boundaries import BoundaryAlignment, has_complete_boundary_evidence
from app.media import FrameCandidate, SilenceInterval
from app.models import (
    Confidence, GENERIC_SPEAKER_LABEL, IDENTITY_EVIDENCE_KINDS,
    GENERIC_INTERVENTION_SPEAKER, ReportModel, SlideChange,
    AnalysisResult, ProviderUsage, Nonnegative,
)
from app.prompts import (
    CONSOLIDATION_PROMPT, REPAIR_PROMPT, VISUAL_PROMPT, WINDOW_PROMPT,
)
from app.openai_limits import ProviderRateGate, parse_reset_seconds
from app.retry import check_cancelled, retry_remote
from app.transcription import TranscriptionResult
from app.usage import record_usage


_VISUAL_BATCH_SIZE = 25
_MAX_VISUAL_REPAIR_CHARS = 30_000
AnalysisStage = Literal["visual", "window", "consolidation", "boundary"]
_PROVIDER_SPEAKER_NAME = re.compile(r"^(?:speaker\s+)?(?:[a-z]|\d+)$", re.I)


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
    "boundaries": "Verifica audio dei confini non riuscita; riprova",
}


class AnalysisError(Exception):
    """Fixed user-facing text, without response bodies or raw upstream errors."""

    def __init__(
        self, code: str, *, status_code: int | None = None,
        retry_after_seconds: float | None = None,
        stage: AnalysisStage = "consolidation",
        detail_code: str | None = None,
    ) -> None:
        if stage not in {"visual", "window", "consolidation", "boundary"}:
            raise ValueError("Fase di analisi non valida")
        super().__init__(_MESSAGES[code])
        self.stage = stage
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.detail_code = detail_code
        self.retryable = code in {"timeout", "rate_limit", "server"}


_BOUNDARY_DETAIL_BY_MESSAGE = {
    "La partizione degli interventi non è valida": "materialize_length",
    "La partizione degli interventi deve includere ogni segmento": "materialize_empty",
    "La partizione degli interventi deve essere ordinata, completa e univoca": "materialize_indexes",
    "La partizione ha un margine di finestra irrisolto": "materialize_margin",
    "La partizione contiene etichette vocali non appartenenti ai segmenti": "materialize_labels",
    "La partizione divide una utterance sorgente in gruppi incompatibili": "materialize_source_conflict",
    "La partizione richiede almeno un secondo": "alignment_invalid",
    "La partizione parlata non può dichiarare una pausa": "alignment_word_evidence",
    "La partizione contiene un segmento vuoto o invertito": "alignment_word_evidence",
    "La partizione contiene una parola vuota": "alignment_word_evidence",
    "La partizione parlata richiede evidenze parola per parola": "alignment_word_evidence",
    "La partizione divide una stessa utterance sorgente": "alignment_source_split",
    "La partizione contiene tempi vocali non validi": "alignment_speech_timing",
    "La partizione contiene parlato sovrapposto non separabile": "alignment_overlap",
    "La partizione non ammette un confine intero senza segmenti vuoti": "alignment_quantization",
    "La partizione non ha confini adiacenti con prova completa": "alignment_incomplete",
}


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


def _name_key(value: str) -> str:
    folded = "".join(
        character for character in unicodedata.normalize("NFKD", value.lower())
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z]+", folded))


def _canonical_name(value: str, hints: Sequence[str]) -> str:
    key = _name_key(value)
    tokens = key.split()
    if len(tokens) < 2:
        return value
    ranked = sorted(
        (
            (SequenceMatcher(None, key, _name_key(hint)).ratio(), hint)
            for hint in hints
            if len(_name_key(hint).split()) >= 2
            and _name_key(hint).split()[-1] == tokens[-1]
        ),
        reverse=True,
    )
    if not ranked or ranked[0][0] < .82:
        return value
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .08:
        return value
    return ranked[0][1]


def _normalize_window_speakers(data: dict, hints: Sequence[str]) -> None:
    for speaker in data.get("speakers", []):
        if not isinstance(speaker, dict):
            continue
        name = speaker.get("display_name")
        if not isinstance(name, str) or not name.strip():
            continue
        if _PROVIDER_SPEAKER_NAME.fullmatch(name.strip()):
            speaker["display_name"] = None
        else:
            speaker["display_name"] = _canonical_name(name.strip(), hints)


def _window_payload(window, hints: Sequence[str], previous_context: dict | None = None,
                    *, slide_hints: Sequence[SlideChange] = ()) -> str:
    payload = json.loads(window.to_payload())
    if previous_context is not None:
        payload["previous_context"] = previous_context
    payload["slide_hints"] = []
    seen_times: set[float] = set()
    for slide in sorted(slide_hints, key=lambda item: (item.timestamp_seconds, item.title or "")):
        if (not window.start_seconds - 30 <= slide.timestamp_seconds <= window.end_seconds + 30
                or slide.timestamp_seconds in seen_times):
            continue
        item = {"timestamp_seconds": slide.timestamp_seconds,
                "title": slide.title[:120] if slide.title is not None else None}
        payload["slide_hints"].append(item)
        if (len(json.dumps({"slide_hints": payload["slide_hints"]}, ensure_ascii=False)) > MAX_SLIDE_HINT_CHARS
                or len(json.dumps(payload, ensure_ascii=False)) > MAX_WINDOW_CHARS):
            payload["slide_hints"].pop()
            break
        seen_times.add(slide.timestamp_seconds)
        if len(payload["slide_hints"]) == 8:
            break
    base_payload = json.dumps(payload, ensure_ascii=False)
    if len(base_payload) > MAX_WINDOW_CHARS:
        raise AnalysisError("boundaries", stage="boundary", detail_code="window_payload")
    cleaned = list(dict.fromkeys(
        name.strip()[:120] for name in hints[:20]
        if isinstance(name, str) and name.strip()
    ))
    if cleaned:
        payload["speaker_name_hints"] = cleaned
    serialized = json.dumps(payload, ensure_ascii=False)
    return serialized if len(serialized) <= MAX_WINDOW_CHARS else base_payload


def _normalize_speakers(data: dict, hints: Sequence[str] = ()) -> None:
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
        provider_label = bool(_PROVIDER_SPEAKER_NAME.fullmatch(name.strip()))
        supported = not provider_label and any(item.get("kind") in IDENTITY_EVIDENCE_KINDS
                        and isinstance(item.get("note"), str) and item["note"].strip()
                        for item in speaker.get("evidence", []))
        if supported:
            speaker["display_name"] = _canonical_name(name.strip(), hints)
            name = speaker["display_name"]
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


def _window_errors(window, result: WindowAnalysis, previous_window=None) -> list[str]:
    indexes = [
        segment_index
        for intervention in result.interventions
        for segment_index in intervention.segment_indexes
    ]
    expected = list(range(len(window.segments)))
    errors: list[str] = []
    if indexes != expected:
        errors.append(
            f"interventions deve partizionare in ordine ogni segment_index una volta: {expected}."
        )
    labels = {segment.diarization_label for segment in window.segments}
    if any(
        label not in labels
        for intervention in result.interventions
        for label in intervention.diarization_labels
    ):
        errors.append("diarization_labels deve usare soltanto etichette presenti nella finestra.")
    if (previous_window is not None and result.previous_continuity not in {"continue", "separate"}
            and not shares_seam_utterance(previous_window, window)):
        # A repair omits transcript context and cannot resolve editorial meaning.
        raise AnalysisError("boundaries", stage="boundary")
    return errors


def _supported_window_speaker_names(
    analyses: Sequence[WindowAnalysis],
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for analysis in analyses:
        for speaker in analysis.speakers:
            name = (speaker.display_name or "").strip()
            supported = any(
                item.kind in IDENTITY_EVIDENCE_KINDS and item.note.strip()
                for item in speaker.evidence
            )
            if not name or not supported or GENERIC_INTERVENTION_SPEAKER.fullmatch(name):
                continue
            for label in speaker.diarization_labels:
                mapping.setdefault(label, name)
    return mapping


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
        validation_error_code: str = "response",
        validation_error_stage: AnalysisStage | None = None,
        model: str = "gpt-5.6-luna",
        max_output_tokens: int = 4000,
        validation_context: dict | None = None,
    ) -> T:
        original_payload = payload
        original_instructions = instructions
        attempts = 0
        repair_used = False
        output_limit_retry_used = False
        current_max_output_tokens = max_output_tokens
        while attempts < 3:
            def request():
                nonlocal attempts
                check_cancelled(cancellation_event)
                # Image data URLs are transport bytes, not text tokens. Keep
                # the textual envelope and account for low-detail images once.
                text_payload = payload if isinstance(payload, str) else [
                    {**message, "content": [part for part in message.get("content", [])
                                            if part.get("type") != "input_image"]}
                    for message in payload
                ]
                serialized = (text_payload if isinstance(text_payload, str)
                              else json.dumps(text_payload, ensure_ascii=False))
                images = (sum(part.get("type") == "input_image" and part.get("detail") == "low"
                              for message in payload for part in message.get("content", []))
                          if isinstance(payload, list) else 0)
                required_tokens = max(
                    current_max_output_tokens, math.ceil(len(serialized) / 3)
                ) + 300 * images
                self._rate_gate.wait(model, required_tokens, cancellation_event)
                check_cancelled(cancellation_event)
                attempts += 1
                try:
                    raw = self._client.responses.with_raw_response.parse(
                        model=model, store=False, text_format=text_format,
                        instructions=instructions, input=payload,
                        max_output_tokens=current_max_output_tokens,
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
                request,
                retryable=lambda exc: isinstance(exc, AnalysisError)
                and (exc.retryable or exc.code == "response"),
                attempts=3 - attempts, delay_for=_analysis_retry_delay,
                cancellation_event=cancellation_event,
            )
            status = getattr(response, "status", "completed")
            if status != "completed":
                details = getattr(response, "incomplete_details", None)
                reason = (
                    details.get("reason") if isinstance(details, dict)
                    else getattr(details, "reason", None)
                )
                if (
                    status == "incomplete"
                    and reason == "max_output_tokens"
                    and not output_limit_retry_used
                    and attempts < 3
                ):
                    # A dense transcript window can legitimately need more JSON
                    # than the normal economical cap. Retry only this explicit
                    # incomplete condition and keep the shared three-call budget.
                    current_max_output_tokens = max_output_tokens * 2
                    output_limit_retry_used = True
                    continue
                raise AnalysisError("response", stage=stage) from None
            try:
                parsed = response.output_parsed
                if parsed is None:
                    raise ValueError
                data = parsed.model_dump(mode="json") if isinstance(parsed, BaseModel) else parsed
                # Detach normalization from caller/SDK-owned objects.
                data = json.loads(json.dumps(data))
                prepare(data)
                result = text_format.model_validate(data, context=validation_context)
                errors = validate(result)
            except ValidationError as exc:
                if attempts < 3:
                    payload = original_payload
                    instructions = original_instructions
                    current_max_output_tokens = max_output_tokens * 2
                    output_limit_retry_used = True
                    continue
                partition_error = any(
                    error.get("loc") and error["loc"][0] in {"interventions", "previous_continuity"}
                    for error in exc.errors(
                        include_url=False, include_context=False, include_input=False,
                    )
                )
                if partition_error and validation_error_code == "boundaries":
                    raise AnalysisError(
                        validation_error_code, stage=validation_error_stage or stage,
                        detail_code="window_contract",
                    ) from None
                raise AnalysisError("response", stage=stage) from None
            except (ValueError, TypeError, AttributeError, KeyError):
                if attempts < 3:
                    payload = original_payload
                    instructions = original_instructions
                    current_max_output_tokens = max_output_tokens * 2
                    output_limit_retry_used = True
                    continue
                raise AnalysisError("response", stage=stage) from None
            if not errors:
                check_cancelled(cancellation_event)
                return result
            if repair_used:
                if attempts < 3:
                    payload = original_payload
                    instructions = original_instructions
                    current_max_output_tokens = max_output_tokens * 2
                    output_limit_retry_used = True
                    continue
                raise AnalysisError(
                    validation_error_code, stage=validation_error_stage or stage,
                    detail_code=(
                        "window_contract" if validation_error_code == "boundaries" else None
                    ),
                )
            if attempts >= 3:
                raise AnalysisError(
                    validation_error_code, stage=validation_error_stage or stage,
                    detail_code=(
                        "window_contract" if validation_error_code == "boundaries" else None
                    ),
                )
            # The repair contains no repeated transcript, images or metadata.
            payload = json.dumps({"errors": errors, "previous_json": data}, ensure_ascii=False)
            if len(payload) > max_repair_chars:
                raise AnalysisError(
                    validation_error_code, stage=validation_error_stage or stage,
                    detail_code=(
                        "window_contract" if validation_error_code == "boundaries" else None
                    ),
                ) from None
            instructions = REPAIR_PROMPT
            repair_used = True
        raise AnalysisError("response", stage=stage)  # Defensive; the loop returns or raises.

    def analyze(
        self, metadata: BunnyVideoMetadata, transcription: TranscriptionResult,
        frames: Sequence[FrameCandidate], *, silence_intervals: Sequence[SilenceInterval],
        cancellation_event: Event | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
        speaker_name_hints: Sequence[str] = (),
    ) -> AnalysisResult:
        check_cancelled(cancellation_event)
        if (
            silence_intervals is None
            or not transcription.segments
            or any(not segment.words for segment in transcription.segments)
        ):
            raise AnalysisError("boundaries", stage="boundary")
        usage = ProviderUsage()
        slides, uncertain_count = self._classify_slides(
            metadata, frames, cancellation_event=cancellation_event,
            progress_callback=progress_callback, usage=usage,
        )
        check_cancelled(cancellation_event)
        slide_data = [slide.model_dump(mode="json") for slide in slides]
        try:
            windows = split_transcript_windows(transcription.segments)
        except ValueError:
            raise AnalysisError("response", stage="window") from None
        analyses: list[WindowAnalysis] = []
        for index, window in enumerate(windows, start=1):
            check_cancelled(cancellation_event)
            previous_window = windows[index - 2] if index > 1 else None
            try:
                context = previous_window_context(previous_window, analyses[-1]) if previous_window else None
            except (AttributeError, IndexError, TypeError, ValueError):
                raise AnalysisError(
                    "boundaries", stage="boundary", detail_code="window_context",
                ) from None
            window_payload = _window_payload(window, speaker_name_hints, context, slide_hints=slides)
            allowed_slide_seconds = [hint["timestamp_seconds"]
                                    for hint in json.loads(window_payload)["slide_hints"]]
            analyses.append(self._structured(
                text_format=WindowAnalysis, instructions=WINDOW_PROMPT,
                payload=window_payload,
                validation_context={"slide_hint_seconds": allowed_slide_seconds},
                prepare=lambda data: _normalize_window_speakers(data, speaker_name_hints),
                validate=lambda result, current=window, previous=previous_window: _window_errors(current, result, previous),
                cancellation_event=cancellation_event,
                usage=usage, model="gpt-4o-mini", max_output_tokens=2000,
                max_repair_chars=MAX_WINDOW_CHARS,
                stage="window",
                validation_error_code="boundaries",
                validation_error_stage="boundary",
            ))
            if progress_callback is not None:
                progress_callback("transcript", index, len(windows))
        check_cancelled(cancellation_event)
        try:
            alignment = materialize_interventions(
                metadata.duration_seconds,
                windows,
                analyses,
                _supported_window_speaker_names(analyses),
                silence_intervals,
                slides,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            detail_code = _BOUNDARY_DETAIL_BY_MESSAGE.get(
                str(exc), "materialize_unknown",
            )
            raise AnalysisError(
                "boundaries", stage="boundary", detail_code=detail_code,
            ) from None
        try:
            payload = build_consolidation_payload(metadata, analyses, slides)
        except ValueError:
            raise AnalysisError("response", stage="consolidation") from None
        if progress_callback is not None:
            progress_callback("consolidation", 0, 1)

        result = self._structured(
            text_format=ConsolidatedTextReport, instructions=CONSOLIDATION_PROMPT, payload=payload,
            prepare=lambda data: _normalize_speakers(data, speaker_name_hints),
            validate=lambda content: _content_errors(content, metadata.duration_seconds),
            cancellation_event=cancellation_event, usage=usage, model="gpt-4o-mini",
            max_output_tokens=4000,
            max_repair_chars=MAX_CONSOLIDATION_CHARS,
            stage="consolidation",
        )
        return self._result_with_slides(
            result, slide_data, alignment, uncertain_count, usage, cancellation_event
        )

    def _classify_slides(
        self, metadata: BunnyVideoMetadata, frames: Sequence[FrameCandidate], *,
        cancellation_event: Event | None,
        progress_callback: Callable[[str, int, int], None] | None,
        usage: ProviderUsage,
    ) -> tuple[list[SlideChange], int]:
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
        return slides, uncertain_count

    def analyze_fast(
        self, metadata: BunnyVideoMetadata, transcription: TranscriptionResult,
        frames: Sequence[FrameCandidate], *, silence_intervals: Sequence[SilenceInterval],
        cancellation_event: Event | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
        speaker_name_hints: Sequence[str] = (),
    ) -> AnalysisResult:
        """Analyze every globally diarized segment through the bounded window path."""
        return self.analyze(
            metadata,
            transcription,
            frames,
            silence_intervals=silence_intervals,
            cancellation_event=cancellation_event,
            progress_callback=progress_callback,
            speaker_name_hints=speaker_name_hints,
        )

    @staticmethod
    def _result_with_slides(
        result: ConsolidatedTextReport, slide_data: list[dict], alignment: BoundaryAlignment,
        uncertain_count: int,
        usage: ProviderUsage, cancellation_event: Event | None,
    ) -> AnalysisResult:
        check_cancelled(cancellation_event)
        data = result.model_dump()
        if uncertain_count:
            note = f"{uncertain_count} frame con classificazione incerta esclusi dalle slide."
            if note not in data["uncertainties"]:
                data["uncertainties"].append(note)
        try:
            content = AnalysisResult(
                **data, slides=slide_data, interventions=alignment.interventions,
                boundaries=alignment.boundaries, speech_blocks=alignment.blocks,
                audio_boundary_version=1, analysis_profile=2, usage=usage,
            )
        except (AttributeError, TypeError, ValidationError, ValueError):
            raise AnalysisError("boundaries", stage="boundary") from None
        if not has_complete_boundary_evidence(content):
            raise AnalysisError("boundaries", stage="boundary")
        return content
