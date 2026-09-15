"""Build the one-video Academy intermediate report v1.1 envelope."""

import math
import re
import unicodedata
from typing import Mapping, Sequence
from uuid import UUID

from app.academy_registry import registered_slug
from app.boundaries import has_complete_boundary_evidence
from app.materials import parse_material_source as parse_declared_material_source, validate_persisted_material
from app.material_registry import canonical_material_title
from app.intermediate_models import (
    IntermediateInterventionV11,
    IntermediateMaterialV11,
    IntermediateReportV11,
    IntermediateSpeakerV11,
    IntermediateVideoV11,
    VerificationRequestV11,
    format_hms,
)
from app.models import AcademyReport, GENERIC_SPEAKER_LABEL
from app.reporting import (
    correct_speaker_name_mentions,
    reconcile_speakers_detailed,
    rewrite_speaker_references,
)


_ASSOHOLDING_ROLE = re.compile(
    r"^(?P<role>.*?)\s+di\s+(?:ass holding|asso holding|assholding|assoholding)\s*$",
    re.IGNORECASE,
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
    r"^\s*(?:(?:speaker|relatore|provider|voce)\s*[-_:]?\s*(?:[a-z]?\d+|[a-z])"
    r"(?:\s*[:\-–]\s*|\s+)|[a-z]\s*(?::|\.|-|–)\s*|"
    r"[b-df-hj-km-np-tv-z]\s+)"
    r"(?P<body>.+?)\s*$",
    re.IGNORECASE,
)
_INTRODUCTORY_VERB = re.compile(
    r"^(?:approfondisce|tratta|descrive|presenta|illustra|discute|spiega|parla di)\s+",
    re.IGNORECASE,
)


def _name_key(value: str) -> str:
    folded = "".join(
        character for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z]+", folded))


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


def _ambiguous_alias_warning(name: str) -> VerificationRequestV11:
    return _verification(
        "avviso", "ALIAS_RELATORE_AMBIGUO", video="v1",
        field=f"relatori:{_name_key(name)}:alias",
        message=(
            f"L'alias relatore {name} corrisponde a più persone: "
            "mantenere l'identità separata fino alla verifica nel Registro."
        ),
    )


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
    report: AcademyReport,
    video_key: str = "v1",
    *,
    stored_to_exported_ids: Mapping[str, str] | None = None,
    duration_seconds: int | None = None,
) -> list[IntermediateInterventionV11]:
    """Copy and deterministically normalize persisted analytical interventions."""
    normalized = []
    duration_seconds = math.floor(report.duration_seconds) if duration_seconds is None else duration_seconds
    ordered_interventions = sorted(report.interventions, key=lambda item: item.start_seconds)
    exported_ids = (
        stored_to_exported_ids
        if stored_to_exported_ids is not None
        else {
            stored.id: f"{video_key}-i{index:03d}"
            for index, stored in enumerate(ordered_interventions, start=1)
        }
    )
    for intervention in ordered_interventions:
        duration = intervention.end_seconds - intervention.start_seconds
        kind = intervention.tipo
        if report.analysis_profile != 2 and kind == "intervento" and duration < 20:
            kind = _short_segment_kind(
                title=intervention.titolo,
                summary=intervention.sintesi,
                speakers=intervention.relatori,
            )
        normalized.append(IntermediateInterventionV11(
            id=exported_ids[intervention.id],
            inizio=intervention.start_seconds if report.analysis_profile == 2 else min(intervention.start_seconds, duration_seconds),
            fine=intervention.end_seconds if report.analysis_profile == 2 else min(intervention.end_seconds, duration_seconds),
            tipo=kind,
            relatori=list(intervention.relatori),
            titolo=intervention.titolo,
            sintesi=_normalize_summary(intervention.sintesi),
            punti_chiave=list(intervention.punti_chiave) if kind == "intervento" else [],
            accesso="iscritti",
            confidenza=intervention.confidenza,
            blocco=intervention.block_id,
            capitolo_numero=intervention.chapter_number,
            capitoli_blocco=intervention.chapters_in_block,
            confine_inizio=intervention.boundary_origin.model_dump() if intervention.boundary_origin else None,
        ))
    return normalized


def _boundary_excerpt(words: Sequence[str], pause: bool) -> str:
    if not words:
        return "(pausa)"
    excerpt = f"«{' '.join(words)}»"
    return f"{excerpt} (pausa)" if pause else excerpt


def build_boundary_verifications(
    report: AcademyReport,
    stored_to_exported_ids: Mapping[str, str],
) -> list[VerificationRequestV11]:
    """Render one auditable warning for every final neighboring pair."""
    if report.analysis_profile == 2:
        ordered = sorted(report.interventions, key=lambda item: item.start_seconds)
        evidence_by_next = {evidence.next_intervention_id: evidence for evidence in report.boundaries}
        complete = (
            report.audio_boundary_version == 1 and bool(ordered)
            and len(evidence_by_next) == len(report.boundaries) == len(ordered) - 1
            and len({item.id for item in ordered}) == len(ordered)
        )
        for previous, following in zip(ordered, ordered[1:]):
            evidence = evidence_by_next.get(following.id)
            complete = complete and evidence is not None and (
                evidence.previous_intervention_id == previous.id
                and evidence.boundary_seconds == previous.end_seconds == following.start_seconds
                and (previous.tipo != "pausa" or (not evidence.words_before and evidence.pause_before))
                and (following.tipo != "pausa" or (not evidence.words_after and evidence.pause_after))
            )
    else:
        complete = has_complete_boundary_evidence(report)
    if not complete:
        raise ValueError("Il report non contiene prove complete dei confini")
    checks = []
    for evidence in sorted(report.boundaries, key=lambda item: item.boundary_seconds):
        previous_id = stored_to_exported_ids[evidence.previous_intervention_id]
        next_id = stored_to_exported_ids[evidence.next_intervention_id]
        message = (
            f"Confine {format_hms(evidence.boundary_seconds)}; termina {previous_id}. "
            f"Prima: {_boundary_excerpt(evidence.words_before, evidence.pause_before)}. "
            f"Dopo: {_boundary_excerpt(evidence.words_after, evidence.pause_after)}."
        )
        checks.append(_verification(
            "avviso", "CONFINE", video="v1", intervention=next_id, field="inizio",
            message=message,
        ))
    return checks


def choose_public_intervention(
    interventions: Sequence[IntermediateInterventionV11],
) -> str | None:
    """Choose only the first chronological didactic chapter of 480–900 seconds."""
    duration = lambda item: item.end_seconds - item.start_seconds
    eligible = sorted((item for item in interventions if item.tipo == "intervento"), key=lambda item: item.start_seconds)
    preview = next((item for item in eligible if 480 <= duration(item) <= 900), None)
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
    material = parse_declared_material_source(value)
    return IntermediateMaterialV11.model_validate(material.model_dump()) if material else None


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
        if item.tipo == "intervento" and duration > 1200:
            checks.append(_verification(
                "avviso", "INTERVENTO_LUNGO", video=video.chiave,
                intervention=item.id, field="durata",
                message="Capitolo didattico superiore a venti minuti.",
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
        if slide.materiale is None or slide.pagina is None:
            checks.append(_verification(
                "avviso", "SLIDE_NON_ABBINATA", video=video.chiave,
                field=f"slide[{index}]",
                message="Slide rilevata senza abbinamento completo a materiale e pagina.",
            ))
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
) -> IntermediateReportV11:
    """Build profile 2 exclusively from persisted evidence, without I/O."""
    duration = math.floor(report.duration_seconds)
    ordered_interventions = sorted(report.interventions, key=lambda item: item.start_seconds)
    ordered_blocks = sorted(report.speech_blocks, key=lambda item: item.start_seconds)
    if report.analysis_profile == 2:
        persisted_materials = [validate_persisted_material(material) for material in report.materials]
        report = report.model_copy(update={
            "interventions": ordered_interventions, "speech_blocks": ordered_blocks,
            "materials": sorted(persisted_materials, key=lambda material: (
                canonical_material_title(material.titolo), material.url or "", material.file or "",
                material.relatore or "", material.pagine or 0,
            )),
        })
    block_ids = {block.id: f"v1-b{index:03d}" for index, block in enumerate(ordered_blocks, 1)}
    if len(block_ids) != len(ordered_blocks) or any(not block_id.strip() for block_id in block_ids):
        raise ValueError("I blocchi parlato richiedono identificativi univoci")
    if report.analysis_profile == 2 and any(
        chapter.tipo not in {"pausa", "logistica"}
        and (chapter.block_id not in block_ids or (index > 0 and chapter.boundary_origin is None))
        for index, chapter in enumerate(ordered_interventions)
    ):
        raise ValueError("Ogni capitolo richiede un blocco persistito e l'origine del confine")
    stored_to_exported_ids = {
        stored.id: f"v1-i{index:03d}"
        for index, stored in enumerate(ordered_interventions, start=1)
    }
    boundary_verifications = build_boundary_verifications(
        report, stored_to_exported_ids,
    )
    materials: list[IntermediateMaterialV11] = []
    material_failures: list[str] = [
        "MATERIALE_NON_RAGGIUNGIBILE" for _ in report.material_failures
    ]
    seen_sources: set[str] = set()
    for source in (() if report.analysis_profile == 2 else material_sources):
        if source in seen_sources:
            continue
        seen_sources.add(source)
        material = parse_material_source(source)
        if material is None:
            material_failures.append("MATERIALE_NON_RAGGIUNGIBILE")
            continue
        materials.append(material)
        material_failures.append("MATERIALE_NON_RAGGIUNGIBILE")
    reconciliation = reconcile_speakers_detailed(report)
    speakers = [
        speaker for speaker in reconciliation.speakers
        if not GENERIC_SPEAKER_LABEL.fullmatch(speaker.display_name)
    ]
    canonical_names = [speaker.display_name for speaker in speakers]
    canonical_by_key = reconciliation.canonical_by_key
    corrected = lambda value: correct_speaker_name_mentions(
        value, canonical_names, canonical_by_key,
        ambiguous_aliases=reconciliation.ambiguous_aliases,
    )

    canonical_materials = []
    for material in report.materials:
        title = corrected(canonical_material_title(material.titolo))
        material_speakers = rewrite_speaker_references(
            [material.relatore] if material.relatore else [], canonical_by_key,
        )
        exported_material = IntermediateMaterialV11.model_validate({
            "titolo": title,
            "relatore": material_speakers[0] if material_speakers else None,
            "url": material.url,
            "file": material.file,
            "pagine": material.pagine,
        })
        canonical_materials.append((material.titolo, exported_material))

    # Validate every canonical record before collapsing any duplicate. Otherwise
    # an A/X, B/Y, A/Y sequence could silently rebind an A slide to source Y.
    material_titles = {}
    material_by_source = {}
    source_by_title = {}
    for original_title, material in canonical_materials:
        key = (material.url, material.file)
        if material.titolo in source_by_title and source_by_title[material.titolo] != key:
            raise ValueError("Identità dei materiali persistiti incoerente")
        if key in material_by_source and material_by_source[key] != material:
            raise ValueError("Metadati dei materiali persistiti incoerenti")
        source_by_title[material.titolo] = key
        material_by_source[key] = material
        material_titles[original_title] = material.titolo
    materials.extend(sorted(material_by_source.values(), key=lambda material: (
        material.titolo, material.url or "", material.file or "",
    )))

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

    normalized = normalize_interventions(
        report, stored_to_exported_ids=stored_to_exported_ids, duration_seconds=duration,
    )
    corrected_interventions = [item.model_copy(update={
        "relatori": rewrite_speaker_references(item.relatori, canonical_by_key),
        "titolo": corrected(item.titolo),
        "sintesi": corrected(item.sintesi),
        "punti_chiave": [corrected(point) for point in item.punti_chiave],
        "blocco": block_ids.get(item.blocco, item.blocco),
    }) for item in normalized]
    public_id = choose_public_intervention(corrected_interventions)
    interventions = [item.model_copy(update={
        "accesso": "pubblico" if item.id == public_id else "iscritti",
    }) for item in corrected_interventions]

    slides = [{
        "inizio": slide.timestamp_seconds if report.analysis_profile == 2 else min(slide.timestamp_seconds, duration),
        "titolo": corrected(slide.title) if slide.title else "Senza titolo",
        "testo_principale": corrected(" · ".join(slide.visible_content))[:500],
        "confidenza": _CONFIDENCE_SCORE[slide.confidence],
        "materiale": material_titles.get(slide.material_title, corrected(canonical_material_title(slide.material_title))) if slide.material_title else None,
        "pagina": slide.page,
    } for slide in report.slides]

    video_data = {
        "chiave": "v1",
        "guid": str(guid),
        "titolo_bunny": corrected(report.bunny_title),
        "durata_secondi": duration,
        "ordine": 1,
        "lingua": report.detected_language,
        "sinossi": corrected(report.synopsis),
        "interventi": interventions,
        "slide": slides,
        "materiali": materials,
    }
    if report.analysis_profile == 2:
        video_data.update({
            "blocchi_parlato": [{
                "id": block_ids[block.id],
                "inizio": block.start_seconds,
                "fine": block.end_seconds,
                "tipo": block.tipo,
                "relatori": rewrite_speaker_references(
                    block.relatori, canonical_by_key,
                ),
                "titolo": corrected(block.titolo),
                "sinossi": corrected(block.sinossi),
            } for block in report.speech_blocks],
            "costo_stimato": {
                "valuta": "USD",
                "minimo": report.cost.estimated_low_usd,
                "massimo": report.cost.estimated_high_usd,
                "banda_bunny": report.cost.bunny_bandwidth_usd,
                "trascrizione": report.cost.transcription_usd,
                "analisi": report.cost.analysis_usd,
                "criterio": report.cost.basis,
            },
        })
    video = IntermediateVideoV11.model_validate(video_data)
    verifications = [
        *build_verifications(video, intermediate_speakers, material_failures),
        *(_ambiguous_alias_warning(name) for name in reconciliation.ambiguous_aliases),
        *boundary_verifications,
    ]
    verifications = _deduplicate_verifications(verifications)
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
