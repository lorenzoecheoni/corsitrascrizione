from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from app.analysis import OpenAIAnalyzer
from app.auth import basic, require_team
from app.bunny import BunnyClient
from app.config import Settings
from app.jobs import JobStore, SingleWorkerRunner
from app.media import FFmpegProcessor
from app.pipeline import AnalysisPipeline
from app.transcription import OpenAITranscriber
from app.web import router


APP_DIRECTORY = Path(__file__).parent


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
    bunny = BunnyClient(app_settings)
    media = FFmpegProcessor()
    openai = OpenAI(api_key=app_settings.openai_api_key, max_retries=0)
    transcriber = OpenAITranscriber(openai)
    analyzer = OpenAIAnalyzer(openai)
    pipeline = AnalysisPipeline(app_settings, bunny, media, transcriber, analyzer)
    store = JobStore()
    runner = SingleWorkerRunner(store, pipeline.run)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            # The active worker owns media cleanup; allow it to finish without
            # blocking the event loop or closing its remote client prematurely.
            runner.shutdown(wait=False)

    app = FastAPI(title="Bunny Video Report", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = app_settings
    app.state.bunny = bunny
    app.state.media = media
    app.state.openai = openai
    app.state.transcriber = transcriber
    app.state.analyzer = analyzer
    app.state.pipeline = pipeline
    app.state.store = store
    app.state.runner = runner
    authenticate = require_team(app_settings.app_password)

    @app.middleware("http")
    async def protect_application(request: Request, call_next):
        if request.url.path != "/healthz":
            try:
                authenticate(await basic(request))
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        response = await call_next(request)
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
