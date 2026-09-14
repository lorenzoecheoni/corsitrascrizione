"""Structured and human-readable renderers for per-video reports."""

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Sequence
from uuid import UUID
import re
import unicodedata

from app.models import AcademyReport, Confidence, Evidence, GENERIC_SPEAKER_LABEL


_CONFIDENCE_SCORE = {"alta": .95, "media": .65, "bassa": .35}
_CONFIDENCE_RANK = {"bassa": 0, "media": 1, "alta": 2}
_FURIO_D_ANDREA_KEYS = {"furio d andrea", "fulvio d andrea"}


@dataclass
class ReconciledSpeaker:
    display_name: str
    role: str | None
    confidence: Confidence
    evidence: list[Evidence]
    origins: list[str]


def _name_key(value: str) -> str:
    folded = "".join(
        character for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z]+", folded))


def _name_pattern(value: str) -> re.Pattern[str] | None:
    tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ]+(?:['’][A-Za-zÀ-ÖØ-öø-ÿ]+)?", value)
    if len(tokens) < 2:
        return None
    def token_pattern(token: str) -> str:
        return r"['’]".join(re.escape(part) for part in re.split(r"['’]", token))

    surname = r"\s+".join(
        token_pattern(token) for token in tokens[1:]
    )
    return re.compile(rf"\b[A-Za-zÀ-ÖØ-öø-ÿ]+\s+{surname}\b", re.IGNORECASE)


def correct_speaker_name_mentions(value: str, canonical_names: Sequence[str]) -> str:
    """Correct only near-identical full names sharing the canonical surname."""
    corrected = value
    replacements: dict[str, str] = {}
    for canonical in canonical_names:
        target = (
            "Furio d'Andrea" if _name_key(canonical) in _FURIO_D_ANDREA_KEYS
            else canonical
        )
        pattern = _name_pattern(canonical)
        if pattern is None:
            continue

        def replace(match: re.Match[str]) -> str:
            score = SequenceMatcher(None, _name_key(match.group()), _name_key(target)).ratio()
            if score < .82:
                return match.group()
            placeholder = f"\x00speaker-{len(replacements)}\x00"
            replacements[placeholder] = target
            return placeholder

        corrected = pattern.sub(replace, corrected)
    for placeholder, target in replacements.items():
        corrected = corrected.replace(placeholder, target)
    return corrected


def _name_origins(evidence: Sequence[Evidence]) -> list[str]:
    origins: list[str] = []
    for item in evidence:
        origin = {
            "introduzione": "audio",
            "sottopancia": "audio",
            "slide": "slide",
            "metadata": "metadata",
        }.get(item.kind)
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def reconcile_speakers(report: AcademyReport) -> list[ReconciledSpeaker]:
    canonical_names = list(dict.fromkeys([
        speaker.display_name for speaker in report.speakers
        if not GENERIC_SPEAKER_LABEL.fullmatch(speaker.display_name)
    ] + [
        name for intervention in report.interventions for name in intervention.relatori
        if not GENERIC_SPEAKER_LABEL.fullmatch(name)
    ]))
    merged: dict[str, ReconciledSpeaker] = {}
    evidence_keys: dict[str, set[tuple[str, float | None, str]]] = {}
    for source in report.speakers:
        speaker = ReconciledSpeaker(
            display_name=correct_speaker_name_mentions(source.display_name, canonical_names),
            role=source.role,
            confidence=source.confidence,
            evidence=[evidence.model_copy(deep=True) for evidence in source.evidence],
            origins=[],
        )
        for evidence in speaker.evidence:
            evidence.note = correct_speaker_name_mentions(evidence.note, canonical_names)
        speaker.origins = _name_origins(speaker.evidence)
        key = _name_key(speaker.display_name)
        if key not in merged:
            merged[key] = speaker
            evidence_keys[key] = {
                (evidence.kind, evidence.timestamp_seconds, evidence.note)
                for evidence in speaker.evidence
            }
            continue
        current = merged[key]
        if _CONFIDENCE_RANK[speaker.confidence] > _CONFIDENCE_RANK[current.confidence]:
            current.confidence = speaker.confidence
        if speaker.role and (not current.role or current.role.casefold() == "relatore"):
            current.role = speaker.role
        for evidence in speaker.evidence:
            evidence_key = (evidence.kind, evidence.timestamp_seconds, evidence.note)
            if evidence_key not in evidence_keys[key]:
                current.evidence.append(evidence)
                evidence_keys[key].add(evidence_key)
        for origin in speaker.origins:
            if origin not in current.origins:
                current.origins.append(origin)
    for intervention in report.interventions:
        for name in intervention.relatori:
            name = correct_speaker_name_mentions(name, canonical_names)
            key = _name_key(name)
            if not key or key in merged or GENERIC_SPEAKER_LABEL.fullmatch(name):
                continue
            merged[key] = ReconciledSpeaker(
                display_name=name,
                role=None,
                confidence="media",
                evidence=[],
                origins=["audio"],
            )
            evidence_keys[key] = set()
    return list(merged.values())


def format_hms_timestamp(seconds: float) -> str:
    """Format a report timestamp using the stable h:mm:ss interchange format."""
    total_seconds = max(0, int(seconds + .5))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"


def build_video_report_payload(report: AcademyReport, guid: UUID) -> dict:
    """Build the factual, one-video JSON interchange document."""
    unique_speakers = reconcile_speakers(report)
    canonical_names = [speaker.display_name for speaker in unique_speakers]
    speakers = []
    for speaker in unique_speakers:
        if GENERIC_SPEAKER_LABEL.fullmatch(speaker.display_name):
            continue
        item = {
            "nome": speaker.display_name,
            "confidenza": _CONFIDENCE_SCORE[speaker.confidence],
            "evidenze": [
                {
                    "tipo": evidence.kind,
                    **(
                        {"inizio": format_hms_timestamp(evidence.timestamp_seconds)}
                        if evidence.timestamp_seconds is not None else {}
                    ),
                    "nota": correct_speaker_name_mentions(evidence.note, canonical_names),
                }
                for evidence in speaker.evidence
            ],
        }
        if speaker.role:
            item["ruolo"] = speaker.role
        speakers.append(item)

    video = {
        "guid": str(guid),
        "titolo_suggerito": correct_speaker_name_mentions(report.title, canonical_names),
        "durata_secondi": int(report.duration_seconds + .5),
        "lingua": report.detected_language,
        "sinossi": correct_speaker_name_mentions(report.synopsis, canonical_names),
    }
    if report.bunny_title:
        video["titolo_bunny"] = correct_speaker_name_mentions(report.bunny_title, canonical_names)

    slides = []
    for slide in report.slides:
        item = {
            "inizio": format_hms_timestamp(slide.timestamp_seconds),
            "testo_principale": correct_speaker_name_mentions(
                " · ".join(slide.visible_content), canonical_names
            )[:500],
            "confidenza": _CONFIDENCE_SCORE[slide.confidence],
        }
        if slide.title:
            item["titolo"] = correct_speaker_name_mentions(slide.title, canonical_names)
        slides.append(item)

    interventions = []
    for intervention in report.interventions:
        item = intervention.model_dump(mode="json", by_alias=True, exclude_none=True)
        item["relatori"] = [
            correct_speaker_name_mentions(name, canonical_names) for name in item["relatori"]
        ]
        for field in ("titolo", "sintesi"):
            item[field] = correct_speaker_name_mentions(item[field], canonical_names)
        item["punti_chiave"] = [
            correct_speaker_name_mentions(point, canonical_names)
            for point in item["punti_chiave"]
        ]
        interventions.append(item)

    return {
        "versione": 1,
        "video": video,
        "relatori": speakers,
        "interventi": interventions,
        "slide": slides,
        "incertezze": [
            correct_speaker_name_mentions(item, canonical_names) for item in report.uncertainties
        ],
    }


def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS, switching to H:MM:SS for hour-long media."""
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes:02d}:{remaining_seconds:02d}"


def _csv(items: list[str]) -> str:
    return ", ".join(items) if items else "Nessuno"


def render_markdown(report: AcademyReport) -> str:
    """Render a stable, human-readable Markdown representation of a report."""
    speakers = reconcile_speakers(report)
    canonical_names = [speaker.display_name for speaker in speakers]
    corrected = lambda value: correct_speaker_name_mentions(value, canonical_names)
    lines = [
        f"# {corrected(report.title)}",
        "",
        f"Titolo originale Bunny: {corrected(report.bunny_title) if report.bunny_title else 'Non disponibile'}",
        f"Durata: {format_timestamp(report.duration_seconds)}",
        f"Lingua rilevata: {report.detected_language}",
        "",
        "## Sinossi",
        corrected(report.synopsis),
        "",
        "## Relatori",
    ]
    for speaker in speakers:
        role = f" — {speaker.role}" if speaker.role else ""
        lines.append(f"- {speaker.display_name}{role} (confidenza: {speaker.confidence})")
        for evidence in speaker.evidence:
            timestamp = (
                f" [{format_timestamp(evidence.timestamp_seconds)}]"
                if evidence.timestamp_seconds is not None
                else ""
            )
            lines.append(f"  - Evidenza{timestamp}: {corrected(evidence.note)}")

    if report.interventions:
        lines.extend(["", "## Interventi"])
        for intervention in report.interventions:
            intervention_speakers = (
                _csv(intervention.relatori) if intervention.relatori else "Da verificare"
            )
            lines.append(
                f"- {format_timestamp(intervention.start_seconds)}–"
                f"{format_timestamp(intervention.end_seconds)} · {intervention.tipo}: "
                f"{corrected(intervention.titolo)}"
            )
            lines.append(
                f"  Relatori: {intervention_speakers}; "
                f"confidenza: {intervention.confidenza:.2f}"
            )
            lines.append(f"  {corrected(intervention.sintesi)}")
            for point in intervention.punti_chiave:
                lines.append(f"  - {corrected(point)}")

    lines.extend(["", "## Slide"])
    for slide in report.slides:
        title = corrected(slide.title) if slide.title else "Senza titolo"
        content = f" — {corrected(_csv(slide.visible_content))}" if slide.visible_content else ""
        lines.append(
            f"- {format_timestamp(slide.timestamp_seconds)}: {title}{content} "
            f"(confidenza: {slide.confidence})"
        )

    lines.extend(["", "## Incertezze"])
    lines.extend(f"- {corrected(uncertainty)}" for uncertainty in report.uncertainties)
    lines.extend(
        [
            "",
            "## Stima costi (USD)",
            f"- Intervallo stimato: ${report.cost.estimated_low_usd:.4f}–${report.cost.estimated_high_usd:.4f}",
            f"- Banda Bunny: ${report.cost.bunny_bandwidth_usd:.4f}",
            f"- Trascrizione: ${report.cost.transcription_usd:.4f}",
            f"- Analisi: ${report.cost.analysis_usd:.4f}",
            f"- Base della stima: {report.cost.basis}",
        ]
    )
    lines.extend(["", "## Utilizzo API restituito"])
    for name, usage in (("Trascrizione", report.usage.transcription), ("Responses", report.usage.responses)):
        def shown(value):
            return str(value) if value is not None else "non disponibile"
        lines.append(f"- {name}: {usage.requests} richieste; token input {shown(usage.input_tokens)}; "
                     f"token output {shown(usage.output_tokens)}.")
        lines.append(f"  Contatori mancanti: input in {usage.missing_input_requests} richieste, "
                     f"output in {usage.missing_output_requests} richieste.")
        if not usage.entries:
            lines.append("  Utilizzo del provider non disponibile; stima da durata.")
        if name == "Trascrizione":
            lines.append(f"  Secondi audio restituiti dal provider: {shown(usage.provider_audio_seconds)}.")
    lines.append("Il costo monetario è una stima applicativa e non sostituisce la fattura dei fornitori.")
    return "\n".join(lines)


def render_text(report: AcademyReport) -> str:
    """Render the report as plain text without Markdown syntax."""
    markdown = render_markdown(report)
    without_headings = re.sub(r"(?m)^#{1,6}\s+", "", markdown)
    return re.sub(r"(?m)^\s*-\s+", "", without_headings)
