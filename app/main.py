from contextlib import asynccontextmanager
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from time import monotonic
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from app.academy import AcademyGenerator
from app.editorial import EditorialEnricher
from app.analysis import OpenAIAnalyzer
from app.assemblyai import AssemblyAITranscriber
from app.auth import SESSION_COOKIE, session_is_valid
from app.bunny import BunnyClient
from app.config import Settings
from app.course_store import CourseStore
from app.courses import CourseConfirmationStore
from app.inventory import DEFAULT_INVENTORY_TABS, InventoryClient, inventory_context_for_video
from app.jobs import JobStore, SingleWorkerRunner
from app.logging_config import configure_logging, log_event
from app.media import FFmpegProcessor
from app.material_registry import AnalysisInventoryContext, match_library_file, scan_library_files
from app.materials import MaterialProcessor
from app.pipeline import AnalysisPipeline
from app.selection import ConfirmationStore
from app.transcription import OpenAITranscriber
from app.web import router


APP_DIRECTORY = Path(__file__).parent


@dataclass(frozen=True)
class Services:
    bunny: BunnyClient
    media: FFmpegProcessor
    openai: OpenAI
    transcriber: OpenAITranscriber
    analyzer: OpenAIAnalyzer
    assemblyai: AssemblyAITranscriber | None
    pipeline: AnalysisPipeline
    store: JobStore
    runner: SingleWorkerRunner
    inventory: InventoryClient
    inventory_context_provider: Callable[[object], AnalysisInventoryContext]
    course_store: CourseStore
    academy_generator: AcademyGenerator
    editorial_enricher: EditorialEnricher


def build_services(settings: Settings) -> Services:
    """Build the production graph; remote boundaries may be substituted in tests."""
    bunny = BunnyClient(settings)
    media = FFmpegProcessor(runtime_seconds=settings.media_runtime_seconds,
        inactivity_seconds=settings.media_inactivity_seconds,
        max_workspace_bytes=settings.media_max_workspace_bytes)
    openai = OpenAI(api_key=settings.openai_api_key, max_retries=0)
    transcriber = OpenAITranscriber(openai)
    analyzer = OpenAIAnalyzer(openai)
    inventory = InventoryClient(
        settings.google_sheet_id,
        DEFAULT_INVENTORY_TABS,
        service_account_json=settings.google_service_account_json,
    )

    library_matcher = None
    if settings.material_library_dir:
        library_directory = Path(settings.material_library_dir)

        def library_matcher(label: str) -> Path | None:
            return match_library_file(label, scan_library_files(library_directory))

    def inventory_context_provider(metadata) -> AnalysisInventoryContext:
        return inventory_context_for_video(
            inventory.fetch(), metadata.video_id, metadata.title,
            library=library_matcher,
        )

    assemblyai = None
    if settings.assemblyai_api_key:
        base_url = (
            "https://api.eu.assemblyai.com"
            if settings.assemblyai_region == "eu"
            else "https://api.assemblyai.com"
        )
        assemblyai = AssemblyAITranscriber(settings.assemblyai_api_key, base_url=base_url)
    pipeline = AnalysisPipeline(
        settings, bunny, media, transcriber, analyzer,
        fast_transcriber=assemblyai,
        context_provider=inventory_context_provider,
        material_processor=MaterialProcessor(settings.parsed_material_allowed_hosts),
    )
    store = JobStore(settings.database_path)
    runner = SingleWorkerRunner(store, pipeline.run)
    course_store = CourseStore(settings.database_path)
    academy_generator = AcademyGenerator(openai)
    editorial_enricher = EditorialEnricher(openai)
    return Services(
        bunny, media, openai, transcriber, analyzer, assemblyai, pipeline,
        store, runner, inventory, inventory_context_provider, course_store,
        academy_generator, editorial_enricher,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    try:
        app_settings = settings or Settings()
    except Exception:
        raise RuntimeError("Configurazione applicazione non valida; verificare le variabili richieste") from None
    configure_logging(app_settings)
    try:
        services = build_services(app_settings)
    except (OSError, sqlite3.Error):
        raise RuntimeError("Configurazione applicazione non valida; verificare le variabili richieste") from None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            # The active worker owns media cleanup; allow it to finish without
            # blocking the event loop or closing its remote client prematurely.
            services.runner.shutdown(wait=False)
            services.inventory.close()
            services.course_store.close()

    app = FastAPI(title="Bunny Video Report", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = app_settings
    app.state.bunny = services.bunny
    app.state.media = services.media
    app.state.openai = services.openai
    app.state.transcriber = services.transcriber
    app.state.analyzer = services.analyzer
    app.state.assemblyai = services.assemblyai
    app.state.pipeline = services.pipeline
    app.state.store = services.store
    app.state.runner = services.runner
    app.state.inventory = services.inventory
    app.state.inventory_context_provider = services.inventory_context_provider
    app.state.course_store = services.course_store
    app.state.academy_generator = services.academy_generator
    app.state.editorial_enricher = services.editorial_enricher
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.confirmation_key = secrets.token_bytes(32)
    app.state.confirmations = ConfirmationStore()
    app.state.course_confirmations = CourseConfirmationStore()
    app.state.session_key = secrets.token_bytes(32)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Dati della richiesta non validi"}, status_code=422)

    @app.middleware("http")
    async def protect_application(request: Request, call_next):
        started = monotonic()
        path = request.url.path
        public = path == "/healthz" or path == "/login" or path.startswith("/static/")
        if not public and not session_is_valid(request.cookies.get(SESSION_COOKIE), app.state.session_key):
            log_event("http", elapsed_seconds=monotonic() - started,
                      status_code=401, error_code="unauthorized",
                      route="unmatched", method=request.method)
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Richiesta non autorizzata"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        # The login form has no authenticated session yet. Some embedded browsers
        # omit Origin and report Sec-Fetch-Site=cross-site, so authenticate it
        # directly with the fixed team credentials. Every post-login mutation
        # remains protected by the synchronizer token below; embedded browsers
        # may also report a nonstandard Origin for otherwise valid forms.
        login_submission = request.method == "POST" and path == "/login"
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not login_submission:
            supplied = request.headers.get("x-csrf-token", "")
            if not supplied:
                # Cache bytes before parsing so the downstream route can read its form.
                await request.body()
                supplied = (await request.form()).get("csrf_token", "")
            if (not isinstance(supplied, str) or not supplied.isascii()
                    or not secrets.compare_digest(supplied, app.state.csrf_token)):
                return JSONResponse({"detail": "Richiesta non autorizzata"}, status_code=403)
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse({"detail": "Impossibile completare la richiesta"}, status_code=500)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        log_event("http", elapsed_seconds=monotonic() - started,
                  status_code=response.status_code,
                  error_code="temporary_failure" if response.status_code >= 500 else
                             "invalid_request" if response.status_code == 422 else "ok",
                  route=route, method=request.method)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.mount("/static", StaticFiles(directory=APP_DIRECTORY / "static"), name="static")
    app.include_router(router)

    @app.get("/healthz")
    def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    return app
