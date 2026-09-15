"""Structured and human-readable renderers for per-video reports."""

from dataclasses import dataclass
from typing import Iterable, Sequence
from uuid import UUID
import re

from app import academy_registry
from app.academy_registry import (
    find_registry_person,
    parse_speaker_identity,
    person_key,
)
from app.models import AcademyReport, Confidence, Evidence, GENERIC_SPEAKER_LABEL


_CONFIDENCE_SCORE = {"alta": .95, "media": .65, "bassa": .35}
_CONFIDENCE_RANK = {"bassa": 0, "media": 1, "alta": 2}
_ASSOHOLDING_ROLE = re.compile(
    r"^(?P<role>.*?)\s+di\s+(?:ass holding|asso holding|assholding|assoholding)\s*$",
    re.IGNORECASE,
)
@dataclass
class ReconciledSpeaker:
    display_name: str
    role: str | None
    confidence: Confidence
    evidence: list[Evidence]
    origins: list[str]


@dataclass
class SpeakerReconciliation:
    speakers: list[ReconciledSpeaker]
    canonical_by_key: dict[str, str]
    ambiguous_aliases: list[str]


def _name_key(value: str) -> str:
    return person_key(value)


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


def canonical_speaker_name_map(names: Sequence[str]) -> dict[str, str]:
    """Map exact normalized identities and explicit Registry aliases."""
    canonical = {}
    for name in names:
        parsed = parse_speaker_identity(name)
        person = find_registry_person(parsed.name)
        display_name = person.nome if person is not None else parsed.name
        canonical.setdefault(_name_key(name), display_name)
        canonical.setdefault(_name_key(parsed.name), display_name)
        if person is not None:
            for alias in person.identity_keys:
                canonical[alias] = person.nome
    return canonical


def correct_speaker_name_mentions(
    value: str,
    canonical_names: Sequence[str],
    canonical_by_key: dict[str, str] | None = None,
) -> str:
    """Correct exact normalized names and explicit aliases in narrative text."""
    corrected = value
    name_map = canonical_by_key or canonical_speaker_name_map(canonical_names)
    replacements: dict[str, str] = {}
    for canonical in canonical_names:
        person = find_registry_person(canonical)
        allowed_keys = (
            set(person.identity_keys) if person is not None else {_name_key(canonical)}
        )
        allowed_keys.update(
            key for key, target in name_map.items() if target == canonical
        )
        pattern = _name_pattern(canonical)
        if pattern is None:
            continue

        def replace(match: re.Match[str]) -> str:
            key = _name_key(match.group())
            if key not in allowed_keys:
                return match.group()
            placeholder = f"\x00speaker-{len(replacements)}\x00"
            replacements[placeholder] = name_map.get(key, canonical)
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


@dataclass(frozen=True)
class _SpeakerOccurrence:
    raw_name: str
    role: str | None
    confidence: Confidence
    evidence: tuple[Evidence, ...]
    origin: str


def _speaker_occurrences(report: AcademyReport) -> list[_SpeakerOccurrence]:
    occurrences = [
        _SpeakerOccurrence(
            source.display_name, source.role, source.confidence,
            tuple(source.evidence), "profile",
        )
        for source in report.speakers
    ]
    occurrences.extend(
        _SpeakerOccurrence(name, None, "media", (), "audio")
        for intervention in report.interventions for name in intervention.relatori
    )
    occurrences.extend(
        _SpeakerOccurrence(name, None, "media", (), "audio")
        for block in report.speech_blocks for name in block.relatori
    )
    occurrences.extend(
        _SpeakerOccurrence(material.relatore, None, "media", (), "inventario")
        for material in report.materials if material.relatore
    )
    return occurrences


def _is_generic_name(value: str) -> bool:
    return bool(GENERIC_SPEAKER_LABEL.fullmatch(value.strip()))


def _report_people(occurrences: Sequence[_SpeakerOccurrence]) -> dict[str, str]:
    registry_surnames = {
        person.surname_key for person in academy_registry.REGISTRY_PEOPLE
    }
    people: dict[str, str] = {}
    for occurrence in occurrences:
        parsed = parse_speaker_identity(occurrence.raw_name)
        key = _name_key(parsed.name)
        if not key or _is_generic_name(parsed.name) or find_registry_person(parsed.name):
            continue
        if key in registry_surnames or len(key.split()) < 2:
            continue
        people.setdefault(key, parsed.name)
    return people


def _surname_candidates(report_people: dict[str, str]) -> dict[str, list[tuple[str, str]]]:
    candidates: dict[str, list[tuple[str, str]]] = {}
    for person in academy_registry.REGISTRY_PEOPLE:
        candidates.setdefault(person.surname_key, []).append(
            (_name_key(person.nome), person.nome)
        )
    for key, name in report_people.items():
        surname = " ".join(key.split()[1:])
        candidate = (key, name)
        if candidate not in candidates.setdefault(surname, []):
            candidates[surname].append(candidate)
    return candidates


def _canonical_identity(
    raw_name: str,
    report_people: dict[str, str],
    surname_candidates: dict[str, list[tuple[str, str]]],
) -> tuple[str, bool]:
    parsed = parse_speaker_identity(raw_name)
    key = _name_key(parsed.name)
    registry_person = find_registry_person(parsed.name)
    if registry_person is not None:
        return registry_person.nome, False
    if key in report_people:
        return report_people[key], False
    candidates = surname_candidates.get(key, [])
    if len(candidates) == 1:
        return candidates[0][1], False
    return parsed.name, len(candidates) > 1


def _role_rank(role: str | None, *, honorific: bool = False) -> int:
    if role is None:
        return 0
    if honorific:
        return 2
    return 1 if role.casefold() == "relatore" else 3


def _adds_compatible_organization(current: str | None, candidate: str | None) -> bool:
    if current is None or candidate is None or _ASSOHOLDING_ROLE.fullmatch(current):
        return False
    match = _ASSOHOLDING_ROLE.fullmatch(candidate)
    return bool(
        match and _name_key(match.group("role")) == _name_key(current)
    )


def _deduplicated_names(names: Iterable[str], canonical_by_key: dict[str, str]) -> list[str]:
    result = []
    seen = set()
    for name in names:
        parsed = parse_speaker_identity(name)
        canonical = canonical_by_key.get(_name_key(name), parsed.name)
        key = _name_key(canonical)
        if key and key not in seen:
            seen.add(key)
            result.append(canonical)
    return result


def rewrite_speaker_references(
    names: Sequence[str], canonical_by_key: dict[str, str],
) -> list[str]:
    """Rewrite and de-duplicate one ordered speaker-reference list."""
    return _deduplicated_names(names, canonical_by_key)


def reconcile_speakers_detailed(report: AcademyReport) -> SpeakerReconciliation:
    occurrences = _speaker_occurrences(report)
    report_people = _report_people(occurrences)
    surname_candidates = _surname_candidates(report_people)
    canonical_by_key: dict[str, str] = {}
    ambiguous_aliases: list[str] = []
    occurrence_identities: list[tuple[_SpeakerOccurrence, str]] = []
    for occurrence in occurrences:
        parsed = parse_speaker_identity(occurrence.raw_name)
        if not parsed.name or (
            _is_generic_name(parsed.name) and occurrence.origin != "profile"
        ):
            continue
        canonical, ambiguous = _canonical_identity(
            occurrence.raw_name, report_people, surname_candidates,
        )
        canonical_by_key[_name_key(occurrence.raw_name)] = canonical
        if not ambiguous:
            canonical_by_key.setdefault(_name_key(parsed.name), canonical)
        elif parsed.name not in ambiguous_aliases:
            ambiguous_aliases.append(parsed.name)
        canonical_by_key.setdefault(_name_key(canonical), canonical)
        occurrence_identities.append((occurrence, canonical))

    canonical_names = list(dict.fromkeys(canonical_by_key.values()))
    merged: dict[str, ReconciledSpeaker] = {}
    evidence_keys: dict[str, set[tuple[str, float | None, str]]] = {}
    role_ranks: dict[str, int] = {}
    for occurrence, canonical in occurrence_identities:
        parsed = parse_speaker_identity(occurrence.raw_name)
        role = occurrence.role or parsed.honorific
        role_rank = _role_rank(role, honorific=occurrence.role is None and role is not None)
        speaker = ReconciledSpeaker(
            display_name=canonical,
            role=role,
            confidence=occurrence.confidence,
            evidence=[evidence.model_copy(deep=True) for evidence in occurrence.evidence],
            origins=[] if occurrence.origin == "profile" else [occurrence.origin],
        )
        for evidence in speaker.evidence:
            evidence.note = correct_speaker_name_mentions(
                evidence.note, canonical_names, canonical_by_key,
            )
        for origin in _name_origins(speaker.evidence):
            if origin not in speaker.origins:
                speaker.origins.append(origin)
        key = _name_key(speaker.display_name)
        if key not in merged:
            merged[key] = speaker
            role_ranks[key] = role_rank
            evidence_keys[key] = {
                (evidence.kind, evidence.timestamp_seconds, evidence.note)
                for evidence in speaker.evidence
            }
            continue
        current = merged[key]
        if _CONFIDENCE_RANK[speaker.confidence] > _CONFIDENCE_RANK[current.confidence]:
            current.confidence = speaker.confidence
        if role_rank > role_ranks[key] or _adds_compatible_organization(
            current.role, speaker.role,
        ):
            current.role = speaker.role
            role_ranks[key] = role_rank
        for evidence in speaker.evidence:
            evidence_key = (evidence.kind, evidence.timestamp_seconds, evidence.note)
            if evidence_key not in evidence_keys[key]:
                current.evidence.append(evidence)
                evidence_keys[key].add(evidence_key)
        for origin in speaker.origins:
            if origin not in current.origins:
                current.origins.append(origin)
    return SpeakerReconciliation(list(merged.values()), canonical_by_key, ambiguous_aliases)


def reconcile_speakers(report: AcademyReport) -> list[ReconciledSpeaker]:
    """Compatibility wrapper returning the reconciled speaker directory."""
    return reconcile_speakers_detailed(report).speakers


def format_hms_timestamp(seconds: float) -> str:
    """Format a report timestamp using the stable h:mm:ss interchange format."""
    total_seconds = max(0, int(seconds + .5))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"


def build_video_report_payload(report: AcademyReport, guid: UUID) -> dict:
    """Build the factual, one-video JSON interchange document."""
    reconciliation = reconcile_speakers_detailed(report)
    unique_speakers = reconciliation.speakers
    canonical_names = [speaker.display_name for speaker in unique_speakers]
    canonical_by_key = reconciliation.canonical_by_key
    corrected = lambda value: correct_speaker_name_mentions(
        value, canonical_names, canonical_by_key,
    )
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
                    "nota": corrected(evidence.note),
                }
                for evidence in speaker.evidence
            ],
        }
        if speaker.role:
            item["ruolo"] = speaker.role
        speakers.append(item)

    video = {
        "guid": str(guid),
        "titolo_suggerito": corrected(report.title),
        "durata_secondi": int(report.duration_seconds + .5),
        "lingua": report.detected_language,
        "sinossi": corrected(report.synopsis),
    }
    if report.bunny_title:
        video["titolo_bunny"] = corrected(report.bunny_title)

    slides = []
    for slide in report.slides:
        item = {
            "inizio": format_hms_timestamp(slide.timestamp_seconds),
            "testo_principale": corrected(" · ".join(slide.visible_content))[:500],
            "confidenza": _CONFIDENCE_SCORE[slide.confidence],
        }
        if slide.title:
            item["titolo"] = corrected(slide.title)
        slides.append(item)

    interventions = []
    for intervention in report.interventions:
        item = intervention.model_dump(mode="json", by_alias=True, exclude_none=True)
        item["relatori"] = rewrite_speaker_references(item["relatori"], canonical_by_key)
        for field in ("titolo", "sintesi"):
            item[field] = corrected(item[field])
        item["punti_chiave"] = [
            corrected(point)
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
            corrected(item) for item in report.uncertainties
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
    reconciliation = reconcile_speakers_detailed(report)
    speakers = reconciliation.speakers
    canonical_names = [speaker.display_name for speaker in speakers]
    canonical_by_key = reconciliation.canonical_by_key
    corrected = lambda value: correct_speaker_name_mentions(
        value, canonical_names, canonical_by_key,
    )
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
                _csv(rewrite_speaker_references(intervention.relatori, canonical_by_key))
                if intervention.relatori else "Da verificare"
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
