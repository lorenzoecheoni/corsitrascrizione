import json
from pathlib import Path
import time
from uuid import UUID

import pytest

from app.course_store import CourseRecord
from app.courses import (
    CourseAssemblyError,
    CourseConfirmationStore,
    build_intermediate_report,
    sign_course_selection,
    verify_course_selection,
)
from app.jobs import JobState, JobStore
from app.models import AcademyReport, Evidence, Intervention, SpeakerProfile


def stored_course():
    from datetime import datetime, timezone

    return CourseRecord(
        id="0:2", foglio="Formazione", gid="0", posizione_foglio=0, riga=2,
        titolo="Governance delle holding", relatori_attesi=["Mario Rossi"],
        materiali=["Slide"], link="bunny", colonna_link="D", guid_esplicito=None,
        attivo=True, sincronizzato_il=datetime.now(timezone.utc),
    )


def report(title, duration, *, generic=False, speaker=True):
    data = json.loads(Path("tests/fixtures/report.json").read_text())
    data["title"] = title
    data["bunny_title"] = title
    data["duration_seconds"] = duration
    data["synopsis"] = f"Sintesi di {title}."
    data["speakers"] = []
    if speaker:
        data["speakers"].append(SpeakerProfile(
            id="mario", display_name="Relatore 1" if generic else "Mario Rossi",
            role="Commercialista", confidence="alta",
            evidence=[] if generic else [Evidence(
                kind="introduzione", timestamp_seconds=1, note="Sono Mario Rossi.",
            )],
        ).model_dump(mode="json"))
    data["slides"] = [{
        "timestamp_seconds": 5, "title": f"Slide {title}",
        "visible_content": ["Uno", "Due"], "confidence": "media",
    }]
    data["interventions"] = [Intervention(
        id="i001", start_seconds=0, end_seconds=duration,
        tipo="intervento", relatori=[] if generic else ["Mario Rossi"],
        titolo=title, sintesi=f"Sintesi di {title}.",
        punti_chiave=["Uno", "Due", "Tre"], confidenza=.9,
    ).model_dump(mode="json", by_alias=True)]
    return AcademyReport.model_validate(data)


def completed_jobs(tmp_path, reports):
    store = JobStore(tmp_path / "jobs.sqlite3")
    jobs = []
    for index, item in enumerate(reports, 1):
        video_id = UUID(int=index)
        job = store.create(
            f"https://iframe.mediadelivery.net/embed/748068/{video_id}",
            source_title=f"Bunny {index}",
        )
        store.update(job.id, state=JobState.PROCESSING)
        jobs.append(store.update(job.id, state=JobState.COMPLETED, report=item))
    return store, jobs


def test_build_intermediate_report_combines_ordered_videos_and_deduplicates_speakers(tmp_path):
    store, jobs = completed_jobs(tmp_path, [report("Parte uno", 600), report("Parte due", 900)])

    result = build_intermediate_report(stored_course(), jobs)

    assert result.corso.titolo == "Governance delle holding"
    assert result.corso.inventario.foglio == "Formazione"
    assert [video.chiave for video in result.video] == ["v1", "v2"]
    assert [video.ordine for video in result.video] == [1, 2]
    assert [video.titolo_bunny for video in result.video] == ["Parte uno", "Parte due"]
    assert result.video[0].interventi[0].id == "v1-i001"
    assert result.video[1].interventi[0].id == "v2-i001"
    assert result.video[1].interventi[-1].end_seconds == 900
    assert [speaker.nome for speaker in result.relatori] == ["Mario Rossi"]
    assert "Sintesi di Parte uno" in result.corso.sinossi_corso
    assert result.video[0].slide[0].testo_principale == "Uno · Due"
    assert result.video[0].slide[0].confidenza == .65
    assert result.verifiche_richieste == []
    serialized = result.model_dump_json(by_alias=True)
    assert "transcript" not in serialized.lower()
    assert "source_url" not in serialized


def test_unknown_speaker_is_omitted_and_creates_critical_verification(tmp_path):
    store, jobs = completed_jobs(tmp_path, [report("Parte uno", 600, generic=True)])

    result = build_intermediate_report(stored_course(), jobs)

    assert [speaker.nome for speaker in result.relatori] == ["Mario Rossi"]
    assert result.relatori[0].origine_nome == ["inventario"]
    assert result.video[0].interventi[0].relatori == []
    assert result.verifiche_richieste[0].livello == "critico"
    assert result.verifiche_richieste[0].codice == "RELATORE_NON_IDENTIFICATO"


def test_legacy_report_without_interventions_requires_reanalysis(tmp_path):
    legacy = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    store, jobs = completed_jobs(tmp_path, [legacy])

    with pytest.raises(CourseAssemblyError, match="rianalizzato"):
        build_intermediate_report(stored_course(), jobs)



def test_course_assembly_rejects_noncompleted_or_mismatched_video_list(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    queued = store.create("canonical")
    with pytest.raises(CourseAssemblyError):
        build_intermediate_report(stored_course(), [queued])
    with pytest.raises(CourseAssemblyError):
        build_intermediate_report(stored_course(), [])


def test_course_confirmation_binds_course_order_and_expiry():
    key = b"k" * 32
    videos = [UUID(int=1), UUID(int=2)]
    token = sign_course_selection("0:2", videos, key, now=100)

    assert verify_course_selection(token, key, now=699)[:2] == ("0:2", videos)
    with pytest.raises(ValueError):
        verify_course_selection(token, key, now=700)
    with pytest.raises(ValueError):
        verify_course_selection(token + "changed", key, now=200)


def test_course_confirmation_is_consumed_once():
    store = CourseConfirmationStore()
    token = sign_course_selection("0:2", [UUID(int=1)], b"k" * 32, now=int(time.time()))
    calls = []

    def create(course_id, video_ids):
        calls.append((course_id, video_ids))
        return UUID(int=10), [UUID(int=20)]

    first = store.create_once(token, b"k" * 32, create)
    second = store.create_once(token, b"k" * 32, create)

    assert first == ("0:2", UUID(int=10), [UUID(int=20)])
    assert second == ("0:2", UUID(int=10), [])
    assert len(calls) == 1
