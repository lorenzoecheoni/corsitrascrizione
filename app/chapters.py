"""Deterministic editorial planning over complete semantic units, in memory.

Semantic analysis owns complete-thought boundaries. Slides may describe an
existing boundary; this module never splits words, sentences, or semantic units.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
import math
import re

from app.boundaries import SemanticIntervention, _group_words, _speech_extent
from app.models import SlideChange


PREFERRED_MIN = 480
PREFERRED_TARGET = 540
PREFERRED_MAX = 600
ACCEPTABLE_MAX = 900
SOFT_MAX = 1200


@dataclass(frozen=True)
class PlannedBlock:
    id: str
    start_seconds: float
    end_seconds: float
    group_indexes: tuple[int, ...]


@dataclass(frozen=True)
class PlannedTimeline:
    groups: tuple[SemanticIntervention, ...]
    blocks: tuple[PlannedBlock, ...]


def duration_penalty(seconds: float) -> float:
    if PREFERRED_MIN <= seconds <= PREFERRED_MAX:
        return abs(seconds - PREFERRED_TARGET)
    if seconds < PREFERRED_MIN:
        return 5000 + (PREFERRED_MIN - seconds) * 8
    if seconds <= ACCEPTABLE_MAX:
        return 1000 + (seconds - PREFERRED_MAX) * 2
    if seconds <= SOFT_MAX:
        return 3000 + (seconds - ACCEPTABLE_MAX) * 6
    return float("inf")


def _cuts(extents: Sequence[tuple[float, float]]) -> tuple[int, ...]:
    # The comparison key specifies every tie: cost, count, earliest cut indexes.
    best: list[tuple[float, int, tuple[int, ...]] | None] = [(0, 0, ())]
    for end in range(1, len(extents) + 1):
        candidates = []
        for start in range(end):
            seconds = extents[end - 1][1] - extents[start][0]
            penalty = duration_penalty(seconds)
            if math.isinf(penalty):
                if end - start != 1:
                    continue
                # An indivisible long unit is mandatory in every valid path;
                # its fixed cost cannot influence the remaining chapter cuts.
                penalty = 0
            previous = best[start]
            if previous is not None:
                candidates.append((previous[0] + penalty, previous[1] + 1,
                                   (*previous[2], end)))
        best.append(min(candidates) if candidates else None)
    return best[-1][2] if best[-1] is not None else ()


_GENERIC_SLIDE = re.compile(
    r"(?:slide|pagina|page|diapositiva)(?:\s*\d+)?|titolo|presentazione|untitled",
    re.IGNORECASE,
)


def _opening_slide(slides: Sequence[SlideChange], start: float) -> SlideChange | None:
    return next((slide for slide in slides
                 if abs(slide.timestamp_seconds - start) <= 30
                 and slide.confidence == "alta" and slide.title and slide.title.strip()
                 and not _GENERIC_SLIDE.fullmatch(slide.title.strip())), None)


def _chapter(units: Sequence[SemanticIntervention], block_id: str, number: int,
             count: int, slides: Sequence[SlideChange]) -> SemanticIntervention:
    start, _ = _speech_extent(_group_words(units[0]))
    slide = _opening_slide(slides, start)
    longest = max(units, key=lambda unit: unit.raw_duration)
    reason = "inizio_blocco" if number == 1 else "cambio_tema"
    if units[0].tipo == "cambio_relatore":
        reason = "cambio_relatore"
    elif number > 1 and slide is not None:
        reason = "slide_e_tema"
    chapter = replace(
        units[0], block_id=block_id, chapter_number=number, chapters_in_block=count,
        boundary_reason=reason,
        slide_hint_seconds=slide.timestamp_seconds if slide is not None else None,
        titolo=slide.title.strip() if slide is not None else longest.titolo,
        sintesi=" ".join(dict.fromkeys(unit.sintesi for unit in units)),
        punti_chiave=tuple(dict.fromkeys(point for unit in units for point in unit.punti_chiave))[:7],
        relatori=tuple(dict.fromkeys(name for unit in units for name in unit.relatori)),
        confidenza=min(unit.confidenza for unit in units),
        segments=tuple(segment for unit in units for segment in unit.segments),
    )
    return replace(chapter, long=chapter.raw_duration > SOFT_MAX)


def plan_semantic_timeline(groups: Sequence[SemanticIntervention],
                           slides: Sequence[SlideChange]) -> PlannedTimeline:
    """Preserve factual voice blocks, then optimize only their semantic seams.

    Inputs must be word-backed units. Only the audio aligner can create a
    measured pause; an AI-declared pause fails closed in ``_group_words``.
    Every didactic unit must supply at least three distinct key points; the
    upstream draft validator should enforce this before regeneration completes.
    Bunny duration is checked by the aligner, which owns final endpoints.
    """
    if not groups:
        return PlannedTimeline(groups=(), blocks=())
    if any(group.tipo == "intervento" and len(set(group.punti_chiave)) < 3
           for group in groups):
        raise ValueError("Un'unità didattica richiede almeno tre punti chiave distinti")
    words = [_group_words(group) for group in groups]
    extents = [_speech_extent(items) for items in words]
    if any(left[1] > right[0] for left, right in zip(extents, extents[1:])):
        raise ValueError("La partizione contiene parlato sovrapposto non separabile")
    labels = [tuple(dict.fromkeys(word.diarization_label for word in items)) for items in words]
    ordered_slides = sorted(slides, key=lambda slide: (slide.timestamp_seconds, slide.title or ""))
    planned: list[SemanticIntervention] = []
    blocks: list[PlannedBlock] = []
    start = 0
    while start < len(groups):
        if groups[start].tipo == "logistica":
            planned.append(replace(groups[start], block_id=None, chapter_number=None,
                                   chapters_in_block=None, slide_hint_seconds=None))
            start += 1
            continue
        end = start + 1
        while (end < len(groups) and groups[start].tipo == groups[end].tipo == "intervento"
               and labels[end] == labels[start]):
            end += 1
        cuts = _cuts(extents[start:end])
        block_id = f"b{len(blocks) + 1:03d}"
        indexes = []
        previous = 0
        for number, cut in enumerate(cuts, 1):
            indexes.append(len(planned))
            planned.append(_chapter(groups[start + previous:start + cut], block_id,
                                    number, len(cuts), ordered_slides))
            previous = cut
        blocks.append(PlannedBlock(block_id, extents[start][0], extents[end - 1][1], tuple(indexes)))
        start = end
    return PlannedTimeline(groups=tuple(planned), blocks=tuple(blocks))
