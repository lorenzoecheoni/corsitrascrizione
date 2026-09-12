"""Exercise safe diagnostics and the opt-in synthetic provider proof."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID

from openai import OpenAI
import pytest

from app.analysis import OpenAIAnalyzer
from app.analysis_chunks import (
    MAX_CONSOLIDATION_CHARS,
    MAX_WINDOW_CHARS,
    ConsolidatedTextReport,
    WindowAnalysis,
    split_transcript_windows,
)
from app.bunny import BunnyVideoMetadata
from app.transcription import TranscriptSegment, TranscriptionResult


_SYNTHETIC_DURATION_SECONDS = 96 * 60
_SYNTHETIC_SEGMENT_TEXT = (
    "Sessione sintetica di formazione: il relatore presenta principi, esempi e conclusioni."
)


def test_synthetic_live_proof_requires_dedicated_opt_in(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-only")
    monkeypatch.delenv("RUN_LIVE_SYNTHETIC_ANALYSIS", raising=False)

    with pytest.raises(pytest.skip.Exception, match="dedicated opt-in"):
        _synthetic_live_api_key()


def test_captured_live_output_raises_static_failure():
    sentinel = "SYNTHETIC_PRIVATE_DIAGNOSTIC_72fada"

    with pytest.raises(pytest.fail.Exception) as caught:
        _fail_if_captured_live_output(sentinel, "")

    assert str(caught.value) == "Synthetic live analysis emitted unexpected output"
    assert sentinel not in str(caught.value)
    assert caught.value.pytrace is False


def _synthetic_live_api_key() -> str:
    if os.environ.get("RUN_LIVE_SYNTHETIC_ANALYSIS") != "1":
        pytest.skip(
            "Synthetic live proof requires dedicated opt-in "
            "RUN_LIVE_SYNTHETIC_ANALYSIS=1"
        )
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("Synthetic live proof requires OPENAI_API_KEY")
    return api_key


def _fail_if_captured_live_output(stdout: str, stderr: str) -> None:
    if stdout or stderr:
        raise pytest.fail.Exception(
            "Synthetic live analysis emitted unexpected output", pytrace=False
        )


class _SafeRecordingOpenAI:
    """Keep only payload lengths at the live-provider boundary."""

    def __init__(self, client: OpenAI) -> None:
        self._client = client.with_options(max_retries=0)
        self.requests: list[tuple[type, int]] = []
        self.responses = self
        self.with_raw_response = self

    def with_options(self, *, max_retries: int):
        return self

    def parse(self, **kwargs):
        payload = kwargs["input"]
        serialized = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.requests.append((kwargs["text_format"], len(serialized)))
        return self._client.responses.with_raw_response.parse(**kwargs)


def _synthetic_long_inputs():
    segments = [
        TranscriptSegment(
            start_seconds=index * 60,
            end_seconds=(index + 1) * 60,
            diarization_label="synthetic-speaker",
            text=_SYNTHETIC_SEGMENT_TEXT,
        )
        for index in range(96)
    ]
    return (
        BunnyVideoMetadata(
            video_id=UUID("12345678-1234-1234-1234-123456789abc"),
            title="Analisi sintetica a blocchi",
            duration_seconds=_SYNTHETIC_DURATION_SECONDS,
        ),
        TranscriptionResult(
            text="",
            segments=segments,
            audio_seconds=_SYNTHETIC_DURATION_SECONDS,
        ),
    )


@pytest.mark.live
def test_chunked_long_report_uses_bounded_synthetic_payloads(capsys):
    """Prove the production map/reduce path without Bunny, media, or images."""
    api_key = _synthetic_live_api_key()

    metadata, transcription = _synthetic_long_inputs()
    windows = split_transcript_windows(transcription.segments)
    assert metadata.duration_seconds == 5760
    assert len(windows) == 10
    assert all(len(window.to_payload()) <= MAX_WINDOW_CHARS for window in windows)

    client = OpenAI(api_key=api_key, max_retries=0)
    audited = _SafeRecordingOpenAI(client)
    started = time.monotonic()
    try:
        result = OpenAIAnalyzer(audited).analyze(metadata, transcription, frames=[])
        assert result.synopsis.strip()
        assert result.speakers
        window_payloads = [size for schema, size in audited.requests if schema is WindowAnalysis]
        consolidation_payloads = [
            size for schema, size in audited.requests if schema is ConsolidatedTextReport
        ]
        assert len(window_payloads) >= len(windows)
        assert consolidation_payloads
        assert all(size <= MAX_WINDOW_CHARS for size in window_payloads)
        assert all(size <= MAX_CONSOLIDATION_CHARS for size in consolidation_payloads)
    except Exception:
        raise pytest.fail.Exception(
            "Synthetic live analysis failed; inspect only safe provider status", pytrace=False,
        ) from None
    finally:
        client.close()

    elapsed_seconds = time.monotonic() - started
    captured = capsys.readouterr()
    _fail_if_captured_live_output(captured.out, captured.err)
    with capsys.disabled():
        print(
            "live_diagnostic "
            f"status=passed requests={result.usage.requests} "
            f"input_tokens={result.usage.input_tokens if result.usage.input_tokens is not None else 'unknown'} "
            f"output_tokens={result.usage.output_tokens if result.usage.output_tokens is not None else 'unknown'} "
            f"duration_seconds={int(metadata.duration_seconds)} "
            f"elapsed_seconds={elapsed_seconds:.2f} windows={len(windows)} "
            f"speakers={len(result.speakers)} slides={len(result.slides)} "
            f"uncertainties={len(result.uncertainties)}"
        )

@pytest.mark.parametrize("phase,message", [
    ("configuration", "Invalid live configuration (details withheld)"),
    ("analysis", "Live analysis failed; inspect only sanitized application diagnostics"),
])
def test_live_failure_output_does_not_disclose_private_input(tmp_path, phase, message):
    # Reintroducing exception chaining in either live boundary leaks this value.
    sentinel = "SYNTHETIC_PRIVATE_DIAGNOSTIC_72fada"
    env = dict(os.environ)
    env.update({
        "RUN_LIVE_BUNNY": "1",
        "BUNNY_LIBRARY_ID": sentinel if phase == "configuration" else "123",
        "BUNNY_STREAM_API_KEY": "TEST_ONLY_BUNNY",
        "BUNNY_CDN_HOSTNAME": "cdn.example.invalid",
        "BUNNY_TOKEN_AUTH_KEY": "",
        "OPENAI_API_KEY": "TEST_ONLY_OPENAI",
        "APP_PASSWORD": "TEST_ONLY_PASSWORD",
        "BUNNY_SAMPLE_VIDEO_URL": "https://example.invalid/authorized-sample",
        "DIAGNOSTIC_SENTINEL": sentinel,
        "PYTEST_ADDOPTS": "",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    })
    live_test = Path(__file__).with_name("test_live_bunny.py").resolve()
    script = """
import os
from pathlib import Path
import socket
import sys
import pytest

sys.path.insert(0, str(Path(sys.argv[1]).parents[1]))

def deny_network(*args, **kwargs):
    raise AssertionError("Offline diagnostic test attempted a network connection")

socket.socket.connect = deny_network

class OfflineFailure:
    def pytest_collection_modifyitems(self, items):
        def fail_services(settings):
            raise RuntimeError(os.environ["DIAGNOSTIC_SENTINEL"])
        for item in items:
            item.module.build_services = fail_services

raise SystemExit(pytest.main([sys.argv[1], "-q", "--tb=long"], plugins=[OfflineFailure()]))
"""
    result = subprocess.run([sys.executable, "-c", script, str(live_test)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert message in output
    assert sentinel not in output
