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


def test_word_aligned_atoms_preserve_each_word_once_for_boundary_evidence():
    from app.analysis_chunks import split_transcript_atoms

    words = [TranscriptWord(
        text=f"parola-{index}", start_seconds=index * 10, end_seconds=index * 10 + .4,
        diarization_label="A", confidence=.9,
    ) for index in range(12)]
    source = TranscriptSegment(
        start_seconds=0, end_seconds=111, diarization_label="A",
        text=" ".join(word.text for word in words), source_utterance_id="assembly-u000001",
        words=words,
    )
    split_group = SemanticIntervention(
        tipo="intervento", relatori=("Mario Rossi",), titolo="Tema", sintesi="Sintesi",
        punti_chiave=("Uno", "Due", "Tre"), confidenza=.9,
        segments=tuple(split_transcript_atoms(source, max_seconds=90)),
    )

    result = align_intervention_boundaries(140, [split_group, group(120, 139)], [])

    assert result.boundaries[0].words_before == [
        "parola-7", "parola-8", "parola-9", "parola-10", "parola-11",
    ]


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


@pytest.mark.parametrize("end,start,silences,expected,rule", [
    (540.2, 543, [SilenceInterval(540.3, 542.9)], 541, "long_pause"),
    (540.1, 541.5, [SilenceInterval(540.2, 541.4)], 541, "short_pause"),
    (540.4, 541, [], 540, "no_pause"),
])
def test_chapters_in_same_block_share_audio_cut_and_origin_without_pause(end, start, silences, expected, rule):
    groups = [
        replace(group(0, end), block_id="b001", chapter_number=1, chapters_in_block=2,
                boundary_reason="inizio_blocco"),
        replace(group(start, 1080), block_id="b001", chapter_number=2, chapters_in_block=2,
                boundary_reason="slide_e_tema", slide_hint_seconds=545),
    ]
    result = align_intervention_boundaries(1080, groups, silences)
    assert [(item.start_seconds, item.end_seconds) for item in result.interventions] == [(0, expected), (expected, 1080)]
    assert len(result.blocks) == 1
    assert (result.blocks[0].start_seconds, result.blocks[0].end_seconds) == (0, 1080)
    assert result.interventions[1].boundary_origin.model_dump() == {
        "motivo_editoriale": "slide_e_tema", "regola_audio": rule, "slide_indizio_seconds": 545,
    }


def test_bunny_fractional_duration_floors_export_and_terminal_provider_rounding():
    original = group(0, 5789.496)
    result = align_intervention_boundaries(5789.248, [original], [])
    assert result.interventions[-1].end_seconds == 5789
    assert original.segments[-1].words[-1].end_seconds == 5789.496
    assert has_complete_boundary_evidence(report_for(result, 5789.248))
    rounded_up = align_intervention_boundaries(5789.9, [group(0, 5789.496)], [])
    assert rounded_up.interventions[-1].end_seconds == 5789
    assert has_complete_boundary_evidence(report_for(rounded_up, 5789.9))


@pytest.mark.parametrize("duration,word_start,word_end,expected", [
    (1080.9, 1080.8, 1080.9, 1080),
    (5789.248, 5789.148, 5789.496, 5789),
])
def test_terminal_word_in_bunny_fractional_tail_preserves_raw_evidence(duration, word_start, word_end, expected):
    terminal_word = TranscriptWord(
        text="Conclusione.", start_seconds=word_start, end_seconds=word_end,
        diarization_label="A", confidence=.99,
    )
    first_word = TranscriptWord(
        text="Apertura.", start_seconds=0, end_seconds=.4,
        diarization_label="A", confidence=.99,
    )
    source = TranscriptSegment(
        start_seconds=0, end_seconds=word_end, text="Apertura. Conclusione.",
        diarization_label="A", source_utterance_id="u1", words=[first_word, terminal_word],
    )
    original = replace(group(0, 1), segments=(source,), block_id="b001",
                       chapter_number=1, chapters_in_block=1)
    before = source.model_dump()

    result = align_intervention_boundaries(duration, [original], [])

    assert [(item.start_seconds, item.end_seconds) for item in result.interventions] == [(0, expected)]
    assert [(block.start_seconds, block.end_seconds) for block in result.blocks] == [(0, expected)]
    assert source.model_dump() == before
    assert source.words[0] is first_word
    assert source.words[-1] is terminal_word
    assert (terminal_word.start_seconds, terminal_word.end_seconds) == (word_start, word_end)
    assert has_complete_boundary_evidence(report_for(result, duration))
    assert result == align_intervention_boundaries(duration, [original], [])


def test_terminal_word_after_raw_bunny_is_accepted_with_earlier_speech_and_tolerated_skew():
    source = group(0, 5788).segments[0]
    terminal_word = TranscriptWord(text="Conclusione.", start_seconds=5789.3,
                                   end_seconds=5789.496, diarization_label="A", confidence=.9)
    source = source.model_copy(update={"words": [*source.words, terminal_word], "end_seconds": 5789.496})
    original = replace(group(0, 5788), segments=(source,), block_id="b001",
                       chapter_number=1, chapters_in_block=1)
    before = source.model_dump()

    result = align_intervention_boundaries(5789.248, [original], [])

    assert [(item.start_seconds, item.end_seconds) for item in result.interventions] == [(0, 5789)]
    assert [(block.start_seconds, block.end_seconds) for block in result.blocks] == [(0, 5789)]
    assert source.model_dump() == before
    assert source.words[-1] is terminal_word
    assert (terminal_word.start_seconds, terminal_word.end_seconds) == (5789.3, 5789.496)
    assert has_complete_boundary_evidence(report_for(result, 5789.248))
    assert result == align_intervention_boundaries(5789.248, [original], [])


def test_terminal_group_after_fractional_bunny_end_without_earlier_speech_is_rejected():
    with pytest.raises(ValueError, match="tempi vocali"):
        align_intervention_boundaries(5789.248, [group(0, 5788), group(5789.3, 5789.496)], [])


def test_terminal_provider_skew_above_one_second_is_rejected_despite_earlier_speech():
    source = group(0, 5788).segments[0]
    terminal_word = TranscriptWord(text="Conclusione.", start_seconds=5789.3,
                                   end_seconds=5790.3, diarization_label="A", confidence=.9)
    source = source.model_copy(update={"words": [*source.words, terminal_word], "end_seconds": 5790.3})
    with pytest.raises(ValueError, match="tempi vocali"):
        align_intervention_boundaries(5789.248, [replace(group(0, 5788), segments=(source,))], [])


def test_standalone_terminal_subsecond_group_cannot_collapse_exported_interval():
    with pytest.raises(ValueError):
        align_intervention_boundaries(1080.9, [group(0, 1080), group(1080.8, 1080.9)], [])


@pytest.mark.parametrize("changes", [
    {"chapter_number": 3}, {"chapters_in_block": 3}, {"block_id": "b002"},
])
def test_block_alignment_rejects_invalid_child_partition(changes):
    first = replace(group(0, 540), block_id="b001", chapter_number=1, chapters_in_block=2)
    second = replace(group(540, 1080), block_id="b001", chapter_number=2, chapters_in_block=2)
    with pytest.raises(ValueError, match="blocch"):
        align_intervention_boundaries(1080, [first, replace(second, **changes)], [])


def test_completeness_rejects_slide_origin_after_floored_bunny_end():
    planned = replace(group(0, 539), block_id="b001", chapter_number=1,
                      chapters_in_block=1, boundary_reason="inizio_blocco")
    result = align_intervention_boundaries(540.9, [planned], [])
    report = report_for(result, 540.9)
    report.analysis_profile = 2
    report.interventions[0].boundary_origin.slide_indizio_seconds = 540.5
    assert not has_complete_boundary_evidence(report)


@pytest.mark.parametrize("profile,expected", [(1, True), (2, False)])
@pytest.mark.parametrize("extra", ["slide", "block", "chapter_origin"])
def test_new_endpoint_checks_preserve_legacy_boundary_eligibility(profile, expected, extra):
    from app.models import ChapterBoundaryOrigin, SlideChange, SpeechBlock

    report = report_for(align_intervention_boundaries(40, [group(0, 39)], []))
    report.analysis_profile = profile
    if extra == "slide":
        report.slides = [SlideChange(timestamp_seconds=50, title="Storica", confidence="alta")]
    elif extra == "block":
        report.speech_blocks = [SpeechBlock(id="b001", start_seconds=0, end_seconds=41,
                                           tipo="intervento", titolo="Tema", sinossi="Sintesi")]
    else:
        report.interventions[0].boundary_origin = ChapterBoundaryOrigin(
            motivo_editoriale="slide_e_tema", regola_audio="no_pause", slide_indizio_seconds=40.5,
        )
    assert has_complete_boundary_evidence(report) is expected


def test_short_pause_adds_half_measured_duration_to_last_word():
    # The detected silence can begin before the provider's last word ends.
    # Anchor the half-pause rule to that last word, not the detector midpoint.
    result = align_intervention_boundaries(
        40, [group(0, 20.4), group(22, 39)], [SilenceInterval(19.1, 21)],
    )
    assert result.boundaries[0].boundary_seconds == 21
