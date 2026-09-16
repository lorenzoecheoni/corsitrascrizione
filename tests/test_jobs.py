from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Event
from uuid import uuid4

import pytest

from app.bunny import BunnyAuthError, BunnyNotFoundError, BunnyTimeoutError
from app.jobs import JobCancelled, JobState, JobStore, SingleWorkerRunner
from app.models import AcademyReport, BoundaryEvidence, Intervention


@pytest.fixture
def report() -> AcademyReport:
    return AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())


def test_pipeline_user_message_reaches_failed_job():
    from app.pipeline import PipelineError
    store = JobStore()
    job = store.create("private source")
    error = PipelineError("unsupported_duration", "Il video supera il limite di quattro ore")
    def pipeline(url, progress, cancellation_event):
        raise error
    runner = SingleWorkerRunner(store, pipeline)
    runner.submit(job.id)
    runner.shutdown(wait=True)
    assert store.get(job.id).error == "Il video supera il limite di quattro ore"


def test_job_reopens_from_same_store_but_not_after_restart() -> None:
    store = JobStore()
    created = store.create("https://player.mediadelivery.net/embed/123/video-id?token=secret")
    store.update(created.id, state=JobState.PROCESSING, progress=25, message="Trascrizione")
    loaded = store.get(created.id)
    assert loaded.progress == 25
    assert loaded.message == "Trascrizione"
    assert loaded.created_at.tzinfo is not None
    assert loaded.updated_at >= loaded.created_at
    assert "source_url" not in loaded.model_dump()
    assert "secret" not in loaded.model_dump_json()
    assert "secret" not in repr(loaded)
    with pytest.raises(KeyError):
        JobStore().get(created.id)


def test_sqlite_store_persists_completed_report_and_ordered_batch(tmp_path, report) -> None:
    path = tmp_path / "reports.sqlite3"
    first = JobStore(path)
    batch, jobs = first.create_batch([
        ("https://private.invalid/first", "Primo"),
        ("https://private.invalid/second", "Secondo"),
    ])
    first.update(jobs[0].id, state=JobState.PROCESSING, progress=75)
    first.update(jobs[0].id, state=JobState.COMPLETED, report=report)

    reopened = JobStore(path)

    loaded = reopened.get(jobs[0].id)
    assert loaded.report == report
    assert loaded.source_url == "https://private.invalid/first"
    assert reopened.get_batch(batch.id).job_ids == batch.job_ids
    loaded.report.speakers.clear()
    assert len(reopened.get(jobs[0].id).report.speakers) == 3


def test_sqlite_round_trip_persists_compact_boundaries(
    tmp_path, report,
) -> None:
    path = tmp_path / "boundary-report.sqlite3"
    report.interventions = [
        Intervention(
            id="i001", start_seconds=0, end_seconds=100, tipo="intervento",
            relatori=[], titolo="Prima parte", sintesi="Prima parte.",
            punti_chiave=["Uno", "Due", "Tre"], confidenza=.9,
        ),
        Intervention(
            id="i002", start_seconds=100, end_seconds=3720, tipo="intervento",
            relatori=[], titolo="Seconda parte", sintesi="Seconda parte.",
            punti_chiave=["Quattro", "Cinque", "Sei"], confidenza=.9,
        ),
    ]
    report.boundaries = [BoundaryEvidence(
        previous_intervention_id="i001", next_intervention_id="i002",
        boundary_seconds=100, words_before=["prima"], words_after=["seconda"],
        pause_before=True, pause_after=True, rule="long_pause",
    )]
    store = JobStore(path)
    job = store.create("private-source")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)

    loaded = JobStore(path).get(job.id).report

    assert loaded is not None
    assert loaded.boundaries == report.boundaries


@pytest.mark.parametrize("verified", [False, True])
def test_sqlite_restart_retains_single_segment_audio_provenance(tmp_path, report, verified):
    from app.boundaries import has_complete_boundary_evidence

    report.interventions = [Intervention(
        id="i001", start_seconds=0, end_seconds=3720, tipo="intervento",
        relatori=[], titolo="Intervento unico", sintesi="Spiegazione completa.",
        punti_chiave=["Uno", "Due", "Tre"], confidenza=.9,
    )]
    data = report.model_dump()
    data.pop("boundaries", None)
    data.pop("audio_boundary_version", None)
    if verified:
        data["audio_boundary_version"] = 1
    report = AcademyReport.model_validate(data)
    path = tmp_path / "single-boundary-report.sqlite3"
    store = JobStore(path)
    job = store.create("source")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)

    loaded = JobStore(path).get(job.id).report

    assert loaded.boundaries == []
    assert has_complete_boundary_evidence(loaded) is verified
    assert has_complete_boundary_evidence(AcademyReport.model_validate_json(loaded.model_dump_json())) is verified
    assert getattr(loaded, "audio_boundary_version", None) == (1 if verified else None)


def test_sqlite_store_marks_interrupted_jobs_failed_on_reopen(tmp_path) -> None:
    path = tmp_path / "reports.sqlite3"
    store = JobStore(path)
    queued = store.create("queued")
    processing = store.create("processing")
    store.update(processing.id, state=JobState.PROCESSING, progress=50)

    reopened = JobStore(path)

    for job_id in (queued.id, processing.id):
        assert reopened.get(job_id).state == JobState.FAILED
        assert reopened.get(job_id).error == (
            "Elaborazione interrotta dal riavvio; avvia nuovamente l’analisi"
        )
    assert reopened.get(queued.id).progress == 0
    assert reopened.get(processing.id).progress == 50


def test_completed_and_uncompleted_lists_are_separate_and_ordered(tmp_path, report) -> None:
    store = JobStore(tmp_path / "reports.sqlite3")
    completed = store.create("completed", source_title="Completato")
    failed = store.create("failed", source_title="Fallito")
    processing = store.create("processing", source_title="In corso")
    store.update(completed.id, state=JobState.PROCESSING)
    store.update(completed.id, state=JobState.COMPLETED, report=report)
    store.update(failed.id, state=JobState.PROCESSING)
    store.update(failed.id, state=JobState.FAILED, error="Errore controllato")
    store.update(processing.id, state=JobState.PROCESSING)

    assert [job.id for job in store.list_completed()] == [completed.id]
    assert [job.id for job in store.list_uncompleted(limit=2)] == [processing.id, failed.id]


def test_delete_completed_removes_only_local_job_and_batch_membership(tmp_path, report) -> None:
    store = JobStore(tmp_path / "reports.sqlite3")
    batch, jobs = store.create_batch([("first", "Primo"), ("second", "Secondo")])
    store.update(jobs[0].id, state=JobState.PROCESSING)
    store.update(jobs[0].id, state=JobState.COMPLETED, report=report)

    store.delete_completed(jobs[0].id)

    with pytest.raises(KeyError):
        store.get(jobs[0].id)
    assert store.get_batch(batch.id).job_ids == [jobs[1].id]
    with pytest.raises(ValueError):
        store.delete_completed(jobs[1].id)
    assert store.get(jobs[1].id).source_title == "Secondo"


def test_delete_last_completed_job_preserves_empty_batch(tmp_path, report) -> None:
    store = JobStore(tmp_path / "reports.sqlite3")
    batch, jobs = store.create_batch([("only", "Unico")])
    store.update(jobs[0].id, state=JobState.PROCESSING)
    store.update(jobs[0].id, state=JobState.COMPLETED, report=report)

    store.delete_completed(jobs[0].id)

    assert store.get_batch(batch.id).job_ids == []


def test_corrupt_saved_report_is_safe_and_does_not_hide_valid_reports(
    tmp_path, report, caplog,
) -> None:
    path = tmp_path / "reports.sqlite3"
    store = JobStore(path)
    corrupt = store.create("corrupt", source_title="Corrotto")
    valid = store.create("valid", source_title="Valido")
    for job in (corrupt, valid):
        store.update(job.id, state=JobState.PROCESSING)
        store.update(job.id, state=JobState.COMPLETED, report=report)
    secret_body = "secret-invalid-report-body"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE jobs SET report_json = ? WHERE id = ?",
            (secret_body, str(corrupt.id)),
        )

    loaded = JobStore(path).list_completed()

    by_id = {job.id: job for job in loaded}
    assert by_id[valid.id].report == report
    assert by_id[corrupt.id].report is None
    assert by_id[corrupt.id].error == "Report salvato non leggibile"
    assert secret_body not in caplog.text


def test_create_batch_keeps_titles_and_order_without_exposing_source_urls() -> None:
    store = JobStore()
    batch, jobs = store.create_batch([
        ("https://private.invalid/first?token=secret-one", "Primo video"),
        ("https://private.invalid/second?token=secret-two", "Secondo video"),
    ])

    assert [job.id for job in jobs] == batch.job_ids
    assert [job.source_title for job in jobs] == ["Primo video", "Secondo video"]
    assert all("source_url" not in job.model_dump() for job in jobs)
    assert "secret-one" not in "".join(job.model_dump_json() for job in jobs)
    assert "secret-two" not in "".join(job.model_dump_json() for job in jobs)


def test_get_batch_returns_a_defensive_copy() -> None:
    store = JobStore()
    batch, _ = store.create_batch([("private-source", "Titolo sicuro")])
    loaded = store.get_batch(batch.id)

    loaded.job_ids.clear()

    assert store.get_batch(batch.id).job_ids == batch.job_ids


def test_list_recent_returns_newest_jobs_first_and_respects_limit() -> None:
    store = JobStore()
    first = store.create("first", source_title="Primo")
    second = store.create("second", source_title="Secondo")
    third = store.create("third", source_title="Terzo")

    recent = store.list_recent(limit=2)

    assert [job.id for job in recent] == [third.id, second.id]
    recent[0].source_title = "Alterato"
    assert store.list_recent(limit=1)[0].source_title == "Terzo"
    for invalid_limit in (0, 101, 1.5, True):
        with pytest.raises(ValueError):
            store.list_recent(limit=invalid_limit)


def test_records_and_nested_reports_are_defensive_copies(report: AcademyReport) -> None:
    store = JobStore()
    created = store.create("private-source")
    created.message = "Alterato"
    assert store.get(created.id).message != "Alterato"
    store.update(created.id, state=JobState.PROCESSING)
    updated = store.update(created.id, state=JobState.COMPLETED, report=report)
    report.speakers[0].display_name = "Alterato"
    updated.report.speakers[0].display_name = "Altro"
    loaded = store.get(created.id)
    assert loaded.report.speakers[0].display_name == "Giulia Bianchi"
    loaded.report.speakers.clear()
    assert len(store.get(created.id).report.speakers) == 3


@pytest.mark.parametrize("start", list(JobState))
@pytest.mark.parametrize("end", list(JobState))
def test_only_valid_state_transitions_are_allowed(start, end, report) -> None:
    store = JobStore()
    job = store.create("source")
    if start != JobState.QUEUED:
        store.update(job.id, state=JobState.PROCESSING)
    if start in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
        store.update(job.id, state=start, **(
            {"report": report} if start == JobState.COMPLETED else
            {"error": "Errore controllato"} if start == JobState.FAILED else {}
        ))
    permitted = {
        (JobState.QUEUED, JobState.PROCESSING),
        (JobState.QUEUED, JobState.CANCELLED),
        (JobState.PROCESSING, JobState.PROCESSING),
        (JobState.PROCESSING, JobState.COMPLETED),
        (JobState.PROCESSING, JobState.FAILED),
        (JobState.PROCESSING, JobState.CANCELLED),
    }
    kwargs = {"state": end}
    if end == JobState.COMPLETED:
        kwargs["report"] = report
    if end == JobState.FAILED:
        kwargs["error"] = "Errore controllato"
    if (start, end) in permitted:
        assert store.update(job.id, **kwargs).state == end
    else:
        before = store.get(job.id)
        with pytest.raises(ValueError):
            store.update(job.id, **kwargs)
        assert store.get(job.id) == before


@pytest.mark.parametrize("progress", [-1, 101, 24, 25.5, True])
def test_invalid_progress_does_not_partially_update_record(progress) -> None:
    store = JobStore()
    job = store.create("source")
    before = store.update(job.id, state=JobState.PROCESSING, progress=25)
    with pytest.raises(ValueError):
        store.update(job.id, progress=progress, message="Modifica rifiutata")
    assert store.get(job.id) == before


def test_progress_can_stay_equal_or_increase() -> None:
    store = JobStore()
    job = store.create("source")
    store.update(job.id, state=JobState.PROCESSING, progress=25)
    store.update(job.id, progress=25, message="Trascrizione")
    assert store.update(job.id, progress=100).progress == 100


def test_concurrent_create_and_read_do_not_lose_jobs() -> None:
    store = JobStore()
    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(lambda _: store.create("source"), range(100)))
        loaded = list(executor.map(lambda record: store.get(record.id), records))
    assert len({record.id for record in loaded}) == 100


@pytest.mark.parametrize("state", [JobState.COMPLETED, JobState.FAILED])
def test_terminal_transition_keeps_report_and_error_optional(state) -> None:
    store = JobStore()
    job = store.create("source")
    store.update(job.id, state=JobState.PROCESSING)
    terminal = store.update(job.id, state=state)
    assert terminal.state == state
    assert terminal.report is None
    assert terminal.error is None


def test_runner_executes_one_pipeline_at_a_time_and_forwards_progress(report) -> None:
    entered, release, second_entered = Event(), Event(), Event()
    sources = []
    store = JobStore()
    first, second = store.create("first"), store.create("second")

    def pipeline(url, progress, cancellation_event):
        sources.append(url)
        assert isinstance(cancellation_event, Event)
        progress(25, "Trascrizione")
        if url == "first":
            entered.set()
            assert release.wait(timeout=3)
        else:
            second_entered.set()
        return report

    runner = SingleWorkerRunner(store, pipeline)
    try:
        first_future = runner.submit(first.id)
        second_future = runner.submit(second.id)
        assert entered.wait(timeout=2)
        assert store.get(first.id).progress == 25
        assert store.get(first.id).message == "Trascrizione"
        assert store.get(second.id).state == JobState.QUEUED
        assert not second_entered.is_set()
        release.set()
        first_future.result(timeout=3)
        second_future.result(timeout=3)
        assert sources == ["first", "second"]
        for job in (first, second):
            record = store.get(job.id)
            assert record.state == JobState.COMPLETED
            assert record.progress == 100
            assert record.report == report
    finally:
        release.set()
        runner.shutdown(wait=True)


@pytest.mark.parametrize("exit_mode", ["cancelled", "return", "error"])
def test_active_cancel_waits_for_controlled_exit_before_starting_next(report, exit_mode) -> None:
    entered, cancel_seen, cleanup_release, second_entered = Event(), Event(), Event(), Event()
    store = JobStore()
    first, second = store.create("first"), store.create("second")

    def pipeline(url, progress, cancellation_event):
        if url == "second":
            second_entered.set()
            return report
        entered.set()
        assert cancellation_event.wait(timeout=3)
        cancel_seen.set()
        assert cleanup_release.wait(timeout=3)
        if exit_mode == "cancelled":
            raise JobCancelled()
        if exit_mode == "error":
            raise RuntimeError("sensitive cleanup diagnostic")
        return report

    runner = SingleWorkerRunner(store, pipeline)
    try:
        first_future = runner.submit(first.id)
        runner.submit(second.id)
        assert entered.wait(timeout=2)
        assert runner.cancel(first.id).state == JobState.PROCESSING
        assert cancel_seen.wait(timeout=2)
        assert store.get(first.id).state == JobState.PROCESSING
        assert not second_entered.is_set()
        cleanup_release.set()
        first_future.result(timeout=3)
        assert second_entered.wait(timeout=2)
        record = store.get(first.id)
        assert record.state == JobState.CANCELLED
        assert record.report is None
        assert record.error is None
    finally:
        cleanup_release.set()
        runner.shutdown(wait=True)


def test_queued_cancellation_never_runs_pipeline(report) -> None:
    entered, release = Event(), Event()
    sources = []
    store = JobStore()
    first, second = store.create("first"), store.create("second")

    def pipeline(url, progress, cancellation_event):
        sources.append(url)
        entered.set()
        assert release.wait(timeout=3)
        return report

    runner = SingleWorkerRunner(store, pipeline)
    try:
        runner.submit(first.id)
        assert entered.wait(timeout=2)
        future = runner.submit(second.id)
        assert runner.cancel(second.id).state == JobState.CANCELLED
        assert future.cancelled()
    finally:
        release.set()
        runner.shutdown(wait=True)
    assert sources == ["first"]


@pytest.mark.parametrize("exception_type", [RuntimeError, BunnyAuthError, BunnyNotFoundError, BunnyTimeoutError])
def test_failures_are_sanitized_and_do_not_stop_the_queue(report, exception_type, caplog, capsys) -> None:
    secret = "https://private.invalid/?token=secret-transcript"
    store = JobStore()
    first, second = store.create(secret), store.create("second")

    def pipeline(url, progress, cancellation_event):
        if url == secret:
            raise exception_type(secret)
        return report

    runner = SingleWorkerRunner(store, pipeline)
    runner.submit(first.id)
    runner.submit(second.id)
    runner.shutdown(wait=True)
    failed = store.get(first.id)
    assert failed.state == JobState.FAILED
    assert failed.error
    assert failed.report is None
    assert store.get(second.id).state == JobState.COMPLETED
    assert "secret-transcript" not in failed.model_dump_json() + caplog.text + str(capsys.readouterr())
    assert "Traceback" not in failed.error


def test_duplicate_submission_and_missing_ids_are_rejected(report) -> None:
    entered, release = Event(), Event()
    store = JobStore()
    job = store.create("source")

    def pipeline(url, progress, cancellation_event):
        entered.set()
        assert release.wait(timeout=3)
        return report

    runner = SingleWorkerRunner(store, pipeline)
    try:
        runner.submit(job.id)
        assert entered.wait(timeout=2)
        with pytest.raises(ValueError):
            runner.submit(job.id)
        with pytest.raises(KeyError):
            runner.submit(uuid4())
        with pytest.raises(KeyError):
            runner.cancel(uuid4())
    finally:
        release.set()
        runner.shutdown(wait=True)
    assert runner.cancel(job.id).state == JobState.COMPLETED


def test_cancel_before_submission_and_submit_after_shutdown(report) -> None:
    store = JobStore()
    runner = SingleWorkerRunner(store, lambda *_: report)
    cancelled = store.create("source")
    assert runner.cancel(cancelled.id).state == JobState.CANCELLED
    with pytest.raises(ValueError):
        runner.submit(cancelled.id)
    runner.shutdown(wait=False)
    queued = store.create("source")
    with pytest.raises(RuntimeError):
        runner.submit(queued.id)
    assert store.get(queued.id).state == JobState.QUEUED


def test_save_editorial_round_trip_requires_a_completed_job(tmp_path, report) -> None:
    path = tmp_path / "reports.sqlite3"
    store = JobStore(path)
    job = store.create("https://private.invalid/video")
    with pytest.raises(ValueError):
        store.save_editorial(job.id, {"corso": {}})
    with pytest.raises(KeyError):
        store.save_editorial(uuid4(), {"corso": {}})
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    editorial = {
        "corso": {"area": "Governance"},
        "interventi": [{"id": "v1-i001", "titolo_lezione": "Lezione 1"}],
        "blocchi": [],
    }

    store.save_editorial(job.id, editorial)

    reopened = JobStore(path)
    assert reopened.get(job.id).editorial == editorial
    assert "editorial" not in reopened.get(job.id).model_dump()


def test_editorial_column_is_added_to_a_pre_existing_database(tmp_path, report) -> None:
    path = tmp_path / "reports.sqlite3"
    store = JobStore(path)
    job = store.create("https://private.invalid/video")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    legacy = sqlite3.connect(path)
    legacy.execute("ALTER TABLE jobs DROP COLUMN editorial_json")
    legacy.commit()
    legacy.close()

    reopened = JobStore(path)

    assert reopened.get(job.id).editorial is None
    reopened.save_editorial(job.id, {"corso": {"area": "Governance"}})
    assert reopened.get(job.id).editorial == {"corso": {"area": "Governance"}}
