"""Course-level assembly and signed analysis confirmations."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import time
from threading import Lock
from uuid import UUID

from app.course_models import (
    IntermediateCourse,
    IntermediateCourseReport,
    IntermediateSlide,
    IntermediateSpeaker,
    IntermediateVideo,
    InventoryReference,
    VerificationRequest,
)
from app.jobs import JobRecord, JobState
from app.models import GENERIC_INTERVENTION_SPEAKER, GENERIC_SPEAKER_LABEL, Intervention


_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_CONFIDENCE = {"alta": .95, "media": .65, "bassa": .35}
_CONFIRMATION_TTL = 600
_MAX_TOKEN_BYTES = 4096


class CourseAssemblyError(ValueError):
    pass


def _name_origins(speaker) -> list[str]:
    origins = []
    for evidence in speaker.evidence:
        origin = "slide" if evidence.kind in {"slide", "sottopancia"} else (
            "metadata" if evidence.kind == "metadata" else "audio"
        )
        if origin not in origins:
            origins.append(origin)
    return origins


def _video_guid(job: JobRecord) -> UUID:
    match = _GUID.search(job.source_url)
    if match is None:
        raise CourseAssemblyError("Il lavoro non contiene un riferimento Bunny valido")
    return UUID(match.group())


def build_intermediate_report(course, ordered_jobs: list[JobRecord]) -> IntermediateCourseReport:
    if not ordered_jobs:
        raise CourseAssemblyError("Il corso richiede almeno un video completato")
    if any(job.state != JobState.COMPLETED or job.report is None for job in ordered_jobs):
        raise CourseAssemblyError("Tutti i video del corso devono essere completati")
    if any(not job.report.interventions for job in ordered_jobs):
        raise CourseAssemblyError("Un report precedente deve essere rianalizzato per ottenere gli interventi")

    speakers: dict[str, IntermediateSpeaker] = {}
    for expected in getattr(course, "relatori_attesi", []):
        name = expected.strip()
        if name and not GENERIC_INTERVENTION_SPEAKER.fullmatch(name):
            speakers[name] = IntermediateSpeaker(
                nome=name, confidenza=.5, origine_nome=["inventario"]
            )
    for job in ordered_jobs:
        for speaker in job.report.speakers:
            name = speaker.display_name.strip()
            origins = _name_origins(speaker)
            if GENERIC_SPEAKER_LABEL.fullmatch(name) or not origins:
                continue
            current = speakers.get(name)
            combined_origins = list(dict.fromkeys([
                *(current.origine_nome if current else []), *origins,
            ]))
            speakers[name] = IntermediateSpeaker(
                nome=name,
                slug=current.slug if current else None,
                ruolo=speaker.role or (current.ruolo if current else None),
                organizzazione=current.organizzazione if current else None,
                confidenza=max(_CONFIDENCE[speaker.confidence], current.confidenza if current else 0),
                origine_nome=combined_origins,
            )

    declared_names = set(speakers)
    videos: list[IntermediateVideo] = []
    verifications: list[VerificationRequest] = []
    synopsis_parts: list[str] = []
    for order, job in enumerate(ordered_jobs, start=1):
        report = job.report
        key = f"v{order}"
        duration = int(report.duration_seconds + .5)
        interventions: list[Intervention] = []
        for position, source in enumerate(report.interventions, start=1):
            names = [name for name in source.relatori if name in declared_names]
            intervention_id = f"{key}-i{position:03d}"
            interventions.append(source.model_copy(update={
                "id": intervention_id,
                "relatori": names,
            }))
            if source.tipo not in {"pausa", "logistica"} and not names:
                verifications.append(VerificationRequest(
                    livello="critico",
                    codice="RELATORE_NON_IDENTIFICATO",
                    video=key,
                    intervento=intervention_id,
                    campo="relatori",
                    messaggio="Identificare il relatore di questo intervento prima della conferma.",
                ))
        slides = [IntermediateSlide(
            start_seconds=int(slide.timestamp_seconds + .5),
            titolo=slide.title,
            testo_principale=" · ".join(slide.visible_content)[:500],
            confidenza=_CONFIDENCE[slide.confidence],
        ) for slide in report.slides]
        videos.append(IntermediateVideo(
            chiave=key,
            guid=str(_video_guid(job)),
            titolo_bunny=report.bunny_title or job.source_title or report.title,
            durata_secondi=duration,
            ordine=order,
            interventi=interventions,
            slide=slides,
        ))
        synopsis = report.synopsis.strip()
        if synopsis:
            synopsis_parts.append(f"{report.title}: {synopsis}")

    return IntermediateCourseReport(
        stato="da_verificare",
        corso=IntermediateCourse(
            titolo=course.titolo,
            sinossi_corso="\n\n".join(synopsis_parts),
            inventario=InventoryReference(foglio=course.foglio, ordine=course.riga),
        ),
        relatori=list(speakers.values()),
        video=videos,
        verifiche_richieste=verifications,
    )


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("Conferma corso non valida")
    try:
        encoded = value.encode("ascii")
        decoded = base64.b64decode(encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error):
        raise ValueError("Conferma corso non valida") from None
    if _encode(decoded) != value:
        raise ValueError("Conferma corso non valida")
    return decoded


def sign_course_selection(
    course_id: str, video_ids: list[UUID], key: bytes, *, now: int | None = None
) -> str:
    if not course_id or not 1 <= len(video_ids) <= 50 or len(video_ids) != len(set(video_ids)):
        raise ValueError("Conferma corso non valida")
    issued = int(time.time()) if now is None else now
    if type(issued) is not int:
        raise ValueError("Conferma corso non valida")
    payload = f"{issued + _CONFIRMATION_TTL}|{course_id}|{','.join(map(str, video_ids))}"
    encoded = _encode(payload.encode("utf-8"))
    signature = _encode(hmac.digest(key, encoded.encode("ascii"), "sha256"))
    return f"{encoded}.{signature}"


def verify_course_selection(
    token: str, key: bytes, *, now: int | None = None
) -> tuple[str, list[UUID], int]:
    try:
        if not isinstance(token, str) or len(token.encode()) > _MAX_TOKEN_BYTES:
            raise ValueError
        encoded, signature = token.split(".")
        if not hmac.compare_digest(
            _decode(signature), hmac.digest(key, encoded.encode("ascii"), "sha256")
        ):
            raise ValueError
        expiry_text, course_id, ids_text = _decode(encoded).decode("utf-8").split("|", 2)
        current = int(time.time()) if now is None else now
        if not expiry_text.isdecimal() or int(expiry_text) <= current or not course_id:
            raise ValueError
        raw_ids = ids_text.split(",")
        video_ids = [UUID(value) for value in raw_ids]
        if not 1 <= len(video_ids) <= 50 or len(video_ids) != len(set(video_ids)):
            raise ValueError
        if any(str(video_id) != raw for video_id, raw in zip(video_ids, raw_ids, strict=True)):
            raise ValueError
        return course_id, video_ids, int(expiry_text)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise ValueError("Conferma corso non valida") from None


class CourseConfirmationStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._used: dict[bytes, tuple[int, UUID]] = {}

    def create_once(self, token: str, key: bytes, create):
        with self._lock:
            now = int(time.time())
            self._used = {digest: value for digest, value in self._used.items() if value[0] > now}
            course_id, video_ids, expiry = verify_course_selection(token, key, now=now)
            digest = hashlib.sha256(token.encode("ascii")).digest()
            if digest in self._used:
                return course_id, self._used[digest][1], []
            batch_id, job_ids = create(course_id, video_ids)
            self._used[digest] = (expiry, batch_id)
            return course_id, batch_id, job_ids
