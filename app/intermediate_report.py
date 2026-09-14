"""Build the one-video Academy intermediate report v1.1 envelope."""

import ipaddress
import re
import time
import unicodedata
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urljoin, urlparse
from uuid import UUID

import httpx

from app.intermediate_models import (
    IntermediateInterventionV11,
    IntermediateMaterialV11,
    IntermediateReportV11,
    IntermediateSpeakerV11,
    IntermediateVideoV11,
    VerificationRequestV11,
)
from app.models import AcademyReport, GENERIC_SPEAKER_LABEL
from app.reporting import correct_speaker_name_mentions, reconcile_speakers


_REGISTERED_SPEAKERS = {
    "vincenzo manfredi": "vincenzo-manfredi",
    "gaetano de vito": "gaetano-de-vito",
    "antonio sibilia": "antonio-sibilia",
    "luigi morra": "luigi-morra",
}
_ASSOHOLDING_ROLE = re.compile(
    r"^(?P<role>.*?)\s+di\s+(?:ass holding|asso holding|assoholding)\s*$", re.IGNORECASE
)
_CONFIDENCE_SCORE = {"alta": .95, "media": .65, "bassa": .35}
_SHORT_SEGMENT_KEYWORDS = {
    "saluti": ("grazie", "ringrazi", "saluto", "saluti", "arrivederci"),
    "cambio_relatore": (
        "passaggio", "passa la parola", "cedo la parola", "presentazione",
        "presenta", "introduce", "introduzione",
    ),
    "domande": ("domanda", "domande", "risposta", "risposte", "chiarimento", "quesito"),
}
_PROVIDER_SUMMARY_PREFIX = re.compile(
    r"^\s*(?:(?:speaker|relatore|provider|voce)\s*[-_:]?\s*[a-z0-9]+"
    r"(?:\s*[:\-–]\s*|\s+)|[a-z]\s*(?::|\.|-|–)\s*|"
    r"[b-df-hj-km-np-tv-z]\s+)"
    r"(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_INTRODUCTORY_VERB = re.compile(
    r"^(?:approfondisce|tratta|descrive|presenta|illustra|discute|spiega|parla di)\s+",
    re.IGNORECASE,
)
_HTTP_URL = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def _name_key(value: str) -> str:
    folded = "".join(
        character for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z]+", folded))


def registered_slug(name: str) -> str | None:
    """Return the fixed Academy registry slug for a known speaker."""
    return _REGISTERED_SPEAKERS.get(_name_key(name))


def split_role_organization(role: str | None) -> tuple[str | None, str | None]:
    """Split only explicit ``di Ass Holding`` organization suffixes."""
    if role is None:
        return None, None
    match = _ASSOHOLDING_ROLE.fullmatch(role)
    if match is None:
        return role, None
    return match.group("role").strip() or None, "Assoholding"


def _speaker_warning(name: str, role: str | None) -> dict[str, str]:
    if role is None:
        message = (
            f"Il relatore {name} non è nel Registro: completare la qualifica "
            "mancante prima della creazione nel Registro."
        )
    else:
        message = f"Il relatore {name} non è nel Registro Academy."
    return {
        "livello": "avviso",
        "codice": "RELATORE_NON_NEL_REGISTRO",
        "video": "v1",
        "campo": f"relatori:{_name_key(name)}:{'ruolo' if role is None else 'nome'}",
        "messaggio": message,
    }


def _normalized_text(value: str) -> str:
    return " ".join(_name_key(value).split())


def _short_segment_kind(*, title: str, summary: str, speakers: Sequence[str]) -> str:
    text = _normalized_text(f"{title} {summary}")
    for kind in ("saluti", "cambio_relatore", "domande"):
        if any(keyword in text for keyword in _SHORT_SEGMENT_KEYWORDS[kind]):
            return kind
    return "domande" if speakers else "pausa"


def _normalize_summary(summary: str) -> str:
    match = _PROVIDER_SUMMARY_PREFIX.match(summary)
    if match is None:
        return summary
    content = _INTRODUCTORY_VERB.sub("", match.group("body")).strip()
    return f"Tema trattato: {content}" if content else summary


def normalize_interventions(
    report: AcademyReport, video_key: str = "v1",
) -> list[IntermediateInterventionV11]:
    """Copy and deterministically normalize persisted analytical interventions."""
    normalized = []
    for index, intervention in enumerate(
        sorted(report.interventions, key=lambda item: item.start_seconds), start=1
    ):
        duration = intervention.end_seconds - intervention.start_seconds
        kind = intervention.tipo
        if kind == "intervento" and duration < 20:
            kind = _short_segment_kind(
                title=intervention.titolo,
                summary=intervention.sintesi,
                speakers=intervention.relatori,
            )
        normalized.append(IntermediateInterventionV11(
            id=f"{video_key}-i{index:03d}",
            inizio=intervention.start_seconds,
            fine=intervention.end_seconds,
            tipo=kind,
            relatori=list(intervention.relatori),
            titolo=intervention.titolo,
            sintesi=_normalize_summary(intervention.sintesi),
            punti_chiave=list(intervention.punti_chiave) if kind == "intervento" else [],
            accesso="iscritti",
            confidenza=intervention.confidenza,
        ))
    return normalized


def choose_public_intervention(
    interventions: Sequence[IntermediateInterventionV11],
) -> str | None:
    """Return the id selected by the fixed Academy preview ranking."""
    duration = lambda item: item.end_seconds - item.start_seconds
    eligible = [item for item in interventions if item.tipo == "intervento"]
    preview = next((item for item in eligible if 480 <= duration(item) < 900), None)
    preview = preview or next((item for item in eligible if duration(item) >= 480), None)
    preview = preview or max(eligible, key=duration, default=None)
    return preview.id if preview else None


def _verification(
    level: str,
    code: str,
    *,
    video: str,
    message: str,
    intervention: str | None = None,
    field: str | None = None,
) -> VerificationRequestV11:
    return VerificationRequestV11.model_validate({
        "livello": level,
        "codice": code,
        "video": video,
        "intervento": intervention,
        "campo": field,
        "messaggio": message,
    })


def _deduplicate_verifications(
    items: Sequence[VerificationRequestV11],
) -> list[VerificationRequestV11]:
    unique = []
    seen = set()
    for item in items:
        key = (item.codice, item.video, item.intervento, item.campo)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def parse_material_source(value: str) -> IntermediateMaterialV11 | None:
    """Conservatively convert one declared inventory material into a source."""
    source = value.strip()
    if not source:
        return None
    match = _HTTP_URL.search(source)
    if match is not None:
        url = match.group().rstrip(").,|")
        title = re.sub(r"[\s|\-]+$", "", source[:match.start()])
        if not title:
            title = Path(urlparse(url).path).name or url
        return IntermediateMaterialV11(titolo=title, url=url)
    path = Path(source)
    return IntermediateMaterialV11(titolo=path.stem, file=path.name)


def _is_safe_material_url(url: str) -> bool:
    """Allow only credential-free HTTP(S) URLs with a public literal address.

    Hostnames fail closed: validating a DNS answer separately from the HTTPX
    connection would allow that hostname to rebind before the actual connect.
    """
    try:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            return False
        return address.is_global
    except ValueError:
        return False


def _material_url_is_reachable(url: str) -> bool:
    """Safely probe a public URL without buffering its body or trusting proxies."""
    deadline = time.monotonic() + 5.0
    current_url = url
    try:
        if not _is_safe_material_url(current_url):
            return False
        with httpx.Client(
            timeout=httpx.Timeout(5.0), follow_redirects=False, trust_env=False,
        ) as client:
            for _ in range(20):
                if not _is_safe_material_url(current_url):
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                with client.stream("GET", current_url, timeout=httpx.Timeout(remaining)) as response:
                    if time.monotonic() > deadline:
                        return False
                    if 200 <= response.status_code < 300:
                        return True
                    if not 300 <= response.status_code < 400:
                        return False
                    location = response.headers.get("location")
                    if location is None:
                        return True
                    current_url = urljoin(current_url, location)
    except (httpx.HTTPError, OSError, ValueError):
        return False
    return False


def build_verifications(
    video: IntermediateVideoV11,
    speakers: Sequence[IntermediateSpeakerV11],
    material_failures: Sequence[str],
) -> list[VerificationRequestV11]:
    """Build all actionable checks from the normalized intermediate data."""
    checks = []
    interventions = video.interventi
    for item in interventions:
        duration = item.end_seconds - item.start_seconds
        if item.tipo not in {"pausa", "logistica"} and not item.relatori:
            checks.append(_verification(
                "critico", "RELATORE_NON_IDENTIFICATO", video=video.chiave,
                intervention=item.id, field="relatori",
                message="Segmento parlato senza relatore identificato.",
            ))
        if item.tipo == "intervento" and 20 <= duration < 120:
            checks.append(_verification(
                "avviso", "INTERVENTO_BREVE", video=video.chiave,
                intervention=item.id, field="durata",
                message="Intervento separato inferiore a due minuti.",
            ))
        if item.confidenza < .8:
            checks.append(_verification(
                "avviso", "CONFIDENZA_BASSA", video=video.chiave,
                intervention=item.id, field="confidenza",
                message="Confidenza dell'intervento inferiore a 0,8.",
            ))

    if interventions and interventions[0].start_seconds != 0:
        checks.append(_verification(
            "critico", "TEMPI_INCOERENTI", video=video.chiave,
            intervention=interventions[0].id, field="inizio",
            message="La timeline non inizia a zero.",
        ))
    for previous, current in zip(interventions, interventions[1:]):
        if current.start_seconds != previous.end_seconds:
            checks.append(_verification(
                "critico", "TEMPI_INCOERENTI", video=video.chiave,
                intervention=current.id, field="inizio",
                message="La timeline contiene un buco o una sovrapposizione.",
            ))
    if interventions and interventions[-1].end_seconds != video.durata_secondi:
        checks.append(_verification(
            "critico", "TEMPI_INCOERENTI", video=video.chiave,
            intervention=interventions[-1].id, field="fine",
            message="La fine della timeline non coincide con la durata del video.",
        ))

    for speaker in speakers:
        if speaker.slug is None:
            checks.append(VerificationRequestV11.model_validate(
                _speaker_warning(speaker.nome, speaker.ruolo)
            ))
    for index, slide in enumerate(video.slide):
        if slide.confidenza < .7:
            checks.append(_verification(
                "avviso", "CONFIDENZA_BASSA", video=video.chiave,
                field=f"slide[{index}].confidenza",
                message="Confidenza della slide inferiore a 0,7.",
            ))
    for source in material_failures:
        checks.append(_verification(
            "avviso", "MATERIALE_NON_RAGGIUNGIBILE", video=video.chiave,
            field=f"materiali:{source}",
            message=f"Materiale non disponibile: {source}.",
        ))
    return _deduplicate_verifications(checks)


def build_intermediate_report(
    report: AcademyReport,
    guid: UUID,
    *,
    material_sources: Sequence[str] = (),
    material_url_checker: Callable[[str], bool] | None = None,
) -> IntermediateReportV11:
    """Transform one persisted report, with bounded checks for declared material URLs."""
    checker = material_url_checker or _material_url_is_reachable
    materials: list[IntermediateMaterialV11] = []
    material_failures: list[str] = []
    seen_sources: set[str] = set()
    for source in material_sources:
        if source in seen_sources:
            continue
        seen_sources.add(source)
        material = parse_material_source(source)
        if material is None:
            continue
        materials.append(material)
        if material.url is None:
            material_failures.append(source)
            continue
        try:
            reachable = checker(material.url)
        except Exception:
            reachable = False
        if not reachable:
            material_failures.append(source)
    speakers = [
        speaker for speaker in reconcile_speakers(report)
        if not GENERIC_SPEAKER_LABEL.fullmatch(speaker.display_name)
    ]
    canonical_names = [speaker.display_name for speaker in speakers]
    corrected = lambda value: correct_speaker_name_mentions(value, canonical_names)

    intermediate_speakers: list[IntermediateSpeakerV11] = []
    for speaker in speakers:
        role, organization = split_role_organization(speaker.role)
        slug = registered_slug(speaker.display_name)
        intermediate_speakers.append(IntermediateSpeakerV11.model_validate({
            "nome": speaker.display_name,
            "slug": slug,
            "ruolo": role,
            "organizzazione": organization,
            "confidenza": _CONFIDENCE_SCORE[speaker.confidence],
            "origine_nome": speaker.origins or ["audio"],
        }))

    normalized = normalize_interventions(report)
    corrected_interventions = [item.model_copy(update={
        "relatori": [corrected(name) for name in item.relatori],
        "titolo": corrected(item.titolo),
        "sintesi": corrected(item.sintesi),
        "punti_chiave": [corrected(point) for point in item.punti_chiave],
    }) for item in normalized]
    public_id = choose_public_intervention(corrected_interventions)
    interventions = [item.model_copy(update={
        "accesso": "pubblico" if item.id == public_id else "iscritti",
    }) for item in corrected_interventions]

    slides = [{
        "inizio": slide.timestamp_seconds,
        "titolo": corrected(slide.title) if slide.title else "Senza titolo",
        "testo_principale": corrected(" · ".join(slide.visible_content))[:500],
        "confidenza": _CONFIDENCE_SCORE[slide.confidence],
    } for slide in report.slides]

    video = IntermediateVideoV11.model_validate({
        "chiave": "v1",
        "guid": str(guid),
        "titolo_bunny": corrected(report.bunny_title),
        "durata_secondi": int(report.duration_seconds + .5),
        "ordine": 1,
        "lingua": report.detected_language,
        "sinossi": corrected(report.synopsis),
        "interventi": interventions,
        "slide": slides,
        "materiali": materials,
    })
    verifications = build_verifications(video, intermediate_speakers, material_failures)
    status = "da_verificare" if any(
        item.livello == "critico" for item in verifications
    ) else "verificato"

    return IntermediateReportV11.model_validate({
        "versione": 1,
        "stato": status,
        "corso": {
            "titolo": corrected(report.title or report.bunny_title),
            "sinossi_corso": corrected(report.synopsis),
        },
        "relatori": intermediate_speakers,
        "video": [video],
        "verifiche_richieste": verifications,
    })
