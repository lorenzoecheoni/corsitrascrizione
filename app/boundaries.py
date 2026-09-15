"""Pure audio alignment; full word evidence remains transient in memory."""

from collections.abc import Sequence
from dataclasses import dataclass, field
import math

from app.media import SilenceInterval
from app.models import (
    AcademyContent, BoundaryEvidence, BoundaryReason, ChapterBoundaryOrigin,
    Intervention, InterventionKind, SpeechBlock,
)
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
    block_id: str | None = None
    chapter_number: int | None = None
    chapters_in_block: int | None = None
    boundary_reason: BoundaryReason = "cambio_tema"
    slide_hint_seconds: float | None = None
    long: bool = False

    @property
    def raw_duration(self) -> float:
        start, end = _speech_extent(_group_words(self))
        return end - start


@dataclass(frozen=True)
class BoundaryAlignment:
    interventions: list[Intervention]
    boundaries: list[BoundaryEvidence]
    blocks: list[SpeechBlock] = field(default_factory=list)


def bunny_end_second(duration_seconds: float) -> int:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("Durata Bunny non valida")
    return math.floor(duration_seconds)


def nearest_second(value: float) -> int:
    """Nearest whole second, resolving exact ties to the preceding second."""
    return math.ceil(value - .5)


def _group_words(group: SemanticIntervention) -> tuple[TranscriptWord, ...]:
    if group.tipo == "pausa":
        raise ValueError("La partizione parlata non può dichiarare una pausa")
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
    if not group.segments or not words:
        raise ValueError("La partizione parlata richiede evidenze parola per parola")
    return words


def _speech_extent(words: Sequence[TranscriptWord]) -> tuple[float, float]:
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
    return max(lower, min(upper, nearest_second(candidate)))


def _pause_group() -> SemanticIntervention:
    return SemanticIntervention(
        tipo="pausa", relatori=(), titolo="Pausa o silenzio",
        sintesi="Pausa rilevata nell'audio tra gli interventi.", punti_chiave=(),
        confidenza=1, segments=(),
    )


def _intervention(index: int, group: SemanticIntervention, start: int, end: int,
                  rule: str) -> Intervention:
    return Intervention(
        id=f"i{index:03d}", start_seconds=start, end_seconds=end,
        tipo=group.tipo, relatori=list(group.relatori), titolo=group.titolo,
        sintesi=group.sintesi, punti_chiave=list(group.punti_chiave), confidenza=group.confidenza,
        block_id=group.block_id, chapter_number=group.chapter_number,
        chapters_in_block=group.chapters_in_block,
        boundary_origin=ChapterBoundaryOrigin(
            motivo_editoriale=group.boundary_reason, regola_audio=rule,
            slide_indizio_seconds=group.slide_hint_seconds,
        ) if group.block_id is not None else None,
    )


def _aligned_blocks(interventions: Sequence[Intervention]) -> list[SpeechBlock]:
    children: dict[str, list[Intervention]] = {}
    for index, item in enumerate(interventions):
        if item.block_id is None:
            if item.chapter_number is not None or item.chapters_in_block is not None:
                raise ValueError("La partizione dei blocchi non è valida")
            continue
        if (not item.block_id.strip() or item.tipo in {"pausa", "logistica"}
                or (item.block_id in children
                    and interventions[index - 1].block_id != item.block_id)):
            raise ValueError("La partizione dei blocchi non è valida")
        children.setdefault(item.block_id, []).append(item)
    blocks = []
    for block_id, chapters in children.items():
        if (any(chapter.chapter_number != index + 1
                or chapter.chapters_in_block != len(chapters)
                or chapter.tipo != chapters[0].tipo
                for index, chapter in enumerate(chapters))
                or any(left.end_seconds != right.start_seconds
                       for left, right in zip(chapters, chapters[1:]))):
            raise ValueError("La partizione dei blocchi non è valida")
        longest = max(chapters, key=lambda chapter: chapter.end_seconds - chapter.start_seconds)
        blocks.append(SpeechBlock(
            id=block_id, start_seconds=chapters[0].start_seconds,
            end_seconds=chapters[-1].end_seconds, tipo=chapters[0].tipo,
            relatori=list(dict.fromkeys(name for chapter in chapters for name in chapter.relatori)),
            titolo=longest.titolo,
            sinossi=" ".join(dict.fromkeys(chapter.sintesi for chapter in chapters)),
        ))
    return blocks


def _evidence(previous: Intervention, following: Intervention, before: Sequence[TranscriptWord],
              after: Sequence[TranscriptWord], rule: str) -> BoundaryEvidence:
    boundary = int(previous.end_seconds)
    before = before[-5:]
    after = after[:5]
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
    if type(report.audio_boundary_version) is not int or report.audio_boundary_version != 1:
        return False
    if not math.isfinite(report.duration_seconds) or report.duration_seconds <= 0:
        return False
    duration = bunny_end_second(report.duration_seconds)
    if any(slide.timestamp_seconds > duration for slide in report.slides):
        return False
    if any(block.end_seconds > duration for block in report.speech_blocks):
        return False
    if any(item.boundary_origin is not None
           and item.boundary_origin.slide_indizio_seconds is not None
           and item.boundary_origin.slide_indizio_seconds > duration
           for item in report.interventions):
        return False
    return _complete(report.interventions, report.boundaries, duration)


def align_intervention_boundaries(
    duration_seconds: float,
    semantic_groups: Sequence[SemanticIntervention],
    silence_intervals: Sequence[SilenceInterval],
) -> BoundaryAlignment:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0 or not semantic_groups:
        raise ValueError("La partizione degli interventi non è valida")
    duration = bunny_end_second(duration_seconds)
    if duration <= 0:
        raise ValueError("La partizione richiede almeno un secondo")

    words = [_group_words(group) for group in semantic_groups]
    extents = [_speech_extent(group_words) for group_words in words]
    for index, group in enumerate(semantic_groups):
        speech_start, speech_end = extents[index]
        is_terminal_group = index == len(semantic_groups) - 1
        # Only AssemblyAI's final rounding can exceed Bunny's source timeline,
        # and only by one second. Internal groups stay strictly bounded so no
        # generated cut can land outside the actual video. The terminal group
        # must still contain speech before Bunny's end even though its final
        # word may extend into the accepted provider rounding second.
        word_start_limit = duration_seconds if is_terminal_group else duration
        if (
            speech_start >= speech_end
            or speech_start >= word_start_limit
            or speech_end > (duration_seconds + 1 if is_terminal_group else duration)
            or any(word.start_seconds >= word_start_limit for word in words[index])
        ):
            raise ValueError("La partizione contiene tempi vocali non validi")
        # Keep the provider's original words untouched. Only the effective
        # terminal extent is limited to Bunny's authoritative whole-second end.
        if is_terminal_group:
            effective_end = min(speech_end, duration)
            if speech_start >= effective_end:
                raise ValueError("La partizione contiene tempi vocali non validi")
            extents[index] = (speech_start, effective_end)
        if (group.slide_hint_seconds is not None
                and (not math.isfinite(group.slide_hint_seconds)
                     or not 0 <= group.slide_hint_seconds <= duration)):
            raise ValueError("La partizione contiene un indizio slide fuori durata")

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
        lower = max((cuts[-1] if cuts else 0) + 1, nearest_second(previous_end))
        upper = min(duration - 1, nearest_second(extents[index + 1][1]) - 1, nearest_second(next_start))
        if semantic_groups[index + 1].tipo in {"saluti", "domande", "cambio_relatore"}:
            before_next_speech = math.ceil(next_start) - 1
            # When the previous word ends exactly as moderator speech starts,
            # no earlier whole second exists. The no-pause rule requires the
            # coincident word edge instead of collapsing either segment.
            if before_next_speech >= lower:
                upper = min(upper, before_next_speech)
            elif not next_start.is_integer() or lower != int(next_start):
                raise ValueError(
                    "La partizione non ammette un confine intero senza segmenti vuoti"
                )
        silence = _matching_silence(previous_end, next_start, silence_intervals)
        rule = "no_pause" if silence is None else "short_pause"
        candidate = previous_end if silence is None else previous_end + (
            silence.end_seconds - silence.start_seconds
        ) / 2
        if silence is not None and silence.end_seconds - silence.start_seconds >= 2:
            rule = "long_pause"
            left = _whole_second(previous_end + 1, lower, upper, silence)
            right = _whole_second(next_start - 1, lower, upper, silence)
            candidate = left
            same_block = (semantic_groups[index].block_id is not None
                          and semantic_groups[index].block_id == semantic_groups[index + 1].block_id)
            if right - left >= 1 and not same_block:
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
        _intervention(index + 1, group, endpoints[index], endpoints[index + 1],
                      rules[index - 1] if index else "no_pause")
        for index, group in enumerate(final_groups)
    ]
    boundaries = [
        _evidence(previous, following, final_words[index], final_words[index + 1], rules[index])
        for index, (previous, following) in enumerate(zip(interventions, interventions[1:]))
    ]
    if not _complete(interventions, boundaries, duration):
        raise ValueError("La partizione non ha confini adiacenti con prova completa")
    return BoundaryAlignment(interventions=interventions, boundaries=boundaries,
                             blocks=_aligned_blocks(interventions))
