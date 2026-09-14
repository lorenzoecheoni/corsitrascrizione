"""Build the one-video Academy intermediate report v1.1 envelope."""

import re
import unicodedata
from typing import Callable, Sequence
from uuid import UUID

from app.intermediate_models import IntermediateReportV11
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
        "campo": "ruolo" if role is None else "nome",
        "messaggio": message,
    }


def build_intermediate_report(
    report: AcademyReport,
    guid: UUID,
    *,
    material_sources: Sequence[str] = (),
    material_url_checker: Callable[[str], bool] | None = None,
) -> IntermediateReportV11:
    """Transform one persisted analytical report without provider or network calls."""
    # Material resolution and URL checks belong to the later material task.
    _ = material_sources, material_url_checker
    speakers = [
        speaker for speaker in reconcile_speakers(report)
        if not GENERIC_SPEAKER_LABEL.fullmatch(speaker.display_name)
    ]
    canonical_names = [speaker.display_name for speaker in speakers]
    corrected = lambda value: correct_speaker_name_mentions(value, canonical_names)

    intermediate_speakers = []
    verifications = []
    for speaker in speakers:
        role, organization = split_role_organization(speaker.role)
        slug = registered_slug(speaker.display_name)
        intermediate_speakers.append({
            "nome": speaker.display_name,
            "slug": slug,
            "ruolo": role,
            "organizzazione": organization,
            "confidenza": _CONFIDENCE_SCORE[speaker.confidence],
            "origine_nome": speaker.origins or ["audio"],
        })
        if slug is None:
            verifications.append(_speaker_warning(speaker.display_name, role))

    interventions = []
    for index, intervention in enumerate(
        sorted(report.interventions, key=lambda item: item.start_seconds), start=1
    ):
        interventions.append({
            "id": f"v1-i{index:03d}",
            "inizio": intervention.start_seconds,
            "fine": intervention.end_seconds,
            "tipo": intervention.tipo,
            "relatori": [corrected(name) for name in intervention.relatori],
            "titolo": corrected(intervention.titolo),
            "sintesi": corrected(intervention.sintesi),
            "punti_chiave": [corrected(point) for point in intervention.punti_chiave],
            "accesso": "iscritti",
            "confidenza": intervention.confidenza,
        })

    slides = [{
        "inizio": slide.timestamp_seconds,
        "titolo": corrected(slide.title) if slide.title else "Senza titolo",
        "testo_principale": corrected(" · ".join(slide.visible_content))[:500],
        "confidenza": _CONFIDENCE_SCORE[slide.confidence],
    } for slide in report.slides]

    return IntermediateReportV11.model_validate({
        "versione": 1,
        "stato": "verificato",
        "corso": {
            "titolo": corrected(report.title or report.bunny_title),
            "sinossi_corso": corrected(report.synopsis),
        },
        "relatori": intermediate_speakers,
        "video": [{
            "chiave": "v1",
            "guid": str(guid),
            "titolo_bunny": corrected(report.bunny_title),
            "durata_secondi": int(report.duration_seconds + .5),
            "ordine": 1,
            "lingua": report.detected_language,
            "sinossi": corrected(report.synopsis),
            "interventi": interventions,
            "slide": slides,
            "materiali": [],
        }],
        "verifiche_richieste": verifications,
    })
