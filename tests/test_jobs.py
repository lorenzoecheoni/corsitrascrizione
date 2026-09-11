from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from app.bunny import BunnyAuthError, BunnyNotFoundError, BunnyTimeoutError
from app.jobs import JobCancelled, JobState, JobStore, SingleWorkerRunner
from app.models import AcademyReport


@pytest.fixture
def report() -> AcademyReport:
    return AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())


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
