"""Connect ephemeral media and AI stages without retaining source artifacts."""

from collections.abc import Callable
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event
from time import monotonic

from app.analysis import AnalysisError, OpenAIAnalyzer
from app.bunny import BunnyAuthError, BunnyClient, BunnyNotFoundError, BunnyUrlError, parse_bunny_url
from app.config import Settings
from app.costs import estimate_cost
from app.jobs import JobCancelled
from app.logging_config import log_event
from app.media import FFmpegProcessor, MediaError, MediaProtectedError, temporary_workspace
from app.models import AcademyReport
from app.transcription import OpenAITranscriber, TranscriptionError


_MESSAGES = {
    "invalid_link": "Il link Bunny non è valido; usa un video della libreria configurata",
    "bunny_auth": "Accesso a Bunny non autorizzato; verifica la chiave API e la libreria configurata",
    "not_found": "Video non trovato nella libreria Bunny",
    "protected_video": "Video protetto o accesso negato: verifica token CDN, restrizioni referrer e DRM nella configurazione Bunny",
    "unsupported_duration": "Il video supera il limite di quattro ore",
    "media_decode": "Impossibile elaborare audio o immagini; verifica il video e l'installazione di FFmpeg",
    "transcription": "Trascrizione non riuscita; verifica la configurazione OpenAI e riprova",
    "analysis": "Analisi non riuscita; verifica la configurazione OpenAI e riprova",
    "temporary_failure": "Servizio temporaneamente non disponibile; riprova più tardi",
}


class PipelineError(Exception):
    """Only application-authored messages may enter this public error boundary."""

    def __init__(self, code: str, user_message: str | None = None) -> None:
        self.code = code
        # Retain the call signature for existing callers, but never trust text
        # supplied by a caller at the public error boundary.
        self.user_message = _MESSAGES[code]
        super().__init__(self.user_message)


class PipelineCancelled(JobCancelled):
    """Cancellation acknowledged after temporary artifacts have been removed."""


class AnalysisPipeline:
    def __init__(
        self, settings: Settings, bunny: BunnyClient, media: FFmpegProcessor,
        transcriber: OpenAITranscriber, analyzer: OpenAIAnalyzer,
        *, temp_root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.bunny = bunny
        self.media = media
        self.transcriber = transcriber
        self.analyzer = analyzer
        self.temp_root = temp_root if temp_root is not None else settings.temp_root

    def run(
        self, source_url: str, progress_callback: Callable[[int, str], None],
        cancellation_event: Event | None = None,
    ) -> AcademyReport:
        event = cancellation_event or Event()
        last_progress = 0
        phase = "validation"
        started = monotonic()

        def next_phase(value: str) -> None:
            nonlocal phase, started
            log_event(phase, elapsed_seconds=monotonic() - started)
            phase, started = value, monotonic()

        def check_cancelled() -> None:
            if event.is_set():
                raise PipelineCancelled()

        def progress(percent: int, message: str) -> None:
            nonlocal last_progress
            check_cancelled()
            last_progress = max(last_progress, percent)
            progress_callback(last_progress, message)
            check_cancelled()

        try:
            check_cancelled()
            ref = parse_bunny_url(source_url, expected_library_id=self.settings.bunny_library_id,
                                  cdn_hostname=self.settings.bunny_cdn_hostname)
            next_phase("metadata")
            metadata = self.bunny.get_metadata(str(ref.video_id))
            progress(5, "Metadati letti")
            if metadata.duration_seconds > 14_400:
                raise PipelineError("unsupported_duration")
            with temporary_workspace(self.temp_root) as workspace:
                next_phase("media")
                progress(10, "Estrazione audio e immagini")
                def media_progress(seconds: float) -> None:
                    fraction = min(1, max(0, seconds / max(1, metadata.duration_seconds)))
                    progress(10 + int(35 * fraction), "Estrazione audio e immagini")

                media = self.media.extract(self.bunny.build_hls_url(str(ref.video_id)),
                                           workspace, media_progress, event)
                progress(45, "Audio e immagini pronti")
                progress(50, "Trascrizione e distinzione dei relatori")
                next_phase("transcription")
                transcript = self.transcriber.transcribe(media.audio_chunks, cancellation_event=event)
                progress(72, "Trascrizione completata")
                progress(75, "Analisi delle slide e dei contenuti")
                next_phase("analysis")
                content = self.analyzer.analyze(metadata, transcript, media.frame_candidates,
                                                cancellation_event=event)
                progress(85, "Analisi completata")
                progress(88, "Preparazione del report")
                next_phase("report")
                report = AcademyReport(**content.model_dump(),
                                       cost=estimate_cost(metadata.duration_seconds, media.downloaded_bytes))
                # No transcript or media references escape this method.
                del transcript
                progress(98, "Report pronto; pulizia dei file temporanei")
            progress(100, "Completato")
            log_event(phase, elapsed_seconds=monotonic() - started)
            return report
        except CancelledError:
            log_event(phase, elapsed_seconds=monotonic() - started, error_code="cancelled")
            raise PipelineCancelled() from None
        except PipelineError as exc:
            log_event(phase, elapsed_seconds=monotonic() - started, error_code=exc.code)
            raise
        except Exception as exc:
            check_cancelled()
            if isinstance(exc, BunnyUrlError):
                code = "invalid_link"
            elif isinstance(exc, BunnyAuthError):
                code = "bunny_auth"
            elif isinstance(exc, BunnyNotFoundError):
                code = "not_found"
            elif isinstance(exc, MediaProtectedError):
                code = "protected_video"
            elif isinstance(exc, MediaError):
                code = "media_decode"
            elif isinstance(exc, TranscriptionError):
                code = "transcription"
            elif isinstance(exc, AnalysisError):
                code = "analysis"
            else:
                code = "temporary_failure"
            log_event(phase, elapsed_seconds=monotonic() - started, error_code=code,
                      status_code=getattr(exc, "status_code", None))
            raise PipelineError(code) from None
