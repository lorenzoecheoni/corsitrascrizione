from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from app.analysis import OpenAIAnalyzer
from app.auth import basic, require_team
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
    media = FFmpegProcessor()
    openai = OpenAI(api_key=settings.openai_api_key, max_retries=0)
    transcriber = OpenAITranscriber(openai)
    analyzer = OpenAIAnalyzer(openai)
    pipeline = AnalysisPipeline(settings, bunny, media, transcriber, analyzer)
    store = JobStore()
    runner = SingleWorkerRunner(store, pipeline.run)
    return Services(bunny, media, openai, transcriber, analyzer, pipeline, store, runner)


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
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
    authenticate = require_team(app_settings.app_password)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "Dati della richiesta non validi"}, status_code=422)

    @app.middleware("http")
    async def protect_application(request: Request, call_next):
        started = monotonic()
        if request.url.path != "/healthz":
            try:
                authenticate(await basic(request))
            except HTTPException as exc:
                log_event("http", elapsed_seconds=monotonic() - started,
                          status_code=exc.status_code, error_code="unauthorized",
                          route="unmatched", method=request.method)
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
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
