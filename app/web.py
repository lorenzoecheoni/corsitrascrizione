"""Authenticated HTTP interface over the current process's volatile jobs."""

from pathlib import Path
from uuid import UUID
import hmac
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.auth import SESSION_COOKIE, SESSION_TTL_SECONDS, credentials_are_valid, issue_session
from app.bunny import BunnyError, BunnyReadinessError, BunnyUrlError, parse_bunny_url, read_metadata
from app.costs import estimate_cost
from app.jobs import JobRecord, JobState
from app.reporting import format_timestamp, render_markdown, render_text


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(""), password: str = Form("")) -> Response:
    if not credentials_are_valid(username, password, request.app.state.settings.app_password):
        return templates.TemplateResponse(request, "login.html", {"error": "Credenziali non valide"},
                                          status_code=401)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, issue_session(request.app.state.session_key),
                        max_age=SESSION_TTL_SECONDS, httponly=True, samesite="strict",
                        secure=secure_cookie(request), path="/")
    return response


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, httponly=True, samesite="strict",
                           secure=secure_cookie(request), path="/")
    return response


def get_job(request: Request, job_id: str) -> JobRecord:
    try:
        return request.app.state.store.get(UUID(job_id))
    except (KeyError, ValueError):
        raise HTTPException(404, "Lavoro non trovato; potrebbe essere terminata la sessione del server") from None


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "home.html")


@router.post("/preview", response_class=HTMLResponse)
def preview_job(request: Request, source_url: str = Form("")) -> HTMLResponse:
    settings = request.app.state.settings
    try:
        ref = parse_bunny_url(source_url, expected_library_id=settings.bunny_library_id,
                              cdn_hostname=settings.bunny_cdn_hostname)
    except BunnyUrlError:
        raise HTTPException(422, "Il link Bunny non è valido; usa un video della libreria configurata") from None
    try:
        metadata = read_metadata(request.app.state.bunny, str(ref.video_id))
    except BunnyReadinessError as exc:
        raise HTTPException(422, str(exc)) from None
    except BunnyError:
        raise HTTPException(422, "Impossibile leggere il video; verificare accesso e disponibilità su Bunny") from None
    payload = f"{ref.video_id}:{int(time.time()) + 600}"
    signature = hmac.digest(request.app.state.confirmation_key, payload.encode(), "sha256").hex()
    return templates.TemplateResponse(request, "preview.html", {
        "metadata": metadata, "duration": format_timestamp(metadata.duration_seconds),
        "cost": estimate_cost(metadata.duration_seconds, 0), "confirmation": f"{payload}:{signature}",
    })


@router.post("/jobs")
def create_job(request: Request, confirmation: str = Form("")) -> RedirectResponse:
    try:
        video_id, expiry, signature = confirmation.split(":")
        payload = f"{video_id}:{expiry}"
        expected = hmac.digest(request.app.state.confirmation_key, payload.encode(), "sha256").hex()
        if not hmac.compare_digest(signature.encode(), expected.encode()) or int(expiry) < time.time():
            raise ValueError
        video_id = UUID(video_id)
    except (ValueError, TypeError):
        raise HTTPException(422, "Conferma non valida o scaduta; ripetere l'anteprima") from None
    # Only the authenticated preview can authorize this canonical video reference.
    settings = request.app.state.settings
    ref = parse_bunny_url(f"https://iframe.mediadelivery.net/embed/{settings.bunny_library_id}/{video_id}",
                          expected_library_id=settings.bunny_library_id,
                          cdn_hostname=settings.bunny_cdn_hostname)
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
