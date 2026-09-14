"""Pure audio alignment; full word evidence remains transient in memory."""

from collections.abc import Sequence
from dataclasses import dataclass, field
import math

from app.media import SilenceInterval
from app.models import AcademyContent, BoundaryEvidence, Intervention, InterventionKind
from app.transcription import TranscriptSegment, TranscriptWord


@dataclass(frozen=True)
class SemanticIntervention:
    tipo: InterventionKind
    relatori: tuple[str, ...]
    titolo: str
    sintesi: str
    punti_chiave: tuple[str, ...]
    confidenza: float
    segments: tuple[TranscriptSegment, ...] = field(repr=False)


@dataclass(frozen=True)
class BoundaryAlignment:
    interventions: list[Intervention]
    boundaries: list[BoundaryEvidence]


def _nearest_second(value: float) -> int:
    """Nearest whole second, resolving exact ties to the preceding second."""
    return math.ceil(value - .5)


def _group_words(group: SemanticIntervention) -> tuple[TranscriptWord, ...]:
    # Payload bounding copies the original word list into each utterance piece.
    # Keep each original word once, without changing its punctuation or spelling.
    unique: dict[tuple, TranscriptWord] = {}
    for segment_index, segment in enumerate(group.segments):
        if segment.end_seconds <= segment.start_seconds:
            raise ValueError("La partizione contiene un segmento vuoto o invertito")
        for word_index, word in enumerate(segment.words):
            if not word.text.strip():
                raise ValueError("La partizione contiene una parola vuota")
            identity = segment.source_utterance_id or (segment_index, word_index)
            key = (identity, word.start_seconds, word.end_seconds, word.diarization_label, word.text)
            unique.setdefault(key, word)
    words = tuple(sorted(unique.values(), key=lambda word: (word.start_seconds, word.end_seconds)))
    if not group.segments or (group.tipo != "pausa" and not words):
        raise ValueError("La partizione parlata richiede evidenze parola per parola")
    return words


def _speech_extent(group: SemanticIntervention, words: Sequence[TranscriptWord]) -> tuple[float, float]:
    if group.tipo == "pausa":
        return min(item.start_seconds for item in group.segments), max(item.end_seconds for item in group.segments)
    return words[0].start_seconds, max(word.end_seconds for word in words)


def _matching_silence(start: float, end: float, intervals: Sequence[SilenceInterval]) -> SilenceInterval | None:
    matches = [interval for interval in intervals
               if min(end, interval.end_seconds) > max(start, interval.start_seconds)]
    return min(
        matches,
        key=lambda item: (
            -(min(end, item.end_seconds) - max(start, item.start_seconds)),
            item.start_seconds,
        ),
    ) if matches else None


def _whole_second(candidate: float, lower: int, upper: int, silence: SilenceInterval | None) -> int:
    if lower > upper:
        raise ValueError("La partizione non ammette un confine intero senza segmenti vuoti")
    if silence is not None:
        inside_lower = max(lower, math.ceil(silence.start_seconds))
        inside_upper = min(upper, math.floor(silence.end_seconds))
        if inside_lower <= inside_upper:
            lower, upper = inside_lower, inside_upper
    return max(lower, min(upper, _nearest_second(candidate)))


def _pause_group() -> SemanticIntervention:
    return SemanticIntervention(
        tipo="pausa", relatori=(), titolo="Pausa o silenzio",
        sintesi="Pausa rilevata nell'audio tra gli interventi.", punti_chiave=(),
        confidenza=1, segments=(),
    )


def _intervention(index: int, group: SemanticIntervention, start: int, end: int) -> Intervention:
    return Intervention(
        id=f"i{index:03d}", start_seconds=start, end_seconds=end,
        tipo=group.tipo, relatori=list(group.relatori), titolo=group.titolo,
        sintesi=group.sintesi, punti_chiave=list(group.punti_chiave), confidenza=group.confidenza,
    )


def _evidence(previous: Intervention, following: Intervention, before: Sequence[TranscriptWord],
              after: Sequence[TranscriptWord], rule: str) -> BoundaryEvidence:
    boundary = int(previous.end_seconds)
    before = () if previous.tipo == "pausa" else before[-5:]
    after = () if following.tipo == "pausa" else after[:5]
    return BoundaryEvidence(
        previous_intervention_id=previous.id, next_intervention_id=following.id,
        boundary_seconds=boundary, words_before=[word.text for word in before],
        words_after=[word.text for word in after],
        pause_before=len(before) < 5 or not any(word.end_seconds > boundary - 1 and word.start_seconds < boundary for word in before),
        pause_after=len(after) < 5 or not any(word.start_seconds < boundary + 1 and word.end_seconds > boundary for word in after),
        rule=rule,
    )


def _complete(interventions: Sequence[Intervention], boundaries: Sequence[BoundaryEvidence], duration: int) -> bool:
    if not interventions or len(boundaries) != len(interventions) - 1:
        return False
    if interventions[0].start_seconds != 0 or interventions[-1].end_seconds != duration:
        return False
    if len({item.id for item in interventions}) != len(interventions):
        return False
    for item in interventions:
        if not item.id.strip() or not (0 <= item.start_seconds < item.end_seconds <= duration):
            return False
        if int(item.start_seconds) != item.start_seconds or int(item.end_seconds) != item.end_seconds:
            return False
    for previous, following, evidence in zip(interventions, interventions[1:], boundaries):
        if (previous.end_seconds != following.start_seconds
                or evidence.boundary_seconds != previous.end_seconds
                or evidence.previous_intervention_id != previous.id
                or evidence.next_intervention_id != following.id):
            return False
        if (previous.tipo == "pausa"
                and (evidence.words_before or not evidence.pause_before)):
            return False
        if (following.tipo == "pausa"
                and (evidence.words_after or not evidence.pause_after)):
            return False
        try:
            BoundaryEvidence.model_validate(evidence.model_dump())
        except ValueError:
            return False
    return True


def has_complete_boundary_evidence(report: AcademyContent) -> bool:
    """Historical content may deserialize without being eligible for export."""
    if not math.isfinite(report.duration_seconds) or report.duration_seconds <= 0:
        return False
    return _complete(report.interventions, report.boundaries, _nearest_second(report.duration_seconds))


def align_intervention_boundaries(
    duration_seconds: float,
    semantic_groups: Sequence[SemanticIntervention],
    silence_intervals: Sequence[SilenceInterval],
) -> BoundaryAlignment:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0 or not semantic_groups:
        raise ValueError("La partizione degli interventi non è valida")
    duration = _nearest_second(duration_seconds)
    if duration <= 0:
        raise ValueError("La partizione richiede almeno un secondo")

    words = [_group_words(group) for group in semantic_groups]
    extents = [_speech_extent(group, group_words) for group, group_words in zip(semantic_groups, words)]
    source_owners: dict[str, int] = {}
    for index, group in enumerate(semantic_groups):
        for segment in group.segments:
            source = segment.source_utterance_id
            if source and source_owners.setdefault(source, index) != index:
                raise ValueError("La partizione divide una stessa utterance sorgente")
        if extents[index][0] >= extents[index][1] or extents[index][1] > duration_seconds:
            raise ValueError("La partizione contiene tempi vocali non validi")

    final_groups = [semantic_groups[0]]
    final_words = [words[0]]
    cuts: list[int] = []
    rules: list[str] = []
    for index in range(len(semantic_groups) - 1):
        previous_end, next_start = extents[index][1], extents[index + 1][0]
        if previous_end > next_start:
            raise ValueError("La partizione contiene parlato sovrapposto non separabile")
        # Quantization may move a word edge by half a second; never move the
        # candidate beyond the quantized speech gap or collapse a final segment.
        lower = max((cuts[-1] if cuts else 0) + 1, _nearest_second(previous_end))
        upper = min(duration - 1, _nearest_second(extents[index + 1][1]) - 1, _nearest_second(next_start))
        if semantic_groups[index + 1].tipo in {"saluti", "domande", "cambio_relatore"}:
            upper = min(upper, math.ceil(next_start) - 1)
        silence = _matching_silence(previous_end, next_start, silence_intervals)
        rule = "no_pause" if silence is None else "short_pause"
        candidate = previous_end if silence is None else (silence.start_seconds + silence.end_seconds) / 2
        if silence is not None and silence.end_seconds - silence.start_seconds >= 2:
            rule = "long_pause"
            left = _whole_second(previous_end + 1, lower, upper, silence)
            right = _whole_second(next_start - 1, lower, upper, silence)
            if right - left >= 1 and semantic_groups[index].tipo != "pausa" and semantic_groups[index + 1].tipo != "pausa":
                cuts.append(left)
                rules.append(rule)
                final_groups.append(_pause_group())
                final_words.append(())
                candidate = right
        cuts.append(_whole_second(candidate, lower, upper, silence))
        rules.append(rule)
        final_groups.append(semantic_groups[index + 1])
        final_words.append(words[index + 1])

    endpoints = [0, *cuts, duration]
    interventions = [
        _intervention(index + 1, group, endpoints[index], endpoints[index + 1])
        for index, group in enumerate(final_groups)
    ]
    boundaries = [
        _evidence(previous, following, final_words[index], final_words[index + 1], rules[index])
        for index, (previous, following) in enumerate(zip(interventions, interventions[1:]))
    ]
    if not _complete(interventions, boundaries, duration):
        raise ValueError("La partizione non ha confini adiacenti con prova completa")
    return BoundaryAlignment(interventions=interventions, boundaries=boundaries)
