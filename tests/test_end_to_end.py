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
from app.bunny import BunnyVideoMetadata
from app.config import Settings
from app.models import AcademyReport


VIDEO_ID = "00000000-0000-0000-0000-000000000001"
SOURCE = f"https://iframe.mediadelivery.net/embed/123/{VIDEO_ID}"


class OfflineOpenAI:
    def __init__(self, **kwargs):
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.transcribe))
        self.responses = SimpleNamespace(parse=self.parse)

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
        else:
            payload = json.loads(input)
            assert payload["transcription"]["audio_seconds"] > 0
            assert payload["transcription"]["segments"][0]["diarization_label"] == "chunk-0:A"
            data = {
                "title": "Corso sintetico", "duration_seconds": 2, "detected_language": "it",
                "synopsis": "Introduzione al corso sintetico.",
                "extended_description": "Una breve introduzione per il collaudo operativo.",
                "target_audience": ["Redattori"], "prerequisites": ["Nessuno dichiarato"],
                "learning_objectives": ["Comprendere il corso"],
                "speakers": [{"id": "a", "display_name": "Relatore 1", "role": None,
                              "confidence": "bassa", "evidence": []}],
                "interventions": [{"start_seconds": 0, "end_seconds": 2,
                                   "speaker_ids": ["a"], "summary": "Introduzione"}],
                "chapters": [{"start_seconds": 0, "end_seconds": 2,
                              "title": "Introduzione", "summary": "Presentazione del corso"}],
                "slides": [], "topics": ["Corso"], "keywords": ["Introduzione"],
                "key_takeaways": ["Seguire il corso"], "uncertainties": [],
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
                        app_password="TEST_ONLY_PASSWORD", temp_root=str(temporary), _env_file=None)
    app = main.create_app(settings)
    auth = ("team", settings.app_password)
    location = None
    try:
        with TestClient(app) as client:
            assert client.get("/").status_code == 401
            assert client.get("/healthz").json() == {"status": "ok"}
            assert client.get("/", auth=auth).status_code == 200
            page = client.get("/", auth=auth)
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)[1]
            preview = client.post("/preview", data={"source_url": SOURCE, "csrf_token": csrf}, auth=auth)
            assert preview.status_code == 200 and "Titolo originale Bunny" in preview.text
            confirmation = re.search(r'name="confirmation" value="([^"]+)"', preview.text)[1]
            response = client.post("/jobs", data={"confirmation": confirmation, "csrf_token": csrf}, auth=auth, follow_redirects=False)
            assert response.status_code == 303
            location = response.headers["location"]
            while monotonic() - started < 12:
                status = client.get("/api" + location, auth=auth).json()
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
            page = client.get(location, auth=auth)
            assert "Corso sintetico" in page.text
            assert "Stampa / Salva PDF" in page.text
            for extension, mime in (("md", "text/markdown"), ("txt", "text/plain")):
                download = client.get(f"{location}/report.{extension}", auth=auth)
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
    with TestClient(main.create_app(settings)) as restarted:
        assert restarted.get(location, auth=auth).status_code == 404
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
