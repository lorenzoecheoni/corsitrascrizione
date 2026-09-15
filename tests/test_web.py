from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import UUID, uuid4
import re

from fastapi.testclient import TestClient
import pytest
import respx

from app.bunny import (
    BunnyAuthError,
    BunnyCatalog,
    BunnyCatalogVideo,
    BunnyCatalogTooLarge,
    BunnyClient,
    BunnyRateLimitError,
    BunnyServerError,
    BunnyVideoMetadata,
)
from app.config import Settings
from app.course_models import AcademyImport, IntermediateCourseReport
from app.jobs import JobState
from app.inventory import InventoryCourse, InventoryError
from app.main import create_app
from app.models import AcademyReport, BoundaryEvidence, ChapterBoundaryOrigin, Intervention, SpeechBlock
from app.pipeline import AnalysisPipeline
from app.selection import sign_selection


VIDEO_ID = "00000000-0000-0000-0000-000000000001"
OTHER_VIDEO_ID = "00000000-0000-0000-0000-000000000002"
VIDEO_TITLE = "Corso di prova"
OTHER_VIDEO_TITLE = "Secondo corso"

from granular_support import governance_report


def test_profile2_downloads_use_only_saved_data_without_network_or_writes(client, monkeypatch):
    report = governance_report()
    store = client.app.state.store
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    before = store.get(job.id).model_dump_json()
    client.app.state.inventory = _ForbiddenProvider()
    client.app.state.bunny = _ForbiddenProvider()
    client.app.state.openai = _ForbiddenProvider()
    client.app.state.assemblyai = _ForbiddenProvider()
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved downloads must perform no network I/O")
    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.getaddrinfo", forbidden)
    first = client.get(f"/jobs/{job.id}/report.json")
    assert first.status_code == 200
    assert first.content == client.get(f"/jobs/{job.id}/report.json").content
    assert first.json()["video"][0]["durata_secondi"] == 5789
    for extension in ("md", "txt"):
        response = client.get(f"/jobs/{job.id}/report.{extension}")
        assert response.status_code == 200
        assert "v1-b001" in response.text and "pagina 2" in response.text
    assert store.get(job.id).model_dump_json() == before


def test_legacy_even_with_audio_provenance_requires_granular_reanalysis(client):
    report = governance_report()
    report.analysis_profile = 1
    store = client.app.state.store
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    response = client.get(f"/jobs/{job.id}/report.json")
    assert response.status_code == 409
    assert response.json() == {"detail": "Rianalisi necessaria per il formato granulare"}
    assert client.get(f"/jobs/{job.id}/report.md").status_code == 200
    assert client.get(f"/jobs/{job.id}/report.txt").status_code == 200
    assert "Download JSON v1.1" not in client.get(f"/jobs/{job.id}").text


def test_malformed_profile2_returns_static_contract_failure(client):
    report = governance_report()
    for chapter in report.interventions:
        chapter.tipo = "saluti"
        chapter.punti_chiave = []
    report.material_failures = ["parser private query token=SECRET"]
    store = client.app.state.store
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    response = client.get(f"/jobs/{job.id}/report.json")
    assert response.status_code == 409
    assert response.json() == {"detail": "Report granulare non valido: rianalisi necessaria"}
    assert "SECRET" not in response.text
    for extension in ("md", "txt"):
        response = client.get(f"/jobs/{job.id}/report.{extension}")
        assert response.status_code == 409
        assert response.json() == {"detail": "Report granulare non valido: rianalisi necessaria"}
    page = client.get(f"/jobs/{job.id}")
    assert page.status_code == 200
    assert "Rianalisi necessaria" in page.text
    assert "Download JSON v1.1" not in page.text


def test_exportable_timeline_warning_remains_downloadable_on_archive_and_job_page(client):
    report = governance_report()
    report.duration_seconds = 5790
    store = client.app.state.store
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    assert client.get(f"/jobs/{job.id}/report.json").json()["stato"] == "da_verificare"
    assert "Download JSON v1.1" in client.get(f"/jobs/{job.id}").text
    assert f"/jobs/{job.id}/report.json" in client.get("/").text


def test_download_does_not_emit_persisted_raw_material_errors(client, caplog):
    report = governance_report()
    report.material_failures = ["worker error https://private.example/?token=SECRET_SENTINEL"]
    store = client.app.state.store
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    for extension in ("json", "md", "txt"):
        response = client.get(f"/jobs/{job.id}/report.{extension}")
        assert response.status_code == 200
        assert "MATERIALE_NON_RAGGIUNGIBILE" in response.text
        assert "SECRET_SENTINEL" not in response.text
    assert "SECRET_SENTINEL" not in caplog.text


def add_complete_boundary_evidence(report: AcademyReport) -> AcademyReport:
    report.audio_boundary_version = 1
    report.analysis_profile = 2
    if not report.interventions:
        report.interventions = [Intervention(
            id=f"i{index:03d}", start_seconds=start, end_seconds=end,
            tipo="intervento", relatori=["Giulia Bianchi"], titolo="Governance",
            sintesi="Il corso tratta governance e controlli.",
            punti_chiave=["Organi", "Deleghe", "Controlli"], confidenza=.92,
        ) for index, (start, end) in enumerate(((0, 600), (600, report.duration_seconds)), 1)]
    report.speech_blocks = []
    for index, chapter in enumerate(report.interventions, 1):
        if chapter.tipo in {"pausa", "logistica"}:
            continue
        chapter.block_id = f"stored-b{index}"
        chapter.chapter_number = chapter.chapters_in_block = 1
        chapter.boundary_origin = ChapterBoundaryOrigin(motivo_editoriale="inizio_blocco", regola_audio="long_pause")
        report.speech_blocks.append(SpeechBlock(
            id=chapter.block_id, start_seconds=chapter.start_seconds, end_seconds=chapter.end_seconds,
            tipo=chapter.tipo, relatori=chapter.relatori, titolo=chapter.titolo, sinossi=chapter.sintesi,
        ))
    report.boundaries = [
        BoundaryEvidence(
            previous_intervention_id=previous.id,
            next_intervention_id=following.id,
            boundary_seconds=int(previous.end_seconds),
            words_before=[], words_after=[], pause_before=True, pause_after=True,
            rule="long_pause",
        )
        for previous, following in zip(report.interventions, report.interventions[1:])
    ]
    return report


def extract_hidden(page: str, name: str) -> str:
    return re.search(rf'name="{name}" value="([^"]+)"', page)[1]


def catalog(*videos: BunnyCatalogVideo) -> BunnyCatalog:
    return BunnyCatalog(videos=list(videos), total_items=len(videos))


def catalog_video(video_id: str, title: str, duration: float) -> BunnyCatalogVideo:
    return BunnyCatalogVideo(
        video_id=video_id,
        title=title,
        duration_seconds=duration,
        status=3,
        description="Descrizione catalogo",
        thumbnail_url=f"https://cdn.example.com/{video_id}/thumb.jpg",
    )


@pytest.fixture
def client(monkeypatch):
    finished = Event()

    def fake_run(self, source_url, progress_callback, cancellation_event):
        finished.wait(2)
        return AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())

    monkeypatch.setattr(AnalysisPipeline, "run", fake_run)
    app = create_app(Settings(
        bunny_library_id=123,
        bunny_stream_api_key="bunny-secret",
        bunny_cdn_hostname="cdn.example.com",
        openai_api_key="openai-secret",
        app_password="team-secret",
        _env_file=None,
    ))
    app.state.bunny.list_videos = lambda: catalog()
    app.state.inventory.fetch = lambda: []
    app.state.bunny.get_metadata = lambda video_id: BunnyVideoMetadata(
        video_id=video_id,
        title=VIDEO_TITLE if str(video_id) == VIDEO_ID else OTHER_VIDEO_TITLE,
        duration_seconds=3600 if str(video_id) == VIDEO_ID else 1800,
        status=3,
        available_resolutions=[240, 720],
    )
    with TestClient(app) as test_client:
        login = test_client.post("/login", data={
            "username": "team",
            "password": "team-secret",
            "csrf_token": app.state.csrf_token,
        }, follow_redirects=False)
        assert login.status_code == 303
        test_client.headers["X-CSRF-Token"] = app.state.csrf_token
        yield test_client
        finished.set()
    app.state.runner.shutdown(wait=True)


def selection_form(client: TestClient, video_ids: list[str]):
    data = {"csrf_token": client.app.state.csrf_token}
    if video_ids:
        data["video_ids"] = video_ids
    return client.post("/selections/preview", data=data)


def test_dashboard_renders_catalog_totals_and_recent_jobs_without_source_url(client):
    app = client.app
    app.state.bunny.list_videos = lambda: catalog(
        catalog_video(VIDEO_ID, VIDEO_TITLE, 3600),
        catalog_video(OTHER_VIDEO_ID, OTHER_VIDEO_TITLE, 1800),
    )
    app.state.store.create("https://private.example/source", source_title="Lavoro recente")

    response = client.get("/")

    assert response.status_code == 200
    assert VIDEO_TITLE in response.text and OTHER_VIDEO_TITLE in response.text
    assert "2 video" in response.text
    assert "1:30:00" in response.text
    assert "Lavoro recente" in response.text
    assert "private.example" not in response.text
    assert "bunny-secret" not in response.text
    assert 'name="video_ids"' in response.text
    assert 'action="/selections/preview"' in response.text
    assert "Ogni video genera un report autonomo" in response.text
    assert "Inventario corsi" not in response.text
    assert 'href="/inventory"' not in response.text


def test_obsolete_inventory_page_redirects_to_per_video_catalog(client):
    response = client.get("/inventory", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_dashboard_separates_saved_reports_from_uncompleted_jobs(client):
    report = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    store = client.app.state.store
    completed = store.create("completed-source", source_title="Report salvato")
    store.update(completed.id, state=JobState.PROCESSING)
    store.update(completed.id, state=JobState.COMPLETED, report=report)
    failed = store.create("failed-source", source_title="Lavoro fallito")
    store.update(failed.id, state=JobState.PROCESSING)
    store.update(failed.id, state=JobState.FAILED, error="Errore controllato")

    response = client.get("/")

    assert response.status_code == 200
    archive = re.search(r'<section[^>]+aria-labelledby="saved-reports".*?</section>', response.text, re.S)[0]
    recent = re.search(r'<section[^>]+aria-labelledby="recent-jobs".*?</section>', response.text, re.S)[0]
    assert "Report salvati" in archive and "Report salvato" in archive
    assert f'/jobs/{completed.id}' in archive
    assert f'/jobs/{completed.id}/report.txt' in archive
    assert f'/jobs/{completed.id}/report.md' in archive
    assert f'/jobs/{completed.id}/report.json' not in archive
    assert "Rianalisi necessaria" in archive
    assert 'href="/#catalog-title"' in archive
    assert "Lavoro fallito" not in archive
    assert "Lavoro fallito" in recent
    assert "Report salvato" not in recent


def test_dashboard_retains_json_download_for_complete_boundary_evidence(client):
    report = add_complete_boundary_evidence(
        AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    )
    store = client.app.state.store
    completed = store.create("completed-source", source_title="Report conforme")
    store.update(completed.id, state=JobState.PROCESSING)
    store.update(completed.id, state=JobState.COMPLETED, report=report)

    archive = client.get("/").text

    assert f'/jobs/{completed.id}/report.json' in archive
    assert "Download JSON v1.1" in archive


def test_dashboard_has_accessible_catalog_controls_and_lazy_bunny_thumbnails(client):
    client.app.state.bunny.list_videos = lambda: catalog(
        catalog_video(VIDEO_ID, VIDEO_TITLE, 3600),
        catalog_video(OTHER_VIDEO_ID, OTHER_VIDEO_TITLE, 1800),
    )

    response = client.get("/")

    assert response.status_code == 200
    assert "<header" in response.text and "<nav" in response.text and "<main" in response.text
    assert 'action="/logout"' in response.text and 'method="post"' in response.text
    assert 'for="catalog-search"' in response.text
    assert 'id="catalog-search"' in response.text
    assert 'for="status-filter"' in response.text and 'id="status-filter"' in response.text
    assert 'for="collection-filter"' in response.text and 'id="collection-filter"' in response.text
    assert 'data-video-row' in response.text and 'data-video-select' in response.text
    assert 'loading="lazy"' in response.text
    assert 'id="analyse-selection"' in response.text and 'disabled' in response.text
    assert 'catalog.js' in response.text
    assert 'id="selection-limit"' in response.text
    assert 'aria-describedby="selection-limit selection-status"' in response.text
    assert 'id="selection-status" role="status"' in response.text
    assert 'id="catalog-view-cards"' in response.text
    assert 'id="catalog-view-list"' in response.text
    assert 'role="group" aria-label="Vista catalogo"' in response.text
    assert 'id="catalog-grid"' in response.text and 'data-view="cards"' in response.text
    assert "Massimo 50 video per conferma" in response.text
    selection_bar = re.search(r'<aside id="selection-bar"(.*?)</aside>', response.text, re.S)[1]
    assert '/ 50 video selezionati' in selection_bar


def test_dashboard_tabs_and_video_order_follow_the_three_sheet_tabs(client):
    client.app.state.bunny.list_videos = lambda: catalog(
        catalog_video(OTHER_VIDEO_ID, OTHER_VIDEO_TITLE, 1800),
        catalog_video(VIDEO_ID, VIDEO_TITLE, 3600),
    )
    client.app.state.inventory.fetch = lambda: [
        inventory_course(),
        InventoryCourse(
            id="0:8", foglio="Formazione", gid="0", posizione_foglio=0, riga=8,
            titolo=OTHER_VIDEO_TITLE, relatori_attesi=[], materiali=[], link=None,
            colonna_link="D", guid_esplicito=None,
        ),
    ]

    response = client.get("/")

    assert response.status_code == 200
    assert response.text.index(VIDEO_TITLE) < response.text.index(OTHER_VIDEO_TITLE)
    assert response.text.count('data-catalog-tab-button') == 4
    assert re.search(r'>\s*Formazione <span>', response.text)
    assert re.search(r'>\s*Corsi premium - Master <span>', response.text)
    assert re.search(r'>\s*Corsi Premium <span>', response.text)
    assert re.search(r'>\s*Altri video Bunny <span>', response.text)
    assert 'data-catalog-tab="sheet-0"' in response.text
    assert 'Riga 2 del foglio' in response.text


def test_dashboard_keeps_bunny_catalog_available_when_sheet_is_unreachable(client):
    from app.inventory import InventoryError

    client.app.state.bunny.list_videos = lambda: catalog(
        catalog_video(VIDEO_ID, VIDEO_TITLE, 3600),
    )
    client.app.state.inventory.fetch = lambda: (_ for _ in ()).throw(InventoryError("safe"))

    response = client.get("/")

    assert response.status_code == 200
    assert VIDEO_TITLE in response.text
    assert "Ordine del foglio temporaneamente non disponibile" in response.text


def test_catalog_script_is_limited_to_the_authenticated_dashboard(client):
    client.app.state.bunny.list_videos = lambda: catalog(catalog_video(VIDEO_ID, VIDEO_TITLE, 3600))

    dashboard = client.get("/")
    login = client.get("/login")
    selection = selection_form(client, [VIDEO_ID])
    job = client.app.state.store.create("https://private.example/source")
    report = client.get(f"/jobs/{job.id}")

    assert 'catalog.js' in dashboard.text
    assert 'catalog.js' not in login.text
    assert 'catalog.js' not in selection.text
    assert 'catalog.js' not in report.text


def test_dashboard_handles_an_empty_catalog(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "Nessun video disponibile" in response.text
    assert '<a id="refresh-catalog"' in response.text
    assert re.search(r'<a id="refresh-catalog"[^>]+href="/"', response.text)


def test_large_catalog_has_distinct_status_and_collection_options(client):
    videos = [catalog_video(str(UUID(int=index + 1)), "Corso", 60) for index in range(10_000)]
    for index, video in enumerate(videos):
        video.status = [3, 4, None][index % 3]
        video.collection_id = UUID(VIDEO_ID) if index % 2 else None
    client.app.state.bunny.list_videos = lambda: catalog(*videos)
    page = client.get("/").text
    status = re.search(r'<select id="status-filter">(.*?)</select>', page, re.S)[1]
    collection = re.search(r'<select id="collection-filter">(.*?)</select>', page, re.S)[1]
    assert re.findall(r'<option value="([^"]+)"', status) == ["all", "3", "4", "unknown"]
    assert re.findall(r'<option value="([^"]+)"', collection) == ["all", "none", VIDEO_ID]


@respx.mock
def test_oversized_catalog_explains_operational_limit_and_offers_refresh(client):
    client.app.state.bunny = BunnyClient(client.app.state.settings)
    respx.get("https://video.bunnycdn.com/library/123/videos", params={"page": 1, "itemsPerPage": 100}).respond(
        200, json={"totalItems": 10_001, "currentPage": 1, "itemsPerPage": 100, "items": [],
                   "upstream": "TEST_ONLY_UPSTREAM_BODY"},
    )
    response = client.get("/")
    assert response.status_code == 200
    assert "10.000" in response.text and "paginazione" in response.text
    assert "TEST_ONLY_UPSTREAM_BODY" not in response.text
    assert re.search(r'<a id="refresh-catalog"[^>]+href="/"', response.text)


@pytest.mark.parametrize(("error", "message"), [
    (BunnyAuthError("bunny-secret upstream body"), "Accesso a Bunny non autorizzato"),
    (BunnyRateLimitError("bunny-secret upstream body"), "Limite di richieste Bunny raggiunto"),
    (BunnyServerError("bunny-secret upstream body"), "Servizio Bunny temporaneamente non disponibile"),
    (BunnyCatalogTooLarge("bunny-secret upstream body"), "Il catalogo supera il limite di 10.000 video"),
])
def test_dashboard_maps_provider_errors_to_safe_messages(client, error, message):
    def unavailable():
        raise error

    client.app.state.bunny.list_videos = unavailable
    response = client.get("/")

    assert response.status_code == 200
    assert message in response.text
    assert "bunny-secret" not in response.text
    assert "upstream body" not in response.text
    assert re.search(r'<a id="refresh-catalog"[^>]+href="/"', response.text)


@pytest.mark.parametrize("video_ids", [[VIDEO_ID], [VIDEO_ID] * 50])
def test_selection_preview_accepts_between_one_and_fifty_unique_canonical_ids(client, video_ids):
    if len(video_ids) == 50:
        video_ids = [str(UUID(int=index + 1)) for index in range(50)]
    response = selection_form(client, video_ids)

    assert response.status_code == 200
    assert extract_hidden(response.text, "confirmation")


def test_dashboard_and_preview_show_fast_provider_when_assemblyai_is_active(client):
    client.app.state.assemblyai = object()
    client.app.state.bunny.list_videos = lambda: catalog(
        catalog_video(VIDEO_ID, VIDEO_TITLE, 3600),
    )

    dashboard = client.get("/")
    preview = selection_form(client, [VIDEO_ID])

    assert "Modalità veloce AssemblyAI attiva" in dashboard.text
    assert "0.2900–0.4300 USD" in preview.text


def test_selection_preview_rejects_an_empty_selection(client):
    response = selection_form(client, [])

    assert response.status_code == 422


@pytest.mark.parametrize("video_ids", [
    [VIDEO_ID, VIDEO_ID],
    ["not-a-uuid"],
    [str(UUID(int=index + 1)) for index in range(51)],
])
def test_selection_preview_rejects_duplicates_invalid_and_oversized_selections(client, video_ids):
    response = selection_form(client, video_ids)

    assert response.status_code == 422
    assert "Conferma non valida" not in response.text


def test_selected_videos_are_rechecked_then_queued(client):
    checked = []

    def get_metadata(video_id):
        checked.append(str(video_id))
        return BunnyVideoMetadata(
            video_id=video_id,
            title=VIDEO_TITLE if str(video_id) == VIDEO_ID else OTHER_VIDEO_TITLE,
            duration_seconds=3600 if str(video_id) == VIDEO_ID else 1800,
            status=3,
            available_resolutions=[240],
        )

    submitted = []
    client.app.state.bunny.get_metadata = get_metadata
    client.app.state.runner.submit = lambda job_id: submitted.append(job_id)
    preview = selection_form(client, [VIDEO_ID, OTHER_VIDEO_ID])
    token = extract_hidden(preview.text, "confirmation")

    assert preview.status_code == 200
    assert checked == [VIDEO_ID, OTHER_VIDEO_ID]
    assert "1:30:00" in preview.text
    assert "0.6000" in preview.text
    created = client.post("/batches", data={
        "confirmation": token,
        "csrf_token": client.app.state.csrf_token,
    }, follow_redirects=False)

    assert created.status_code == 303
    assert len(submitted) == 2
    assert checked == [VIDEO_ID, OTHER_VIDEO_ID, VIDEO_ID, OTHER_VIDEO_ID]
    batch = client.get(created.headers["location"])
    assert VIDEO_TITLE in batch.text and OTHER_VIDEO_TITLE in batch.text
    for secret in ("bunny-secret", "cdn.example.com", "iframe.mediadelivery.net"):
        assert secret not in batch.text


def test_same_confirmation_reuses_batch_without_rechecking_or_resubmitting(client):
    token = extract_hidden(selection_form(client, [VIDEO_ID, OTHER_VIDEO_ID]).text, "confirmation")
    submitted = []
    client.app.state.runner.submit = submitted.append
    first = client.post("/batches", data={"confirmation": token}, follow_redirects=False)

    def unavailable(video_id):
        raise BunnyAuthError("upstream body")

    client.app.state.bunny.get_metadata = unavailable
    second = client.post("/batches", data={"confirmation": token}, follow_redirects=False)
    assert first.status_code == second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    assert len(client.app.state.store.list_recent()) == len(submitted) == 2
    assert client.post("/batches", data={"confirmation": token},
                       headers={"X-CSRF-Token": ""}).status_code == 403


def test_metadata_failure_does_not_consume_confirmation_or_leave_partial_jobs(client):
    token = extract_hidden(selection_form(client, [VIDEO_ID, OTHER_VIDEO_ID]).text, "confirmation")
    original = client.app.state.bunny.get_metadata

    def unavailable(video_id):
        if str(video_id) == OTHER_VIDEO_ID:
            raise BunnyAuthError("upstream body")
        return original(video_id)

    client.app.state.bunny.get_metadata = unavailable
    assert client.post("/batches", data={"confirmation": token}).status_code == 422
    assert not client.app.state.store.list_recent()
    client.app.state.bunny.get_metadata = original
    submitted = []
    client.app.state.runner.submit = submitted.append
    response = client.post("/batches", data={"confirmation": token}, follow_redirects=False)
    assert response.status_code == 303
    assert len(client.app.state.store.list_recent()) == len(submitted) == 2


def test_concurrent_reposts_create_and_submit_only_one_batch(client):
    token = extract_hidden(selection_form(client, [VIDEO_ID, OTHER_VIDEO_ID]).text, "confirmation")
    submitted = []
    client.app.state.runner.submit = submitted.append
    start = Barrier(8)

    def confirm():
        start.wait(timeout=5)
        return client.post("/batches", data={"confirmation": token}, follow_redirects=False)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: confirm(), range(8)))
    assert all(response.status_code == 303 for response in responses)
    assert len({response.headers["location"] for response in responses}) == 1
    assert len(client.app.state.store.list_recent()) == len(submitted) == 2


def test_expired_confirmation_is_rejected_and_cached_association_is_pruned(client, monkeypatch):
    monkeypatch.setattr("app.selection.time.time", lambda: 1_000)
    token = extract_hidden(selection_form(client, [VIDEO_ID]).text, "confirmation")
    client.app.state.runner.submit = lambda _: None
    response = client.post("/batches", data={"confirmation": token}, follow_redirects=False)
    assert response.status_code == 303
    monkeypatch.setattr("app.selection.time.time", lambda: 1_600)
    assert client.post("/batches", data={"confirmation": token}).status_code == 422
    assert not client.app.state.confirmations._confirmed
    assert len(client.app.state.store.list_recent()) == 1


@pytest.mark.parametrize("confirmation", ["changed", sign_selection([UUID(VIDEO_ID)], b"x" * 32, now=0)])
def test_batch_rejects_altered_or_expired_confirmation(client, confirmation):
    response = client.post("/batches", data={
        "confirmation": confirmation,
        "csrf_token": client.app.state.csrf_token,
    })

    assert response.status_code == 422
    assert "Conferma non valida o scaduta" in response.text


@pytest.mark.parametrize(("path", "data"), [
    ("/selections/preview", {"video_ids": VIDEO_ID}),
    ("/batches", {"confirmation": "changed"}),
])
def test_batch_posts_require_csrf(client, path, data):
    response = client.post(path, data=data, headers={"X-CSRF-Token": ""})

    assert response.status_code == 403


def test_submit_failure_cancels_only_unscheduled_job_and_keeps_batch_visible(client):
    preview = selection_form(client, [VIDEO_ID, OTHER_VIDEO_ID])
    token = extract_hidden(preview.text, "confirmation")
    submitted = []

    def submit(job_id):
        if not submitted:
            submitted.append(job_id)
            raise RuntimeError("bunny-secret upstream response")
        submitted.append(job_id)

    client.app.state.runner.submit = submit
    response = client.post("/batches", data={
        "confirmation": token,
        "csrf_token": client.app.state.csrf_token,
    }, follow_redirects=False)

    assert response.status_code == 303
    assert len(submitted) == 2
    batch = client.get(response.headers["location"])
    assert batch.status_code == 200
    assert "Annullato" in batch.text
    assert "bunny-secret" not in batch.text
    assert "upstream response" not in batch.text
    repeated = client.post("/batches", data={"confirmation": token}, follow_redirects=False)
    assert repeated.status_code == 303
    assert repeated.headers["location"] == response.headers["location"]
    assert len(submitted) == 2


def test_missing_batch_is_404(client):
    assert client.get(f"/batches/{uuid4()}").status_code == 404


def test_batch_status_api_preserves_order_and_excludes_private_fields(client):
    batch, jobs = client.app.state.store.create_batch([
        ("https://private.example/first?token=secret-one", "Primo"),
        ("https://private.example/second?token=secret-two", "Secondo"),
    ])
    client.app.state.store.update(
        jobs[0].id, state=JobState.PROCESSING, progress=37, message="Analisi",
    )

    response = client.get(f"/api/batches/{batch.id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == str(batch.id)
    assert [job["id"] for job in payload["jobs"]] == [str(job.id) for job in jobs]
    assert payload["jobs"][0]["progress"] == 37
    serialized = response.text
    assert "source_url" not in serialized
    assert "report" not in serialized
    assert "secret-one" not in serialized and "secret-two" not in serialized


def test_missing_batch_status_api_is_404(client):
    assert client.get(f"/api/batches/{uuid4()}").status_code == 404


@pytest.mark.parametrize(("state", "message", "expected_progress"), [
    (JobState.COMPLETED, "Completato", 100),
    (JobState.FAILED, "Errore di elaborazione", 37),
    (JobState.CANCELLED, "Annullato", 37),
])
def test_batch_refresh_shows_progress_and_terminal_transitions(client, state, message, expected_progress):
    batch, jobs = client.app.state.store.create_batch([("https://private.example/source", "Corso")])
    location = f"/batches/{batch.id}"
    page = client.get(location).text
    assert re.search(rf'<a id="refresh-batch"[^>]+href="{location}"', page)
    assert 'batch.js' in page
    assert f'data-batch-id="{batch.id}"' in page
    assert f'data-job-id="{jobs[0].id}"' in page
    assert 'data-job-message' in page and 'data-job-label' in page and 'data-job-progress' in page
    assert 'id="batch-poll-status"' in page
    assert f'for="batch-progress-{jobs[0].id}"' in page
    assert re.search(r'<progress[^>]+value="0"[^>]+max="100"', page)
    client.app.state.store.update(jobs[0].id, state=JobState.PROCESSING, progress=37, message="Analisi in corso")
    page = client.get(location).text
    assert "Analisi in corso" in page and "37%" in page
    assert re.search(r'<progress[^>]+value="37"[^>]+max="100"', page)
    client.app.state.store.update(jobs[0].id, state=state, message=message)
    page = client.get(location).text
    assert message in page and f"{expected_progress}%" in page
    assert "private.example" not in page


def test_exports_require_completion_and_queued_job_can_be_cancelled(client):
    job = client.app.state.store.create("https://private.example/source")
    for extension in ("md", "txt", "json"):
        assert client.get(f"/jobs/{job.id}/report.{extension}").status_code == 409
    response = client.post(f"/jobs/{job.id}/cancel", follow_redirects=False)
    assert response.status_code == 303
    assert client.get(f"/api/jobs/{job.id}").json()["state"] == "cancelled"


@pytest.mark.parametrize("single_segment", [False, True])
def test_historical_json_requires_reanalysis_before_any_external_work(client, monkeypatch, single_segment):
    report = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    if single_segment:
        report.interventions = [Intervention(
            id="i001", start_seconds=0, end_seconds=report.duration_seconds, tipo="intervento",
            relatori=[], titolo="Intervento unico", sintesi="Spiegazione completa.",
            punti_chiave=["Uno", "Due", "Tre"], confidenza=.9,
        )]
    store = client.app.state.store
    job = store.create("not-even-a-bunny-url", source_title="Report storico")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    original_inventory, original_bunny = client.app.state.inventory, client.app.state.bunny
    client.app.state.inventory = _ForbiddenProvider()
    client.app.state.bunny = _ForbiddenProvider()
    client.app.state.assemblyai = _ForbiddenProvider()
    client.app.state.openai = _ForbiddenProvider()
    monkeypatch.setattr(
        "app.materials.parse_material_source",
        lambda *args: (_ for _ in ()).throw(AssertionError("material checker called")),
    )
    monkeypatch.setattr(
        "app.web.build_intermediate_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("JSON builder called")),
    )

    response = client.get(f"/jobs/{job.id}/report.json")

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Rianalisi necessaria per il formato granulare",
    }
    assert client.get(f"/jobs/{job.id}/report.md").status_code == 200
    assert client.get(f"/jobs/{job.id}/report.txt").status_code == 200
    page = client.get(f"/jobs/{job.id}").text
    assert "Rianalisi necessaria" in page
    assert f'href="/#catalog-title"' in page
    assert "Download JSON v1.1" not in page
    client.app.state.inventory, client.app.state.bunny = original_inventory, original_bunny
    home = client.get("/").text
    assert "Rianalisi necessaria" in home
    assert f'href="/jobs/{job.id}/report.json"' not in home


def test_completed_job_downloads_and_page_escape_untrusted_content(client):
    report = add_complete_boundary_evidence(
        AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    )
    report.title = "<script>alert('unsafe')</script>"
    store = client.app.state.store
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    client.app.state.inventory.fetch = lambda: [InventoryCourse(
        id="0:2", foglio="Formazione", gid="0", posizione_foglio=0, riga=2,
        titolo="Corso inventario", relatori_attesi=[], materiali=["dispensa.pdf"],
        link="bunny", colonna_link="D", guid_esplicito=UUID(VIDEO_ID),
    )]
    client.app.state.bunny.get_metadata = lambda _: (_ for _ in ()).throw(
        AssertionError("A saved JSON report must not read Bunny metadata")
    )
    client.app.state.assemblyai = _ForbiddenProvider()
    client.app.state.openai = _ForbiddenProvider()
    for extension, mime in (("md", "text/markdown"), ("txt", "text/plain")):
        response = client.get(f"/jobs/{job.id}/report.{extension}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(mime)
        assert "attachment" in response.headers["content-disposition"]
        assert report.synopsis in response.text
        for section in ("Sinossi", "Relatori", "Slide"):
            assert section in response.text
        for obsolete_section in ("Punti chiave", "Obiettivi formativi"):
            assert obsolete_section not in response.text
    json_response = client.get(f"/jobs/{job.id}/report.json")
    assert json_response.status_code == 200
    assert json_response.headers["content-type"].startswith("application/json")
    assert "attachment" in json_response.headers["content-disposition"]
    assert 'filename="report-intermedio-' in json_response.headers["content-disposition"]
    payload = json_response.json()
    assert set(payload) == {
        "versione", "stato", "corso", "relatori", "video", "verifiche_richieste",
    }
    assert payload["versione"] == 1
    assert payload["video"][0]["guid"] == VIDEO_ID
    assert payload["video"][0]["chiave"] == "v1"
    assert payload["video"][0]["ordine"] == 1
    assert payload["video"][0]["durata_secondi"] == 3720
    assert payload["relatori"][0]["nome"] == "Giulia Bianchi"
    assert "Relatore 2" not in [speaker["nome"] for speaker in payload["relatori"]]
    assert payload["video"][0]["slide"][0] == {
        "inizio": "0:01:35",
        "titolo": "Agenda",
        "testo_principale": "Obiettivi · Flusso di pubblicazione",
        "confidenza": 0.95,
    }
    assert payload["video"][0]["materiali"] == []
    assert "incertezze" not in payload
    assert "transcript" not in json_response.text.lower()
    page = client.get(f"/jobs/{job.id}")
    assert "<script>alert('unsafe')</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    for section in ("Sinossi", "Relatori", "Slide"):
        assert section in page.text
    for obsolete_section in ("Punti chiave", "Obiettivi formativi"):
        assert obsolete_section not in page.text
    for control in ("Download JSON v1.1", "Download Markdown", "Download TXT", "Stampa / Salva PDF"):
        assert control in page.text
    assert f'action="/jobs/{job.id}/delete"' in page.text
    assert "non elimina il video da Bunny" in page.text


def test_video_json_export_serializes_each_intervention_with_exact_hms(client):
    report = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    report.bunny_title = "Titolo Bunny originale"
    report.interventions = [
        Intervention(
            id="lead-in", start_seconds=0, end_seconds=125, tipo="saluti",
            relatori=[], titolo="Attesa", sintesi="Attesa iniziale.",
            punti_chiave=[], confidenza=.92,
        ),
        Intervention(
        id="i001", start_seconds=125, end_seconds=905, tipo="intervento",
        relatori=["Marco Rossi"], titolo="Assetti di governance",
        sintesi="Il relatore illustra gli assetti di governance.",
        punti_chiave=["Organi", "Deleghe", "Controlli"], confidenza=.92,
        ),
        Intervention(
            id="tail", start_seconds=905, end_seconds=3720, tipo="saluti",
            relatori=[], titolo="Coda", sintesi="Attesa finale.",
            punti_chiave=[], confidenza=.92,
        ),
    ]
    add_complete_boundary_evidence(report)
    store = client.app.state.store
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)

    payload = client.get(f"/jobs/{job.id}/report.json").json()

    video = payload["video"][0]
    assert video["titolo_bunny"] == "Titolo Bunny originale"
    assert video["chiave"] == "v1"
    assert video["ordine"] == 1
    assert video["interventi"][1] == {
        "id": "v1-i002", "inizio": "0:02:05", "fine": "0:15:05",
        "tipo": "intervento", "relatori": ["Marco Rossi"],
        "titolo": "Assetti di governance",
        "sintesi": "Il relatore illustra gli assetti di governance.",
        "punti_chiave": ["Organi", "Deleghe", "Controlli"], "accesso": "pubblico",
        "confidenza": .92,
        "blocco": "v1-b002", "capitolo_numero": 1, "capitoli_blocco": 1,
        "confine_inizio": {"motivo_editoriale": "inizio_blocco", "regola_audio": "long_pause"},
    }
    assert "incertezze" not in payload


@pytest.mark.parametrize("materials", [[], ["https://[::1/dispensa.pdf"]])
def test_json_export_preserves_accented_identity_and_survives_malformed_material(client, materials):
    report = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    report.speakers = [report.speakers[0].model_copy(update={"display_name": "José Núñez"})]
    report.interventions = [Intervention(
        id="i001", start_seconds=0, end_seconds=600, tipo="intervento",
        relatori=["Jose Nunez"], titolo="Governance", sintesi="Assetti e controlli.",
        punti_chiave=["Organi", "Deleghe", "Controlli"], confidenza=.92,
    )]
    report.duration_seconds = 600
    report.slides = report.slides[:1]
    add_complete_boundary_evidence(report)
    store = client.app.state.store
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    stored = store.get(job.id).model_dump()
    client.app.state.inventory.fetch = lambda: [InventoryCourse(
        id="0:2", foglio="Formazione", gid="0", posizione_foglio=0, riga=2,
        titolo="Corso", relatori_attesi=[], materiali=materials,
        link="bunny", colonna_link="D", guid_esplicito=UUID(VIDEO_ID),
    )]
    client.app.state.bunny = _ForbiddenProvider()
    client.app.state.assemblyai = _ForbiddenProvider()
    client.app.state.openai = _ForbiddenProvider()

    response = client.get(f"/jobs/{job.id}/report.json")

    assert response.status_code == 200
    payload = response.json()
    assert [speaker["nome"] for speaker in payload["relatori"]] == ["José Núñez"]
    assert payload["video"][0]["interventi"][0]["relatori"] == ["José Núñez"]
    assert payload["video"][0]["materiali"] == []
    assert store.get(job.id).model_dump() == stored


def test_json_export_survives_inventory_failure_without_leaking_cell_data(client):
    report = add_complete_boundary_evidence(
        AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    )
    store = client.app.state.store
    job = store.create(f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    client.app.state.inventory.fetch = lambda: (_ for _ in ()).throw(
        InventoryError("Cella privata: segreto del foglio")
    )

    response = client.get(f"/jobs/{job.id}/report.json")

    assert response.status_code == 200
    assert response.json()["video"][0]["materiali"] == []
    assert "Cella privata" not in response.text


class _ForbiddenProvider:
    def __getattr__(self, name):
        raise AssertionError(f"Saved JSON reports must not call {name}")


def test_completed_report_can_be_deleted_without_calling_bunny(client):
    report = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    store = client.app.state.store
    job = store.create("canonical-source", source_title="Da eliminare")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED, report=report)
    client.app.state.bunny.get_metadata = lambda _: (_ for _ in ()).throw(
        AssertionError("Deleting a local report must not call Bunny")
    )

    response = client.post(f"/jobs/{job.id}/delete", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get(f"/jobs/{job.id}").status_code == 404
    assert client.get(f"/jobs/{job.id}/report.txt").status_code == 404


def test_report_deletion_rejects_missing_noncompleted_and_invalid_csrf(client):
    queued = client.app.state.store.create("queued")

    assert client.post(f"/jobs/{queued.id}/delete").status_code == 409
    assert client.app.state.store.get(queued.id).state == JobState.QUEUED
    assert client.post(f"/jobs/{uuid4()}/delete").status_code == 404
    assert client.post("/jobs/not-a-uuid/delete").status_code == 404
    assert client.post(
        f"/jobs/{queued.id}/delete", headers={"X-CSRF-Token": ""},
    ).status_code == 403


def test_completed_job_without_readable_report_still_exposes_delete_action(client):
    store = client.app.state.store
    job = store.create("canonical-source", source_title="Non leggibile")
    store.update(job.id, state=JobState.PROCESSING)
    store.update(job.id, state=JobState.COMPLETED)

    page = client.get(f"/jobs/{job.id}")

    status_panel = re.search(
        r'<section class="panel no-print" aria-label="Stato del lavoro">(.*?)</section>',
        page.text,
        re.S,
    )[1]
    assert f'action="/jobs/{job.id}/delete"' in status_panel


def inventory_course():
    return InventoryCourse(
        id="0:2", foglio="Formazione", gid="0", posizione_foglio=0, riga=2,
        titolo="Corso di prova", relatori_attesi=[], materiali=[], link="bunny",
        colonna_link="D", guid_esplicito=None,
    )


def course_report():
    report = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    report.duration_seconds = 3600
    report.bunny_title = VIDEO_TITLE
    report.interventions = [Intervention(
        id="i001", start_seconds=0, end_seconds=3600, tipo="intervento",
        relatori=["Marco Rossi"], titolo="Corso di prova",
        sintesi="Il relatore sviluppa il tema del corso.",
        punti_chiave=["Uno", "Due", "Tre"], confidenza=.9,
    )]
    return report


def prepare_course(client):
    client.app.state.course_store.sync_inventory([inventory_course()])
    client.app.state.course_store.confirm_videos("0:2", [UUID(VIDEO_ID)])


def test_course_preview_and_start_are_bound_to_confirmed_order(client):
    prepare_course(client)
    submitted = []
    client.app.state.runner.submit = submitted.append

    preview = client.get("/courses/0:2/analysis-preview")
    token = extract_hidden(preview.text, "confirmation")
    started = client.post(
        "/courses/0:2/analyze", data={"confirmation": token}, follow_redirects=False,
    )

    assert preview.status_code == 200
    assert "L'analisi a pagamento parte solo dopo questa conferma" in preview.text
    assert started.status_code == 303
    assert started.headers["location"] == "/courses/0:2"
    record = client.app.state.course_store.get_course("0:2")
    assert len(record.job_ids) == len(submitted) == 1
    assert record.batch_id is not None


def test_course_start_rejects_stale_video_association(client):
    prepare_course(client)
    token = extract_hidden(client.get("/courses/0:2/analysis-preview").text, "confirmation")
    client.app.state.course_store.confirm_videos("0:2", [UUID(OTHER_VIDEO_ID)])

    response = client.post("/courses/0:2/analyze", data={"confirmation": token})

    assert response.status_code == 422
    assert client.app.state.course_store.get_course("0:2").job_ids == []


def test_course_status_assembles_one_intermediate_report_and_hides_report_bodies(client):
    prepare_course(client)
    client.app.state.runner.submit = lambda _job_id: None
    token = extract_hidden(client.get("/courses/0:2/analysis-preview").text, "confirmation")
    client.post("/courses/0:2/analyze", data={"confirmation": token})
    record = client.app.state.course_store.get_course("0:2")
    job_id = record.job_ids[0]
    client.app.state.store.update(job_id, state=JobState.PROCESSING)
    client.app.state.store.update(job_id, state=JobState.COMPLETED, report=course_report())

    response = client.get("/api/courses/0:2")

    assert response.status_code == 200
    assert response.json()["stato"] == "da_verificare"
    assert response.json()["intermedio_disponibile"] is True
    saved = client.app.state.course_store.get_course("0:2").intermediate
    assert saved.video[0].guid == VIDEO_ID
    assert "report" not in response.text
    assert "source_url" not in response.text
    assert "iframe.mediadelivery.net" not in response.text


def intermediate_report(*, critical=False):
    speakers = [] if critical else [{
        "nome": "Marco Rossi", "ruolo": "Relatore", "confidenza": .95,
        "origine_nome": ["audio"],
    }]
    verifications = [{
        "livello": "critico", "codice": "RELATORE_NON_IDENTIFICATO",
        "video": "v1", "intervento": "v1-i001", "campo": "relatori",
        "messaggio": "Identificare il relatore.",
    }] if critical else []
    return IntermediateCourseReport.model_validate({
        "versione": 1, "stato": "da_verificare",
        "corso": {
            "titolo": "Corso di prova", "sinossi_corso": "Sintesi del corso.",
            "inventario": {"foglio": "Formazione", "ordine": 2},
        },
        "relatori": speakers,
        "video": [{
            "chiave": "v1", "guid": VIDEO_ID, "titolo_bunny": VIDEO_TITLE,
            "durata_secondi": 3600, "ordine": 1,
            "interventi": [{
                "id": "v1-i001", "inizio": "0:00:00", "fine": "1:00:00",
                "tipo": "intervento", "relatori": [] if critical else ["Marco Rossi"],
                "titolo": "Corso di prova", "sintesi": "Sviluppo del tema.",
                "punti_chiave": ["Uno", "Due", "Tre"], "confidenza": .9,
            }],
            "slide": [{
                "inizio": "0:05:00", "titolo": "Slide introduttiva",
                "testo_principale": "Contenuto visibile", "confidenza": .8,
            }],
        }],
        "verifiche_richieste": verifications,
    })


def seed_intermediate(client, *, critical=False):
    prepare_course(client)
    client.app.state.course_store.attach_run("0:2", UUID(int=50), [UUID(int=51)])
    client.app.state.course_store.save_intermediate("0:2", intermediate_report(critical=critical))


def academy_result():
    question = {
        "testo": "Qual è un principio del corso?",
        "risposte": [
            {"testo": "La risposta corretta", "corretta": True},
            {"testo": "Distrattore uno"}, {"testo": "Distrattore due"},
            {"testo": "Distrattore tre"},
        ],
        "spiegazione": "La risposta è documentata nel contenuto.",
    }
    return AcademyImport.model_validate({
        "versione": 1,
        "corso": {
            "titolo": "Corso di prova", "sottotitolo": "Sottotitolo del corso",
            "area": "Governance", "prezzo": 97,
            "presentazione": "Primo paragrafo.\n\nSecondo paragrafo.\n\nTerzo paragrafo.",
            "competenze": ["Comprendere", "Valutare", "Distinguere", "Applicare", "Riconoscere"],
            "profili": ["Commercialisti | Che assistono le imprese."],
        },
        "relatori": [{"nome": "Marco Rossi"}],
        "video": [{
            "chiave": "v1", "sorgente": "bunny", "guid": VIDEO_ID,
            "durata_secondi": 3600, "titolo": VIDEO_TITLE,
        }],
        "moduli": [{
            "titolo": "Modulo 1 · Fondamenti", "lezioni": [
                {"titolo": "Prima lezione", "video": "v1", "inizio": "0:00:00",
                 "fine": "0:10:00", "relatori": ["Marco Rossi"],
                 "descrizione": "Prima riga.\nSeconda riga.", "hero": True, "anteprima": True},
                {"titolo": "Seconda lezione", "video": "v1", "inizio": "0:10:00",
                 "fine": "0:20:00", "relatori": ["Marco Rossi"],
                 "descrizione": "Prima riga.\nSeconda riga."},
                {"titolo": "Verifica", "tipo": "quiz", "domande": [question] * 3},
            ],
        }],
    })


def test_inventory_page_no_longer_exposes_course_matching(client):
    client.app.state.course_store.sync_inventory([
        inventory_course(),
        InventoryCourse(
            id="0:3", foglio="Formazione", gid="0", posizione_foglio=0, riga=3,
            titolo="Secondo corso", relatori_attesi=[], materiali=[], link=None,
            colonna_link="D", guid_esplicito=None,
        ),
    ])

    response = client.get("/inventory")

    assert response.status_code == 200
    assert "Catalogo video" in response.text
    assert "Inventario corsi" not in response.text
    assert 'action="/inventory/sync"' not in response.text
    assert "inventory.js" not in response.text


def test_inventory_sync_saves_rows_and_proposals_without_writing_sheet(client):
    writes = []
    client.app.state.inventory.fetch = lambda: [inventory_course()]
    client.app.state.inventory.update_link = lambda *_: writes.append(True)
    client.app.state.bunny.list_videos = lambda: catalog(
        catalog_video(VIDEO_ID, VIDEO_TITLE, 3600)
    )

    response = client.post("/inventory/sync", follow_redirects=False)

    assert response.status_code == 303
    record = client.app.state.course_store.get_course("0:2")
    assert record.proposte[0].video_id == UUID(VIDEO_ID)
    assert writes == []


def test_match_confirmation_and_separate_sheet_write_are_explicit(client):
    client.app.state.course_store.sync_inventory([inventory_course()])
    from app.inventory import MatchProposal
    client.app.state.course_store.replace_proposals([
        MatchProposal(course_id="0:2", video_id=UUID(VIDEO_ID), score=1, reason="titolo_univoco")
    ])
    written = []
    client.app.state.inventory.update_link = lambda course, url: written.append((course.id, url))

    confirmed = client.post(
        "/courses/0:2/match", data={"video_id": VIDEO_ID}, follow_redirects=False,
    )
    assert confirmed.status_code == 303
    assert client.app.state.course_store.get_course("0:2").video_confermati == [UUID(VIDEO_ID)]
    assert written == []

    saved = client.post("/courses/0:2/write-link", follow_redirects=False)
    assert saved.status_code == 303
    assert written == [("0:2", f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}")]


def test_review_page_links_timestamps_to_streaming_player_and_downloads_json(client):
    seed_intermediate(client)

    page = client.get("/courses/0:2/review")
    download = client.get("/courses/0:2/report-intermedio.json")

    assert page.status_code == 200
    assert 'data-intervention-row' in page.text
    assert 'data-speaker-list' in page.text
    assert 'data-field="inizio"' in page.text
    assert f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}" in page.text
    assert "#t=0" in page.text
    assert "course_review.js" in page.text
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/json")
    assert "attachment" in download.headers["content-disposition"]
    assert download.json()["video"][0]["interventi"][0]["inizio"] == "0:00:00"


def test_review_api_validates_timeline_and_saves_literal_edits(client):
    seed_intermediate(client)
    payload = intermediate_report().model_dump(mode="json", by_alias=True, exclude_none=True)
    payload["video"][0]["interventi"][0]["fine"] = "0:59:59"
    assert client.put("/api/courses/0:2/report", json=payload).status_code == 422

    payload = intermediate_report().model_dump(mode="json", by_alias=True, exclude_none=True)
    payload["video"][0]["interventi"][0]["titolo"] = "<script>testo letterale</script>"
    response = client.put("/api/courses/0:2/report", json=payload)

    assert response.status_code == 200
    saved = client.app.state.course_store.get_course("0:2").intermediate
    assert saved.video[0].interventi[0].titolo == "<script>testo letterale</script>"
    assert "<script>testo letterale</script>" not in client.get("/courses/0:2/review").text


def test_review_api_can_resolve_an_unknown_speaker_with_a_human_verified_name(client):
    seed_intermediate(client, critical=True)
    payload = intermediate_report(critical=True).model_dump(
        mode="json", by_alias=True, exclude_none=True,
    )
    payload["relatori"] = [{
        "nome": "Maria Verdi", "ruolo": "Relatrice", "confidenza": 1,
        "origine_nome": ["revisione"],
    }]
    payload["video"][0]["interventi"][0]["relatori"] = ["Maria Verdi"]

    saved = client.put("/api/courses/0:2/report", json=payload)
    confirmed = client.post("/courses/0:2/confirm", follow_redirects=False)

    assert saved.status_code == 200
    assert saved.json()["verifiche_richieste"] == 0
    assert confirmed.status_code == 303
    assert client.app.state.course_store.get_course("0:2").intermediate.relatori[0].nome == "Maria Verdi"


def test_critical_verification_blocks_confirmation_then_valid_report_confirms(client):
    seed_intermediate(client, critical=True)

    blocked = client.post("/courses/0:2/confirm")
    assert blocked.status_code == 409
    client.app.state.course_store.save_intermediate("0:2", intermediate_report())
    confirmed = client.post("/courses/0:2/confirm", follow_redirects=False)

    assert confirmed.status_code == 303
    assert client.app.state.course_store.get_course("0:2").intermediate.stato == "verificato"


def test_verified_report_generates_persists_and_downloads_academy_json(client):
    seed_intermediate(client)
    source = intermediate_report().model_copy(update={"stato": "verificato"})
    client.app.state.course_store.confirm_intermediate("0:2", source)

    calls = []
    client.app.state.academy_generator = type("FakeGenerator", (), {
        "generate": lambda self, report: calls.append(report) or academy_result(),
    })()

    generated = client.post("/courses/0:2/generate-academy", follow_redirects=False)
    download = client.get("/courses/0:2/import-academy.json")
    page = client.get("/courses/0:2/review")

    assert generated.status_code == 303
    assert generated.headers["location"] == "/courses/0:2/review"
    assert len(calls) == 1 and calls[0].stato == "verificato"
    assert download.status_code == 200
    assert download.json()["corso"]["prezzo"] == 97
    assert "attachment" in download.headers["content-disposition"]
    assert "Scarica JSON Academy" in page.text
    saved = client.app.state.course_store.get_course("0:2")
    assert (saved.contract_version, saved.prompt_version) == (1, 1)


def test_academy_generation_is_blocked_before_report_confirmation(client):
    seed_intermediate(client)
    calls = []
    client.app.state.academy_generator = type("FakeGenerator", (), {
        "generate": lambda self, report: calls.append(report) or academy_result(),
    })()

    response = client.post("/courses/0:2/generate-academy")

    assert response.status_code == 409
    assert calls == []
    assert client.app.state.course_store.get_course("0:2").academy_json is None


@pytest.mark.parametrize("method,path", [
    ("post", "/inventory/sync"),
    ("post", "/courses/0:2/match"),
    ("put", "/api/courses/0:2/report"),
    ("post", "/courses/0:2/confirm"),
    ("post", "/courses/0:2/generate-academy"),
])
def test_inventory_and_review_mutations_require_csrf(client, method, path):
    response = getattr(client, method)(path, headers={"X-CSRF-Token": ""})
    assert response.status_code == 403
