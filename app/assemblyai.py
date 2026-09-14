"""Ephemeral AssemblyAI transcription using one remote pre-recorded job."""

from concurrent.futures import CancelledError
import math
import re
from threading import Event
from time import monotonic
from typing import Any

import httpx

from app.models import ProviderUsage, UsageEntry
from app.retry import check_cancelled, retry_remote
from app.transcription import TranscriptSegment, TranscriptWord, TranscriptionError, TranscriptionResult


_TRANSCRIPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_GENERIC_PROVIDER_SPEAKER = re.compile(r"^(?:(?:speaker\s+)?[A-Z]|\d+)$", re.IGNORECASE)
_ALLOWED_BASE_URLS = {
    "https://api.assemblyai.com",
    "https://api.eu.assemblyai.com",
}


class WordEvidenceError(TranscriptionError):
    """A completed transcript lacks valid audio word evidence, not availability."""

    def __init__(self) -> None:
        super().__init__("response")


def _error_for_status(status: int) -> TranscriptionError:
    code = (
        "rate_limit" if status == 429
        else "server" if 500 <= status <= 599
        else "auth" if status in {401, 403}
        else "request"
    )
    return TranscriptionError(code, status_code=status)


class AssemblyAITranscriber:
    """Transcribe a Bunny MP4 URL globally, then delete the provider artifact."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.eu.assemblyai.com",
        transport: httpx.BaseTransport | None = None,
        poll_interval_seconds: float = 3,
        timeout_seconds: float = 21_600,
    ) -> None:
        if not api_key.strip() or base_url not in _ALLOWED_BASE_URLS:
            raise ValueError("Configurazione AssemblyAI non valida")
        if poll_interval_seconds < 0 or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Tempi AssemblyAI non validi")
        self._client = httpx.Client(
            base_url=base_url,
            headers={"authorization": api_key, "content-type": "application/json"},
            timeout=30,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._poll_interval_seconds = poll_interval_seconds
        self._timeout_seconds = timeout_seconds

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        cancellation_event: Event | None = None,
    ) -> httpx.Response:
        def send() -> httpx.Response:
            check_cancelled(cancellation_event)
            try:
                response = self._client.request(method, path, json=json_body)
            except CancelledError:
                raise
            except (httpx.TimeoutException, TimeoutError):
                raise TranscriptionError("timeout") from None
            except httpx.RequestError:
                raise TranscriptionError("transport") from None
            if response.status_code < 200 or response.status_code >= 300:
                raise _error_for_status(response.status_code) from None
            # A successful submit must reach the caller so its transcript ID is
            # captured before cancellation can unwind into the deletion guard.
            if method != "POST":
                check_cancelled(cancellation_event)
            return response

        return retry_remote(
            send,
            # A failed POST can be ambiguous: retrying it could create a second
            # paid transcript whose id we never receive and therefore cannot
            # delete. Polling and deletion are safe to retry by transcript id.
            retryable=lambda exc: (
                method != "POST" and isinstance(exc, TranscriptionError) and exc.retryable
            ),
            # retry_remote normally checks cancellation after the call. For a
            # submit response that would discard the newly created ID before
            # the caller can enter its guaranteed deletion path. `send` still
            # performs the pre-call cancellation check for POST requests.
            cancellation_event=None if method == "POST" else cancellation_event,
        )

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            value = response.json()
        except (ValueError, TypeError):
            raise TranscriptionError("response") from None
        if not isinstance(value, dict):
            raise TranscriptionError("response")
        return value

    def _delete(self, transcript_id: str) -> None:
        # The API confirms deletion with a 2xx response. Its response object can
        # retain the transcript's terminal status (for example "completed").
        self._request("DELETE", f"/v2/transcript/{transcript_id}")

    @staticmethod
    def _parse_result(payload: dict[str, Any], expected_duration: float) -> TranscriptionResult:
        utterances = payload.get("utterances")
        if not isinstance(utterances, list) or not utterances:
            raise TranscriptionError("response")
        try:
            provider_duration = float(payload.get("audio_duration", expected_duration))
        except (TypeError, ValueError):
            raise TranscriptionError("response") from None
        if not math.isfinite(provider_duration) or provider_duration <= 0:
            raise TranscriptionError("response")
        # Bunny metadata describes the source media requested for this job. A
        # differing provider-reported duration must not extend word evidence
        # beyond the source timeline used by the boundary aligner.
        word_duration_limit = expected_duration

        originals: list[TranscriptSegment] = []
        mapping: dict[str, str] = {}
        next_speaker = 1
        for utterance_index, utterance in enumerate(utterances, start=1):
            try:
                speaker = utterance["speaker"]
                text = utterance["text"]
                start = float(utterance["start"]) / 1000
                end = float(utterance["end"]) / 1000
            except (KeyError, TypeError, ValueError):
                raise TranscriptionError("response") from None
            if (
                not isinstance(speaker, str) or not speaker.strip() or len(speaker) > 200
                or not isinstance(text, str) or not text.strip()
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or end < start or end > max(expected_duration, provider_duration) + 60
            ):
                raise TranscriptionError("response")
            label = f"assembly:{speaker.strip()}"
            raw_words = utterance.get("words") if isinstance(utterance, dict) else None
            if not isinstance(raw_words, list) or not raw_words:
                raise WordEvidenceError()
            words: list[TranscriptWord] = []
            previous_start = -1.0
            for raw_word in raw_words:
                try:
                    word_text = raw_word["text"]
                    word_speaker = raw_word["speaker"]
                    word_start = float(raw_word["start"]) / 1000
                    word_end = float(raw_word["end"]) / 1000
                    confidence = float(raw_word["confidence"])
                except (KeyError, TypeError, ValueError, OverflowError):
                    raise WordEvidenceError() from None
                if (
                    not isinstance(word_text, str) or not word_text.strip()
                    or not isinstance(word_speaker, str) or word_speaker.strip() != speaker.strip()
                    or not math.isfinite(word_start) or not math.isfinite(word_end)
                    or not math.isfinite(confidence)
                    or word_start < 0 or word_end > word_duration_limit
                    or word_start < previous_start
                ):
                    raise WordEvidenceError()
                try:
                    parsed_word = TranscriptWord(
                        text=word_text.strip(), start_seconds=word_start, end_seconds=word_end,
                        diarization_label=label, confidence=confidence,
                    )
                except (TypeError, ValueError):
                    raise WordEvidenceError() from None
                words.append(parsed_word)
                previous_start = word_start
            originals.append(TranscriptSegment(
                start_seconds=min(start, words[0].start_seconds),
                end_seconds=max(end, max(word.end_seconds for word in words)),
                diarization_label=label,
                text=text.strip(),
                source_utterance_id=f"assembly-u{utterance_index:06d}",
                words=words,
            ))
            if label not in mapping:
                if _GENERIC_PROVIDER_SPEAKER.fullmatch(speaker.strip()):
                    mapping[label] = f"Relatore {next_speaker}"
                    next_speaker += 1
                else:
                    mapping[label] = speaker.strip()

        originals.sort(key=lambda item: (item.start_seconds, item.end_seconds))
        language = payload.get("language_code")
        if not isinstance(language, str) or not language.strip():
            language = "und"
        full_text = payload.get("text")
        if not isinstance(full_text, str) or not full_text.strip():
            full_text = " ".join(segment.text for segment in originals)
        usage = ProviderUsage(entries=[UsageEntry(
            provider_audio_seconds=provider_duration,
            request_audio_seconds=expected_duration,
        )])
        return TranscriptionResult(
            provider="assemblyai",
            language=language.strip(),
            text=full_text.strip(),
            # Keep provider utterances distinct so the bounded report sampler
            # can represent the beginning, middle and end of a long monologue.
            segments=[segment.model_copy() for segment in originals],
            original_segments=originals,
            speaker_mapping=mapping,
            audio_seconds=provider_duration,
            usage=usage,
        )

    def transcribe_url(
        self,
        audio_url: str,
        *,
        duration_seconds: float,
        cancellation_event: Event | None = None,
    ) -> TranscriptionResult:
        if (
            not isinstance(audio_url, str)
            or not audio_url.startswith("https://")
            or not math.isfinite(duration_seconds)
            or duration_seconds <= 0
            or duration_seconds > 14_400
        ):
            raise TranscriptionError("chunks")
        check_cancelled(cancellation_event)
        transcript_id: str | None = None
        try:
            response = self._request(
                "POST",
                "/v2/transcript",
                json_body={
                    "audio_url": audio_url,
                    "speech_models": ["universal-3-5-pro", "universal-2"],
                    "language_detection": True,
                    "speaker_labels": True,
                    "speech_understanding": {
                        "request": {"speaker_identification": {"speaker_type": "name"}}
                    },
                },
                cancellation_event=cancellation_event,
            )
            transcript_id = self._json(response).get("id")
            if not isinstance(transcript_id, str) or not _TRANSCRIPT_ID.fullmatch(transcript_id):
                raise TranscriptionError("response")
            check_cancelled(cancellation_event)

            deadline = monotonic() + self._timeout_seconds
            while True:
                response = self._request(
                    "GET",
                    f"/v2/transcript/{transcript_id}",
                    cancellation_event=cancellation_event,
                )
                payload = self._json(response)
                status = payload.get("status")
                if status == "completed":
                    return self._parse_result(payload, duration_seconds)
                if status == "error":
                    raise TranscriptionError("request")
                if status not in {"queued", "processing"} or monotonic() >= deadline:
                    raise TranscriptionError("timeout" if monotonic() >= deadline else "response")
                if cancellation_event is None:
                    Event().wait(self._poll_interval_seconds)
                elif cancellation_event.wait(self._poll_interval_seconds):
                    raise CancelledError()
        finally:
            if transcript_id is not None:
                self._delete(transcript_id)
