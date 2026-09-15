from dataclasses import replace

import pytest

from app.boundaries import SemanticIntervention, align_intervention_boundaries
from app.chapters import plan_semantic_timeline
from app.models import SlideChange
from app.transcription import TranscriptSegment, TranscriptWord


def topic_unit(start, end, speaker="Furio D’Andrea", label="A", kind="intervento"):
    words = [
        TranscriptWord(text="Apertura.", start_seconds=start, end_seconds=start + .4,
                       diarization_label=label, confidence=.99),
        TranscriptWord(text="Chiusura.", start_seconds=end - .4, end_seconds=end,
                       diarization_label=label, confidence=.99),
    ]
    source = TranscriptSegment(
        start_seconds=start, end_seconds=end, diarization_label=label,
        text="Apertura. Chiusura.", source_utterance_id=f"u-{start}", words=words,
    )
    return SemanticIntervention(
        tipo=kind, relatori=(speaker,), titolo=f"Tema {start}",
        sintesi=f"Contenuto didattico da {start} a {end}.",
        punti_chiave=("Primo", "Secondo", "Terzo") if kind == "intervento" else (),
        confidenza=.95, segments=(source,), boundary_reason="cambio_tema",
    )


def test_long_speaker_block_is_preserved_and_partitioned_near_nine_minutes():
    units = [topic_unit(i * 180, (i + 1) * 180) for i in range(13)]
    plan = plan_semantic_timeline(units, slides=[
        SlideChange(timestamp_seconds=1280, title="Poteri", confidence="alta"),
        SlideChange(timestamp_seconds=1369, title="Decisioni assembleari", confidence="alta"),
        SlideChange(timestamp_seconds=2546, title="Direttive", confidence="alta"),
    ])
    assert len(plan.blocks) == 1
    assert (plan.blocks[0].start_seconds, plan.blocks[0].end_seconds) == (0, 2340)
    assert all(chapter.block_id == "b001" for chapter in plan.groups)
    assert all(480 <= chapter.raw_duration <= 900 for chapter in plan.groups)
    assert [chapter.chapter_number for chapter in plan.groups] == [1, 2, 3, 4]
    assert [chapter.chapters_in_block for chapter in plan.groups] == [4, 4, 4, 4]
    assert plan.blocks[0].group_indexes == (0, 1, 2, 3)
    assert [segment for chapter in plan.groups for segment in chapter.segments] == [
        unit.segments[0] for unit in units
    ]


def test_indivisible_long_unit_is_retained_even_with_internal_slides():
    unit = topic_unit(0, 1560)
    plan = plan_semantic_timeline([unit], [
        SlideChange(timestamp_seconds=540, title="Nuovo tema", confidence="alta"),
    ])
    assert len(plan.groups) == 1
    assert plan.groups[0].long is True
    assert plan.groups[0].raw_duration == 1560
    assert plan.groups[0].segments == unit.segments


def test_moderator_and_logistics_end_voice_blocks_and_alias_does_not():
    units = [topic_unit(0, 180), topic_unit(180, 540, "F. D’Andrea"),
             topic_unit(540, 550, "Moderatore", "M", "cambio_relatore"),
             topic_unit(550, 1090), topic_unit(1090, 1100, kind="logistica"),
             topic_unit(1100, 1640)]
    plan = plan_semantic_timeline(units, [])
    assert [item.block_id for item in plan.groups] == ["b001", "b002", "b003", None, "b004"]
    assert [block.group_indexes for block in plan.blocks] == [(0,), (1,), (2,), (4,)]
    assert plan.groups[0].relatori == ("Furio D’Andrea", "F. D’Andrea")


def test_diarization_change_splits_block_even_when_display_name_matches():
    plan = plan_semantic_timeline([topic_unit(0, 540), topic_unit(540, 1080, label="B")], [])
    assert [item.block_id for item in plan.groups] == ["b001", "b002"]


def test_deterministic_ties_choose_earliest_cut_and_same_metadata():
    units = [topic_unit(i * 180, (i + 1) * 180) for i in range(13)]
    first = plan_semantic_timeline(units, [])
    assert first == plan_semantic_timeline(units, [])
    assert [chapter.raw_duration for chapter in first.groups] == [540, 540, 540, 720]


def test_nearby_slide_marks_semantic_boundary_without_moving_it():
    units = [topic_unit(0, 540), topic_unit(540, 1080)]
    plan = plan_semantic_timeline(units, [
        SlideChange(timestamp_seconds=565, title="Decisioni assembleari", confidence="alta"),
    ])
    assert len(plan.groups) == 2
    assert plan.groups[1].segments[0].start_seconds == 540
    assert plan.groups[1].boundary_reason == "slide_e_tema"
    assert plan.groups[1].slide_hint_seconds == 565
    assert plan.groups[1].titolo == "Decisioni assembleari"


def test_titles_summaries_and_key_points_derive_from_constituent_evidence():
    units = [topic_unit(0, 180), topic_unit(180, 540)]
    units[1] = replace(units[1], punti_chiave=("Terzo", "Quarto", "Quinto", "Sesto", "Settimo", "Ottavo"))
    plan = plan_semantic_timeline(units, [
        SlideChange(timestamp_seconds=0, title="Slide 1", confidence="alta"),
        SlideChange(timestamp_seconds=1, title="Titolo incerto", confidence="bassa"),
    ])
    assert plan.groups[0].titolo == "Tema 180"
    assert plan.groups[0].sintesi == "Contenuto didattico da 0 a 180. Contenuto didattico da 180 a 540."
    assert plan.groups[0].punti_chiave == ("Primo", "Secondo", "Terzo", "Quarto", "Quinto", "Sesto", "Settimo")
    aligned = align_intervention_boundaries(540, plan.groups, [])
    assert aligned.blocks[0].titolo == "Tema 180"
    assert aligned.blocks[0].sinossi == plan.groups[0].sintesi


def test_planner_rejects_overlapping_semantic_units_before_merging():
    with pytest.raises(ValueError, match="sovrapposto"):
        plan_semantic_timeline([topic_unit(0, 600), topic_unit(590, 700)], [])
