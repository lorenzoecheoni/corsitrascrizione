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
from app.bunny import BunnyCatalog, BunnyVideoMetadata
from app.config import Settings
from app.models import AcademyReport


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
