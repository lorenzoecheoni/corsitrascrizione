# Bunny Video Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Realizzare un tool interno che riceve un link Bunny Stream, analizza senza conservare il media e restituisce un report testuale esportabile per costruire una academy.

**Architecture:** Una web app FastAPI protetta da password inserisce i lavori in una coda in memoria a singolo worker. La pipeline legge metadati e HLS da Bunny, usa FFmpeg in una directory temporanea per audio compresso e fotogrammi candidati, invia l'audio a OpenAI per trascrizione con diarizzazione e usa un secondo passaggio OpenAI strutturato per slide, relatori e report. La directory temporanea viene rimossa in ogni esito; stato e report restano solo nella memoria del processo.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, vanilla HTML/CSS/JavaScript, Pydantic v2, HTTPX, OpenAI Python SDK, FFmpeg/ffprobe, Pillow/ImageHash, pytest, respx, Docker.

**Spec:** [2026-09-11-bunny-video-report-design.md](../specs/2026-09-11-bunny-video-report-design.md)

## Global Constraints

- Non salvare video, audio o fotogrammi oltre la singola esecuzione; la pulizia deve avvenire su successo, errore e cancellazione.
- Non usare biometria vocale. I nomi possono derivare solo da introduzioni parlate, sottopancia, slide o metadati; altrimenti usare etichette come `Relatore 1`.
- Accettare solo host Bunny noti e il CDN configurato, con corrispondenza esatta dell'host e HTTPS, per evitare SSRF.
- Usare esclusivamente richieste `GET` verso Bunny e non scrivere mai chiavi, URL firmati o trascrizioni nei log.
- Elaborare un solo video alla volta. Lo stato è volatile: un riavvio annulla coda, lavori e report.
- Supportare video pubblici e Bunny Token Authentication standard. DRM e restrizioni basate sul referrer producono un errore esplicito e non aggirabile.
- Impostare `store=False` su ogni chiamata Responses API.
- Limitare a tre i tentativi verso servizi remoti, con backoff progressivo e senza ritentare errori di autenticazione o input.
- Tutti gli importi sono stime in USD e devono essere presentati come tali.
- Ogni task termina con test verdi e un commit piccolo e autonomo.

---

## Task 1: Fondazione del progetto, configurazione e autenticazione

**Files:**

- Create: `pyproject.toml`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `app/auth.py`
- Create: `app/main.py`
- Create: `app/templates/home.html`
- Create: `app/static/app.css`
- Create: `tests/conftest.py`
- Create: `tests/test_auth.py`

- [ ] **Step 1: Scrivere i test iniziali di configurazione e accesso**

```python
# tests/test_auth.py
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def settings() -> Settings:
    return Settings(
        bunny_library_id=123,
        bunny_stream_api_key="bunny-secret",
        bunny_cdn_hostname="academy.example.b-cdn.net",
        openai_api_key="openai-secret",
        app_password="team-secret",
    )


def test_home_requires_basic_auth() -> None:
    response = TestClient(create_app(settings())).get("/")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


def test_home_accepts_team_password() -> None:
    response = TestClient(create_app(settings())).get(
        "/", auth=("team", "team-secret")
    )
    assert response.status_code == 200
    assert "Bunny Video Report" in response.text


def test_healthcheck_is_public() -> None:
    response = TestClient(create_app(settings())).get("/healthz")
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: Eseguire il test e verificare che fallisca per moduli mancanti**

Run: `python -m pytest tests/test_auth.py -q`

Expected: FAIL con `ModuleNotFoundError: No module named 'app'`.

- [ ] **Step 3: Creare il packaging e le dipendenze**

```toml
# pyproject.toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "bunny-video-report"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.116,<1",
  "httpx>=0.28,<1",
  "jinja2>=3.1,<4",
  "openai>=1.99,<3",
  "pillow>=11,<13",
  "imagehash>=4.3,<5",
  "pydantic-settings>=2.10,<3",
  "python-multipart>=0.0.20,<1",
  "uvicorn[standard]>=0.35,<1",
]

[project.optional-dependencies]
test = [
  "pytest>=8.4,<10",
  "pytest-asyncio>=1.1,<2",
  "respx>=0.22,<1",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = ["live: richiede credenziali reali e accesso a Bunny/OpenAI"]

[tool.hatch.build.targets.wheel]
packages = ["app"]
```

Run: `python -m pip install -e '.[test]'`

- [ ] **Step 4: Implementare settings, password condivisa e app factory**

```python
# app/config.py
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bunny_library_id: int
    bunny_stream_api_key: str = Field(repr=False)
    bunny_cdn_hostname: str
    bunny_token_auth_key: str | None = Field(default=None, repr=False)
    openai_api_key: str = Field(repr=False)
    app_password: str = Field(repr=False)
    temp_root: str | None = None

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
```

```python
# app/auth.py
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials


basic = HTTPBasic(auto_error=False)


def require_team(password: str):
    def dependency(
        credentials: HTTPBasicCredentials | None = Depends(basic),
    ) -> str:
        valid = credentials is not None
        valid = valid and secrets.compare_digest(credentials.username, "team")
        valid = valid and secrets.compare_digest(credentials.password, password)
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credenziali non valide",
                headers={"WWW-Authenticate": "Basic"},
            )
        return "team"

    return dependency
```

`create_app(settings: Settings | None = None) -> FastAPI` deve caricare `Settings()` solo quando non viene iniettato, montare `/static`, rendere `home.html`, esporre `/healthz` senza autenticazione e proteggere `/` con `require_team`.

- [ ] **Step 5: Eseguire i test e verificare il passaggio**

Run: `python -m pytest tests/test_auth.py -q`

Expected: PASS, `3 passed`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml app tests/test_auth.py tests/conftest.py
git commit -m "feat: avvia applicazione e autenticazione"
```

---

## Task 2: Modello strutturato del report, stima costi ed export testuale

**Files:**

- Create: `app/models.py`
- Create: `app/costs.py`
- Create: `app/reporting.py`
- Create: `tests/test_models_reporting.py`
- Create: `tests/fixtures/report.json`

- [ ] **Step 1: Scrivere un fixture completo e i test del contratto**

Il fixture deve includere due relatori, un presentatore, un intervento con due relatori, due cambi slide, un'incertezza e il costo stimato.

```python
# tests/test_models_reporting.py
import json
from pathlib import Path

from app.costs import estimate_cost
from app.models import AcademyReport
from app.reporting import format_timestamp, render_markdown, render_text


def load_report() -> AcademyReport:
    data = json.loads(Path("tests/fixtures/report.json").read_text())
    return AcademyReport.model_validate(data)


def test_report_fixture_covers_multi_speaker_session() -> None:
    report = load_report()
    assert len(report.speakers) == 3
    assert len(report.interventions[1].speaker_ids) == 2
    assert report.slides[0].timestamp_seconds == 95


def test_one_hour_cost_is_in_approved_range() -> None:
    cost = estimate_cost(duration_seconds=3600, downloaded_bytes=450_000_000)
    assert 0.40 <= cost.estimated_low_usd <= cost.estimated_high_usd <= 0.70


def test_markdown_and_text_are_exportable() -> None:
    report = load_report()
    markdown = render_markdown(report)
    plain = render_text(report)
    assert "## Relatori" in markdown
    assert "01:35" in markdown
    assert "Relatori" in plain
    assert format_timestamp(3661) == "1:01:01"
```

- [ ] **Step 2: Verificare il fallimento**

Run: `python -m pytest tests/test_models_reporting.py -q`

Expected: FAIL perché `app.models`, `app.costs` e `app.reporting` non esistono.

- [ ] **Step 3: Implementare i modelli Pydantic**

Definire in `app/models.py` questi contratti, senza campi liberi non validati:

```python
from typing import Literal
from pydantic import BaseModel, Field

Confidence = Literal["alta", "media", "bassa"]


class Evidence(BaseModel):
    kind: Literal["introduzione", "sottopancia", "slide", "metadata", "inferenza"]
    timestamp_seconds: float | None = None
    note: str


class SpeakerProfile(BaseModel):
    id: str
    display_name: str
    role: str | None = None
    confidence: Confidence
    evidence: list[Evidence] = Field(default_factory=list)


class Intervention(BaseModel):
    start_seconds: float
    end_seconds: float
    speaker_ids: list[str]
    summary: str


class Chapter(BaseModel):
    start_seconds: float
    end_seconds: float
    title: str
    summary: str


class SlideChange(BaseModel):
    timestamp_seconds: float
    title: str | None = None
    visible_content: list[str] = Field(default_factory=list)
    confidence: Confidence


class CostEstimate(BaseModel):
    estimated_low_usd: float
    estimated_high_usd: float
    bunny_bandwidth_usd: float
    transcription_usd: float
    analysis_usd: float
    basis: str


class AcademyContent(BaseModel):
    title: str
    duration_seconds: float
    detected_language: str
    synopsis: str
    extended_description: str
    target_audience: list[str]
    prerequisites: list[str]
    learning_objectives: list[str]
    speakers: list[SpeakerProfile]
    interventions: list[Intervention]
    chapters: list[Chapter]
    slides: list[SlideChange]
    topics: list[str]
    keywords: list[str]
    key_takeaways: list[str]
    uncertainties: list[str]


class AcademyReport(AcademyContent):
    cost: CostEstimate
```

- [ ] **Step 4: Implementare stima ed export**

`estimate_cost(duration_seconds, downloaded_bytes)` usa queste costanti iniziali, isolate e documentate per poterle calibrare dopo il test live:

```python
BUNNY_USD_PER_GB = 0.01
TRANSCRIPTION_LOW_USD_PER_HOUR = 0.36
TRANSCRIPTION_HIGH_USD_PER_HOUR = 0.50
ANALYSIS_LOW_USD_PER_HOUR = 0.04
ANALYSIS_HIGH_USD_PER_HOUR = 0.18


def estimate_cost(duration_seconds: float, downloaded_bytes: int) -> CostEstimate:
    hours = duration_seconds / 3600
    bunny = downloaded_bytes / 1_000_000_000 * BUNNY_USD_PER_GB
    transcription_low = hours * TRANSCRIPTION_LOW_USD_PER_HOUR
    transcription_high = hours * TRANSCRIPTION_HIGH_USD_PER_HOUR
    analysis_low = hours * ANALYSIS_LOW_USD_PER_HOUR
    analysis_high = hours * ANALYSIS_HIGH_USD_PER_HOUR
    return CostEstimate(
        estimated_low_usd=round(bunny + transcription_low + analysis_low, 4),
        estimated_high_usd=round(bunny + transcription_high + analysis_high, 4),
        bunny_bandwidth_usd=round(bunny, 4),
        transcription_usd=round((transcription_low + transcription_high) / 2, 4),
        analysis_usd=round((analysis_low + analysis_high) / 2, 4),
        basis="Stima iniziale da durata e byte letti; calibrare con gli usage reali.",
    )
```

`render_markdown` deve creare sezioni in ordine stabile e timestamp cliccabili come testo; `render_text` deve rimuovere la sintassi Markdown senza perdere contenuto.

- [ ] **Step 5: Eseguire i test**

Run: `python -m pytest tests/test_models_reporting.py -q`

Expected: PASS, `3 passed`.

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/costs.py app/reporting.py tests/test_models_reporting.py tests/fixtures/report.json
git commit -m "feat: definisci report academy ed export"
```

---

## Task 3: Parsing sicuro dei link Bunny, metadati e URL HLS

**Files:**

- Create: `app/bunny.py`
- Create: `tests/test_bunny.py`

- [ ] **Step 1: Scrivere test di parsing, allowlist e API mockata**

```python
# tests/test_bunny.py
import respx
from httpx import Response
import pytest

from app.bunny import BunnyClient, BunnyUrlError, build_cdn_token_url, parse_bunny_url


VIDEO_ID = "11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize("host", ["iframe.mediadelivery.net", "player.mediadelivery.net"])
def test_parse_embed_link(host: str) -> None:
    ref = parse_bunny_url(
        f"https://{host}/embed/123/{VIDEO_ID}",
        expected_library_id=123,
        cdn_hostname="academy.example.b-cdn.net",
    )
    assert ref.video_id == VIDEO_ID


def test_rejects_untrusted_host() -> None:
    with pytest.raises(BunnyUrlError):
        parse_bunny_url(
            f"https://evil.example/embed/123/{VIDEO_ID}",
            expected_library_id=123,
            cdn_hostname="academy.example.b-cdn.net",
        )


@respx.mock
def test_get_metadata_is_read_only_and_uses_access_key(settings) -> None:
    route = respx.get(
        f"https://video.bunnycdn.com/library/123/videos/{VIDEO_ID}"
    ).mock(return_value=Response(200, json={"guid": VIDEO_ID, "title": "Corso", "length": 7200}))
    metadata = BunnyClient(settings).get_metadata(VIDEO_ID)
    assert metadata.duration_seconds == 7200
    assert route.calls[0].request.headers["AccessKey"] == "bunny-secret"


def test_tokenized_url_is_deterministic() -> None:
    url = build_cdn_token_url(
        hostname="academy.example.b-cdn.net",
        video_id=VIDEO_ID,
        key="token-secret",
        expires=2_000_000_000,
    )
    assert url.startswith("https://academy.example.b-cdn.net/bcdn_token=")
    assert "token_path=%2F11111111-2222-3333-4444-555555555555%2F" in url
    assert url.endswith(f"/{VIDEO_ID}/playlist.m3u8")
```

- [ ] **Step 2: Eseguire i test e confermare il fallimento**

Run: `python -m pytest tests/test_bunny.py -q`

Expected: FAIL perché `app.bunny` non esiste.

- [ ] **Step 3: Implementare tipi e parser**

In `app/bunny.py` creare:

```python
class BunnyVideoRef(BaseModel):
    library_id: int
    video_id: UUID


class BunnyVideoMetadata(BaseModel):
    video_id: UUID
    title: str
    duration_seconds: float
    captions: list[dict[str, object]] = Field(default_factory=list)
    chapters: list[dict[str, object]] = Field(default_factory=list)


def parse_bunny_url(
    url: str, *, expected_library_id: int, cdn_hostname: str
) -> BunnyVideoRef:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise BunnyUrlError("Porta URL non valida") from exc
    if parsed.scheme != "https" or parsed.username or parsed.password or port not in (None, 443):
        raise BunnyUrlError("Il link deve essere un URL HTTPS Bunny valido")

    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    embed_hosts = {"iframe.mediadelivery.net", "player.mediadelivery.net"}
    try:
        if host in embed_hosts and len(parts) == 3 and parts[0] == "embed":
            library_id = int(parts[1])
            video_id = UUID(parts[2])
        elif host == cdn_hostname.lower().rstrip(".") and len(parts) == 2 and parts[1] == "playlist.m3u8":
            library_id = expected_library_id
            video_id = UUID(parts[0])
        else:
            raise BunnyUrlError("Formato link Bunny non riconosciuto")
    except (ValueError, TypeError) as exc:
        raise BunnyUrlError("Identificativo Bunny non valido") from exc

    if library_id != expected_library_id:
        raise BunnyUrlError("Il video appartiene a una libreria diversa")
    return BunnyVideoRef(library_id=library_id, video_id=video_id)
```

Il parser deve accettare:

- `https://iframe.mediadelivery.net/embed/{library_id}/{video_id}`
- `https://player.mediadelivery.net/embed/{library_id}/{video_id}`
- `https://{cdn_hostname}/{video_id}/playlist.m3u8`

Deve rifiutare schema diverso da HTTPS, credenziali nell'URL, porte non standard, host con suffissi ingannevoli, library diversa e GUID non valido.

- [ ] **Step 4: Implementare client e firma Token Authentication**

`BunnyClient.get_metadata(video_id)` effettua soltanto:

```text
GET https://video.bunnycdn.com/library/{library_id}/videos/{video_id}
AccessKey: {BUNNY_STREAM_API_KEY}
```

Converte `guid`, `title`, `length`, `captions` e `chapters`; timeout complessivo 30 secondi. `build_hls_url(video_id)` restituisce il playlist pubblico se la token key non è configurata, altrimenti usa Bunny CDN Token Authentication V2 path-style, con percorso firmato `/{video_id}/`, SHA-256 raw, Base64 URL-safe senza padding, `expires` e `token_path`. Non loggare mai l'URL risultante.

- [ ] **Step 5: Aggiungere test per 401, 404, timeout e segreti nei messaggi**

Ogni caso deve produrre una sottoclasse di `BunnyError` con messaggio utente in italiano che non contiene API key né URL firmato. Gli errori 401/403 non devono essere ritentati; 429 e 5xx potranno essere ritentati dal wrapper del Task 6.

- [ ] **Step 6: Eseguire i test**

Run: `python -m pytest tests/test_bunny.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/bunny.py tests/test_bunny.py
git commit -m "feat: integra lettura sicura da Bunny Stream"
```

---

## Task 4: Registro lavori volatile e coda a singolo worker

**Files:**

- Create: `app/jobs.py`
- Create: `tests/test_jobs.py`

- [ ] **Step 1: Scrivere test per transizioni, serializzazione e riapertura**

```python
# tests/test_jobs.py
from threading import Event

from app.jobs import JobState, JobStore, SingleWorkerRunner


def test_job_can_be_reloaded_from_memory() -> None:
    store = JobStore()
    created = store.create("https://player.mediadelivery.net/embed/123/video-id")
    store.update(created.id, state=JobState.PROCESSING, progress=25, message="Trascrizione")
    loaded = store.get(created.id)
    assert loaded.progress == 25
    assert loaded.message == "Trascrizione"


def test_runner_executes_only_one_pipeline_at_a_time() -> None:
    entered = Event()
    release = Event()
    active_counts: list[int] = []
    active = 0

    def pipeline(url, progress, cancellation_event):
        nonlocal active
        active += 1
        active_counts.append(active)
        entered.set()
        release.wait(timeout=2)
        active -= 1
        return {"title": url}

    store = JobStore()
    runner = SingleWorkerRunner(store, pipeline)
    first = store.create("first")
    second = store.create("second")
    runner.submit(first.id)
    runner.submit(second.id)
    assert entered.wait(timeout=1)
    release.set()
    runner.shutdown(wait=True)
    assert max(active_counts) == 1
```

- [ ] **Step 2: Verificare il fallimento**

Run: `python -m pytest tests/test_jobs.py -q`

Expected: FAIL perché `app.jobs` non esiste.

- [ ] **Step 3: Implementare il registro thread-safe**

```python
class JobState(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobRecord(BaseModel):
    id: UUID
    source_url: str = Field(exclude=True)
    state: JobState
    progress: int = Field(ge=0, le=100)
    message: str
    created_at: datetime
    updated_at: datetime
    report: AcademyReport | None = None
    error: str | None = None
```

`JobStore` deve usare `threading.RLock`, restituire copie dei record e consentire solo transizioni `queued -> processing -> completed|failed|cancelled` e `queued -> cancelled`. La sorgente non deve comparire nella risposta JSON.

- [ ] **Step 4: Implementare `SingleWorkerRunner`**

Usare `ThreadPoolExecutor(max_workers=1)`. Il runner riceve una callable `pipeline(source_url, progress_callback, cancellation_event) -> AcademyReport`, aggiorna progressi e messaggi nel registro e converte eccezioni note in errori utente senza stack trace o segreti. Conservare un `threading.Event` per lavoro: `cancel(job_id)` annulla una future ancora in coda oppure imposta l'evento del lavoro attivo. Esporre `shutdown(wait: bool)` per i test e l'arresto applicativo.

Aggiungere un test che annulla il primo lavoro durante l'esecuzione, verifica lo stato `cancelled` e conferma che il secondo lavoro parte soltanto dopo la terminazione controllata del primo.

- [ ] **Step 5: Eseguire i test**

Run: `python -m pytest tests/test_jobs.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/jobs.py tests/test_jobs.py
git commit -m "feat: aggiungi coda volatile a singolo worker"
```

---

## Task 5: Estrazione media temporanea e rilevamento dei cambi scena

**Files:**

- Create: `app/media.py`
- Create: `tests/test_media.py`

- [ ] **Step 1: Scrivere test per directory temporanea e deduplica percettiva**

```python
# tests/test_media.py
from pathlib import Path
from PIL import Image
import pytest

from app.media import FrameCandidate, deduplicate_frames, temporary_workspace


def test_workspace_is_removed_after_exception(tmp_path: Path) -> None:
    created: Path | None = None
    with pytest.raises(RuntimeError):
        with temporary_workspace(tmp_path) as workspace:
            created = workspace
            (workspace / "audio.m4a").write_bytes(b"temporary")
            raise RuntimeError("stop")
    assert created is not None
    assert not created.exists()


def test_near_identical_frames_are_deduplicated(tmp_path: Path) -> None:
    first = tmp_path / "one.jpg"
    second = tmp_path / "two.jpg"
    Image.new("RGB", (64, 64), "white").save(first)
    Image.new("RGB", (64, 64), (254, 254, 254)).save(second)
    kept = deduplicate_frames([
        FrameCandidate(path=first, timestamp_seconds=10),
        FrameCandidate(path=second, timestamp_seconds=12),
    ])
    assert len(kept) == 1
```

- [ ] **Step 2: Aggiungere un test FFmpeg con video sintetico**

Il fixture genera localmente un MP4 di 12 secondi con tre schermate colore e audio sinusoidale. Il test chiama `FFmpegProcessor.extract` e verifica:

- almeno un chunk audio mono a 16 kHz e bitrate basso;
- ogni chunk inferiore a 25 MB;
- fotogrammi con timestamp monotoni;
- durata complessiva entro un secondo dalla sorgente.

Se `ffmpeg` o `ffprobe` mancano, il test deve fallire con istruzione esplicita di installazione, non essere saltato.

- [ ] **Step 3: Verificare il fallimento**

Run: `python -m pytest tests/test_media.py -q`

Expected: FAIL perché `app.media` non esiste.

- [ ] **Step 4: Implementare contratti e workspace**

```python
@dataclass(frozen=True)
class AudioChunk:
    path: Path
    start_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class FrameCandidate:
    path: Path
    timestamp_seconds: float


@dataclass(frozen=True)
class MediaArtifacts:
    audio_chunks: list[AudioChunk]
    frame_candidates: list[FrameCandidate]
    downloaded_bytes: int
```

`temporary_workspace(root: Path | None)` deve usare `tempfile.mkdtemp` e `shutil.rmtree` in `finally`.

- [ ] **Step 5: Implementare un'unica lettura FFmpeg dell'HLS**

`FFmpegProcessor.extract(source_url, workspace, progress_callback, cancellation_event)` esegue un solo input FFmpeg e produce in parallelo:

- audio AAC mono, 16 kHz, 24 kbps, segmenti da massimo 5.400 secondi con `-segment_time 5400`;
- JPEG ridimensionati a massimo 960 px quando `select='gt(scene,0.18)'` rileva un cambio;
- progressi da `-progress pipe:1` e timestamp frame da `showinfo` su stderr.

Passare gli argomenti come lista a `subprocess.Popen`, mai attraverso una shell. Non inserire il source URL negli errori. Controllare `cancellation_event` durante la lettura del progresso: se impostato, inviare `terminate`, attendere al massimo cinque secondi e poi usare `kill`; il context manager eliminerà gli artefatti. Dopo FFmpeg, usare ffprobe per durata e dimensione dei chunk; se un chunk supera 24 MB, suddividerlo ulteriormente prima di restituirlo.

- [ ] **Step 6: Implementare deduplica e limiti**

`deduplicate_frames` usa `imagehash.phash`, distanza Hamming massima 8 e intervallo minimo di due secondi. Imporre massimo 600 candidati dopo deduplica; se sono di più, campionare uniformemente mantenendo primo e ultimo.

- [ ] **Step 7: Eseguire i test**

Run: `python -m pytest tests/test_media.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/media.py tests/test_media.py
git commit -m "feat: estrae audio e cambi scena temporanei"
```

---

## Task 6: Trascrizione diarizzata a chunk e riconciliazione relatori

**Files:**

- Create: `app/retry.py`
- Create: `app/transcription.py`
- Create: `tests/test_retry.py`
- Create: `tests/test_transcription.py`

- [ ] **Step 1: Scrivere test del retry selettivo**

```python
# tests/test_retry.py
import pytest

from app.retry import retry_remote


def test_retry_stops_after_three_attempts(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.retry.time.sleep", sleeps.append)
    attempts = 0

    def fails():
        nonlocal attempts
        attempts += 1
        raise TimeoutError("temporary")

    with pytest.raises(TimeoutError):
        retry_remote(fails, retryable=lambda exc: True)
    assert attempts == 3
    assert sleeps == [1.0, 2.0]
```

- [ ] **Step 2: Scrivere test con un client OpenAI finto**

Il test deve fornire due `AudioChunk` con offset 0 e 300, simulare `diarized_json` con speaker locali `A/B` e verificare che:

- `model="gpt-4o-transcribe-diarize"`;
- `response_format="diarized_json"`;
- `chunking_strategy="auto"`;
- i timestamp del secondo chunk siano traslati di 300 secondi;
- i segmenti adiacenti compatibili siano uniti;
- i relatori tra chunk siano riconciliati solo con evidenza testuale o una reference esplicita, mai con confronto biometrico autonomo.

- [ ] **Step 3: Verificare il fallimento**

Run: `python -m pytest tests/test_retry.py tests/test_transcription.py -q`

Expected: FAIL perché i moduli non esistono.

- [ ] **Step 4: Implementare il retry**

```python
def retry_remote(call, *, retryable, attempts: int = 3, base_delay: float = 1.0):
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:
            if attempt == attempts - 1 or not retryable(exc):
                raise
            time.sleep(base_delay * (2**attempt))
```

Le funzioni chiamanti devono classificare timeout, 429 e 5xx come ritentabili; 400, 401, 403 e 404 come definitivi.

- [ ] **Step 5: Implementare i modelli di trascrizione**

```python
class TranscriptSegment(BaseModel):
    start_seconds: float
    end_seconds: float
    diarization_label: str
    text: str


class TranscriptionResult(BaseModel):
    language: str
    text: str
    segments: list[TranscriptSegment]
    audio_seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
```

`OpenAITranscriber(client).transcribe(chunks, known_speakers=[])` apre ogni file solo per la durata della richiesta, accetta fino a quattro clip reference esplicitamente fornite e non scrive audio su altri percorsi.

- [ ] **Step 6: Implementare traslazione e riconciliazione**

Prefissare le label locali con l'indice chunk (`chunk-0:A`). La riconciliazione tra chunk deve essere conservativa: usare lo stesso speaker globale solo quando una reference fornita lo identifica o quando testo contiguo e sovrapposizione controllata lo rendono esplicito; altrimenti creare `Relatore N`. Conservare i segmenti originali per consentire all'analisi finale di correggere ruoli e nomi tramite introduzioni.

- [ ] **Step 7: Eseguire i test**

Run: `python -m pytest tests/test_retry.py tests/test_transcription.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/retry.py app/transcription.py tests/test_retry.py tests/test_transcription.py
git commit -m "feat: trascrive audio con diarizzazione"
```

---

## Task 7: Classificazione slide e generazione strutturata del report

**Files:**

- Create: `app/analysis.py`
- Create: `app/prompts.py`
- Create: `tests/test_analysis.py`

- [ ] **Step 1: Scrivere test del classificatore visuale**

Con un client Responses finto e tre frame, verificare che le immagini siano inviate in batch da massimo 20, che la risposta distingua `slide`, `camera_change` e `uncertain`, e che solo le slide arrivino alla generazione del report.

```python
def test_all_responses_calls_disable_storage(fake_responses_client, sample_inputs) -> None:
    analyzer = OpenAIAnalyzer(fake_responses_client)
    analyzer.analyze(**sample_inputs)
    assert fake_responses_client.calls
    assert all(call["store"] is False for call in fake_responses_client.calls)
```

- [ ] **Step 2: Scrivere test del report multi-relatore**

Il client finto restituisce un `AcademyContent` con presentatore, due relatori e una sessione congiunta. Verificare che nomi senza `Evidence` valida vengano sostituiti da `Relatore N`, che i ruoli incerti siano segnalati e che gli interventi coprano timestamp validi entro la durata.

- [ ] **Step 3: Verificare il fallimento**

Run: `python -m pytest tests/test_analysis.py -q`

Expected: FAIL perché `app.analysis` non esiste.

- [ ] **Step 4: Implementare schema slide e conversione immagini**

```python
class ClassifiedFrame(BaseModel):
    timestamp_seconds: float
    kind: Literal["slide", "camera_change", "uncertain"]
    title: str | None = None
    visible_content: list[str] = Field(default_factory=list)
    confidence: Confidence


class SlideBatchResult(BaseModel):
    frames: list[ClassifiedFrame]
```

Ridimensionare già in Task 5; codificare ogni JPEG come data URL solo mentre si costruisce la richiesta e rilasciare la stringa al termine del batch.

- [ ] **Step 5: Implementare due passaggi Responses API**

`OpenAIAnalyzer` deve usare `client.responses.parse` con `model="gpt-5.6-luna"`, `store=False` e Pydantic come `text_format`:

1. batch visuali per classificare veri cambi slide ed estrarre titolo/contenuto visibile;
2. contenuto finale da metadati, trascrizione diarizzata e slide classificate, con `text_format=AcademyContent`; il costo viene aggiunto deterministicamente dalla pipeline e non dal modello.

Il prompt in `app/prompts.py` deve imporre:

- nessuna invenzione di nome o ruolo;
- evidenza e confidenza per ogni identità;
- supporto a presentatore/moderatore assente, presente o coincidente con un relatore;
- interventi con uno o più `speaker_ids`;
- capitoli academy non sovrapposti e compresi nella durata;
- sinossi breve, descrizione estesa, pubblico, prerequisiti, obiettivi, temi, keyword e takeaway;
- incertezze esplicite invece di completamenti creativi.

- [ ] **Step 6: Validare semanticamente la risposta**

Dopo Pydantic, controllare timestamp, riferimenti speaker e ordinamento. Al primo errore semanticamente riparabile, effettuare una sola richiesta di correzione includendo solo gli errori e il JSON precedente; al secondo errore fallire con messaggio utente. Non superare comunque tre tentativi remoti complessivi per chiamata.

- [ ] **Step 7: Eseguire i test**

Run: `python -m pytest tests/test_analysis.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/analysis.py app/prompts.py tests/test_analysis.py
git commit -m "feat: genera report strutturato da audio e slide"
```

---

## Task 8: Pipeline completa, pagine lavoro e download

**Files:**

- Create: `app/pipeline.py`
- Create: `app/web.py`
- Modify: `app/main.py`
- Modify: `app/templates/home.html`
- Create: `app/templates/job.html`
- Modify: `app/static/app.css`
- Create: `app/static/job.js`
- Create: `tests/test_pipeline.py`
- Create: `tests/test_web.py`

- [ ] **Step 1: Scrivere test di orchestrazione e pulizia**

```python
# tests/test_pipeline.py
def test_pipeline_cleans_media_after_success(fake_components, tmp_path) -> None:
    pipeline = build_test_pipeline(fake_components, temp_root=tmp_path)
    report = pipeline.run(fake_components.source_url, lambda percent, message: None)
    assert report.title == "Corso di prova"
    assert list(tmp_path.iterdir()) == []


def test_pipeline_cleans_media_after_analysis_error(fake_components, tmp_path) -> None:
    fake_components.analyzer.raise_error = True
    pipeline = build_test_pipeline(fake_components, temp_root=tmp_path)
    with pytest.raises(PipelineError):
        pipeline.run(fake_components.source_url, lambda percent, message: None)
    assert list(tmp_path.iterdir()) == []
```

Verificare anche l'ordine delle fasi e progressi monotoni: metadati 5%, media 10-45%, trascrizione 50-72%, slide 75-85%, report 88-98%, completamento 100%.

- [ ] **Step 2: Scrivere test HTTP dei lavori**

```python
# tests/test_web.py
def test_create_job_redirects_to_status_page(client) -> None:
    response = client.post(
        "/jobs",
        data={"source_url": VALID_BUNNY_URL},
        auth=("team", "team-secret"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/jobs/")


def test_completed_job_downloads_markdown_and_text(client, completed_job) -> None:
    md = client.get(f"/jobs/{completed_job.id}/report.md", auth=("team", "team-secret"))
    txt = client.get(f"/jobs/{completed_job.id}/report.txt", auth=("team", "team-secret"))
    assert md.headers["content-type"].startswith("text/markdown")
    assert "attachment" in md.headers["content-disposition"]
    assert txt.headers["content-type"].startswith("text/plain")
```

- [ ] **Step 3: Verificare il fallimento**

Run: `python -m pytest tests/test_pipeline.py tests/test_web.py -q`

Expected: FAIL perché pipeline e route non esistono.

- [ ] **Step 4: Implementare `AnalysisPipeline`**

```python
class AnalysisPipeline:
    def run(
        self,
        source_url: str,
        progress: Callable[[int, str], None],
        cancellation_event: threading.Event | None = None,
    ) -> AcademyReport:
        cancellation_event = cancellation_event or threading.Event()
        ref = parse_bunny_url(
            source_url,
            expected_library_id=self.settings.bunny_library_id,
            cdn_hostname=self.settings.bunny_cdn_hostname,
        )
        metadata = self.bunny.get_metadata(str(ref.video_id))
        if metadata.duration_seconds > 14_400:
            raise PipelineError("unsupported_duration", "Il video supera il limite di quattro ore")
        with temporary_workspace(self.temp_root) as workspace:
            media = self.media.extract(
                self.bunny.build_hls_url(str(ref.video_id)),
                workspace,
                progress,
                cancellation_event,
            )
            transcript = self.transcriber.transcribe(media.audio_chunks)
            content = self.analyzer.analyze(metadata, transcript, media.frame_candidates)
            return AcademyReport(
                **content.model_dump(),
                cost=estimate_cost(metadata.duration_seconds, media.downloaded_bytes),
            )
```

Applicare la pulizia tramite context manager attorno a tutte le fasi media/AI. Controllare l'evento di cancellazione fra ogni fase e sollevare `PipelineCancelled`. Convertire errori tecnici in `PipelineError(code, user_message)`; codici previsti: `invalid_link`, `bunny_auth`, `not_found`, `protected_video`, `unsupported_duration`, `media_decode`, `transcription`, `analysis`, `temporary_failure`.

Aggiungere test di confine: una durata esattamente pari a quattro ore è accettata; una durata superiore di un secondo viene rifiutata prima di leggere l'HLS.

- [ ] **Step 5: Implementare le route protette**

In `app/web.py` creare un router con:

- `POST /jobs` — valida link prima dell'accodamento e risponde 303;
- `GET /jobs/{job_id}` — pagina riapribile finché il processo è vivo;
- `GET /api/jobs/{job_id}` — JSON senza source URL, segreti o trascrizione grezza;
- `POST /jobs/{job_id}/cancel` — richiede l'annullamento e torna alla pagina del lavoro;
- `GET /jobs/{job_id}/report.md` — download Markdown solo a completamento;
- `GET /jobs/{job_id}/report.txt` — download testo solo a completamento.

Restituire 404 per ID assente, 409 per export non ancora pronto e 422 per link non valido.

- [ ] **Step 6: Implementare interfaccia minima**

La home contiene un campo URL e la nota “Il video non viene conservato”. `job.html` mostra stato, barra percentuale, messaggio, errore sanificato e report. `job.js` interroga `/api/jobs/{id}` ogni tre secondi finché lo stato è terminale. Durante coda o elaborazione mostra “Annulla”; a completamento mostra copia negli appunti, download Markdown/TXT e pulsante “Stampa / Salva PDF” con `window.print()`.

- [ ] **Step 7: Collegare dipendenze e lifecycle**

`create_app` costruisce una sola istanza di Bunny client, media processor, transcriber, analyzer, pipeline, store e runner; li salva in `app.state`. Nel lifespan, `runner.shutdown(wait=False)` evita nuovi lavori. Il worker non deve bloccare l'event loop.

- [ ] **Step 8: Eseguire test di task e regressione**

Run: `python -m pytest tests/test_pipeline.py tests/test_web.py tests/test_auth.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add app/pipeline.py app/web.py app/main.py app/templates app/static tests/test_pipeline.py tests/test_web.py
git commit -m "feat: completa flusso web ed export report"
```

---

## Task 9: Sicurezza dei log e test degli errori sensibili

**Files:**

- Create: `app/logging_config.py`
- Modify: `app/main.py`
- Modify: `app/bunny.py`
- Modify: `app/pipeline.py`
- Create: `tests/test_security.py`

- [ ] **Step 1: Scrivere test di non divulgazione**

I test devono usare valori sentinella per API key, password, token key, URL firmato e testo della trascrizione; provocare errori in ogni fase e verificare con `caplog` e risposta HTTP che nessun valore sentinella sia presente.

```python
def test_secrets_and_transcript_never_reach_logs(caplog, failing_pipeline, sentinels) -> None:
    with pytest.raises(PipelineError):
        failing_pipeline.run(sentinels.source_url, lambda percent, message: None)
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    for secret in sentinels.all:
        assert secret not in rendered_logs
```

- [ ] **Step 2: Verificare che il test intercetti almeno una perdita artificiale**

Introdurre la sentinella solo nel fake logger del test, eseguire il test per vedere il FAIL, poi rimuovere la riga dal fake. Non aggiungere mai una chiave reale al repository.

- [ ] **Step 3: Implementare logging per eventi strutturati**

Registrare solo `job_id`, fase, durata, progressivo del tentativo, status code remoto e codice errore. Aggiungere un filtro che maschera header `AccessKey`/`Authorization`, parametri `token`, `bcdn_token`, `expires`, `token_path` e valori di configurazione noti. Disabilitare access log con query string in produzione o configurarli per registrare solo metodo, route template e status.

- [ ] **Step 4: Eseguire test sicurezza e suite corrente**

Run: `python -m pytest tests/test_security.py tests/test_bunny.py tests/test_pipeline.py tests/test_web.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/logging_config.py app/main.py app/bunny.py app/pipeline.py tests/test_security.py
git commit -m "feat: protegge segreti e contenuti nei log"
```

---

## Task 10: Docker, documentazione operativa e collaudo end-to-end

**Files:**

- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `README.md`
- Create: `tests/test_end_to_end.py`
- Create: `tests/test_live_bunny.py`

- [ ] **Step 1: Scrivere smoke test end-to-end senza servizi reali**

Il test deve generare un video sintetico, usare Bunny/OpenAI finti ma il vero FFmpeg, inviare il form HTTP, attendere il worker con timeout massimo 15 secondi e verificare pagina finale, schema del report, download MD/TXT e directory temporanea vuota.

- [ ] **Step 2: Scrivere test live esplicitamente opt-in**

```python
@pytest.mark.live
def test_real_bunny_video_produces_valid_report(live_settings):
    source_url = os.environ["BUNNY_SAMPLE_VIDEO_URL"]
    report = build_pipeline(live_settings).run(source_url, lambda percent, message: None)
    AcademyReport.model_validate(report)
    assert report.duration_seconds > 0
```

Il fixture deve fare `pytest.skip` se manca una qualunque variabile live. Il test non stampa report completo, transcript o URL firmati.

- [ ] **Step 3: Verificare il fallimento dello smoke test**

Run: `python -m pytest tests/test_end_to_end.py -q`

Expected: FAIL finché il wiring di produzione non è importabile in modo sostituibile.

- [ ] **Step 4: Rendere esplicita la factory delle dipendenze**

Esporre in `app/main.py` `build_services(settings) -> Services`, così i test sostituiscono solo Bunny/OpenAI mantenendo veri app, queue, pipeline, report e FFmpeg. Eseguire nuovamente lo smoke test fino al PASS.

- [ ] **Step 5: Creare immagine Docker minimale**

```dockerfile
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
RUN useradd --create-home appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
```

`.dockerignore` deve escludere `.git`, `.env`, cache, test e directory temporanee. `.gitignore` deve escludere `.env`, `.venv`, cache, coverage e output media comuni (`*.m4a`, `*.mp4`, `frame_*.jpg`).

- [ ] **Step 6: Documentare installazione e costi**

`README.md` deve contenere:

- avvio locale e Docker;
- configurazione delle sei variabili, inclusa token key opzionale;
- permesso Bunny read-only e nessuna operazione di scrittura;
- formati link supportati ed esclusioni DRM/referrer;
- dati temporanei e comportamento al riavvio;
- limite 1-4 ore e una lavorazione alla volta;
- export MD, TXT e stampa PDF;
- costo stimato `$0.40-$0.70` per ora, con avviso di ricalibrazione sul primo video reale;
- uso di HTTPS davanti all'app se accessibile fuori dalla rete locale;
- comando del test live e modo sicuro di fornire `.env`.

- [ ] **Step 7: Eseguire tutta la suite offline**

Run: `python -m pytest -m "not live" -q`

Expected: PASS senza rete e senza credenziali reali.

- [ ] **Step 8: Costruire e verificare il container**

Run: `docker build -t bunny-video-report:local .`

Expected: build completata.

Run: `docker run --rm bunny-video-report:local ffmpeg -version`

Expected: exit 0 e versione FFmpeg stampata.

- [ ] **Step 9: Eseguire il test reale quando l'utente fornisce le credenziali**

Run: `set -a && source .env && set +a && python -m pytest tests/test_live_bunny.py -m live -q`

Expected: PASS su un video Bunny campione. Se il costo effettivo differisce dall'intervallo, aggiornare soltanto le costanti documentate in `app/costs.py` e il README sulla base degli usage restituiti dalle API.

- [ ] **Step 10: Controllare che non siano versionati segreti o media**

Run: `git status --short && git grep -n -E 'bunny-secret|openai-secret|team-secret|sk-[A-Za-z0-9_-]+' -- ':!.env.example'`

Expected: nessun file media, `.env` o segreto reale in staging; `git grep` senza risultati.

- [ ] **Step 11: Commit**

```bash
git add Dockerfile .dockerignore .gitignore .env.example README.md app/main.py tests/test_end_to_end.py tests/test_live_bunny.py
git commit -m "docs: prepara rilascio e collaudo operativo"
```

---

## Final Verification Checklist

- [ ] Eseguire `python -m pytest -m "not live" -q` e conservare il conteggio dei test passati.
- [ ] Eseguire `docker build -t bunny-video-report:local .` e verificare FFmpeg nel container.
- [ ] Avviare localmente con credenziali non reali e verificare che `/` risponda 401 senza Basic Auth.
- [ ] Con credenziali reali, analizzare prima un video breve, poi uno da almeno un'ora.
- [ ] Verificare manualmente almeno dieci timestamp fra interventi, capitoli e cambi slide.
- [ ] Controllare che presentatore, moderatore assente e sessione con due relatori siano rappresentati correttamente.
- [ ] Interrompere deliberatamente una lavorazione e verificare che la directory temporanea sia vuota.
- [ ] Riavviare l'app e confermare che i vecchi lavori non siano disponibili, come dichiarato.
- [ ] Aprire MD e TXT; usare la stampa browser per salvare un PDF leggibile.
- [ ] Registrare costo e durata reali del primo test live e confrontarli con `$0.40-$0.70/ora`.
- [ ] Eseguire `git status --short` e assicurarsi che restino solo cambi intenzionali.
