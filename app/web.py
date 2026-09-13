"""Authenticated HTTP interface over persistent report jobs."""

from pathlib import Path
from uuid import UUID
import hmac
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.auth import SESSION_COOKIE, SESSION_TTL_SECONDS, credentials_are_valid, issue_session
from app.bunny import (
    BunnyAuthError,
    BunnyCatalogTooLarge,
    BunnyError,
    BunnyRateLimitError,
    BunnyReadinessError,
    BunnyServerError,
    BunnyTimeoutError,
    BunnyTransportError,
    BunnyUrlError,
    parse_bunny_url,
    read_metadata,
)
from app.costs import estimate_cost
from app.courses import CourseAssemblyError, build_intermediate_report, sign_course_selection
from app.inventory import InventoryError, propose_matches
from app.jobs import JobRecord, JobState
from app.reporting import format_timestamp, render_markdown, render_text
from app.selection import sign_selection


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["format_timestamp"] = format_timestamp


def _transcription_provider(request: Request) -> str:
    return "assemblyai" if request.app.state.assemblyai is not None else "openai"


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
        raise HTTPException(404, "Lavoro non trovato") from None


def _catalog_error_message(error: BunnyError) -> str:
    """Map provider failures to fixed UI text without exposing exception data."""
    if isinstance(error, BunnyCatalogTooLarge):
        return "Il catalogo supera il limite di 10.000 video; occorre introdurre la paginazione prima di proseguire"
    if isinstance(error, BunnyAuthError):
        return "Accesso a Bunny non autorizzato; verifica la configurazione"
    if isinstance(error, BunnyRateLimitError):
        return "Limite di richieste Bunny raggiunto; riprova più tardi"
    if isinstance(error, BunnyServerError):
        return "Servizio Bunny temporaneamente non disponibile; riprova più tardi"
    if isinstance(error, (BunnyTimeoutError, BunnyTransportError)):
        return "Bunny non è disponibile al momento; riprova più tardi"
    if isinstance(error, BunnyUrlError):
        return "Configurazione Bunny non disponibile; verifica le impostazioni"
    return "Impossibile caricare il catalogo Bunny; riprova più tardi"


def _selection_error() -> HTTPException:
    return HTTPException(422, "Selezione video non valida; ripetere la scelta")


def _canonical_video_ids(video_ids: list[str]) -> list[UUID]:
    if not 1 <= len(video_ids) <= 50:
        raise _selection_error()
    try:
        parsed = [UUID(video_id) for video_id in video_ids]
    except (TypeError, ValueError, AttributeError):
        raise _selection_error() from None
    if any(str(video_id) != raw for video_id, raw in zip(parsed, video_ids, strict=True)):
        raise _selection_error()
    if len(set(parsed)) != len(parsed):
        raise _selection_error()
    return parsed


def _read_selected_metadata(request: Request, video_ids: list[UUID]):
    try:
        return [read_metadata(request.app.state.bunny, str(video_id)) for video_id in video_ids]
    except BunnyReadinessError as exc:
        raise HTTPException(422, str(exc)) from None
    except BunnyError:
        raise HTTPException(422, "Impossibile leggere i video selezionati; riprova più tardi") from None


def get_batch(request: Request, batch_id: str):
    try:
        return request.app.state.store.get_batch(UUID(batch_id))
    except (KeyError, ValueError):
        raise HTTPException(404, "Gruppo di lavori non trovato") from None


def get_course(request: Request, course_id: str):
    try:
        return request.app.state.course_store.get_course(course_id)
    except KeyError:
        raise HTTPException(404, "Corso non trovato") from None


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    try:
        catalog = request.app.state.bunny.list_videos()
    except BunnyError as exc:
        return templates.TemplateResponse(request, "home.html", {
            "catalog_error": _catalog_error_message(exc), "videos": [], "total_items": 0,
            "total_duration": 0, "recent_jobs": request.app.state.store.list_uncompleted(),
            "saved_reports": request.app.state.store.list_completed(),
            "fast_mode": request.app.state.assemblyai is not None,
        })
    return templates.TemplateResponse(request, "home.html", {
        "videos": catalog.videos,
        "status_options": list(dict.fromkeys(video.status for video in catalog.videos)),
        "collection_options": list(dict.fromkeys(video.collection_id for video in catalog.videos)),
        "total_items": catalog.total_items,
        "total_duration": sum(video.duration_seconds for video in catalog.videos),
        "recent_jobs": request.app.state.store.list_uncompleted(),
        "saved_reports": request.app.state.store.list_completed(),
        "fast_mode": request.app.state.assemblyai is not None,
    })


@router.post("/inventory/sync")
def sync_inventory(request: Request) -> RedirectResponse:
    try:
        courses = request.app.state.inventory.fetch()
        catalog = request.app.state.bunny.list_videos()
        proposals = propose_matches(
            courses,
            catalog.videos,
            threshold=request.app.state.settings.inventory_match_threshold,
            margin=request.app.state.settings.inventory_match_margin,
        )
        request.app.state.course_store.sync_inventory(courses)
        request.app.state.course_store.replace_proposals(proposals)
    except (InventoryError, BunnyError):
        raise HTTPException(503, "Impossibile sincronizzare l'inventario; riprova più tardi") from None
    return RedirectResponse("/inventory", status_code=303)


@router.post("/courses/{course_id}/videos")
def confirm_course_videos(
    request: Request, course_id: str, video_ids: list[str] = Form(default=[]),
) -> RedirectResponse:
    parsed = _canonical_video_ids(video_ids)
    get_course(request, course_id)
    try:
        request.app.state.course_store.confirm_videos(course_id, parsed)
    except (KeyError, ValueError):
        raise _selection_error() from None
    return RedirectResponse(f"/courses/{course_id}", status_code=303)


@router.get("/courses/{course_id}/analysis-preview", response_class=HTMLResponse)
def course_analysis_preview(request: Request, course_id: str) -> HTMLResponse:
    course = get_course(request, course_id)
    if not course.video_confermati:
        raise HTTPException(409, "Conferma prima i video del corso")
    metadata = _read_selected_metadata(request, course.video_confermati)
    total_duration = sum(video.duration_seconds for video in metadata)
    return templates.TemplateResponse(request, "course_analysis_preview.html", {
        "course": course,
        "videos": metadata,
        "total_duration": total_duration,
        "cost": estimate_cost(
            total_duration, 0, transcription_provider=_transcription_provider(request),
        ),
        "confirmation": sign_course_selection(
            course.id, course.video_confermati, request.app.state.confirmation_key,
        ),
    })


@router.post("/courses/{course_id}/analyze")
def start_course_analysis(
    request: Request, course_id: str, confirmation: str = Form(""),
) -> RedirectResponse:
    def create(token_course_id: str, video_ids: list[UUID]) -> tuple[UUID, list[UUID]]:
        if token_course_id != course_id:
            raise ValueError
        course = get_course(request, course_id)
        if course.video_confermati != video_ids:
            raise ValueError
        metadata = _read_selected_metadata(request, video_ids)
        library_id = request.app.state.settings.bunny_library_id
        batch, jobs = request.app.state.store.create_batch([
            (f"https://iframe.mediadelivery.net/embed/{library_id}/{video_id}", item.title)
            for video_id, item in zip(video_ids, metadata, strict=True)
        ])
        request.app.state.course_store.attach_run(
            course_id, batch.id, [job.id for job in jobs]
        )
        return batch.id, [job.id for job in jobs]

    try:
        token_course_id, batch_id, job_ids = request.app.state.course_confirmations.create_once(
            confirmation, request.app.state.confirmation_key, create,
        )
        if token_course_id != course_id:
            raise ValueError
    except (ValueError, KeyError):
        raise HTTPException(422, "Conferma corso non valida o scaduta; ripetere l'anteprima") from None
    for job_id in job_ids:
        try:
            request.app.state.runner.submit(job_id)
        except Exception:
            request.app.state.runner.cancel(job_id)
    return RedirectResponse(f"/courses/{course_id}", status_code=303)


def _refresh_course_report(request: Request, course):
    if course.intermediate is not None or not course.job_ids:
        return course, None
    jobs = [request.app.state.store.get(job_id) for job_id in course.job_ids]
    if all(job.state == JobState.COMPLETED and job.report is not None for job in jobs):
        try:
            report = build_intermediate_report(course, jobs)
            request.app.state.course_store.save_intermediate(course.id, report)
            return request.app.state.course_store.get_course(course.id), None
        except CourseAssemblyError:
            return course, "I video devono essere rianalizzati per creare il report Academy"
    return course, None


@router.get("/api/courses/{course_id}")
def course_status(request: Request, course_id: str) -> dict:
    course, assembly_error = _refresh_course_report(request, get_course(request, course_id))
    jobs = [request.app.state.store.get(job_id) for job_id in course.job_ids]
    if assembly_error:
        state = "da_rianalizzare"
    elif any(job.state == JobState.FAILED for job in jobs):
        state = "fallito"
    elif any(job.state == JobState.CANCELLED for job in jobs):
        state = "annullato"
    else:
        state = course.stato
    return {
        "id": course.id,
        "stato": state,
        "messaggio": assembly_error,
        "intermedio_disponibile": course.intermediate is not None,
        "academy_disponibile": course.academy_json is not None,
        "jobs": [
            {
                "id": str(job.id),
                "state": job.state.value,
                "progress": job.progress,
                "message": job.message,
            }
            for job in jobs
        ],
    }


@router.post("/selections/preview", response_class=HTMLResponse)
def selection_preview(request: Request, video_ids: list[str] = Form(default=[])) -> HTMLResponse:
    selected_ids = _canonical_video_ids(video_ids)
    metadata = _read_selected_metadata(request, selected_ids)
    total_duration = sum(video.duration_seconds for video in metadata)
    return templates.TemplateResponse(request, "selection_preview.html", {
        "videos": metadata,
        "total_duration": total_duration,
        "cost": estimate_cost(
            total_duration, 0,
            transcription_provider=_transcription_provider(request),
        ),
        "confirmation": sign_selection(selected_ids, request.app.state.confirmation_key),
    })


@router.post("/batches")
def create_batch(request: Request, confirmation: str = Form("")) -> RedirectResponse:
    def create(video_ids: list[UUID]) -> tuple[UUID, list[UUID]]:
        metadata = _read_selected_metadata(request, video_ids)
        library_id = request.app.state.settings.bunny_library_id
        items = [
            (f"https://iframe.mediadelivery.net/embed/{library_id}/{video_id}", video.title)
            for video_id, video in zip(video_ids, metadata, strict=True)
        ]
        batch, jobs = request.app.state.store.create_batch(items)
        return batch.id, [job.id for job in jobs]

    try:
        batch_id, job_ids = request.app.state.confirmations.create_once(
            confirmation, request.app.state.confirmation_key, create,
        )
    except ValueError:
        raise HTTPException(422, "Conferma non valida o scaduta; ripetere la selezione") from None

    for job_id in job_ids:
        try:
            request.app.state.runner.submit(job_id)
        except Exception:
            # submit() raises before returning a future. Cancel this newly-created,
            # unscheduled job, then leave the rest of the batch independent.
            request.app.state.runner.cancel(job_id)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_page(request: Request, batch_id: str) -> HTMLResponse:
    batch = get_batch(request, batch_id)
    jobs = [request.app.state.store.get(job_id) for job_id in batch.job_ids]
    return templates.TemplateResponse(request, "batch.html", {"batch": batch, "jobs": jobs})


@router.get("/api/batches/{batch_id}")
def batch_status(request: Request, batch_id: str) -> dict:
    batch = get_batch(request, batch_id)
    jobs = [request.app.state.store.get(job_id) for job_id in batch.job_ids]
    return {
        "id": str(batch.id),
        "jobs": [job.model_dump(mode="json", exclude={"report"}) for job in jobs],
    }


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
        "cost": estimate_cost(
            metadata.duration_seconds, 0,
            transcription_provider=_transcription_provider(request),
        ),
        "confirmation": f"{payload}:{signature}",
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


@router.post("/jobs/{job_id}/delete")
def delete_report(request: Request, job_id: str) -> RedirectResponse:
    try:
        parsed_id = UUID(job_id)
    except ValueError:
        raise HTTPException(404, "Report non trovato") from None
    try:
        request.app.state.store.delete_completed(parsed_id)
    except KeyError:
        raise HTTPException(404, "Report non trovato") from None
    except ValueError:
        raise HTTPException(409, "Solo un report completato può essere eliminato") from None
    return RedirectResponse("/", status_code=303)


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
