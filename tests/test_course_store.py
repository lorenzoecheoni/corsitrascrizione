from uuid import UUID

import pytest

from app.course_models import IntermediateCourseReport
from app.course_store import CourseStore
from app.inventory import InventoryCourse, MatchProposal


def course(title="Governance", *, row=2):
    return InventoryCourse(
        id=f"0:{row}", foglio="Formazione", gid="0", posizione_foglio=0,
        riga=row, titolo=title, relatori_attesi=["Mario Rossi"], materiali=["Slide"],
        link="bunny", colonna_link="D", guid_esplicito=None,
    )


def intermediate_json():
    return IntermediateCourseReport.model_validate({
        "versione": 1,
        "stato": "da_verificare",
        "corso": {"titolo": "Governance", "sinossi_corso": "Sintesi."},
        "relatori": [],
        "video": [{
            "chiave": "v1", "guid": "00000000-0000-0000-0000-000000000001",
            "titolo_bunny": "Governance", "durata_secondi": 600, "ordine": 1,
            "interventi": [{
                "id": "v1-i001", "inizio": "0:00:00", "fine": "0:10:00",
                "tipo": "intervento", "titolo": "Governance", "sintesi": "Sintesi.",
                "punti_chiave": ["Uno", "Due", "Tre"], "confidenza": .9,
            }],
            "slide": [],
        }],
        "verifiche_richieste": [],
    })


def test_sync_upserts_source_fields_and_preserves_confirmed_associations(tmp_path):
    path = tmp_path / "courses.sqlite3"
    store = CourseStore(path)
    video_id = UUID("00000000-0000-0000-0000-000000000001")
    store.sync_inventory([course()])
    store.confirm_videos("0:2", [video_id])

    store.sync_inventory([course("Governance aggiornata")])
    record = store.get_course("0:2")

    assert record.titolo == "Governance aggiornata"
    assert record.video_confermati == [video_id]
    store.close()

    reopened = CourseStore(path)
    try:
        assert reopened.get_course("0:2").video_confermati == [video_id]
    finally:
        reopened.close()


def test_sync_marks_missing_rows_inactive_without_deleting_last_snapshot(tmp_path):
    store = CourseStore(tmp_path / "courses.sqlite3")
    store.sync_inventory([course(), course("Trust", row=3)])

    store.sync_inventory([course()])

    assert [item.id for item in store.list_courses()] == ["0:2"]
    assert store.get_course("0:3", include_inactive=True).attivo is False
    store.close()


def test_proposals_never_replace_confirmed_videos(tmp_path):
    store = CourseStore(tmp_path / "courses.sqlite3")
    store.sync_inventory([course()])
    confirmed = UUID("00000000-0000-0000-0000-000000000001")
    proposed = UUID("00000000-0000-0000-0000-000000000002")
    store.confirm_videos("0:2", [confirmed])

    store.replace_proposals([MatchProposal(
        course_id="0:2", video_id=proposed, score=.97, reason="titolo_univoco",
    )])

    record = store.get_course("0:2")
    assert record.video_confermati == [confirmed]
    assert record.proposte == []
    store.close()


def test_store_persists_jobs_intermediate_confirmation_and_academy_versions(tmp_path):
    path = tmp_path / "courses.sqlite3"
    store = CourseStore(path)
    store.sync_inventory([course()])
    job_id = UUID("10000000-0000-0000-0000-000000000001")
    batch_id = UUID("20000000-0000-0000-0000-000000000001")
    store.attach_run("0:2", batch_id, [job_id])
    source = intermediate_json()
    store.save_intermediate("0:2", source)
    store.confirm_intermediate("0:2", source.model_copy(update={"stato": "verificato"}))
    store.save_academy("0:2", {"versione": 1, "corso": {"titolo": "Governance"}}, 1, 1)
    store.close()

    reopened = CourseStore(path)
    try:
        record = reopened.get_course("0:2")
        assert record.batch_id == batch_id
        assert record.job_ids == [job_id]
        assert record.intermediate is not None and record.intermediate.stato == "verificato"
        assert record.academy_json["versione"] == 1
        assert (record.contract_version, record.prompt_version) == (1, 1)
    finally:
        reopened.close()


def test_store_refuses_academy_output_until_intermediate_is_verified(tmp_path):
    store = CourseStore(tmp_path / "courses.sqlite3")
    store.sync_inventory([course()])
    store.attach_run("0:2", UUID(int=1), [UUID(int=2)])
    store.save_intermediate("0:2", intermediate_json())

    with pytest.raises(ValueError, match="verificato"):
        store.save_academy("0:2", {"versione": 1}, 1, 1)

    assert store.get_course("0:2").academy_json is None
    store.close()
