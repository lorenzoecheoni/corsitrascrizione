"""Connect ephemeral media and AI stages without retaining source artifacts."""

from collections.abc import Callable, Sequence
from contextlib import ExitStack
from concurrent.futures import CancelledError, FIRST_EXCEPTION, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event
from time import monotonic
from urllib.parse import urlsplit
from collections import Counter

from app.analysis import AnalysisError, OpenAIAnalyzer
from app.boundaries import has_complete_boundary_evidence
from app.assemblyai import AssemblyAITranscriber, WordEvidenceError
from app.bunny import (BunnyAuthError, BunnyClient, BunnyNotFoundError, BunnyUrlError,
                       BunnyPlaybackError, BunnyReadinessError, BunnyVideoMetadata, parse_bunny_url, read_metadata)
from app.config import Settings
from app.costs import estimate_cost
from app.jobs import JobCancelled
from app.inventory import InventoryError
from app.logging_config import log_event
from app.media import FFmpegProcessor, MediaError, MediaProtectedError, SilenceEvidenceError, temporary_workspace
from app.material_registry import AnalysisInventoryContext, canonical_material_title
from app.materials import (
    MaterialAnalysis, MaterialError, MaterialInternalError, MaterialProcessor,
    _validate_url, validate_persisted_material,
)
from app.models import AcademyReport, APIUsage, ProviderUsage, ReportMaterial, SlideChange
from app.transcription import OpenAITranscriber, TranscriptionError


_MESSAGES = {
    "invalid_link": "Il link Bunny non è valido; usa un video della libreria configurata",
    "bunny_auth": "Accesso a Bunny non autorizzato; verifica la chiave API e la libreria configurata",
    "not_found": "Video non trovato nella libreria Bunny",
    "protected_video": "Video protetto o accesso negato: verifica token CDN, restrizioni referrer e DRM nella configurazione Bunny",
    "unsupported_duration": "Il video supera il limite di quattro ore",
    "media_decode": "Impossibile elaborare audio o immagini; verifica il video e l'installazione di FFmpeg",
    "transcription": "Trascrizione non riuscita; verifica la configurazione del servizio e riprova",
    "analysis": "Analisi non riuscita; verifica la configurazione OpenAI e riprova",
    "analysis_rate_limit": "Limite OpenAI temporaneamente raggiunto; riprova più tardi",
    "analysis_visual": "Analisi delle slide non riuscita; verifica la configurazione OpenAI e riprova",
    "analysis_window": "Analisi della finestra testuale non riuscita; verifica la configurazione OpenAI e riprova",
    "analysis_consolidation": "Consolidamento del report non riuscito; verifica la configurazione OpenAI e riprova",
    "analysis_visual_rate_limit": "Limite OpenAI raggiunto durante l'analisi delle slide; riprova più tardi",
    "analysis_window_rate_limit": "Limite OpenAI raggiunto durante l'analisi della finestra testuale; riprova più tardi",
    "analysis_consolidation_rate_limit": "Limite OpenAI raggiunto durante il consolidamento del report; riprova più tardi",
    "temporary_failure": "Servizio temporaneamente non disponibile; riprova più tardi",
    "not_ready": "Video ancora in elaborazione su Bunny; attendere la fine della codifica",
    "encoding_failed": "Codifica o caricamento Bunny fallito; verificare il video nella libreria",
    "unsupported_media": "Formato o risoluzioni Bunny non supportati; verificare la codifica HLS",
    "boundaries": "Verifica audio dei confini non riuscita; riprova",
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


def _verified_material_result(
    context: AnalysisInventoryContext, analysis: MaterialAnalysis,
    slide_snapshot: tuple[str, ...], workspace: Path, allowed_hosts: Sequence[str],
) -> MaterialAnalysis:
    """Validate the internal processor contract before any result is persisted."""
    candidates = {}
    for source in context.material_sources:
        if " | " in source:
            title, url = source.split(" | ", 1)
            candidates.setdefault(("url", url), set()).add(canonical_material_title(title))
        elif source.startswith(("https://", "http://")):
            candidates.setdefault(("url", source), set()).add(
                canonical_material_title(Path(urlsplit(source).path).name or source))
        else:
            path = Path(source)
            candidates.setdefault(("file", str(path)), set()).add(canonical_material_title(path.stem))

    materials, omitted, failures, all_materials = [], set(), [], []
    # Revalidate models as model_copy/in-place assignment bypasses validators.
    for item in analysis.materials:
        if item.pagine is not None and type(item.pagine) is not int:
            raise MaterialInternalError()
        material = ReportMaterial.model_validate(item.model_dump())
        key = ("url", material.url) if material.url is not None else ("file", material.file)
        if key not in candidates or material.titolo not in candidates[key]:
            raise MaterialInternalError()
        if material.file is not None and Path(material.file).resolve().is_relative_to(workspace.resolve()):
            raise MaterialInternalError()
        if material.url is not None:
            try:
                _validate_url(material.url, allowed_hosts)
            except (MaterialError, ValueError):
                raise MaterialInternalError() from None
            url = urlsplit(material.url)
            if url.username is not None or url.password is not None:
                raise MaterialInternalError()
        all_materials.append(material)
        try:
            # Use exactly the offline export grammar before promotion. A
            # successful fetch cannot establish a different durable source.
            validate_persisted_material(material)
        except ValueError:
            omitted.add(material.titolo)
            failures.append("MATERIALE_NON_RAGGIUNGIBILE")
            continue
        materials.append(material)
    counts = Counter(material.titolo for material in all_materials)
    # Inspect every original verified identity before removing any result.
    # Choosing a title winner would silently rebind the report's slide links.
    sources_by_title = {}
    for material in all_materials:
        source = (("url", *_validate_url(material.url, allowed_hosts))
                  if material.url is not None else ("file", material.file))
        sources_by_title.setdefault(material.titolo, set()).add(source)
    ambiguous = {title for title, sources in sources_by_title.items() if len(sources) > 1}
    omitted.update(ambiguous)
    failures.extend("MATERIALE_NON_RAGGIUNGIBILE" for _ in ambiguous)
    materials = [material for material in materials if material.titolo not in omitted]
    verified = {material.titolo: material for material in all_materials}
    slides = []
    for snapshot, matched in zip(slide_snapshot, analysis.slides, strict=True):
        title, page = matched.material_title, matched.page
        if title is None and page is None:
            pass
        elif (title not in verified or counts[title] != 1
              or type(page) is not int or verified[title].pagine is None
              or not 1 <= page <= verified[title].pagine):
            raise MaterialInternalError()
        elif title in omitted:
            title, page = None, None
        slides.append(SlideChange.model_validate_json(snapshot).model_copy(update={
            "material_title": title, "page": page,
        }))
    failures.extend("MATERIALE_NON_RAGGIUNGIBILE" for _ in analysis.failures)
    return MaterialAnalysis(tuple(materials), tuple(slides), tuple(failures))


class _LinkedCancellation:
    """Read-only Event view that wakes when either source event is set."""

    def __init__(self, *events: Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else monotonic() + max(0, timeout)
        while not self.is_set():
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                delay = min(.05, remaining)
            else:
                delay = .05
            self._events[0].wait(delay)
        return True


class AnalysisPipeline:
    def __init__(
        self, settings: Settings, bunny: BunnyClient, media: FFmpegProcessor,
        transcriber: OpenAITranscriber, analyzer: OpenAIAnalyzer,
        *, fast_transcriber: AssemblyAITranscriber | None = None,
        speaker_hint_provider: Callable[[object], Sequence[str]] | None = None,
        context_provider: Callable[[BunnyVideoMetadata], AnalysisInventoryContext] | None = None,
        material_processor: MaterialProcessor | None = None,
        temp_root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.bunny = bunny
        self.media = media
        self.transcriber = transcriber
        self.fast_transcriber = fast_transcriber
        self.analyzer = analyzer
        self.speaker_hint_provider = speaker_hint_provider
        self.context_provider = context_provider
        self.material_processor = material_processor
        self.temp_root = temp_root if temp_root is not None else settings.temp_root

    def _run_fast_media_and_transcription(
        self, metadata, workspace: Path, media_progress: Callable[[float], None], event: Event,
    ):
        """Run the local slide scan and remote transcription concurrently."""
        internal_cancel = Event()
        linked_cancel = _LinkedCancellation(event, internal_cancel)

        def extract_visual():
            for attempt in range(2):
                try:
                    return self.media.extract_visual(
                        self.bunny.build_mp4_url(metadata), workspace,
                        media_progress, linked_cancel,
                        duration_seconds=metadata.duration_seconds,
                    )
                except (MediaProtectedError, BunnyPlaybackError):
                    if attempt or not self.settings.bunny_token_auth_key:
                        raise PipelineError("protected_video") from None
            raise AssertionError("Unreachable")

        def transcribe():
            return self.fast_transcriber.transcribe_url(
                self.bunny.build_mp4_url(metadata),
                duration_seconds=metadata.duration_seconds,
                cancellation_event=linked_cancel,
            )

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fast-analysis") as executor:
            media_future = executor.submit(extract_visual)
            transcript_future = executor.submit(transcribe)
            futures = {media_future, transcript_future}
            pending = set(futures)
            try:
                while pending:
                    done, pending = wait(pending, timeout=.1, return_when=FIRST_EXCEPTION)
                    if event.is_set() or any(future.exception() is not None for future in done):
                        internal_cancel.set()
            finally:
                internal_cancel.set()

        if event.is_set():
            raise PipelineCancelled()
        errors = [future.exception() for future in (media_future, transcript_future)]
        for error in errors:
            if error is not None and not isinstance(error, (CancelledError, PipelineCancelled)):
                raise error
        if any(error is not None for error in errors):
            raise PipelineCancelled()
        return media_future.result(), transcript_future.result()

    def run(
        self, source_url: str, progress_callback: Callable[[int, str], None],
        cancellation_event: Event | None = None,
    ) -> AcademyReport:
        event = cancellation_event or Event()
        last_progress = 0
        phase = "validation"
        started = monotonic()

        def next_phase(value: str, *, error_code: str = "ok") -> None:
            nonlocal phase, started
            log_event(phase, elapsed_seconds=monotonic() - started, error_code=error_code)
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
            metadata = read_metadata(self.bunny, str(ref.video_id), event)
            progress(5, "Metadati letti")
            context = AnalysisInventoryContext((), ())
            material_failures: list[str] = []
            speaker_name_hints: Sequence[str] = ()
            if self.context_provider is not None:
                try:
                    context = self.context_provider(metadata)
                except InventoryError:
                    material_failures.append("MATERIALE_NON_RAGGIUNGIBILE")
                check_cancelled()
                speaker_name_hints = context.speaker_hints
                material_failures.extend("MATERIALE_NON_RAGGIUNGIBILE" for _ in context.material_failures)
            elif self.speaker_hint_provider is not None:
                try:
                    speaker_name_hints = self.speaker_hint_provider(metadata)
                except CancelledError:
                    raise
                except Exception:
                    # Inventory names improve spelling only; the report must
                    # remain available when the optional sheet cannot be read.
                    speaker_name_hints = ()
            if metadata.duration_seconds > 14_400:
                raise PipelineError("unsupported_duration")
            with ExitStack() as workspaces:
                next_phase("media")
                media_message = (
                    "Rilevamento slide e trascrizione in parallelo"
                    if self.fast_transcriber is not None
                    else "Estrazione audio e immagini"
                )
                progress(10, media_message)
                def media_progress(seconds: float) -> None:
                    fraction = min(1, max(0, seconds / max(1, metadata.duration_seconds)))
                    progress(10 + int(35 * fraction), media_message)

                if self.fast_transcriber is not None:
                    workspace = workspaces.enter_context(temporary_workspace(self.temp_root))
                    media, transcript = self._run_fast_media_and_transcription(
                        metadata, workspace, media_progress, event,
                    )
                else:
                    for attempt in range(2):
                        workspace = workspaces.enter_context(temporary_workspace(self.temp_root))
                        try:
                            url = self.bunny.select_hls_url(metadata, cancellation_event=event)
                            media = self.media.extract(url, workspace, media_progress, event)
                            break
                        except (MediaProtectedError, BunnyPlaybackError):
                            workspaces.close()
                            check_cancelled()
                            if attempt or not self.settings.bunny_token_auth_key:
                                raise PipelineError("protected_video") from None
                if not media.silence_measured:
                    next_phase("boundary")
                    raise PipelineError("boundaries")
                progress(45, "Immagini pronte" if self.fast_transcriber is not None else "Audio e immagini pronti")
                progress(50, "Trascrizione e distinzione dei relatori")
                next_phase("transcription")
                if self.fast_transcriber is None:
                    transcript = self.transcriber.transcribe(media.audio_chunks, cancellation_event=event)
                progress(72, "Trascrizione completata")
                progress(75, "Analisi delle slide e dei contenuti")
                next_phase("analysis")

                def analysis_progress(stage: str, completed: int, total: int) -> None:
                    fraction = min(1, max(0, completed / max(1, total)))
                    if stage == "slides":
                        progress(75 + int(7 * fraction), f"Slide {completed}/{total}")
                    elif stage == "transcript":
                        progress(82 + int(7 * fraction), f"Interventi {completed}/{total}")
                    elif stage == "consolidation":
                        progress(90, "Consolidamento del report")

                analyze = self.analyzer.analyze_fast if transcript.provider == "assemblyai" else self.analyzer.analyze
                analysis_options = {
                    "cancellation_event": event,
                    "progress_callback": analysis_progress,
                    "silence_intervals": (
                        media.silence_intervals if media.silence_measured else None
                    ),
                }
                if self.context_provider is not None or self.speaker_hint_provider is not None:
                    analysis_options["speaker_name_hints"] = speaker_name_hints
                content = analyze(metadata, transcript, media.frame_candidates, **analysis_options)
                progress(92, "Verifica dei materiali del corso")
                next_phase("materials")
                # AI output cannot promote materials or create verified links.
                # Strings in a tuple form a deep immutable observation snapshot.
                # The processor receives entirely separate models/nested lists.
                slide_snapshot = tuple(slide.model_dump_json(exclude={"material_title", "page"})
                                       for slide in content.slides)
                slides = [SlideChange.model_validate_json(snapshot) for snapshot in slide_snapshot]
                material_analysis = MaterialAnalysis((), tuple(slides), ())
                if self.material_processor is not None:
                    try:
                        material_analysis = self.material_processor.process(
                            context.material_sources, [slide.model_copy(deep=True) for slide in slides], workspace, event,
                        )
                    except MaterialError:
                        material_failures.append("MATERIALE_NON_RAGGIUNGIBILE")
                check_cancelled()
                material_analysis = _verified_material_result(
                    context, material_analysis, slide_snapshot, workspace, self.settings.parsed_material_allowed_hosts,
                )
                materials = material_analysis.materials
                material_failures.extend("MATERIALE_NON_RAGGIUNGIBILE" for _ in material_analysis.failures)
                slides = material_analysis.slides
                progress(96, "Preparazione del report")
                next_phase("report", error_code="MATERIALE_NON_RAGGIUNGIBILE" if material_failures else "ok")
                usage = APIUsage(transcription=transcript.usage,
                                 responses=getattr(content, "usage", ProviderUsage()))
                report = AcademyReport(**content.model_dump(exclude={"usage", "materials", "material_failures", "slides"}),
                    materials=materials, material_failures=material_failures, slides=slides, bunny_title=metadata.title,
                    usage=usage, cost=estimate_cost(
                        metadata.duration_seconds, media.downloaded_bytes, usage=usage,
                        transcription_provider=transcript.provider,
                    ))
                # No transcript or media references escape this method.
                del transcript
                next_phase("boundary")
                if not has_complete_boundary_evidence(report):
                    raise PipelineError("boundaries")
                next_phase("report")
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
            elif isinstance(exc, BunnyReadinessError):
                code = exc.code
            elif isinstance(exc, BunnyNotFoundError):
                code = "not_found"
            elif isinstance(exc, MediaProtectedError):
                code = "protected_video"
            elif isinstance(exc, (WordEvidenceError, SilenceEvidenceError)):
                code = "boundaries"
                phase = "boundary"
            elif isinstance(exc, MediaError):
                code = "media_decode"
            elif isinstance(exc, TranscriptionError):
                code = "transcription"
            elif isinstance(exc, AnalysisError):
                if exc.code == "boundaries" or exc.stage == "boundary":
                    code = "boundaries"
                    phase = "boundary"
                else:
                    code = f"analysis_{exc.stage}"
                    if exc.code == "rate_limit":
                        code += "_rate_limit"
            else:
                code = "temporary_failure"
            log_event(phase, elapsed_seconds=monotonic() - started, error_code=code,
                      status_code=getattr(exc, "status_code", None),
                      detail_code=getattr(exc, "detail_code", None))
            raise PipelineError(code) from None
