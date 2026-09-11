"""Authenticated HTTP interface over the current process's volatile jobs."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.bunny import BunnyUrlError, parse_bunny_url
from app.jobs import JobRecord, JobState
from app.reporting import render_markdown, render_text


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_job(request: Request, job_id: str) -> JobRecord:
    try:
        return request.app.state.store.get(UUID(job_id))
    except (KeyError, ValueError):
        raise HTTPException(404, "Lavoro non trovato; potrebbe essere terminata la sessione del server") from None


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "home.html")


@router.post("/jobs")
def create_job(request: Request, source_url: str = Form("")) -> RedirectResponse:
    settings = request.app.state.settings
    try:
        ref = parse_bunny_url(source_url, expected_library_id=settings.bunny_library_id,
                              cdn_hostname=settings.bunny_cdn_hostname)
    except BunnyUrlError:
        raise HTTPException(422, "Il link Bunny non è valido; usa un video della libreria configurata") from None
    # Keep only the video reference; discard user-supplied tokens immediately.
    canonical_url = f"https://iframe.mediadelivery.net/embed/{ref.library_id}/{ref.video_id}"
    job = request.app.state.store.create(canonical_url)
    request.app.state.runner.submit(job.id)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(request: Request, job_id: str) -> HTMLResponse:
    job = get_job(request, job_id)
    report_text = render_text(job.report) if job.state == JobState.COMPLETED and job.report else ""
    return templates.TemplateResponse(request, "job.html", {"job": job, "report_text": report_text})


@router.get("/api/jobs/{job_id}")
def job_status(request: Request, job_id: str) -> dict:
    job = get_job(request, job_id)
    data = job.model_dump(mode="json")
    data["report_text"] = render_text(job.report) if job.state == JobState.COMPLETED and job.report else None
    return data


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str) -> RedirectResponse:
    job = get_job(request, job_id)
    request.app.state.runner.cancel(job.id)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


def export_report(request: Request, job_id: str, extension: str) -> Response:
    job = get_job(request, job_id)
    if job.state != JobState.COMPLETED or job.report is None:
        raise HTTPException(409, "Il report non è ancora disponibile")
    renderer, media_type = ((render_markdown, "text/markdown") if extension == "md"
                            else (render_text, "text/plain"))
    return Response(renderer(job.report), media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="report-{job.id}.{extension}"'})


@router.get("/jobs/{job_id}/report.md")
def markdown_report(request: Request, job_id: str) -> Response:
    return export_report(request, job_id, "md")


@router.get("/jobs/{job_id}/report.txt")
def text_report(request: Request, job_id: str) -> Response:
    return export_report(request, job_id, "txt")
