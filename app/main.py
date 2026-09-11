from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.auth import require_team
from app.config import Settings


APP_DIRECTORY = Path(__file__).parent
templates = Jinja2Templates(directory=str(APP_DIRECTORY / "templates"))


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()
    app = FastAPI(title="Bunny Video Report")
    app.mount("/static", StaticFiles(directory=APP_DIRECTORY / "static"), name="static")

    @app.get("/healthz")
    def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        _: str = Depends(require_team(app_settings.app_password)),
    ) -> HTMLResponse:
        return templates.TemplateResponse(request, "home.html")

    return app
