from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from app.analysis import OpenAIAnalyzer
from app.auth import SESSION_COOKIE, session_is_valid
from app.bunny import BunnyClient
from app.config import Settings
from app.jobs import JobStore, SingleWorkerRunner
from app.logging_config import configure_logging, log_event
from app.media import FFmpegProcessor
from app.pipeline import AnalysisPipeline
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
    pipeline: AnalysisPipeline
    store: JobStore
    runner: SingleWorkerRunner


def build_services(settings: Settings) -> Services:
    """Build the production graph; remote boundaries may be substituted in tests."""
    bunny = BunnyClient(settings)
    media = FFmpegProcessor(runtime_seconds=settings.media_runtime_seconds,
        inactivity_seconds=settings.media_inactivity_seconds,
        max_workspace_bytes=settings.media_max_workspace_bytes)
    openai = OpenAI(api_key=settings.openai_api_key, max_retries=0)
    transcriber = OpenAITranscriber(openai)
    analyzer = OpenAIAnalyzer(openai)
    pipeline = AnalysisPipeline(settings, bunny, media, transcriber, analyzer)
    store = JobStore()
    runner = SingleWorkerRunner(store, pipeline.run)
    return Services(bunny, media, openai, transcriber, analyzer, pipeline, store, runner)


def create_app(settings: Settings | None = None) -> FastAPI:
    try:
        app_settings = settings or Settings()
    except Exception:
        raise RuntimeError("Configurazione applicazione non valida; verificare le variabili richieste") from None
    configure_logging(app_settings)
    services = build_services(app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            # The active worker owns media cleanup; allow it to finish without
            # blocking the event loop or closing its remote client prematurely.
            services.runner.shutdown(wait=False)

    app = FastAPI(title="Bunny Video Report", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = app_settings
    app.state.bunny = services.bunny
    app.state.media = services.media
    app.state.openai = services.openai
    app.state.transcriber = services.transcriber
    app.state.analyzer = services.analyzer
    app.state.pipeline = services.pipeline
    app.state.store = services.store
    app.state.runner = services.runner
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.confirmation_key = secrets.token_bytes(32)
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
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            same_origin = f"{request.url.scheme}://{request.url.netloc}"
            if ((origin is not None and origin != same_origin)
                    or request.headers.get("sec-fetch-site") == "cross-site"):
                return JSONResponse({"detail": "Richiesta non autorizzata"}, status_code=403)
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
