"""Shared offline/live acceptance checks; never write or print report content."""

import json
import math

from app.intermediate_models import IntermediateReportV11
from app.intermediate_report import build_intermediate_report
from app.reporting import render_markdown, render_text


def assert_no_transient_fields(value):
    if isinstance(value, dict):
        assert not {
            "transcript", "transcript_text", "word_timings", "word_timestamps",
            "segments", "words", "diarization_label", "source_utterance_id",
            "frames", "deck_pages", "raw_text", "prompt", "payload",
        }.intersection(value)
        for child in value.values():
            assert_no_transient_fields(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_transient_fields(child)


def assert_granular_analysis(report, bunny_duration):
    """Check factual coverage independently of prose and transcript density."""
    assert report.analysis_profile == 2
    assert report.audio_boundary_version == 1
    assert report.duration_seconds == bunny_duration
    end = math.floor(bunny_duration)
    assert end > 0
    assert report.speech_blocks and report.interventions
    chapters = report.interventions
    blocks = report.speech_blocks
    assert len({item.id for item in chapters}) == len(chapters)
    assert len({item.id for item in blocks}) == len(blocks)
    assert all(item.id.strip() for item in [*chapters, *blocks])
    assert chapters[0].start_seconds == 0
    assert chapters[-1].end_seconds == end
    assert all(left.end_seconds == right.start_seconds
               for left, right in zip(chapters, chapters[1:]))
    assert all(0 <= item.start_seconds < item.end_seconds <= end
               for item in [*chapters, *blocks])
    linked = set()
    for block in blocks:
        children = [chapter for chapter in chapters if chapter.block_id == block.id]
        assert children
        assert children[0].start_seconds == block.start_seconds
        assert children[-1].end_seconds == block.end_seconds
        assert all(left.end_seconds == right.start_seconds
                   for left, right in zip(children, children[1:]))
        assert [child.chapter_number for child in children] == list(range(1, len(children) + 1))
        assert all(child.chapters_in_block == len(children) and child.tipo == block.tipo
                   for child in children)
        linked.update(child.id for child in children)
    for chapter in chapters:
        if chapter.id not in linked:
            assert chapter.tipo in {"pausa", "logistica"}
            assert chapter.block_id is None
            assert chapter.chapter_number is chapter.chapters_in_block is None
        elif chapter is not chapters[0]:
            assert chapter.boundary_origin is not None
        if chapter.boundary_origin and chapter.boundary_origin.slide_indizio_seconds is not None:
            assert 0 <= chapter.boundary_origin.slide_indizio_seconds <= end
    assert len(report.boundaries) == len(chapters) - 1
    assert [(item.previous_intervention_id, item.next_intervention_id, item.boundary_seconds)
            for item in report.boundaries] == [
        (left.id, right.id, left.end_seconds) for left, right in zip(chapters, chapters[1:])
    ]
    assert all(0 <= slide.timestamp_seconds <= end for slide in report.slides)
    assert all(0 <= evidence.timestamp_seconds <= end
               for speaker in report.speakers for evidence in speaker.evidence
               if evidence.timestamp_seconds is not None)
    assert_no_transient_fields(json.loads(report.model_dump_json()))


def assert_report_delivery(report, metadata, *, private_values=()):
    assert_granular_analysis(report, metadata.duration_seconds)
    snapshot = report.model_dump_json()
    exported = build_intermediate_report(report, metadata.video_id)
    serialized = exported.model_dump_json(by_alias=True, exclude_none=True)
    assert IntermediateReportV11.model_validate_json(serialized) == exported
    assert serialized == build_intermediate_report(report, metadata.video_id).model_dump_json(
        by_alias=True, exclude_none=True,
    )
    assert exported.versione == 1 and len(exported.video) == 1
    video = exported.video[0]
    assert (video.chiave, video.guid, video.ordine) == ("v1", str(metadata.video_id), 1)
    assert video.durata_secondi == math.floor(metadata.duration_seconds)
    assert [item.id for item in video.blocchi_parlato] == [
        f"v1-b{index:03d}" for index in range(1, len(report.speech_blocks) + 1)
    ]
    assert [item.id for item in video.interventi] == [
        f"v1-i{index:03d}" for index in range(1, len(report.interventions) + 1)
    ]
    eligible = [item for item in video.interventi if item.tipo == "intervento"
                and 480 <= item.end_seconds - item.start_seconds <= 900]
    public = [item for item in video.interventi if item.accesso == "pubblico"]
    assert public == eligible[:1]
    # The current granular export contract rejects a report with no preview;
    # do not turn that rejection into a successful live acceptance.
    assert len(public) == 1
    assert all(item.accesso == "iscritti" for item in video.interventi if item not in public)
    for speaker in exported.relatori:
        if speaker.slug == "furio-dandrea":
            assert speaker.nome == "Furio D'Andrea"
    bodies = {"json": serialized, "md": render_markdown(report), "txt": render_text(report)}
    for extension in ("md", "txt"):
        assert bodies[extension].strip()
        for section in ("Blocchi parlato", "Capitoli e segmenti", "Stima costi (USD)"):
            assert section in bodies[extension]
        assert all(item.id in bodies[extension] for item in [*video.blocchi_parlato, *video.interventi])
    assert_no_transient_fields(json.loads(serialized))
    for private in private_values:
        if private:
            assert private not in snapshot
            assert all(private not in body for body in bodies.values())
    assert report.model_dump_json() == snapshot
    return bodies


def assert_http_exports(client, job_id, bodies):
    for extension, media_type in (("json", "application/json"), ("md", "text/markdown"),
                                  ("txt", "text/plain")):
        response = client.get(f"/jobs/{job_id}/report.{extension}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(media_type)
        assert "attachment" in response.headers["content-disposition"]
        if extension == "json":
            assert response.json() == json.loads(bodies[extension])
            assert client.get(f"/jobs/{job_id}/report.json").content == response.content
        else:
            assert response.text == bodies[extension]
