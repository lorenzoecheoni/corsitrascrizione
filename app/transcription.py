"""In-memory diarized transcription with explicit-only speaker references.

    The caller owns audio files and their temporary workspace. This module never
    writes audio or transcripts. Original segments have global timestamps and
    namespaced local labels; speaker_mapping supplies conservative display names.
    No cross-chunk identity is inferred from voice or unsupported text heuristics.
"""

import base64
from collections.abc import Sequence
from concurrent.futures import CancelledError
from dataclasses import dataclass
import math
from pathlib import Path
from threading import Event
from typing import Literal

import httpx
from openai import APIStatusError, APITimeoutError, OpenAI
from openai.types.audio import TranscriptionDiarized
from pydantic import BaseModel, Field, model_validator

from app.media import AudioChunk
from app.retry import check_cancelled, retry_remote
from app.models import ProviderUsage
from app.usage import record_usage


class TranscriptWord(BaseModel):
    text: str = Field(min_length=1, repr=False)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    diarization_label: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered_interval(self) -> "TranscriptWord":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Intervallo parola non valido")
        return self


class TranscriptSegment(BaseModel):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    diarization_label: str = Field(min_length=1)
    text: str = Field(repr=False)
    source_utterance_id: str = ""
    words: list[TranscriptWord] = Field(default_factory=list, repr=False)

    @model_validator(mode="after")
    def ordered_interval(self) -> "TranscriptSegment":
        if self.end_seconds < self.start_seconds:
            raise ValueError("Intervallo di trascrizione non valido")
        return self


class TranscriptionResult(BaseModel):
    provider: Literal["openai", "assemblyai"] = "openai"
    # Diarized JSON has no language field; downstream analysis infers language.
    language: str = "und"
    text: str = Field(repr=False)
    segments: list[TranscriptSegment]
    audio_seconds: float = Field(ge=0, allow_inf_nan=False)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    usage: ProviderUsage = Field(default_factory=ProviderUsage)
    original_segments: list[TranscriptSegment] = Field(default_factory=list)
    speaker_mapping: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class KnownSpeaker:
    """Explicit user-supplied local reference, with caller-verified duration.

    The caller measures the clip duration; this interface never cuts a reference
    out of source audio or discovers a person's identity autonomously.
    """

    name: str
    path: Path
    duration_seconds: float

    def __post_init__(self) -> None:
        if not self.name.strip() or not math.isfinite(self.duration_seconds) or not 2 <= self.duration_seconds <= 10:
            raise ValueError("La reference richiede un nome e una durata tra 2 e 10 secondi")


_ERROR_MESSAGES = {
    "timeout": "OpenAI non ha risposto entro il tempo previsto; riprova più tardi",
    "rate_limit": "Limite di richieste OpenAI raggiunto; riprova più tardi",
    "server": "Servizio OpenAI temporaneamente non disponibile",
    "auth": "Accesso a OpenAI non autorizzato; verifica la configurazione",
    "request": "Richiesta di trascrizione OpenAI non accettata",
    "transport": "Impossibile completare la richiesta di trascrizione",
    "audio": "Impossibile leggere il segmento audio",
    "reference": "Reference relatori non valide; fornire fino a quattro clip da 2 a 10 secondi",
    "response": "Risposta di trascrizione OpenAI non valida",
    "chunks": "Segmenti audio non validi o non ordinati",
}


class TranscriptionError(Exception):
    """Only fixed application text and safe classification, never remote data."""

    def __init__(self, code: str, *, status_code: int | None = None) -> None:
        super().__init__(_ERROR_MESSAGES[code])
        self.code = code
        self.status_code = status_code
        self.retryable = code in {"timeout", "rate_limit", "server"}


def _classify_remote_error(exc: Exception) -> TranscriptionError:
    if isinstance(exc, (APITimeoutError, TimeoutError, httpx.TimeoutException)):
        return TranscriptionError("timeout")
    if isinstance(exc, APIStatusError):
        status = exc.status_code
        if status == 429:
            code = "rate_limit"
        elif 500 <= status <= 599:
            code = "server"
        elif status in {401, 403}:
            code = "auth"
        else:
            code = "request"
        return TranscriptionError(code, status_code=status)
    return TranscriptionError("transport")


_AUDIO_TYPES = {
    ".flac": "audio/flac", ".mp3": "audio/mpeg", ".mpga": "audio/mpeg",
    ".mpeg": "audio/mpeg", ".mp4": "audio/mp4", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".wav": "audio/wav", ".webm": "audio/webm",
}


def _references(speakers: Sequence[KnownSpeaker], event: Event | None) -> dict:
    if len(speakers) > 4 or len({speaker.name for speaker in speakers}) != len(speakers):
        raise TranscriptionError("reference")
    if not speakers:
        return {}
    references = []
    for speaker in speakers:
        check_cancelled(event)
        mime = _AUDIO_TYPES.get(speaker.path.suffix.lower())
        if mime is None:
            raise TranscriptionError("reference")
        try:
            with speaker.path.open("rb") as file:
                encoded = base64.b64encode(file.read()).decode("ascii")
        except OSError:
            raise TranscriptionError("reference") from None
        references.append(f"data:{mime};base64,{encoded}")
    return {"known_speaker_names": [speaker.name for speaker in speakers],
            "known_speaker_references": references}


class OpenAITranscriber:
    def __init__(self, client: OpenAI) -> None:
        # SDK defaults also retry 408/409 and connection failures. Disable those
        # retries so exactly our selective, three-attempt policy controls calls.
        self._client = client.with_options(max_retries=0)

    def transcribe(
        self, chunks: Sequence[AudioChunk], known_speakers: Sequence[KnownSpeaker] = (),
        *, cancellation_event: Event | None = None,
    ) -> TranscriptionResult:
        check_cancelled(cancellation_event)
        references = _references(known_speakers, cancellation_event)
        known_names = {speaker.name for speaker in known_speakers}
        originals: list[TranscriptSegment] = []
        mapping: dict[str, str] = {}
        texts: list[str] = []
        next_speaker = 1
        input_tokens = output_tokens = 0
        complete_usage = bool(chunks)
        usage = ProviderUsage()
        previous_start = -1.0
        for index, chunk in enumerate(chunks):
            check_cancelled(cancellation_event)
            if (not math.isfinite(chunk.start_seconds) or not math.isfinite(chunk.duration_seconds)
                    or chunk.start_seconds < max(0, previous_start) or chunk.duration_seconds <= 0):
                raise TranscriptionError("chunks")
            previous_start = chunk.start_seconds

            def request() -> TranscriptionDiarized:
                check_cancelled(cancellation_event)
                try:
                    file = chunk.path.open("rb")
                except OSError:
                    raise TranscriptionError("audio") from None
                with file:
                    try:
                        result = self._client.audio.transcriptions.create(
                            file=file, model="gpt-4o-transcribe-diarize",
                            response_format="diarized_json", chunking_strategy="auto",
                            **references,
                        )
                    except CancelledError:
                        raise
                    except Exception as exc:
                        record_usage(usage, exc, audio_seconds=chunk.duration_seconds)
                        check_cancelled(cancellation_event)
                        raise _classify_remote_error(exc) from None
                record_usage(usage, result, audio_seconds=chunk.duration_seconds)
                check_cancelled(cancellation_event)
                return result

            response = retry_remote(
                request, retryable=lambda exc: isinstance(exc, TranscriptionError) and exc.retryable,
                cancellation_event=cancellation_event,
            )
            try:
                # Explicit validation catches malformed SDK bodies, including
                # fields the SDK's permissive parsing may leave missing.
                response = TranscriptionDiarized.model_validate(response.model_dump())
                chunk_segments = []
                provider_segments = list(response.segments)
                for segment in provider_segments:
                    if (not math.isfinite(segment.start) or not math.isfinite(segment.end)
                            or segment.start < 0 or segment.end < segment.start
                            or not segment.speaker.strip()):
                        raise ValueError
                # Diarization can legitimately return slightly overlapping
                # turns out of order. Preserve every valid turn and normalize
                # the provider order before downstream role analysis.
                provider_segments.sort(key=lambda segment: (segment.start, segment.end))
                for segment in provider_segments:
                    chunk_segments.append(TranscriptSegment(
                        start_seconds=chunk.start_seconds + segment.start,
                        end_seconds=chunk.start_seconds + segment.end,
                        diarization_label=f"chunk-{index}:{segment.speaker}", text=segment.text,
                    ))
                    label = chunk_segments[-1].diarization_label
                    if label not in mapping:
                        if segment.speaker in known_names:
                            mapping[label] = segment.speaker
                        else:
                            # Reserve supplied names so an explicit 'Relatore 1'
                            # can never collide with a generic identity.
                            while f"Relatore {next_speaker}" in known_names:
                                next_speaker += 1
                            mapping[label] = f"Relatore {next_speaker}"
                            next_speaker += 1
                originals.extend(chunk_segments)
                texts.append(response.text.strip())
                if response.usage is not None and response.usage.type == "tokens":
                    input_tokens += response.usage.input_tokens
                    output_tokens += response.usage.output_tokens
                else:
                    complete_usage = False
            except (ValueError, TypeError, AttributeError):
                raise TranscriptionError("response") from None

        # Keep every original in memory for downstream role/name corrections.
        # Sorting is stable even when callers provide controlled overlap chunks.
        originals.sort(key=lambda segment: segment.start_seconds)
        merged: list[TranscriptSegment] = []
        for segment in originals:
            if (merged and mapping[merged[-1].diarization_label] == mapping[segment.diarization_label]
                    and 0 <= segment.start_seconds - merged[-1].end_seconds <= 0.5):
                merged[-1].end_seconds = segment.end_seconds
                merged[-1].text = f"{merged[-1].text.rstrip()} {segment.text.lstrip()}".strip()
            else:
                merged.append(segment.model_copy())
        check_cancelled(cancellation_event)
        return TranscriptionResult(
            text=" ".join(text for text in texts if text), segments=merged,
            original_segments=originals, speaker_mapping=mapping,
            audio_seconds=sum(chunk.duration_seconds for chunk in chunks),
            input_tokens=input_tokens if complete_usage else None,
            output_tokens=output_tokens if complete_usage else None,
            usage=usage,
        )
