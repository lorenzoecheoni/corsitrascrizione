"""Build the v2 intermediate report: factual data plus the persisted editorial layer."""

from datetime import date
import re
from uuid import UUID

from app.editorial import EditorialDraft
from app.intermediate_models import (
    IntermediateReportV11,
    IntermediateVideoV11,
    format_hms,
)
from app.intermediate_models_v2 import (
    BlockRefV2,
    BoundaryNoteV2,
    CostV2,
    CourseV2,
    DiagnosticsV2,
    FaqV2,
    InterventionV2,
    MaterialV2,
    ReportV2,
    SlideV2,
    SpeakerV2,
    VerificationV2,
    VideoV2,
)
from app.intermediate_report import _truncate_text, build_intermediate_report
from app.models import AcademyReport
from app.reporting import correct_speaker_name_mentions, reconcile_speakers_detailed
from app.storage import slugify


_DIDACTIC_KINDS = {"intervento", "domande"}
_LEVEL_V2 = {"critico": "bloccante", "avviso": "avviso"}
_BUNNY_TITLE_DATE = re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?!\d)")
_SLIDE_TEXT_LIMIT = 2000


def _course_code(bunny_title: str, guid: UUID) -> str:
    match = _BUNNY_TITLE_DATE.search(bunny_title or "")
    if match:
        day, month, year = (int(part) for part in match.groups())
        try:
            when = date(year, month, day)
        except ValueError:
            pass
        else:
            return f"webinar-{when.isoformat()}"
    return f"video-{guid}"


def _unique_slugs(names: list[str]) -> dict[str, str]:
    slugs: dict[str, str] = {}
    taken: set[str] = set()
    for name in names:
        base = slugify(name) or "relatore"
        slug = base
        suffix = 2
        while slug in taken:
            slug = f"{base}-{suffix}"
            suffix += 1
        slugs[name] = slug
        taken.add(slug)
    return slugs


def _normalize_person(value: str) -> str:
    return " ".join(
        re.findall(r"[a-z0-9]+", value.replace("’", "'").replace("'", " ").casefold())
    )


def _material_speaker_slug(relatore: str | None, slug_by_name: dict[str, str]) -> str | None:
    if not relatore:
        return None
    normalized = _normalize_person(relatore)
    for name, slug in slug_by_name.items():
        if _normalize_person(name) in normalized:
            return slug
    return None


def _containing_intervention(
    interventions: list[InterventionV2], timestamp: float
) -> InterventionV2 | None:
    for index, item in enumerate(interventions):
        if item.inizio_secondi <= timestamp < item.fine_secondi:
            return item
        if index == len(interventions) - 1 and timestamp == item.fine_secondi:
            return item
    return None


def _build_interventions(
    video: IntermediateVideoV11,
    editorial: EditorialDraft,
    slug_by_name: dict[str, str],
    block_titles: dict[str, str],
    report: AcademyReport,
) -> list[InterventionV2]:
    by_id = {item.id: item for item in editorial.interventi}
    slide_titles = sorted(
        (
            slide.timestamp_seconds
            if report.analysis_profile == 2
            else min(slide.timestamp_seconds, video.durata_secondi),
            slide.title,
        )
        for slide in report.slides
        if slide.title
    )
    interventions = []
    for item in video.interventi:
        start = int(item.start_seconds + 0.5)
        end = int(item.end_seconds + 0.5)
        enrichment = by_id.get(item.id)
        if item.tipo in _DIDACTIC_KINDS and enrichment is None:
            raise ValueError(f"Manca l'arricchimento editoriale per {item.id}")
        slide_title = next(
            (title for timestamp, title in slide_titles if start <= timestamp < end),
            None,
        )
        interventions.append(InterventionV2.model_validate({
            "id": item.id,
            "inizio": format_hms(item.start_seconds),
            "fine": format_hms(item.end_seconds),
            "inizio_secondi": start,
            "fine_secondi": end,
            "tipo": item.tipo,
            "relatori": [slug_by_name[name] for name in item.relatori],
            "blocco": (
                {"id": item.blocco, "titolo": block_titles[item.blocco]}
                if item.blocco is not None else None
            ),
            "titolo": enrichment.titolo_lezione if enrichment else item.titolo,
            "titolo_slide": slide_title,
            "descrizione": enrichment.descrizione if enrichment else None,
            "sintesi": item.sintesi,
            "punti_chiave": item.punti_chiave,
            "casi": enrichment.casi if enrichment else [],
            "riferimenti": enrichment.riferimenti if enrichment else [],
            "accesso": item.accesso,
            "confidenza": item.confidenza,
        }))
    return interventions


def _build_slides(
    report: AcademyReport,
    video: IntermediateVideoV11,
    interventions: list[InterventionV2],
    material_ids: dict[str, str],
    corrected,
) -> list[SlideV2]:
    slides = []
    for raw, exported in zip(report.slides, video.slide, strict=True):
        timestamp = (
            raw.timestamp_seconds
            if report.analysis_profile == 2
            else min(raw.timestamp_seconds, video.durata_secondi)
        )
        container = _containing_intervention(interventions, timestamp)
        if container is None:
            raise ValueError("una slide non trova l'intervento che la contiene")
        second = int(timestamp + 0.5)
        slides.append(SlideV2.model_validate({
            "inizio": format_hms(timestamp),
            "inizio_secondi": second,
            "intervento": container.id,
            "titolo": exported.titolo,
            "testo": _truncate_text(
                corrected(" · ".join(raw.visible_content)), limit=_SLIDE_TEXT_LIMIT
            ),
            "materiale": material_ids.get(exported.materiale or ""),
            "pagina": exported.pagina,
            "confidenza": exported.confidenza,
        }))
    return slides


def _build_verifications(
    source: IntermediateReportV11,
    video_key: str,
    interventions: list[InterventionV2],
    slides: list[SlideV2],
    materials: list[MaterialV2],
    speakers: list[SpeakerV2],
) -> tuple[list[VerificationV2], list[BoundaryNoteV2]]:
    checks: list[VerificationV2] = []
    boundaries: list[BoundaryNoteV2] = []
    for item in source.verifiche_richieste:
        if item.codice == "CONFINE":
            boundaries.append(BoundaryNoteV2(
                video=item.video or video_key,
                intervento=item.intervento or "",
                messaggio=item.messaggio,
            ))
            continue
        checks.append(VerificationV2.model_validate({
            "livello": _LEVEL_V2[item.livello],
            "codice": item.codice,
            "video": item.video,
            "intervento": item.intervento,
            "campo": item.campo,
            "messaggio": item.messaggio,
        }))
    with_slides = {slide.intervento for slide in slides}
    for item in interventions:
        if item.tipo == "intervento" and item.id not in with_slides:
            checks.append(VerificationV2(
                livello="avviso", codice="INTERVENTO_SENZA_SLIDE", video=video_key,
                intervento=item.id, campo="slide",
                messaggio="Intervento senza slide nel suo intervallo.",
            ))
    with_materials = {material.relatore for material in materials if material.relatore}
    for speaker in speakers:
        didactic = any(
            speaker.slug in item.relatori and item.tipo in _DIDACTIC_KINDS
            for item in interventions
        )
        if didactic and speaker.slug not in with_materials:
            checks.append(VerificationV2(
                livello="avviso", codice="RELATORE_SENZA_MATERIALE", video=video_key,
                campo="materiali",
                messaggio=f"Il relatore {speaker.nome} non ha un materiale abbinato.",
            ))
    return checks, boundaries


def build_report_v2(report: AcademyReport, guid: UUID, editorial: EditorialDraft) -> ReportV2:
    """Assemble the v2 envelope; the editorial layer must cover the factual source."""
    source = build_intermediate_report(report, guid)
    video = source.video[0]
    slug_by_name = _unique_slugs([speaker.nome for speaker in source.relatori])
    for speaker in source.relatori:
        if speaker.slug:
            slug_by_name[speaker.nome] = speaker.slug
    speakers = [
        SpeakerV2.model_validate({
            "slug": slug_by_name[speaker.nome],
            "nome": speaker.nome,
            "ruolo": speaker.ruolo,
            "organizzazione": speaker.organizzazione,
            "confidenza": speaker.confidenza,
        })
        for speaker in source.relatori
    ]
    block_titles = {block.id: block.titolo_modulo for block in editorial.blocchi}
    reconciliation = reconcile_speakers_detailed(report)
    corrected = lambda value: correct_speaker_name_mentions(
        value, [speaker.display_name for speaker in reconciliation.speakers],
        reconciliation.canonical_by_key,
        ambiguous_aliases=reconciliation.ambiguous_aliases,
    )
    interventions = _build_interventions(video, editorial, slug_by_name, block_titles, report)
    material_ids: dict[str, str] = {}
    taken: set[str] = set()
    for material in video.materiali:
        base = slugify(material.titolo) or "materiale"
        material_id = base
        suffix = 2
        while material_id in taken:
            material_id = f"{base}-{suffix}"
            suffix += 1
        taken.add(material_id)
        material_ids[material.titolo] = material_id
    slides = _build_slides(report, video, interventions, material_ids, corrected)
    materials = []
    for material in video.materiali:
        material_id = material_ids[material.titolo]
        covered = sorted({
            slide.intervento for slide in slides if slide.materiale == material_id
        }, key=lambda item: int(item.rsplit("-i", 1)[1]))
        materials.append(MaterialV2.model_validate({
            "id": material_id,
            "titolo": material.titolo,
            "relatore": _material_speaker_slug(material.relatore, slug_by_name),
            "url": material.url,
            "file": material.file,
            "pagine": material.pagine,
            "accesso": material.accesso,
            "interventi": covered,
        }))
    checks, boundaries = _build_verifications(
        source, video.chiave, interventions, slides, materials, speakers,
    )
    cost = video.costo_stimato
    diagnostica = DiagnosticsV2(
        confini=boundaries,
        costo_stimato=(
            CostV2(
                valuta=cost.valuta, minimo=cost.minimo, massimo=cost.massimo,
                banda_bunny=cost.banda_bunny, trascrizione=cost.trascrizione,
                analisi=cost.analisi, criterio=cost.criterio,
            )
            if cost is not None else None
        ),
    )
    return ReportV2.model_validate({
        "versione": 2,
        "stato": (
            "da_verificare"
            if any(check.livello == "bloccante" for check in checks)
            else "verificato"
        ),
        "corso": {
            "codice": _course_code(video.titolo_bunny, guid),
            "titolo": source.corso.titolo,
            "sottotitolo": editorial.corso.sottotitolo,
            "area": editorial.corso.area,
            "sinossi": source.corso.sinossi_corso,
            "competenze": editorial.corso.competenze,
            "profili": editorial.corso.profili,
            "faq": [faq.model_dump() for faq in editorial.corso.faq],
        },
        "relatori": [speaker.model_dump() for speaker in speakers],
        "video": [{
            "chiave": video.chiave,
            "guid": video.guid,
            "titolo_bunny": video.titolo_bunny,
            "durata_secondi": video.durata_secondi,
            "ordine": video.ordine,
            "lingua": video.lingua,
            "sinossi": video.sinossi,
            "interventi": [item.model_dump() for item in interventions],
            "slide": [slide.model_dump() for slide in slides],
            "materiali": [material.model_dump() for material in materials],
        }],
        "verifiche_richieste": [check.model_dump() for check in checks],
        "diagnostica": diagnostica.model_dump(),
    })


__all__ = ["build_report_v2", "slugify"]
