from dataclasses import replace

import pytest

from app.boundaries import (
    SemanticIntervention, align_intervention_boundaries, has_complete_boundary_evidence,
)
from app.media import SilenceInterval
from app.models import AcademyContent
from app.transcription import TranscriptSegment, TranscriptWord


def group(start, end, texts=("parola",), *, kind="intervento", source=""):
    words = [TranscriptWord(
        text=text, start_seconds=start + (end - start) * index / len(texts),
        end_seconds=start + (end - start) * (index + 1) / len(texts),
        diarization_label="A", confidence=.9,
    ) for index, text in enumerate(texts)]
    segment = TranscriptSegment(
        start_seconds=start, end_seconds=end, text=" ".join(texts),
        diarization_label="A", source_utterance_id=source, words=words,
    )
    return SemanticIntervention(
        tipo=kind, relatori=("Mario Rossi",), titolo="Tema", sintesi="Sintesi",
        punti_chiave=("Uno", "Due", "Tre") if kind == "intervento" else (),
        confidenza=.9, segments=(segment,),
    )


def report_for(alignment, duration=40):
    return AcademyContent(
        title="Corso", duration_seconds=duration, detected_language="it", synopsis="",
        speakers=[], slides=[], uncertainties=[], interventions=alignment.interventions,
        boundaries=alignment.boundaries, audio_boundary_version=1,
    )


def test_single_historical_intervention_without_provenance_is_not_verified():
    report = report_for(align_intervention_boundaries(40, [group(0, 39)], []))
    report = AcademyContent.model_validate(report.model_dump(exclude={"audio_boundary_version"}))
    assert report.boundaries == []
    assert not has_complete_boundary_evidence(report)


@pytest.mark.parametrize("end,start,silences,expected,rule", [
    (10.2, 13, [SilenceInterval(10.3, 12.9)], [(0, 11), (11, 12), (12, 40)], "long_pause"),
    (20.1, 21.5, [SilenceInterval(20.2, 21.4)], [(0, 21), (21, 40)], "short_pause"),
    (30.4, 31, [], [(0, 30), (30, 40)], "no_pause"),
    (20, 23, [SilenceInterval(20.6, 22.4)], [(0, 21), (21, 40)], "short_pause"),
    (20.5, 23, [], [(0, 20), (20, 40)], "no_pause"),
    (10.2, 12.6, [SilenceInterval(10.3, 12.5)], [(0, 11), (11, 12), (12, 40)], "long_pause"),
    (10.4, 12.5, [SilenceInterval(10.4, 12.5)], [(0, 11), (11, 40)], "long_pause"),
])
def test_alignment_rules_whole_seconds_and_complete_adjacency(end, start, silences, expected, rule):
    result = align_intervention_boundaries(40.2, [group(1, end), group(start, 39)], silences)

    assert [(item.start_seconds, item.end_seconds) for item in result.interventions] == expected
    assert len(result.boundaries) == len(result.interventions) - 1
    assert all(item.rule == rule for item in result.boundaries)
    assert has_complete_boundary_evidence(report_for(result, 40.2))
    if len(expected) == 3:
        assert result.interventions[1].tipo == "pausa"
        assert result.boundaries[0].words_after == []
        assert result.boundaries[0].pause_after is True
        assert result.boundaries[1].words_before == []
        assert result.boundaries[1].pause_before is True


def test_prefers_integer_inside_measured_silence():
    result = align_intervention_boundaries(
        30, [group(0, 10), group(12.1, 29)], [SilenceInterval(10.9, 12.05)],
    )
    assert result.boundaries[0].boundary_seconds == 11


def test_partial_overlap_preserves_original_silence_duration_for_long_pause():
    result = align_intervention_boundaries(
        20,
        [group(0, 10.2), group(13, 19)],
        [SilenceInterval(9.5, 12)],
    )

    assert [(item.start_seconds, item.end_seconds, item.tipo) for item in result.interventions] == [
        (0, 11, "intervento"),
        (11, 12, "pausa"),
        (12, 20, "intervento"),
    ]
    assert [item.rule for item in result.boundaries] == ["long_pause", "long_pause"]


@pytest.mark.parametrize("texts,before,after", [
    (("uno,",), ["uno,"], ["uno,"]),
    (("uno,", "due", "tre", "quattro!"), ["uno,", "due", "tre", "quattro!"], ["uno,", "due", "tre", "quattro!"]),
    (("uno,", "due", "tre", "quattro", "cinque!"), ["uno,", "due", "tre", "quattro", "cinque!"], ["uno,", "due", "tre", "quattro", "cinque!"]),
    (("uno,", "due", "tre", "quattro", "cinque", "sei!"), ["due", "tre", "quattro", "cinque", "sei!"], ["uno,", "due", "tre", "quattro", "cinque"]),
])
def test_evidence_keeps_nearest_five_verbatim_words(texts, before, after):
    first, second = group(0, 10, texts), group(10, 20, texts)
    result = align_intervention_boundaries(20, [first, second], [])
    evidence = result.boundaries[0]
    assert evidence.words_before == before
    assert evidence.words_after == after
    assert evidence.pause_before is (len(texts) < 5)
    assert evidence.pause_after is (len(texts) < 5)
    assert [word.text for word in first.segments[0].words] == list(texts)


def test_word_deduplication_applies_only_to_copies_of_the_same_source_utterance():
    first = group(0, 10, ("eco",), source="u1")
    copied_piece = first.segments[0].model_copy(update={"start_seconds": 5})
    distinct = group(0, 10, ("eco",), source="u2")
    combined = replace(first, segments=(first.segments[0], copied_piece, distinct.segments[0]))

    result = align_intervention_boundaries(20, [combined, group(12, 19)], [])

    assert result.boundaries[0].words_before == ["eco", "eco"]


def test_absence_of_words_in_adjacent_second_sets_pause_flags():
    texts = ("uno", "due", "tre", "quattro", "cinque")
    result = align_intervention_boundaries(
        30, [group(0, 10, texts), group(13, 30, texts)], [SilenceInterval(11, 12.9)],
    )
    assert result.boundaries[0].pause_before is True
    assert result.boundaries[0].pause_after is True


def test_continued_example_is_not_split_at_internal_utterances_or_silences():
    first = group(0, 5, source="u1")
    continued = replace(first, segments=first.segments + group(7, 10, source="u2").segments + group(12, 15, source="u3").segments)
    result = align_intervention_boundaries(16, [continued], [SilenceInterval(5, 7), SilenceInterval(10, 12)])
    assert [(item.start_seconds, item.end_seconds) for item in result.interventions] == [(0, 16)]
    assert result.boundaries == []


@pytest.mark.parametrize("silences", [[], [SilenceInterval(9, 10)]])
def test_ai_pause_cannot_discard_word_bearing_utterance(silences):
    with pytest.raises(ValueError, match="partizione"):
        align_intervention_boundaries(20, [
            group(0, 9, ("Spiegazione", "effettivamente", "trascritta."), kind="pausa"),
            group(10, 19, ("Grazie",), kind="saluti"),
        ], silences)


def test_ai_wordless_pause_requires_measured_generation():
    with pytest.raises(ValueError, match="partizione"):
        align_intervention_boundaries(20, [group(0, 9, (), kind="pausa"), group(10, 19)], [])


def test_moderator_is_preserved_between_speakers():
    result = align_intervention_boundaries(30, [group(0, 10), group(11, 13, kind="cambio_relatore"), group(14, 29)], [])
    assert [item.tipo for item in result.interventions] == ["intervento", "cambio_relatore", "intervento"]
    assert [item.boundary_seconds for item in result.boundaries] == [10, 13]
    assert result.boundaries[0].boundary_seconds < 11


def test_fractional_moderator_start_fails_when_no_prior_valid_second():
    with pytest.raises(ValueError, match="confine intero"):
        align_intervention_boundaries(
            20,
            [group(0, 10.6), group(10.7, 13, kind="cambio_relatore"), group(14, 19)],
            [],
        )


def test_nonfinal_group_cannot_extend_into_terminal_provider_rounding_tolerance():
    with pytest.raises(ValueError, match="tempi vocali"):
        align_intervention_boundaries(
            5789,
            [group(0, 5789.4), group(5789.5, 5790)],
            [],
        )


def test_terminal_group_must_contain_speech_before_bunny_end():
    with pytest.raises(ValueError, match="tempi vocali"):
        align_intervention_boundaries(
            5789,
            [group(0, 10), group(5789, 5789.5)],
            [],
        )


@pytest.mark.parametrize("groups,duration", [
    ([], 10),
    ([group(0, 10, ())], 10),
    ([group(0, 1), group(1, 1.2), group(1.2, 2)], 2),
    ([group(0, 10), group(9, 20)], 20),
    ([group(0, 10, source="same"), group(11, 20, source="same")], 20),
    ([group(10, 20), group(0, 5)], 20),
    ([group(0, 10)], float("nan")),
    ([group(0, 10)], .1),
])
def test_invalid_alignment_fails_closed(groups, duration):
    with pytest.raises(ValueError):
        align_intervention_boundaries(duration, groups, [])


@pytest.mark.parametrize("change", ["missing", "duplicate", "reordered", "wrong-second", "nonconsecutive", "gap", "duplicate-id"])
def test_completeness_rejects_invalid_evidence_or_timeline(change):
    report = report_for(align_intervention_boundaries(40, [group(0, 10), group(12, 20), group(22, 39)], []))
    if change == "missing":
        report.boundaries.pop()
    elif change == "duplicate":
        report.boundaries.append(report.boundaries[0])
    elif change == "reordered":
        report.boundaries.reverse()
    elif change == "wrong-second":
        report.boundaries[0].boundary_seconds += 1
    elif change == "nonconsecutive":
        report.boundaries[0].next_intervention_id = "i003"
    elif change == "gap":
        report.interventions[1].start_seconds += 1
    else:
        report.interventions[2].id = "i001"
    assert not has_complete_boundary_evidence(report)


def test_completeness_rejects_false_pause_flag_for_short_evidence():
    report = report_for(align_intervention_boundaries(20, [group(0, 10), group(12, 19)], []), 20)
    report.boundaries[0].pause_before = False

    assert not has_complete_boundary_evidence(report)


@pytest.mark.parametrize(
    "boundary_index,words_field",
    [(0, "words_after"), (1, "words_before")],
)
def test_completeness_rejects_words_on_a_generated_pause_side(boundary_index, words_field):
    report = report_for(align_intervention_boundaries(
        20,
        [group(0, 10.2), group(13, 19)],
        [SilenceInterval(10.3, 12.9)],
    ), 20)
    setattr(report.boundaries[boundary_index], words_field, ["inventata"] * 5)

    assert not has_complete_boundary_evidence(report)
