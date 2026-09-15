"""Authenticated HTTP interface over persistent report jobs."""

from pathlib import Path
from uuid import UUID
import hmac
import time

from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.academy import AcademyGenerationError
from app.academy_prompt import CONTRACT_VERSION, PROMPT_VERSION
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
from app.courses import CourseAssemblyError, build_intermediate_report as build_course_intermediate_report, sign_course_selection
from app.course_models import AcademyImport, IntermediateCourseReport, format_hms
from app.intermediate_report import build_intermediate_report
from app.inventory import (
    InventoryError,
    organize_catalog,
    propose_matches,
)
from app.jobs import JobRecord, JobState
from app.reporting import format_timestamp, render_markdown, render_text
from app.selection import sign_selection
from pydantic import ValidationError


router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["format_timestamp"] = format_timestamp
templates.env.globals["format_hms"] = format_hms


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


def _requires_granular_reanalysis(report) -> bool:
    if report.analysis_profile != 2:
        return True
    try:
        build_intermediate_report(report, UUID(int=0))
    except ValueError:
        return True
    return False


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    saved_reports = request.app.state.store.list_completed()
    boundary_reanalysis_job_ids = {
        job.id for job in saved_reports
        if job.report is not None and _requires_granular_reanalysis(job.report)
    }
    try:
        catalog = request.app.state.bunny.list_videos()
    except BunnyError as exc:
        return templates.TemplateResponse(request, "home.html", {
            "catalog_error": _catalog_error_message(exc), "videos": [], "total_items": 0,
            "total_duration": 0, "recent_jobs": request.app.state.store.list_uncompleted(),
            "saved_reports": saved_reports,
            "boundary_reanalysis_job_ids": boundary_reanalysis_job_ids,
            "fast_mode": request.app.state.assemblyai is not None,
        })
    inventory_error = None
    try:
        courses = request.app.state.inventory.fetch()
    except Exception:
        # The sheet controls presentation order only: any connector or parsing
        # failure must leave the core per-video Bunny workflow available.
        courses = []
        inventory_error = "Ordine del foglio temporaneamente non disponibile; mostro comunque tutti i video Bunny."
    groups = organize_catalog(
        courses,
        catalog.videos,
        threshold=request.app.state.settings.inventory_match_threshold,
        margin=request.app.state.settings.inventory_match_margin,
    )
    active_group = next((group.key for group in groups if group.items), groups[0].key)
    return templates.TemplateResponse(request, "home.html", {
        "videos": catalog.videos,
        "catalog_groups": groups,
        "active_catalog_group": active_group,
        "catalog_inventory_error": inventory_error,
        "status_options": list(dict.fromkeys(video.status for video in catalog.videos)),
        "collection_options": list(dict.fromkeys(video.collection_id for video in catalog.videos)),
        "total_items": catalog.total_items,
        "total_duration": sum(video.duration_seconds for video in catalog.videos),
        "recent_jobs": request.app.state.store.list_uncompleted(),
        "saved_reports": saved_reports,
        "boundary_reanalysis_job_ids": boundary_reanalysis_job_ids,
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


@router.get("/inventory", response_class=HTMLResponse)
def inventory_page(request: Request) -> RedirectResponse:
    """Keep old bookmarks safe while returning users to the per-video workflow."""
    return RedirectResponse("/", status_code=303)


@router.post("/courses/{course_id}/match")
def confirm_proposed_match(
    request: Request, course_id: str, video_id: str = Form(""),
) -> RedirectResponse:
    course = get_course(request, course_id)
    try:
        parsed = UUID(video_id)
    except ValueError:
        raise _selection_error() from None
    if parsed not in {proposal.video_id for proposal in course.proposte}:
        raise _selection_error()
    request.app.state.course_store.confirm_videos(course_id, [parsed])
    return RedirectResponse(f"/courses/{course_id}", status_code=303)


@router.post("/courses/{course_id}/write-link")
def write_confirmed_match(request: Request, course_id: str) -> RedirectResponse:
    course = get_course(request, course_id)
    if len(course.video_confermati) != 1:
        raise HTTPException(409, "Il link può essere scritto solo per un singolo video confermato")
    video_id = course.video_confermati[0]
    url = (
        f"https://iframe.mediadelivery.net/embed/"
        f"{request.app.state.settings.bunny_library_id}/{video_id}"
    )
    try:
        request.app.state.inventory.update_link(course, url)
    except InventoryError:
        raise HTTPException(503, "Scrittura Google non disponibile; verifica il service account") from None
    return RedirectResponse(f"/courses/{course_id}", status_code=303)


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
            report = build_course_intermediate_report(course, jobs)
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


@router.get("/courses/{course_id}", response_class=HTMLResponse)
def course_page(request: Request, course_id: str) -> HTMLResponse:
    course, assembly_error = _refresh_course_report(request, get_course(request, course_id))
    try:
        catalog = request.app.state.bunny.list_videos()
        videos = catalog.videos
        catalog_error = None
    except BunnyError as exc:
        videos = []
        catalog_error = _catalog_error_message(exc)
    video_by_id = {str(video.video_id): video for video in videos}
    jobs = [request.app.state.store.get(job_id) for job_id in course.job_ids]
    return templates.TemplateResponse(request, "course.html", {
        "course": course,
        "videos": videos,
        "video_by_id": video_by_id,
        "confirmed_ids": {str(item) for item in course.video_confermati},
        "jobs": jobs,
        "assembly_error": assembly_error,
        "catalog_error": catalog_error,
    })


def _same_report_sources(
    current: IntermediateCourseReport, candidate: IntermediateCourseReport
) -> bool:
    if current.versione != candidate.versione or current.corso.titolo != candidate.corso.titolo:
        return False
    current_videos = [
        (item.chiave, item.guid, item.durata_secondi, item.ordine, item.titolo_bunny)
        for item in current.video
    ]
    candidate_videos = [
        (item.chiave, item.guid, item.durata_secondi, item.ordine, item.titolo_bunny)
        for item in candidate.video
    ]
    return current_videos == candidate_videos


def _with_required_speaker_checks(report: IntermediateCourseReport) -> IntermediateCourseReport:
    data = report.model_dump(mode="json", by_alias=True, exclude_none=True)
    checks = [
        item for item in data["verifiche_richieste"]
        if item.get("codice") != "RELATORE_NON_IDENTIFICATO"
    ]
    for video in report.video:
        for intervention in video.interventi:
            if intervention.tipo in {"pausa", "logistica"} or intervention.relatori:
                continue
            checks.append({
                "livello": "critico",
                "codice": "RELATORE_NON_IDENTIFICATO",
                "video": video.chiave,
                "intervento": intervention.id,
                "campo": "relatori",
                "messaggio": "Identificare il relatore di questo intervento prima della conferma.",
            })
    data["verifiche_richieste"] = checks
    data["stato"] = "da_verificare"
    return IntermediateCourseReport.model_validate(data)


@router.get("/courses/{course_id}/review", response_class=HTMLResponse)
def course_review_page(request: Request, course_id: str) -> HTMLResponse:
    course, assembly_error = _refresh_course_report(request, get_course(request, course_id))
    if assembly_error:
        raise HTTPException(409, assembly_error)
    if course.intermediate is None:
        raise HTTPException(409, "Il report intermedio non è ancora disponibile")
    report_data = course.intermediate.model_dump(mode="json", by_alias=True, exclude_none=True)
    return templates.TemplateResponse(request, "course_review.html", {
        "course": course,
        "report": course.intermediate,
        "report_data": report_data,
        "library_id": request.app.state.settings.bunny_library_id,
    })


@router.put("/api/courses/{course_id}/report")
def save_course_review(
    request: Request, course_id: str, payload: dict = Body(...),
) -> dict:
    course = get_course(request, course_id)
    if course.intermediate is None:
        raise HTTPException(409, "Il report intermedio non è disponibile")
    try:
        candidate = IntermediateCourseReport.model_validate(payload)
        if not _same_report_sources(course.intermediate, candidate):
            raise ValueError
        candidate = _with_required_speaker_checks(candidate)
        request.app.state.course_store.save_intermediate(course_id, candidate)
    except (ValidationError, ValueError):
        raise HTTPException(422, "Il report non rispetta timeline e contratto") from None
    return {"ok": True, "stato": candidate.stato,
            "verifiche_richieste": len(candidate.verifiche_richieste)}


@router.post("/courses/{course_id}/confirm")
def confirm_course_report(request: Request, course_id: str) -> RedirectResponse:
    course = get_course(request, course_id)
    if course.intermediate is None:
        raise HTTPException(409, "Il report intermedio non è disponibile")
    checked = _with_required_speaker_checks(course.intermediate)
    if any(item.livello == "critico" for item in checked.verifiche_richieste):
        raise HTTPException(409, "Risolvi le verifiche critiche prima della conferma")
    data = checked.model_dump(mode="json", by_alias=True, exclude_none=True)
    data["stato"] = "verificato"
    try:
        verified = IntermediateCourseReport.model_validate(data)
        request.app.state.course_store.confirm_intermediate(course_id, verified)
    except (ValidationError, ValueError):
        raise HTTPException(422, "Il report non può essere confermato") from None
    return RedirectResponse(f"/courses/{course_id}/review", status_code=303)


@router.post("/courses/{course_id}/generate-academy")
def generate_academy_import(request: Request, course_id: str) -> RedirectResponse:
    course = get_course(request, course_id)
    if course.intermediate is None:
        raise HTTPException(409, "Il report intermedio non è disponibile")
    if course.intermediate.stato not in {"verificato", "confermato"}:
        raise HTTPException(409, "Conferma il report prima di generare il JSON Academy")
    if any(item.livello == "critico" for item in course.intermediate.verifiche_richieste):
        raise HTTPException(409, "Risolvi le verifiche critiche prima della generazione")
    try:
        result = request.app.state.academy_generator.generate(course.intermediate)
        data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        request.app.state.course_store.save_academy(
            course_id, data, CONTRACT_VERSION, PROMPT_VERSION,
        )
    except AcademyGenerationError as exc:
        raise HTTPException(503, str(exc)) from None
    except (ValidationError, ValueError):
        raise HTTPException(422, "Il JSON Academy generato non rispetta il contratto") from None
    return RedirectResponse(f"/courses/{course_id}/review", status_code=303)


@router.get("/courses/{course_id}/report-intermedio.json")
def download_intermediate(request: Request, course_id: str) -> Response:
    course = get_course(request, course_id)
    if course.intermediate is None:
        raise HTTPException(409, "Il report intermedio non è disponibile")
    body = course.intermediate.model_dump_json(
        indent=2, by_alias=True, exclude_none=True
    )
    return Response(
        body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="report-intermedio-{course_id.replace(":", "-")}.json"'
        },
    )


@router.get("/courses/{course_id}/import-academy.json")
def download_academy_import(request: Request, course_id: str) -> Response:
    course = get_course(request, course_id)
    if course.academy_json is None:
        raise HTTPException(409, "Il JSON Academy non è ancora disponibile")
    try:
        report = AcademyImport.model_validate(course.academy_json)
    except ValidationError:
        raise HTTPException(409, "Il JSON Academy salvato non è valido") from None
    return Response(
        report.model_dump_json(indent=2, by_alias=True, exclude_none=True),
        media_type="application/json",
        headers={
            "Content-Disposition":
                f'attachment; filename="import-academy-{course_id.replace(":", "-")}.json"'
        },
    )


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
    report_text = ""
    boundary_reanalysis_required = False
    if job.state == JobState.COMPLETED and job.report is not None:
        boundary_reanalysis_required = job.report.analysis_profile != 2
        try:
            report_text = render_text(job.report)
        except ValueError:
            report_text = "Report granulare non valido: rianalisi necessaria"
            boundary_reanalysis_required = True
    return templates.TemplateResponse(request, "job.html", {
        "job": job,
        "report_text": report_text,
        "boundary_reanalysis_required": boundary_reanalysis_required,
    })


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
    try:
        body = renderer(job.report)
    except ValueError:
        raise HTTPException(409, "Report granulare non valido: rianalisi necessaria") from None
    return Response(body, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="report-{job.id}.{extension}"'})


@router.get("/jobs/{job_id}/report.md")
def markdown_report(request: Request, job_id: str) -> Response:
    return export_report(request, job_id, "md")


@router.get("/jobs/{job_id}/report.txt")
def text_report(request: Request, job_id: str) -> Response:
    return export_report(request, job_id, "txt")


@router.get("/jobs/{job_id}/report.json")
def json_report(request: Request, job_id: str) -> Response:
    job = get_job(request, job_id)
    if job.state != JobState.COMPLETED or job.report is None:
        raise HTTPException(409, "Il report non è ancora disponibile")
    if job.report.analysis_profile != 2:
        raise HTTPException(
            409, "Rianalisi necessaria per il formato granulare",
        )
    settings = request.app.state.settings
    try:
        reference = parse_bunny_url(
            job.source_url,
            expected_library_id=settings.bunny_library_id,
            cdn_hostname=settings.bunny_cdn_hostname,
        )
    except BunnyUrlError:
        raise HTTPException(409, "Il report non contiene un riferimento Bunny valido") from None
    try:
        report = build_intermediate_report(job.report, reference.video_id)
    except ValueError:
        raise HTTPException(409, "Report granulare non valido: rianalisi necessaria") from None
    return Response(
        report.model_dump_json(indent=2, by_alias=True, exclude_none=True),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="report-intermedio-{job.id}.json"'},
    )
