"""Exercise safe diagnostics and the opt-in synthetic provider proof."""

from collections import Counter
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import UUID

import httpx
from openai import APIStatusError, OpenAI
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
from app.models import ProviderUsage
from app.transcription import TranscriptSegment, TranscriptionResult


_SYNTHETIC_DURATION_SECONDS = 96 * 60
_SYNTHETIC_INTRODUCTIONS = (
    "Sono Elena Verdi, responsabile del progetto didattico.",
    "Sono Paolo Neri, relatore sui processi organizzativi.",
    "Sono Marta Blu, relatrice sulla valutazione dei risultati.",
    "Sono la moderatrice della sessione; il mio nome non viene comunicato.",
)
_SYNTHETIC_TOPICS = (
    "La pianificazione parte dagli obiettivi: una biblioteca vuole aumentare i prestiti digitali. "
    "Definisce destinatari, scadenze, risorse e criteri di successo. Il gruppo distingue desideri "
    "generici da risultati verificabili e assegna un referente a ciascuna attività.",
    "La ricerca ascolta gli utenti con interviste, questionari e osservazione. Si raccolgono "
    "esigenze diverse di studenti, insegnanti e pensionati. Un campione piccolo può suggerire "
    "ipotesi, ma richiede prudenza prima di estendere le conclusioni alla popolazione.",
    "Il catalogo descrive libri, riviste, audiolibri e materiali accessibili. Titoli, autori, "
    "lingue, argomenti e formati devono essere coerenti. La revisione dei duplicati migliora "
    "la ricerca; una scheda incompleta può rendere invisibile una risorsa disponibile.",
    "L'accessibilità comprende tastiera, contrasto, ingrandimento, sottotitoli e descrizioni. "
    "Una pagina leggibile riduce ostacoli per molti visitatori. Le verifiche coinvolgono persone "
    "con esigenze differenti, evitando di assumere che uno strumento automatico trovi tutto.",
    "La formazione dei volontari usa esercitazioni guidate, dimostrazioni e simulazioni. "
    "Chi partecipa prova prenotazione, rinnovo e restituzione. Le domande ricorrenti diventano "
    "materiale didattico; il tutor osserva gli errori e modifica le spiegazioni troppo astratte.",
    "La comunicazione presenta benefici concreti attraverso newsletter, manifesti e incontri. "
    "Il messaggio cambia secondo il destinatario e mantiene un linguaggio comprensibile. "
    "La squadra confronta canali, frequenza, adesioni e richieste di assistenza ricevute.",
    "Il servizio di assistenza raccoglie problemi tecnici, dubbi e suggerimenti. Ogni richiesta "
    "riceve una categoria e una priorità. Una procedura indica quando coinvolgere uno specialista; "
    "i tempi di risposta vengono confrontati con la complessità e con la disponibilità del personale.",
    "La qualità dei dati richiede controlli su valori mancanti, date incoerenti e registrazioni "
    "duplicate. Il gruppo documenta le correzioni senza cancellare il significato originario. "
    "Indicatori comparabili dipendono da definizioni stabili e da fonti chiaramente descritte.",
    "La valutazione considera iscrizioni, prestiti, soddisfazione e capacità di usare il servizio. "
    "Un aumento degli accessi può accompagnarsi a molte difficoltà. Per questo il rapporto combina "
    "conteggi, testimonianze e confronti con il periodo iniziale, spiegando le possibili distorsioni.",
    "La gestione degli imprevisti prevede assenze, ritardi nelle consegne e interruzioni tecniche. "
    "Una simulazione chiarisce responsabilità e alternative. Il coordinatore aggiorna il calendario "
    "e comunica le modifiche a chi ne è coinvolto, mantenendo traccia delle decisioni adottate.",
    "La collaborazione con scuole e associazioni distribuisce spazi, materiali e competenze. "
    "Gli accordi chiariscono contributi e aspettative. Riunioni brevi controllano gli avanzamenti, "
    "mentre un referente raccoglie proposte contrastanti e prepara una decisione motivata.",
    "La chiusura restituisce risultati, limiti e attività ancora aperte. La biblioteca confronta "
    "quanto previsto con quanto osservato e conserva le lezioni utili per il ciclo successivo. "
    "Il seminario termina con una revisione delle priorità e con l'indicazione dei prossimi passi.",
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


def test_synthetic_workload_has_representative_varied_text_volume():
    metadata, transcription = _synthetic_long_inputs()
    windows = split_transcript_windows(transcription.segments)
    sizes = [len(window.to_payload()) for window in windows]

    assert metadata.duration_seconds == 5760
    assert sum(len(segment.text) for segment in transcription.segments) >= 100_000
    assert len({segment.text for segment in transcription.segments}) == 96
    assert len(set(" ".join(segment.text for segment in transcription.segments).split())) >= 250
    assert len(windows) >= 10
    assert all(10_800 <= size <= 12_000 for size in sizes[:-1])
    assert max(sizes) >= 11_000
    assert all(window.end_seconds - window.start_seconds <= 600 for window in windows)


def test_live_acceptance_rejects_recovered_429_and_counts_only_safe_statuses(monkeypatch):
    monkeypatch.setattr("app.retry.time.sleep", lambda _: None)
    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "0"},
                                  json={"error": {"message": "PRIVATE_PROVIDER_BODY"}})
        return httpx.Response(200, json={
            "id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
            "model": "gpt-4o-mini", "output": [{"id": "msg_test", "type": "message",
                "role": "assistant", "status": "completed", "content": [{"type": "output_text",
                "annotations": [], "text": json.dumps({"detected_language": "it",
                    "synopsis_notes": ["Sintesi."], "speakers": [], "uncertainties": []})}]}],
        })

    with OpenAI(api_key="test-only", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        audited = _SafeRecordingOpenAI(client)
        result = OpenAIAnalyzer(audited)._structured(
            text_format=WindowAnalysis, instructions="Synthetic test", payload="{}",
            prepare=lambda _: None, validate=lambda _: [], cancellation_event=None,
            usage=ProviderUsage(), max_repair_chars=12_000, stage="window", model="gpt-4o-mini")

    assert result.detected_language == "it"
    assert getattr(audited, "status_counts", {}) == {429: 1, 200: 1}
    with pytest.raises(AssertionError):
        _assert_live_statuses(audited)


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
    """Keep only lengths, schema types and numeric HTTP counts in memory."""

    def __init__(self, client: OpenAI) -> None:
        self._client = client.with_options(max_retries=0)
        self.requests: list[tuple[type, int]] = []
        self.status_counts: Counter[int] = Counter()
        self.responses = self
        self.with_raw_response = self

    def with_options(self, *, max_retries: int):
        return self

    def parse(self, **kwargs):
        payload = kwargs["input"]
        serialized = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.requests.append((kwargs["text_format"], len(serialized)))
        try:
            raw = self._client.responses.with_raw_response.parse(**kwargs)
        except APIStatusError as exc:
            self.status_counts[exc.status_code] += 1
            raise
        self.status_counts[raw.status_code] += 1
        return raw


def _assert_live_statuses(audited: _SafeRecordingOpenAI) -> None:
    assert audited.status_counts[429] == 0
    assert sum(audited.status_counts.values()) == len(audited.requests)
    assert all(200 <= status < 300 for status in audited.status_counts)


def _synthetic_segment_text(index: int) -> str:
    # Roughly 170 spoken Italian words/minute; topics and numeric cases vary.
    text = (
        f"{_SYNTHETIC_INTRODUCTIONS[index % 4]} Intervento {index + 1} del seminario sintetico. "
        f"{_SYNTHETIC_TOPICS[index // 8]} "
        f"Nel caso numero {index + 1}, il laboratorio dispone di {12 + index} partecipanti "
        f"e di {3 + index % 7} postazioni. La prima settimana comprende {2 + index % 5} incontri. "
        "Si confrontano due proposte: organizzare attività individuali oppure lavorare in piccoli gruppi. "
        "La scelta dipende dall'esperienza iniziale, dagli strumenti disponibili e dal tempo necessario "
        "per accompagnare ciascuno. Durante la discussione emergono dubbi sui passaggi più complessi. "
        "Un partecipante chiede un esempio concreto e un altro segnala una difficoltà pratica. "
        "La risposta propone una prova limitata, una raccolta di osservazioni e una successiva revisione. "
        "Il verbale descrive decisioni, responsabilità e scadenze senza attribuire certezze ai dati incompleti. "
        "Alla fine dell'esercizio il gruppo verifica il risultato, confronta le alternative e formula "
        "una raccomandazione motivata per il prossimo incontro."
    )
    return text[:1100]


def _synthetic_long_inputs():
    segments = [
        TranscriptSegment(
            start_seconds=index * 60,
            end_seconds=(index + 1) * 60,
            diarization_label=f"chunk-{index // 10}:{'ABCD'[index % 4]}",
            text=_synthetic_segment_text(index),
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
    assert sum(len(segment.text) for segment in transcription.segments) >= 100_000
    assert len(windows) >= 10
    assert all(len(window.to_payload()) >= 10_800 for window in windows[:-1])
    assert all(len(window.to_payload()) <= MAX_WINDOW_CHARS for window in windows)

    client = OpenAI(api_key=api_key, max_retries=0)
    audited = _SafeRecordingOpenAI(client)
    started = time.monotonic()
    result = None
    passed = False
    try:
        result = OpenAIAnalyzer(audited).analyze(metadata, transcription, frames=[])
        assert result.synopsis.strip()
        assert result.speakers
        _assert_live_statuses(audited)
        window_payloads = [size for schema, size in audited.requests if schema is WindowAnalysis]
        consolidation_payloads = [
            size for schema, size in audited.requests if schema is ConsolidatedTextReport
        ]
        assert len(window_payloads) >= len(windows)
        assert consolidation_payloads
        assert all(size <= MAX_WINDOW_CHARS for size in window_payloads)
        assert all(size <= MAX_CONSOLIDATION_CHARS for size in consolidation_payloads)
        # The real map output must exercise a substantial consolidation too.
        assert max(consolidation_payloads) >= 10_000
        passed = True
    except Exception:
        pass  # Emit numeric diagnostics below, then fail with static text only.
    finally:
        client.close()

    elapsed_seconds = time.monotonic() - started
    captured = capsys.readouterr()
    _fail_if_captured_live_output(captured.out, captured.err)
    with capsys.disabled():
        print(
            "live_diagnostic "
            f"status={'passed' if passed else 'failed'} requests={len(audited.requests)} "
            f"status_2xx={sum(count for status, count in audited.status_counts.items() if 200 <= status < 300)} "
            f"status_429={audited.status_counts[429]} "
            f"status_other={sum(count for status, count in audited.status_counts.items() if not 200 <= status < 300 and status != 429)} "
            f"input_tokens={result.usage.input_tokens if result is not None else 'unknown'} "
            f"output_tokens={result.usage.output_tokens if result is not None else 'unknown'} "
            f"duration_seconds={int(metadata.duration_seconds)} "
            f"elapsed_seconds={elapsed_seconds:.2f} windows={len(windows)} "
            f"source_chars={sum(len(segment.text) for segment in transcription.segments)} "
            f"window_min_chars={min(len(window.to_payload()) for window in windows)} "
            f"window_max_chars={max(len(window.to_payload()) for window in windows)} "
            f"consolidation_max_chars={max((size for schema, size in audited.requests if schema is ConsolidatedTextReport), default=0)} "
            f"speakers={len(result.speakers) if result is not None else 0} "
            f"slides={len(result.slides) if result is not None else 0} "
            f"uncertainties={len(result.uncertainties) if result is not None else 0}"
        )
    if not passed:
        raise pytest.fail.Exception(
            "Synthetic live analysis failed; inspect only safe provider status", pytrace=False,
        ) from None


@pytest.mark.live
def test_announced_presenter_and_speakers_survive_live_consolidation():
    """Keep announced people even when their names cannot be mapped to voice labels."""
    api_key = _synthetic_live_api_key()
    expected_names = {
        "Vincenzo Manfredi",
        "Gaetano De Vito",
        "Antonio Sibiglia",
        "Furio D'Andrea",
        "Luigi Morra",
    }
    metadata = BunnyVideoMetadata(
        video_id=UUID("12345678-1234-1234-1234-123456789abc"),
        title="Seminario sintetico con più relatori",
        duration_seconds=120,
    )
    transcription = TranscriptionResult(
        text="",
        segments=[TranscriptSegment(
            start_seconds=0,
            end_seconds=60,
            diarization_label="chunk-0:A",
            text=(
                "Buongiorno, sono Vincenzo Manfredi e presento questo seminario. "
                "I nostri relatori sono il Presidente Gaetano De Vito, Antonio Sibiglia, "
                "l'Avvocato Furio D'Andrea e Luigi Morra. Li introduco ora; dal solo "
                "annuncio non sappiamo ancora quale voce appartenga a ciascuno di loro."
            ),
        )],
        audio_seconds=60,
    )

    client = OpenAI(api_key=api_key, max_retries=0)
    audited = _SafeRecordingOpenAI(client)
    names: set[str] = set()
    passed = False
    try:
        result = OpenAIAnalyzer(audited).analyze(metadata, transcription, frames=[])
        names = {speaker.display_name for speaker in result.speakers}
        assert expected_names <= names
        assert sum(audited.status_counts.values()) == len(audited.requests)
        assert all(status == 429 or 200 <= status < 300 for status in audited.status_counts)
        assert sum(count for status, count in audited.status_counts.items() if 200 <= status < 300) >= 2
        passed = True
    except Exception:
        pass
    finally:
        client.close()
    print(
        "announced_speaker_diagnostic "
        f"status={'passed' if passed else 'failed'} requests={len(audited.requests)} "
        f"status_2xx={sum(count for status, count in audited.status_counts.items() if 200 <= status < 300)} "
        f"status_429={audited.status_counts[429]} returned={len(names)} "
        f"missing={','.join(sorted(expected_names - names)) or 'none'}"
    )
    if not passed:
        raise pytest.fail.Exception(
            "Announced-speaker live analysis failed; inspect only safe provider status",
            pytrace=False,
        ) from None


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
