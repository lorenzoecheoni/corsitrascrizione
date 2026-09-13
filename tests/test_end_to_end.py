"""Offline release smoke: real web/worker/pipeline/FFmpeg, fake remote APIs."""

import json
import socket
import subprocess
import sys
from pathlib import Path
import re
from time import monotonic, sleep
from types import SimpleNamespace

from fastapi.testclient import TestClient
from openai.types.audio import TranscriptionDiarized

import app.main as main
from app.analysis import SlideBatchResult
from app.analysis_chunks import WindowAnalysis
from app.bunny import BunnyCatalog, BunnyCatalogVideo, BunnyVideoMetadata
from app.config import Settings
from app.course_models import AcademyImport
from app.inventory import InventoryCourse
from app.jobs import JobState
from app.models import AcademyReport, Evidence, Intervention, SpeakerProfile


VIDEO_ID = "00000000-0000-0000-0000-000000000001"
SOURCE = f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}"


class OfflineOpenAI:
    def __init__(self, **kwargs):
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.transcribe))
        self.responses = SimpleNamespace(with_raw_response=SimpleNamespace(parse=self.raw_parse))

    def raw_parse(self, **kwargs):
        response = self.parse(**kwargs)
        return SimpleNamespace(headers={}, parse=lambda: response)

    def with_options(self, **kwargs):
        return self

    def transcribe(self, *, file, **kwargs):
        # Require actual encoded audio from the production FFmpeg extraction.
        assert b"ftyp" in file.read(32)
        return TranscriptionDiarized.model_validate({
            "duration": 2, "task": "transcribe", "text": "Introduzione al corso.",
            "segments": [{"id": "0", "type": "transcript.text.segment", "start": 0,
                          "end": 2, "speaker": "A", "text": "Introduzione al corso."}],
            "usage": {"type": "duration", "seconds": 2},
        })

    def parse(self, *, text_format, input, **kwargs):
        if text_format is SlideBatchResult:
            parts = input[0]["content"]
            assert any(p.get("image_url", "").startswith("data:image/jpeg;base64,") for p in parts)
            data = {"frames": [
                {"timestamp_seconds": float(p["text"].split("=")[1]), "kind": "slide",
                 "title": "Introduzione", "visible_content": ["Corso"], "confidence": "alta"}
                for p in parts if p["type"] == "input_text"
            ]}
        elif text_format is WindowAnalysis:
            payload = json.loads(input)
            assert payload["segments"][0]["diarization_label"] == "chunk-0:A"
            data = {
                "detected_language": "it", "synopsis_notes": ["Introduzione al corso sintetico."],
                "speakers": [{"diarization_labels": ["chunk-0:A"], "display_name": None,
                              "role": None, "confidence": "bassa", "evidence": []}],
                "uncertainties": [],
                "interventions": [{
                    "segment_indexes": [0], "tipo": "intervento",
                    "diarization_labels": ["chunk-0:A"], "titolo": "Introduzione",
                    "sintesi": "Introduzione al corso.",
                    "punti_chiave": ["Corso", "Obiettivi", "Programma"],
                    "confidenza": 0.9,
                }],
            }
        else:
            payload = json.loads(input)
            assert "segments" not in payload and "transcription" not in payload
            assert payload["synopsis_notes"] == ["Introduzione al corso sintetico."]
            data = {
                "title": "Corso sintetico", "duration_seconds": 2, "detected_language": "it",
                "synopsis": "Introduzione al corso sintetico.",
                "speakers": [{"id": "a", "display_name": "Relatore 1", "role": None,
                              "confidence": "bassa", "evidence": []}],
                "uncertainties": [],
            }
        return SimpleNamespace(status="completed", output_parsed=text_format.model_validate(data))


def run_smoke_scenario(tmp_path, monkeypatch):
    # Catches lost production wiring, export/schema regressions and media leaks.
    assert callable(getattr(main, "build_services", None)), "Expose the substitutable service factory"
    started = monotonic()
    video = tmp_path / "synthetic.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=white:s=160x90:r=5:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-shortest",
        "-c:v", "mpeg4", "-c:a", "aac", str(video),
    ], check=True, capture_output=True, timeout=5)
    temporary = tmp_path / "processing"
    temporary.mkdir()

    class OfflineBunny:
        def __init__(self, settings):
            pass

        def get_metadata(self, video_id):
            assert video_id == VIDEO_ID
            return BunnyVideoMetadata(video_id=VIDEO_ID, title="Titolo originale Bunny", duration_seconds=2,
                                      status=3, available_resolutions=[240])

        def list_videos(self):
            return BunnyCatalog(videos=[], total_items=0)

        def select_hls_url(self, metadata, *, cancellation_event):
            assert str(metadata.video_id) == VIDEO_ID
            return str(video)

    def no_network(*args, **kwargs):
        raise AssertionError("The offline smoke test must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(main, "BunnyClient", OfflineBunny)
    monkeypatch.setattr(main, "OpenAI", OfflineOpenAI)
    settings = Settings(bunny_library_id=123, bunny_stream_api_key="TEST_ONLY_BUNNY",
                        bunny_cdn_hostname="cdn.example.invalid", openai_api_key="TEST_ONLY_OPENAI",
                        app_password="TEST_ONLY_PASSWORD", temp_root=str(temporary),
                        database_path=str(tmp_path / "reports.sqlite3"), _env_file=None)
    app = main.create_app(settings)
    location = None
    try:
        with TestClient(app) as client:
            assert client.get("/", follow_redirects=False).status_code == 303
            assert client.get("/healthz").json() == {"status": "ok"}
            login = client.post("/login", data={
                "username": "team", "password": settings.app_password,
                "csrf_token": app.state.csrf_token,
            }, follow_redirects=False)
            assert login.status_code == 303
            assert client.get("/").status_code == 200
            page = client.get("/")
            csrf = app.state.csrf_token
            preview = client.post("/preview", data={"source_url": SOURCE, "csrf_token": csrf})
            assert preview.status_code == 200 and "Titolo originale Bunny" in preview.text
            confirmation = re.search(r'name="confirmation" value="([^"]+)"', preview.text)[1]
            response = client.post("/jobs", data={"confirmation": confirmation, "csrf_token": csrf}, follow_redirects=False)
            assert response.status_code == 303
            location = response.headers["location"]
            while monotonic() - started < 12:
                status = client.get("/api" + location).json()
                if status["state"] in {"completed", "failed", "cancelled"}:
                    break
                sleep(.02)
            assert status["state"] == "completed", status.get("error")
            report = AcademyReport.model_validate(status["report"])
            assert report.duration_seconds == 2
            assert report.slides[0].timestamp_seconds == 0
            assert report.cost.estimated_high_usd > 0
            assert report.bunny_title == "Titolo originale Bunny"
            assert report.usage.transcription.provider_audio_seconds == 2
            assert report.usage.responses.requests == 2
            assert status["progress"] == 100
            page = client.get(location)
            assert "Corso sintetico" in page.text
            assert "Stampa / Salva PDF" in page.text
            for extension, mime in (("md", "text/markdown"), ("txt", "text/plain")):
                download = client.get(f"{location}/report.{extension}")
                assert download.status_code == 200
                assert download.headers["content-type"].startswith(mime)
                assert "attachment" in download.headers["content-disposition"]
                assert "Introduzione al corso sintetico." in download.text
            assert list(temporary.iterdir()) == []
    finally:
        if location:
            from uuid import UUID
            app.state.runner.cancel(UUID(location.rsplit("/", 1)[1]))
        app.state.runner.shutdown(wait=True)
    restarted_app = main.create_app(settings)
    with TestClient(restarted_app) as restarted:
        login = restarted.post("/login", data={
            "username": "team", "password": settings.app_password,
            "csrf_token": restarted_app.state.csrf_token,
        }, follow_redirects=False)
        assert login.status_code == 303
        reopened = restarted.get(location)
        assert reopened.status_code == 200
        assert "Corso sintetico" in reopened.text
        assert restarted.get(f"{location}/report.txt").status_code == 200
    assert monotonic() - started < 15


def test_form_to_report_with_real_ffmpeg_and_ephemeral_cleanup(tmp_path):
    from smoke_support import run_bounded_smoke
    script = """
import sys
from pathlib import Path
import pytest
sys.path.insert(0, 'tests')
from test_end_to_end import run_smoke_scenario
with pytest.MonkeyPatch.context() as patch:
    run_smoke_scenario(Path(sys.argv[1]), patch)
"""
    run_bounded_smoke([sys.executable, "-c", script], tmp_path, timeout=12)
    assert not list(tmp_path.iterdir())


def _course_video_report(title):
    report = AcademyReport.model_validate_json(Path("tests/fixtures/report.json").read_text())
    report.title = title
    report.bunny_title = title
    report.duration_seconds = 600
    report.synopsis = f"Sintesi di {title}."
    report.speakers = [SpeakerProfile(
        id="mario-rossi", display_name="Mario Rossi", role="Relatore",
        confidence="alta", evidence=[Evidence(
            kind="introduzione", timestamp_seconds=0, note="Il relatore si presenta.",
        )],
    )]
    report.interventions = [Intervention(
        id="i001", start_seconds=0, end_seconds=600, tipo="intervento",
        relatori=["Mario Rossi"], titolo=title, sintesi=f"Contenuti di {title}.",
        punti_chiave=["Principio", "Applicazione", "Verifica"], confidenza=.95,
    )]
    report.slides = []
    return report


def _academy_from_source(source):
    question = {
        "testo": "Quale principio è illustrato nel corso?",
        "risposte": [
            {"testo": "Il principio documentato", "corretta": True},
            {"testo": "Un dato assente"}, {"testo": "Un tema estraneo"},
            {"testo": "Nessuno dei precedenti"},
        ],
        "spiegazione": "Il principio è presente nel report verificato.",
    }
    return AcademyImport.model_validate({
        "versione": 1,
        "corso": {
            "titolo": source.corso.titolo,
            "sottotitolo": "Percorso operativo per le holding",
            "area": "Governance", "prezzo": 97,
            "presentazione": "Primo paragrafo.\n\nSecondo paragrafo.\n\nTerzo paragrafo.",
            "competenze": ["Comprendere", "Valutare", "Distinguere", "Applicare", "Riconoscere"],
            "profili": ["Commercialisti | Che assistono gruppi societari."],
        },
        "relatori": [{"nome": "Mario Rossi", "ruolo": "Relatore"}],
        "video": [{
            "chiave": video.chiave, "sorgente": "bunny", "guid": video.guid,
            "durata_secondi": video.durata_secondi, "titolo": video.titolo_bunny,
        } for video in source.video],
        "moduli": [{
            "titolo": "Modulo 1 · Fondamenti", "lezioni": [
                {"titolo": source.video[0].interventi[0].titolo, "video": "v1",
                 "inizio": "0:00:00", "fine": "0:10:00", "relatori": ["Mario Rossi"],
                 "descrizione": "Principi iniziali.\nApplicazioni operative.",
                 "hero": True, "anteprima": True},
                {"titolo": source.video[1].interventi[0].titolo, "video": "v2",
                 "inizio": "0:00:00", "fine": "0:10:00", "relatori": ["Mario Rossi"],
                 "descrizione": "Sviluppo del tema.\nVerifica dei contenuti."},
                {"titolo": "Verifica", "tipo": "quiz", "domande": [question] * 3},
            ],
        }],
    })


def test_complete_multi_video_course_workflow_persists_both_json_files(tmp_path):
    database = tmp_path / "course-reports.sqlite3"
    settings = Settings(
        bunny_library_id=123, bunny_stream_api_key="TEST_ONLY_BUNNY",
        bunny_cdn_hostname="cdn.example.invalid", openai_api_key="TEST_ONLY_OPENAI",
        app_password="TEST_ONLY_PASSWORD", database_path=str(database), _env_file=None,
    )
    first_id = "00000000-0000-0000-0000-000000000011"
    second_id = "00000000-0000-0000-0000-000000000012"
    inventory_course = InventoryCourse(
        id="0:2", foglio="Formazione", gid="0", posizione_foglio=0, riga=2,
        titolo="Percorso completo", relatori_attesi=["Mario Rossi"], materiali=[],
        link=None, colonna_link="D", guid_esplicito=None,
    )
    catalog = BunnyCatalog(videos=[
        BunnyCatalogVideo(video_id=first_id, title="Prima parte", duration_seconds=600, status=3),
        BunnyCatalogVideo(video_id=second_id, title="Seconda parte", duration_seconds=600, status=3),
    ], total_items=2)

    app = main.create_app(settings)
    app.state.inventory.fetch = lambda: [inventory_course]
    app.state.bunny.list_videos = lambda: catalog
    app.state.bunny.get_metadata = lambda video_id: BunnyVideoMetadata(
        video_id=video_id,
        title="Prima parte" if str(video_id) == first_id else "Seconda parte",
        duration_seconds=600, status=3, available_resolutions=[240],
    )
    app.state.runner.submit = lambda _job_id: None
    app.state.academy_generator = type("OfflineAcademy", (), {
        "generate": lambda self, source: _academy_from_source(source),
    })()

    with TestClient(app) as client:
        assert client.post("/login", data={
            "username": "team", "password": settings.app_password,
        }, follow_redirects=False).status_code == 303
        client.headers["X-CSRF-Token"] = app.state.csrf_token
        assert client.post("/inventory/sync", follow_redirects=False).status_code == 303
        assert client.post("/courses/0:2/videos", data={
            "video_ids": [first_id, second_id],
        }, follow_redirects=False).status_code == 303
        preview = client.get("/courses/0:2/analysis-preview")
        confirmation = re.search(r'name="confirmation" value="([^"]+)"', preview.text)[1]
        assert client.post("/courses/0:2/analyze", data={
            "confirmation": confirmation,
        }, follow_redirects=False).status_code == 303

        run = app.state.course_store.get_course("0:2")
        for job_id, title in zip(run.job_ids, ["Prima parte", "Seconda parte"], strict=True):
            app.state.store.update(job_id, state=JobState.PROCESSING)
            app.state.store.update(
                job_id, state=JobState.COMPLETED, report=_course_video_report(title),
            )
        assert client.get("/api/courses/0:2").json()["intermedio_disponibile"] is True
        source = app.state.course_store.get_course("0:2").intermediate
        assert client.put(
            "/api/courses/0:2/report",
            json=source.model_dump(mode="json", by_alias=True, exclude_none=True),
        ).status_code == 200
        assert client.post("/courses/0:2/confirm", follow_redirects=False).status_code == 303
        assert client.post(
            "/courses/0:2/generate-academy", follow_redirects=False,
        ).status_code == 303
        intermediate = client.get("/courses/0:2/report-intermedio.json")
        academy = client.get("/courses/0:2/import-academy.json")
        assert intermediate.status_code == academy.status_code == 200
        assert [video["chiave"] for video in intermediate.json()["video"]] == ["v1", "v2"]
        assert academy.json()["corso"]["prezzo"] == 97
        assert app.state.course_store.get_course("0:2").stato == "pronto_academy"

    stored = database.read_bytes()
    assert b"transcript" not in stored.lower()
    assert b".m4a" not in stored and b".mp4" not in stored

    restarted_app = main.create_app(settings)
    with TestClient(restarted_app) as client:
        assert client.post("/login", data={
            "username": "team", "password": settings.app_password,
        }, follow_redirects=False).status_code == 303
        assert client.get("/courses/0:2/report-intermedio.json").status_code == 200
        assert client.get("/courses/0:2/import-academy.json").status_code == 200
        assert restarted_app.state.course_store.get_course("0:2").stato == "pronto_academy"
