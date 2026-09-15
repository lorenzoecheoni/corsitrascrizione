# Granular Report Chapters and Verifiable Materials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produrre per ogni video un report Academy v1.1 ripetibile che conservi i blocchi fattuali, proponga capitoli didattici audio-allineati, normalizzi le persone sul Registro, colleghi le slide a deck e pagine reali ed esporti il costo stimato.

**Architecture:** La trascrizione AssemblyAI viene prima divisa in atomi con parole e timestamp reali. Un pianificatore deterministico aggrega le unità tematiche in capitoli da 8–15 minuti senza perdere il blocco di parlato sorgente; l'allineatore colloca ogni confine sul silenzio e registra la ragione editoriale. I materiali vengono risolti dall'inventario o dal registro GUID, scaricati nello spazio temporaneo del lavoro, letti pagina per pagina e associati alle slide soltanto sopra soglie conservative.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, OpenAI Responses API, AssemblyAI, FFmpeg, httpx, Google Sheets API, `zipfile`/XML per PPTX, pypdf, pytest/respx, SQLite, Railway.

**Spec:** `docs/superpowers/specs/2026-09-15-report-granulare-materiali-design.md`

## Global Constraints

- Il report resta per singolo video, con `"versione": 1`, `chiave: "v1"` e `ordine: 1`.
- `durata_secondi` proviene soltanto da Bunny ed è normalizzata per difetto al secondo intero; nessun tempo può superarla.
- I capitoli didattici preferiscono 8–10 minuti, puntano a 8–15, tollerano fino a 20 e non vengono spezzati a forza.
- Il cambio slide è soltanto un indizio; il confine esportato è sempre determinato su parole e silenzio.
- I capitoli di un blocco sono contigui e ne coprono l'intera durata; gli intervalli fra blocchi sono dichiarati `logistica` o `pausa`.
- Un solo capitolo `intervento` da 8–15 minuti è `pubblico`; saluti e segmenti non didattici restano `iscritti`.
- `INTERVENTO_LUNGO`, `SLIDE_NON_ABBINATA` e `ALIAS_RELATORE_AMBIGUO` sono avvisi e non bloccano l'export.
- Materiali privi di URL reale o file realmente disponibile non vengono emessi.
- Video, audio, trascrizione parola-per-parola, fotogrammi e deck restano temporanei.
- Il corso Academy pubblicato e `/academy/governance-holding-report-15-09/` non vengono modificati.

---

### Task 1: Estendere i modelli persistiti e il contratto v1.1

**Files:**
- Modify: `app/models.py`
- Modify: `app/intermediate_models.py`
- Test: `tests/test_models_reporting.py`
- Test: `tests/test_intermediate_models.py`

**Interfaces:**
- Consumes: `ReportModel`, `InterventionKind`, `CostEstimate` esistenti.
- Produces: `ChapterBoundaryOrigin`, `SpeechBlock`, `ReportMaterial`, campi di provenienza su `Intervention`; `IntermediateSpeechBlockV11`, `IntermediateCostV11` e i nuovi codici di verifica.

- [ ] **Step 1: Scrivere i test fallenti dei nuovi modelli persistiti**

```python
def test_report_models_retain_blocks_chapter_origin_and_material_page():
    origin = ChapterBoundaryOrigin(
        motivo_editoriale="slide_e_tema",
        regola_audio="short_pause",
        slide_indizio_seconds=1369,
    )
    chapter = make_intervention(1369, 1931).model_copy(update={
        "block_id": "b003", "chapter_number": 2, "chapters_in_block": 4,
        "boundary_origin": origin,
    })
    block = SpeechBlock(
        id="b003", start_seconds=862, end_seconds=3135, tipo="intervento",
        relatori=["Furio D'Andrea"], titolo="Governance delle holding",
        sinossi="Il blocco tratta poteri, assemblea e direttive.",
    )
    material = ReportMaterial(
        titolo="Slide · Furio D'Andrea", relatore="Furio D'Andrea",
        url="https://www.assoholding.it/materiali/furio.pptx", pagine=18,
    )
    report = make_report([chapter]).model_copy(update={
        "speech_blocks": [block], "materials": [material], "analysis_profile": 2,
    })
    assert report.interventions[0].boundary_origin == origin
    assert report.speech_blocks[0].id == "b003"
    assert report.materials[0].pagine == 18
```

- [ ] **Step 2: Eseguire i test dei modelli e verificare il fallimento**

Run: `.venv/bin/pytest tests/test_models_reporting.py tests/test_intermediate_models.py -q`

Expected: FAIL perché `ChapterBoundaryOrigin`, `SpeechBlock`, `ReportMaterial`, `IntermediateSpeechBlockV11` e `IntermediateCostV11` non esistono.

- [ ] **Step 3: Aggiungere i modelli applicativi con default compatibili con i report storici**

```python
BoundaryReason = Literal[
    "inizio_blocco", "cambio_tema", "slide_e_tema", "cambio_relatore"
]

class ChapterBoundaryOrigin(ReportModel):
    motivo_editoriale: BoundaryReason
    regola_audio: Literal["long_pause", "short_pause", "no_pause"]
    slide_indizio_seconds: Nonnegative | None = None

class SpeechBlock(ReportModel):
    id: str
    start_seconds: Nonnegative
    end_seconds: Nonnegative
    tipo: InterventionKind
    relatori: list[str] = Field(default_factory=list)
    titolo: str
    sinossi: str

class ReportMaterial(ReportModel):
    titolo: str
    relatore: str | None = None
    url: str | None = None
    file: str | None = None
    pagine: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def exactly_one_source(self) -> "ReportMaterial":
        if (self.url is None) == (self.file is None):
            raise ValueError("un materiale richiede esattamente una sorgente")
        return self
```

Add to `Intervention`: optional `block_id`, `chapter_number`, `chapters_in_block`, `boundary_origin`. Add to `SlideChange`: optional `material_title` and `page`. Add to `AcademyContent`: `speech_blocks=[]`, `materials=[]`, `material_failures=[]`, `analysis_profile=1`; new pipeline output will explicitly set profile `2`.

- [ ] **Step 4: Aggiungere il contratto intermedio e le validazioni incrociate**

```python
VerificationCode = Literal[
    "RELATORE_NON_IDENTIFICATO", "TEMPI_INCOERENTI",
    "RELATORE_NON_NEL_REGISTRO", "INTERVENTO_BREVE", "CONFIDENZA_BASSA",
    "MATERIALE_NON_RAGGIUNGIBILE", "CONFINE", "INTERVENTO_LUNGO",
    "SLIDE_NON_ABBINATA", "ALIAS_RELATORE_AMBIGUO",
]

class IntermediateCostV11(_Model):
    valuta: Literal["USD"] = "USD"
    minimo: Nonnegative
    massimo: Nonnegative
    banda_bunny: Nonnegative
    trascrizione: Nonnegative
    analisi: Nonnegative
    criterio: str

class IntermediateSpeechBlockV11(_Model):
    id: str
    start_seconds: Nonnegative = Field(
        validation_alias=AliasChoices("start_seconds", "inizio"),
        serialization_alias="inizio",
    )
    end_seconds: Nonnegative = Field(
        validation_alias=AliasChoices("end_seconds", "fine"),
        serialization_alias="fine",
    )
    tipo: InterventionKind
    relatori: list[str]
    titolo: str
    sinossi: str
```

Extend `IntermediateInterventionV11` with optional `blocco`, `capitolo_numero`, `capitoli_blocco` and `confine_inizio`. Its validator requires the first three fields together for every spoken segment and permits them to be absent only for `pausa` or `logistica` between blocks. Extend `IntermediateVideoV11` with `blocchi_parlato` and `costo_stimato`. Its validator must reject: end times after Bunny duration, incomplete block coverage, duplicate ids, more or fewer than one public didactic chapter, or a public chapter outside 480–900 seconds.

- [ ] **Step 5: Eseguire i test e fare commit**

Run: `.venv/bin/pytest tests/test_models_reporting.py tests/test_intermediate_models.py -q`

Expected: PASS.

```bash
git add app/models.py app/intermediate_models.py tests/test_models_reporting.py tests/test_intermediate_models.py
git commit -m "feat: model factual blocks and granular chapters"
```

### Task 2: Dividere le utterance AssemblyAI su parole reali

**Files:**
- Modify: `app/analysis_chunks.py`
- Test: `tests/test_analysis_chunks.py`
- Test: `tests/test_boundaries.py`

**Interfaces:**
- Consumes: `TranscriptSegment.words: list[TranscriptWord]` ordinati.
- Produces: `split_transcript_atoms(segment, max_seconds=90, max_chars=3000) -> list[TranscriptSegment]`, usato da `split_transcript_windows`.

- [ ] **Step 1: Scrivere test che impediscano duplicazioni e tempi proporzionali**

```python
def test_long_utterance_becomes_word_aligned_atoms_without_duplicates():
    words = [word(f"parola-{index}.", index * 10, index * 10 + .4)
             for index in range(25)]
    source = segment(0, 241, "A", words=words, source_utterance_id="assembly-u000001")
    atoms = split_transcript_atoms(source, max_seconds=90, max_chars=3000)
    assert [word.text for atom in atoms for word in atom.words] == [word.text for word in words]
    assert all(atom.start_seconds == atom.words[0].start_seconds for atom in atoms)
    assert all(atom.end_seconds == atom.words[-1].end_seconds for atom in atoms)
    assert all(atom.source_utterance_id == "assembly-u000001" for atom in atoms)
    assert max(atom.end_seconds - atom.start_seconds for atom in atoms) <= 90
```

Add a second test where a long sentence crosses 90 seconds: the splitter uses the last sentence ending before 90 seconds, or the last complete word before the cap when no sentence end exists.

- [ ] **Step 2: Eseguire i test mirati e osservare il fallimento**

Run: `.venv/bin/pytest tests/test_analysis_chunks.py tests/test_boundaries.py -q`

Expected: FAIL perché `_split_long_segment` copia la lista completa delle parole e calcola tempi proporzionali.

- [ ] **Step 3: Implementare lo splitter word-aligned**

```python
ATOM_MAX_SECONDS = 90
ATOM_MAX_CHARS = 3000

def _segment_from_words(source: TranscriptSegment, words: Sequence[TranscriptWord]) -> TranscriptSegment:
    return source.model_copy(update={
        "start_seconds": words[0].start_seconds,
        "end_seconds": words[-1].end_seconds,
        "text": " ".join(word.text for word in words),
        "words": list(words),
    })

def split_transcript_atoms(
    segment: TranscriptSegment,
    *,
    max_seconds: float = ATOM_MAX_SECONDS,
    max_chars: int = ATOM_MAX_CHARS,
) -> list[TranscriptSegment]:
    if not segment.words:
        raise ValueError("Un segmento richiede parole per la divisione editoriale")
    atoms, start = [], 0
    while start < len(segment.words):
        candidates, chars = [], 0
        for index in range(start, len(segment.words)):
            word = segment.words[index]
            chars += len(word.text) + (index > start)
            if word.end_seconds - segment.words[start].start_seconds > max_seconds or chars > max_chars:
                break
            candidates.append(index)
        if not candidates:
            candidates = [start]
        sentence_ends = [index for index in candidates if re.search(r"[.!?…][\"'’)]*$", segment.words[index].text)]
        end = sentence_ends[-1] if sentence_ends else candidates[-1]
        atoms.append(_segment_from_words(segment, segment.words[start:end + 1]))
        start = end + 1
    return atoms
```

Replace both proportional time splitting and character slicing with repeated word-boundary splitting. Keep fail-closed behavior for missing word evidence, because the analyzer already requires it.

- [ ] **Step 4: Verificare finestre e allineamento**

Run: `.venv/bin/pytest tests/test_analysis_chunks.py tests/test_boundaries.py tests/test_assemblyai.py -q`

Expected: PASS, including the zero-duration AssemblyAI word regression tests.

- [ ] **Step 5: Commit**

```bash
git add app/analysis_chunks.py tests/test_analysis_chunks.py tests/test_boundaries.py
git commit -m "fix: split transcript windows on real word timings"
```

### Task 3: Pianificare blocchi fattuali e capitoli deterministici

**Files:**
- Create: `app/chapters.py`
- Modify: `app/analysis_chunks.py`
- Modify: `app/boundaries.py`
- Test: `tests/test_chapters.py`
- Test: `tests/test_boundaries.py`

**Interfaces:**
- Consumes: `Sequence[SemanticIntervention]`, `Sequence[SlideChange]` e silenzi misurati.
- Produces: `plan_semantic_timeline(groups: Sequence[SemanticIntervention], slides: Sequence[SlideChange]) -> PlannedTimeline`; `align_intervention_boundaries` restituisce anche `blocks` e origini audio.

- [ ] **Step 1: Scrivere i test del pianificatore**

```python
def topic_unit(start: float, end: float, speaker: str) -> SemanticIntervention:
    words = (
        TranscriptWord(text="Apertura.", start_seconds=start, end_seconds=start + .4,
                       diarization_label="A", confidence=.99),
        TranscriptWord(text="Chiusura.", start_seconds=end - .4, end_seconds=end,
                       diarization_label="A", confidence=.99),
    )
    source = TranscriptSegment(
        start_seconds=start, end_seconds=end, diarization_label="A",
        text="Apertura. Chiusura.", source_utterance_id=f"u-{start}", words=list(words),
    )
    return SemanticIntervention(
        tipo="intervento", relatori=(speaker,), titolo=f"Tema {start}",
        sintesi=f"Contenuto didattico da {start} a {end}.",
        punti_chiave=("Primo", "Secondo", "Terzo"), confidenza=.95,
        segments=(source,), boundary_reason="cambio_tema",
    )

def test_long_speaker_block_is_preserved_and_partitioned_near_nine_minutes():
    units = [topic_unit(index * 180, (index + 1) * 180, "Furio D'Andrea")
             for index in range(13)]
    plan = plan_semantic_timeline(units, slides=[
        SlideChange(timestamp_seconds=1280, title="Poteri", confidence="alta"),
        SlideChange(timestamp_seconds=1369, title="Decisioni assembleari", confidence="alta"),
        SlideChange(timestamp_seconds=2546, title="Direttive", confidence="alta"),
    ])
    assert len(plan.blocks) == 1
    assert plan.blocks[0].start_seconds == 0
    assert plan.blocks[0].end_seconds == 2340
    assert all(chapter.block_id == "b001" for chapter in plan.groups)
    assert all(480 <= chapter.raw_duration <= 900 for chapter in plan.groups)
    assert [chapter.chapter_number for chapter in plan.groups] == list(range(1, len(plan.groups) + 1))
```

Add tests for: one indivisible 1,560-second unit retained with `long=True`; a moderator transition ending one block; deterministic output over two calls; a slide within 30 seconds producing `slide_e_tema`, while a slide without semantic closure never creates a cut.

- [ ] **Step 2: Eseguire i nuovi test e verificare il fallimento**

Run: `.venv/bin/pytest tests/test_chapters.py tests/test_boundaries.py -q`

Expected: FAIL perché `app.chapters` e la metadata del blocco non esistono.

- [ ] **Step 3: Implementare il pianificatore con funzione costo deterministica**

```python
PREFERRED_MIN = 480
PREFERRED_TARGET = 540
PREFERRED_MAX = 600
ACCEPTABLE_MAX = 900
SOFT_MAX = 1200

def duration_penalty(seconds: float) -> float:
    if PREFERRED_MIN <= seconds <= PREFERRED_MAX:
        return abs(seconds - PREFERRED_TARGET)
    if seconds < PREFERRED_MIN:
        return 5000 + (PREFERRED_MIN - seconds) * 8
    if seconds <= ACCEPTABLE_MAX:
        return 1000 + (seconds - PREFERRED_MAX) * 2
    if seconds <= SOFT_MAX:
        return 3000 + (seconds - ACCEPTABLE_MAX) * 6
    return float("inf")
```

Use dynamic programming over semantic-unit boundaries. Compare candidates with `(total_penalty, chapter_count, tuple(cut_indexes))`, so equal inputs always choose the same cuts. A single semantic unit over 1,200 seconds remains intact and is marked long. Build factual blocks before duration optimization: consecutive `intervento` units with the same ordered diarization-label set belong to one block; display names are metadata and cannot split a voice block merely because an alias changes. `saluti`, `domande` and `cambio_relatore` form their own spoken blocks, while `pausa` and `logistica` close the block and remain explicit unlinked gap segments.

For every merged chapter, choose the first high-confidence non-generic slide title near its opening; otherwise use the title of the longest constituent thematic unit. Build `sintesi` by joining distinct constituent summaries in chronological order, and deduplicate key points while retaining the first seven. This keeps the chapter summary specific without adding another generative call. Build the factual block title from its longest chapter and its `sinossi` from the ordered chapter summaries.

Define the planner result explicitly:

```python
@dataclass(frozen=True)
class PlannedBlock:
    id: str
    start_seconds: float
    end_seconds: float
    group_indexes: tuple[int, ...]

@dataclass(frozen=True)
class PlannedTimeline:
    groups: tuple[SemanticIntervention, ...]
    blocks: tuple[PlannedBlock, ...]
```

- [ ] **Step 4: Integrare i blocchi nell'allineatore audio**

Extend `SemanticIntervention` with `block_id`, chapter counters, editorial boundary reason and optional slide hint. For two adjacent chapters in the same block, use the selected audio boundary but do not insert a standalone pause; their end/start stay coincident. A long silence between different blocks may generate the explicit `pausa` segment already supported.

```python
@dataclass(frozen=True)
class BoundaryAlignment:
    interventions: list[Intervention]
    boundaries: list[BoundaryEvidence]
    blocks: list[SpeechBlock]
```

After selecting `long_pause`, `short_pause` or `no_pause`, store it in the following chapter's `ChapterBoundaryOrigin`. Construct each `SpeechBlock` from the first and last aligned chapter carrying its id, and verify the child chapters form an exact partition.

Replace the current rounded media endpoint with a single helper:

```python
def bunny_end_second(duration_seconds: float) -> int:
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("Durata Bunny non valida")
    return math.floor(duration_seconds)
```

Use this helper in alignment completeness checks and reject every word, block, chapter or slide endpoint above it. Add a regression using Bunny `5789.248` and provider audio `5789.496`: the exported/aligned endpoint remains `5789`.

- [ ] **Step 5: Eseguire i test e commit**

Run: `.venv/bin/pytest tests/test_chapters.py tests/test_boundaries.py tests/test_analysis_chunks.py -q`

Expected: PASS.

```bash
git add app/chapters.py app/analysis_chunks.py app/boundaries.py tests/test_chapters.py tests/test_boundaries.py
git commit -m "feat: preserve speech blocks and plan lesson-sized chapters"
```

### Task 4: Fornire slide-indizio al modello e integrare il nuovo piano nell'analisi

**Files:**
- Modify: `app/analysis_chunks.py`
- Modify: `app/analysis.py`
- Modify: `app/prompts.py`
- Modify: `app/models.py`
- Test: `tests/test_analysis.py`
- Test: `tests/test_analysis_chunks.py`

**Interfaces:**
- Consumes: slide classificate, finestre atomiche, `plan_semantic_timeline`.
- Produces: `AnalysisResult` con `speech_blocks`, capitoli, origini dei confini e `analysis_profile=2`.

- [ ] **Step 1: Scrivere test per payload, prompt e risultato**

```python
def test_window_payload_includes_only_nearby_slide_hints():
    transcript_window = TranscriptWindow(
        start_seconds=600,
        end_seconds=1200,
        segments=[TranscriptSegment(
            start_seconds=600, end_seconds=1200, diarization_label="A",
            text="Contenuto.", source_utterance_id="u-1",
            words=[TranscriptWord(
                text="Contenuto.", start_seconds=600, end_seconds=600.5,
                diarization_label="A", confidence=.99,
            )],
        )],
    )
    payload = json.loads(_window_payload(
        transcript_window, [], None,
        slide_hints=[
            SlideChange(timestamp_seconds=590, title="Premesse", confidence="alta"),
            SlideChange(timestamp_seconds=900, title="Assemblea", confidence="alta"),
            SlideChange(timestamp_seconds=1300, title="Fuori", confidence="alta"),
        ],
    ))
    assert payload["slide_hints"] == [
        {"timestamp_seconds": 590, "title": "Premesse"},
        {"timestamp_seconds": 900, "title": "Assemblea"},
    ]
```

Add an analyzer test asserting the planner receives every window group and all slide timestamps, and `_result_with_slides` returns blocks and `analysis_profile == 2` without changing Bunny duration.

- [ ] **Step 2: Eseguire i test e osservare il fallimento**

Run: `.venv/bin/pytest tests/test_analysis.py tests/test_analysis_chunks.py -q`

Expected: FAIL perché il payload non contiene `slide_hints` e il risultato non contiene blocchi.

- [ ] **Step 3: Estendere la bozza AI senza delegarle il timestamp finale**

Add to `WindowInterventionDraft`:

```python
confine_motivo: Literal["inizio_blocco", "cambio_tema", "slide_e_tema", "cambio_relatore"]
slide_indizio_seconds: Nonnegative | None = None
```

Validate that `slide_indizio_seconds` is present only for `slide_e_tema` and equals one of the supplied slide hints. Update `WINDOW_PROMPT` to ask for small homogeneous thematic units and to use a slide only when the spoken content also closes a theme. Keep the explicit rule that the model never chooses the final second.

- [ ] **Step 4: Collegare pianificatore, allineatore e risultato**

Pass bounded slide hints into each `_window_payload`. In `materialize_interventions`, convert drafts to topic units, call `plan_semantic_timeline`, then call the audio aligner. In `_result_with_slides`, set:

```python
content = AnalysisResult(
    **data,
    slides=slide_data,
    interventions=alignment.interventions,
    boundaries=alignment.boundaries,
    speech_blocks=alignment.blocks,
    audio_boundary_version=1,
    analysis_profile=2,
    usage=usage,
)
```

The final local validation must reject missing chapter/block references, non-contiguous child chapters, duplicated ids and any endpoint beyond `floor(metadata.duration_seconds)`.

- [ ] **Step 5: Eseguire test e commit**

Run: `.venv/bin/pytest tests/test_analysis.py tests/test_analysis_chunks.py tests/test_boundaries.py tests/test_chapters.py -q`

Expected: PASS.

```bash
git add app/analysis.py app/analysis_chunks.py app/prompts.py app/models.py tests/test_analysis.py tests/test_analysis_chunks.py
git commit -m "feat: derive chapter cuts from themes slides and silence"
```

### Task 5: Canonicalizzare relatori e alias contro il Registro

**Files:**
- Create: `app/academy_registry.py`
- Modify: `app/reporting.py`
- Modify: `app/intermediate_report.py`
- Test: `tests/test_models_reporting.py`
- Test: `tests/test_intermediate_report.py`

**Interfaces:**
- Consumes: nomi/ruoli da profili, interventi, inventario e Registro incorporato.
- Produces: `reconcile_speakers_detailed(report) -> SpeakerReconciliation` con persone canoniche, mappa alias e avvisi ambigui.

- [ ] **Step 1: Scrivere test sui cinque relatori Governance e sull'omonimia**

```python
def test_governance_aliases_collapse_to_registry_people_and_rewrite_references():
    report = report_with_names([
        "Furio d'Andrea", "Furio D’Andrea", "Furio D ’ Andrea",
        "Avvocato Furio D'Andrea", "Luigi Morra",
        "Dottor Morra", "Antonio Sibilia", "Dottor Sibilia",
        "Gaetano De Vito", "Vincenzo Manfredi",
    ])
    result = build_intermediate_report(report, TARGET_GUID)
    assert [(person.nome, person.slug) for person in result.relatori] == [
        ("Furio D'Andrea", "furio-dandrea"),
        ("Luigi Morra", "luigi-morra"),
        ("Antonio Sibilia", "antonio-sibilia"),
        ("Gaetano De Vito", "gaetano-de-vito"),
        ("Vincenzo Manfredi", "vincenzo-manfredi"),
    ]
    assert all("Dottor" not in name and "Avvocato" not in name
               for item in result.video[0].interventi for name in item.relatori)
```

Add a test with two registered people sharing a surname: a surname-only alias must stay separate and emit `ALIAS_RELATORE_AMBIGUO`.

- [ ] **Step 2: Eseguire test mirati e verificare il fallimento**

Run: `.venv/bin/pytest tests/test_models_reporting.py tests/test_intermediate_report.py -q`

Expected: FAIL perché il Registro non contiene Furio e i titoli onorifici restano nel nome.

- [ ] **Step 3: Creare il Registro canonico e il parser degli onorifici**

```python
@dataclass(frozen=True)
class RegistryPerson:
    nome: str
    slug: str

REGISTRY_PEOPLE = (
    RegistryPerson("Vincenzo Manfredi", "vincenzo-manfredi"),
    RegistryPerson("Gaetano De Vito", "gaetano-de-vito"),
    RegistryPerson("Furio D'Andrea", "furio-dandrea"),
    RegistryPerson("Antonio Sibilia", "antonio-sibilia"),
    RegistryPerson("Luigi Morra", "luigi-morra"),
)
```

Strip `avv.`, `avvocato`, `avvocata`, `dott.`, `dottor`, `dottore`, `dottoressa`, `prof.`, `professore`, `professoressa` before matching. Return the stripped title as a fallback qualification only when no more specific role exists. Match surname-only aliases only when exactly one candidate exists in the report plus Registry.

- [ ] **Step 4: Refactor della riconciliazione e riscrittura dei riferimenti**

Keep `reconcile_speakers(report)` as a compatibility wrapper returning the speaker list. Add:

```python
@dataclass
class SpeakerReconciliation:
    speakers: list[ReconciledSpeaker]
    canonical_by_key: dict[str, str]
    ambiguous_aliases: list[str]
```

Use the detailed result in the intermediate builder. Merge evidence, origins, roles and organizations; canonicalize every block, chapter, slide title and synopsis with the same map. Emit `ALIAS_RELATORE_AMBIGUO` only for unresolved aliases, never for a successful Registry match.

- [ ] **Step 5: Eseguire test e commit**

Run: `.venv/bin/pytest tests/test_models_reporting.py tests/test_intermediate_report.py -q`

Expected: PASS.

```bash
git add app/academy_registry.py app/reporting.py app/intermediate_report.py tests/test_models_reporting.py tests/test_intermediate_report.py
git commit -m "fix: canonicalize Academy speakers and honorific aliases"
```

### Task 6: Recuperare soltanto sorgenti materiali reali

**Files:**
- Create: `app/material_registry.py`
- Modify: `app/inventory.py`
- Modify: `app/main.py`
- Test: `tests/test_inventory.py`

**Interfaces:**
- Consumes: celle inventario, hyperlink Google Sheets e GUID Bunny.
- Produces: `material_sources_for_video(courses: Sequence[InventoryCourse], video_id: UUID, title: str) -> list[str]` con `Titolo | URL` reali; `AnalysisInventoryContext` con speaker hints e materiali.

- [ ] **Step 1: Scrivere i test per registro GUID e rich-text hyperlink**

```python
def test_governance_guid_returns_curated_real_materials_before_sheet_labels():
    sources = material_sources_for_video(
        [inventory_course(materiali=["Slide Furio D'Andrea", "Slide Luigi Morra"])],
        UUID("7f254c4d-fe34-4fd3-a4cf-cda4f447e438"),
        "Governance delle holding e conferimenti a realizzo controllato",
    )
    assert sources == [
        "Slide · Furio D'Andrea | https://www.assoholding.it/wp-content/uploads/2026/07/19072026_PP-Avv.-Furio-DAndrea_Webinar-22-luglio-2026.pptx",
        "Slide · Luigi Morra | https://www.assoholding.it/wp-content/uploads/2026/07/Slide-Morra-Conferimenti-1.pptx",
    ]
```

Add a Sheets API fixture where a material cell has `formattedValue: "Slide · Persona"` and `hyperlink: "https://www.assoholding.it/deck.pdf"`; `InventoryClient.fetch()` must retain the combined source string.

- [ ] **Step 2: Eseguire i test e osservare il fallimento**

Run: `.venv/bin/pytest tests/test_inventory.py -q`

Expected: FAIL perché il CSV perde hyperlink e non esiste il registro GUID.

- [ ] **Step 3: Creare il registro materiali curato**

```python
GOVERNANCE_GUID = UUID("7f254c4d-fe34-4fd3-a4cf-cda4f447e438")
CURATED_MATERIALS = {
    GOVERNANCE_GUID: (
        "Slide · Furio D'Andrea | https://www.assoholding.it/wp-content/uploads/2026/07/19072026_PP-Avv.-Furio-DAndrea_Webinar-22-luglio-2026.pptx",
        "Slide · Luigi Morra | https://www.assoholding.it/wp-content/uploads/2026/07/Slide-Morra-Conferimenti-1.pptx",
    ),
}
```

When a GUID has curated entries, use them first and discard bare labels referring to the same deck. Preserve unrelated explicit URLs from the sheet and deduplicate by normalized URL.

- [ ] **Step 4: Leggere hyperlink con Google Sheets API quando il service account è configurato**

Request `spreadsheets.get` with `includeGridData=true` and fields limited to sheet id/title, formatted values, cell hyperlinks and text-format-run links. Reconstruct the same rows used by `_parse_tab`; for material columns convert a linked label to `label | URL`. Without a service account, retain the current public CSV path. A failed authenticated read remains an `InventoryError` and must not silently turn linked materials into fake filenames.

Replace the separate speaker provider in `main.py` with:

```python
@dataclass(frozen=True)
class AnalysisInventoryContext:
    speaker_hints: tuple[str, ...]
    material_sources: tuple[str, ...]
```

- [ ] **Step 5: Eseguire test e commit**

Run: `.venv/bin/pytest tests/test_inventory.py tests/test_web.py -q`

Expected: PASS.

```bash
git add app/material_registry.py app/inventory.py app/main.py tests/test_inventory.py
git commit -m "feat: resolve real inventory material URLs"
```

### Task 7: Estrarre PPTX/PDF e abbinare slide, materiale e pagina

**Files:**
- Create: `app/materials.py`
- Modify: `app/config.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Test: `tests/test_materials.py`

**Interfaces:**
- Consumes: sorgenti dichiarate, `SlideChange`, workspace temporaneo e host consentiti.
- Produces: `MaterialAnalysis(materials, slides, failures)`; nessun file esce dal workspace.

- [ ] **Step 1: Scrivere test con PPTX/PDF sintetici e casi ambigui**

```python
def write_test_pptx(path: Path, pages: list[list[str]]) -> Path:
    with ZipFile(path, "w") as archive:
        for number, lines in enumerate(pages, start=1):
            runs = "".join(
                f"<a:r><a:t>{escape(line)}</a:t></a:r>" for line in lines
            )
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                "<p:sld xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\" "
                "xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\">"
                f"<p:cSld><a:p>{runs}</a:p></p:cSld></p:sld>",
            )
    return path

def test_pptx_pages_are_matched_to_video_slides(tmp_path):
    deck = write_test_pptx(tmp_path / "governance.pptx", [
        ["PREMESSE", "Poteri e responsabilità"],
        ["DECISIONI ASSEMBLEARI", "Quorum e maggioranze"],
    ])
    material = ReportMaterial(titolo="Slide · Furio D'Andrea", file=str(deck))
    pages = extract_deck_pages(deck)
    slides = match_slides_to_material(
        [SlideChange(timestamp_seconds=10, title="Decisioni assembleari",
                     visible_content=["Quorum e maggioranze"], confidence="alta")],
        material,
        pages,
    )
    assert slides[0].material_title == "Slide · Furio D'Andrea"
    assert slides[0].page == 2
    assert len(pages) == 2
```

Add tests for PDF extraction, a score below threshold, a runner-up margin below threshold, a page-order regression, URL credentials, disallowed hosts, redirect to a disallowed host, 50 MB download limit and workspace cleanup after an exception.

- [ ] **Step 2: Eseguire i test e verificare il fallimento**

Run: `.venv/bin/pytest tests/test_materials.py -q`

Expected: FAIL perché `app.materials` non esiste.

- [ ] **Step 3: Implementare fetch e parser transitori**

Add `pypdf>=6,<7` to project dependencies. Add `material_allowed_hosts: str = "www.assoholding.it"` to `Settings`; parse it into exact lowercase hostnames.

```python
@dataclass(frozen=True)
class DeckPage:
    number: int
    text: str

@dataclass(frozen=True)
class MaterialAnalysis:
    materials: tuple[ReportMaterial, ...]
    slides: tuple[SlideChange, ...]
    failures: tuple[str, ...]
```

Expose `extract_deck_pages(path: Path) -> tuple[DeckPage, ...]` and
`match_slides_to_material(slides: Sequence[SlideChange], material: ReportMaterial, pages: Sequence[DeckPage]) -> tuple[SlideChange, ...]` for the processor and focused tests.

Stream HTTPS responses to a file inside the supplied workspace, with credentials forbidden, exact host allowlist, redirect revalidation, 20-second total timeout and 50 MB maximum. Parse PPTX through `zipfile` and `ppt/slides/slideN.xml`, enforcing a 100 MB total uncompressed limit. Parse PDF pages through `pypdf.PdfReader`. Never execute macros or embedded links.

- [ ] **Step 4: Implementare l'abbinamento conservativo**

Normalize title/OCR and page text into lowercase alphanumeric tokens. Score each candidate with `0.6 * token_jaccard + 0.4 * SequenceMatcher.ratio()`. Accept only `score >= 0.55`, margin over the second candidate `>= 0.10`, and non-decreasing page order within the same material. When no candidate passes, leave `material_title/page` absent; the intermediate builder will emit `SLIDE_NON_ABBINATA`.

- [ ] **Step 5: Eseguire test e commit**

Run: `.venv/bin/pytest tests/test_materials.py tests/test_security.py -q`

Expected: PASS.

```bash
git add app/materials.py app/config.py pyproject.toml .env.example tests/test_materials.py
git commit -m "feat: map detected slides to verified deck pages"
```

### Task 8: Elaborare i materiali dentro il lavoro e persistere solo il risultato testuale

**Files:**
- Modify: `app/pipeline.py`
- Modify: `app/main.py`
- Modify: `app/models.py`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_end_to_end.py`

**Interfaces:**
- Consumes: `AnalysisInventoryContext`, `MaterialProcessor`, workspace ancora aperto.
- Produces: `AcademyReport` persistito con materiali verificati, pagine slide, failure sintetiche e costo esistente.

- [ ] **Step 1: Scrivere test di pipeline per persistenza e pulizia**

```python
def test_pipeline_persists_material_metadata_but_removes_downloaded_deck(components, tmp_path):
    components.pipeline.context_provider = lambda metadata: AnalysisInventoryContext(
        speaker_hints=(),
        material_sources=("Slide · Furio D'Andrea | https://www.assoholding.it/furio.pptx",),
    )

    class FakeMaterialProcessor:
        def process(self, sources, slides, workspace, cancellation_event):
            deck = workspace / "furio.pptx"
            deck.write_bytes(b"temporary deck bytes")
            return MaterialAnalysis(
                materials=(ReportMaterial(
                    titolo="Slide · Furio D'Andrea", relatore="Furio D'Andrea",
                    url="https://www.assoholding.it/furio.pptx", pagine=2,
                ),),
                slides=tuple(slide.model_copy(update={
                    "material_title": "Slide · Furio D'Andrea", "page": 2,
                }) for slide in slides),
                failures=(),
            )

    components.pipeline.material_processor = FakeMaterialProcessor()
    report = components.pipeline.run(SOURCE, lambda percent, message: None)
    assert report.materials[0].titolo == "Slide · Furio D'Andrea"
    assert report.slides[0].page == 2
    assert list(tmp_path.iterdir()) == []
    serialized = report.model_dump_json()
    assert "Poteri e responsabilità" not in serialized
```

Add a test where material download fails: analysis still reaches 100%, `materials` stays empty and `material_failures` contains only an application-authored message without URL credentials or response body.

- [ ] **Step 2: Eseguire i test e osservare il fallimento**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_end_to_end.py -q`

Expected: FAIL perché il pipeline non riceve materiali e non invoca il processor.

- [ ] **Step 3: Integrare il contesto inventario e il processor**

Change `AnalysisPipeline.__init__` to accept:

```python
context_provider: Callable[[BunnyVideoMetadata], AnalysisInventoryContext] | None = None
material_processor: MaterialProcessor | None = None
```

Fetch the context once after Bunny metadata. Pass `speaker_hints` to the analyzer. After slide/content analysis and before the `ExitStack` closes, call the material processor with `context.material_sources`, `content.slides`, current workspace and cancellation event. Copy only `ReportMaterial`, updated `SlideChange` and safe failure codes into `AcademyReport`.

- [ ] **Step 4: Conservare la non-fatalità dei materiali**

Network, parse and ambiguous-match failures must not become `PipelineError`. Cancellation still aborts. Unexpected local programming errors remain failures. Verify logs contain only phase, safe code and counts; no deck text or signed URL.

- [ ] **Step 5: Eseguire test e commit**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_end_to_end.py tests/test_security.py -q`

Expected: PASS.

```bash
git add app/pipeline.py app/main.py app/models.py tests/test_pipeline.py tests/test_end_to_end.py
git commit -m "feat: analyze course materials inside ephemeral jobs"
```

### Task 9: Costruire il JSON finale con durata Bunny, accesso, costo e avvisi

**Files:**
- Modify: `app/intermediate_report.py`
- Modify: `app/intermediate_models.py`
- Modify: `app/web.py`
- Modify: `app/reporting.py`
- Test: `tests/test_intermediate_report.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: nuovo `AcademyReport` persistito con `analysis_profile=2`; i report storici restano leggibili in Markdown/TXT e richiedono rianalisi per il nuovo JSON.
- Produces: JSON v1.1 completo e Markdown/TXT leggibili con blocchi, capitoli, materiali e costo.

- [ ] **Step 1: Scrivere test completo della busta Governance**

```python
def test_intermediate_json_exposes_blocks_chapters_cost_and_one_public_preview():
    result = build_intermediate_report(governance_report(), TARGET_GUID)
    video = result.video[0]
    assert video.durata_secondi == 5789
    assert max(item.end_seconds for item in video.interventi) <= 5789
    assert [item.id for item in video.blocchi_parlato] == ["v1-b001", "v1-b002"]
    assert all(item.blocco in {block.id for block in video.blocchi_parlato}
               for item in video.interventi if item.tipo not in {"pausa", "logistica"})
    public = [item for item in video.interventi if item.accesso == "pubblico"]
    assert len(public) == 1
    assert public[0].tipo == "intervento"
    assert 480 <= public[0].end_seconds - public[0].start_seconds <= 900
    assert video.costo_stimato.valuta == "USD"
    assert video.costo_stimato.massimo >= video.costo_stimato.minimo
```

Add assertions for `INTERVENTO_LUNGO` warning/non-blocking status, `SLIDE_NON_ABBINATA`, material/page serialization, canonical titles, block coverage, exact cost mapping and stable `v1-bNNN`/`v1-iNNN` ids across two builds.

- [ ] **Step 2: Eseguire test e osservare il fallimento**

Run: `.venv/bin/pytest tests/test_intermediate_report.py tests/test_web.py -q`

Expected: FAIL perché il builder esporta ancora timeline piatta, usa `nearest_second` e recupera materiali soltanto al download.

- [ ] **Step 3: Rifattorizzare il builder intermedio**

Use `math.floor(report.duration_seconds)` once for `durata_secondi` and clamp only legacy report endpoints to it. Map stored block ids to `v1-bNNN`; map chapter ids chronologically to `v1-iNNN`; rewrite each `blocco` reference after canonical speaker reconciliation. Copy `confine_inizio` from the persisted boundary origin and serialize its optional slide hint in `h:mm:ss`.

Map cost exactly:

```python
cost = IntermediateCostV11(
    valuta="USD",
    minimo=report.cost.estimated_low_usd,
    massimo=report.cost.estimated_high_usd,
    banda_bunny=report.cost.bunny_bandwidth_usd,
    trascrizione=report.cost.transcription_usd,
    analisi=report.cost.analysis_usd,
    criterio=report.cost.basis,
)
```

Select exactly one public chapter from the first chronological didactic chapter between 480 and 900 seconds. Do not use the old fallback that selected an oversized intervention.

- [ ] **Step 4: Generare verifiche e aggiornare le rotte**

Emit `INTERVENTO_LUNGO` for `intervento > 1200` seconds, `SLIDE_NON_ABBINATA` for a detected slide without material/page, `ALIAS_RELATORE_AMBIGUO` from reconciliation and `MATERIALE_NON_RAGGIUNGIBILE` from stored failure codes. Only `RELATORE_NON_IDENTIFICATO` and `TEMPI_INCOERENTI` set `da_verificare`.

For profile-2 reports, `/jobs/{id}/report.json` uses persisted materials and performs no network request. For legacy reports, Markdown/TXT remain available, while `/report.json` returns `409 Rianalisi necessaria per il formato granulare` instead of inventing blocks or chapter metadata. Remove the export-time inventory/material probe. Keep a strict source parser in `app.materials`: any bare string without URL or recognized existing file is rejected. Render blocks, chapter origins, page references and cost in Markdown/TXT.

- [ ] **Step 5: Eseguire test e commit**

Run: `.venv/bin/pytest tests/test_intermediate_report.py tests/test_web.py tests/test_models_reporting.py -q`

Expected: PASS.

```bash
git add app/intermediate_report.py app/intermediate_models.py app/web.py app/reporting.py tests/test_intermediate_report.py tests/test_web.py
git commit -m "feat: export complete deterministic Academy video reports"
```

### Task 10: Regressione completa, ripetibilità reale e pubblicazione Railway

**Files:**
- Modify: `tests/fixtures/report.json`
- Modify: `tests/test_live_bunny.py`
- Modify: `tests/test_live_diagnostics.py`
- Modify: `README.md`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: tutte le componenti precedenti e le credenziali runtime già configurate.
- Produces: suite verde, wheel coerente, deploy Railway sano e due report Governance confrontabili.

- [ ] **Step 1: Aggiornare fixture, live assertions e documentazione operativa**

Extend the fixture with one factual block, chapter metadata, `analysis_profile: 2`, empty verified materials and an eligible public chapter. In `test_real_bunny_video_produces_valid_report`, assert complete block coverage, no endpoint beyond Bunny duration, stable ids, one public candidate and no persisted transient text. Document `MATERIAL_ALLOWED_HOSTS=www.assoholding.it`, pypdf, material limits and the three warning codes in README.

- [ ] **Step 2: Rigenerare il lock e lanciare la suite offline completa**

Run: `uv lock`

Run: `.venv/bin/python -m pytest -m 'not live' -q --ignore=tests/test_media.py -k 'not test_form_to_report_with_real_ffmpeg_and_ephemeral_cleanup'`

Expected: tutti i test offline che non richiedono il binario FFmpeg locale PASS; i test live risultano deselected. I test FFmpeg esclusi vengono coperti dal container Railway nel passo di pubblicazione e dal lavoro reale Governance.

- [ ] **Step 3: Verificare build e contenuto del pacchetto**

Run: `uv build`

Run: `unzip -l dist/bunny_video_report-0.1.0-py3-none-any.whl | rg 'app/(materials|chapters|academy_registry|material_registry)\.py|app/templates|app/static'`

Expected: wheel creata e contenente i quattro nuovi moduli, template e asset statici.

- [ ] **Step 4: Commit finale e pubblicazione**

```bash
git add tests/fixtures/report.json tests/test_live_bunny.py tests/test_live_diagnostics.py README.md uv.lock
git commit -m "test: verify granular report delivery contract"
git push origin main
```

Wait for Railway deployment, then verify `GET /healthz` returns `{"status":"ok"}` and the authenticated catalog loads. Do not open or modify either Academy comparison course.

- [ ] **Step 5: Eseguire due analisi Governance e confrontare i risultati**

From the authenticated web UI, analyze GUID `7f254c4d-fe34-4fd3-a4cf-cda4f447e438` twice with the same deployed revision. Download both `report.json` files and run a local comparison that removes only `costo_stimato` before checking:

```python
assert [item["id"] for item in first_video["interventi"]] == [
    item["id"] for item in second_video["interventi"]
]
assert len(first_video["interventi"]) == len(second_video["interventi"])
for left, right in zip(first_video["interventi"], second_video["interventi"], strict=True):
    assert abs(parse_hms(left["inizio"]) - parse_hms(right["inizio"])) <= 1
    assert abs(parse_hms(left["fine"]) - parse_hms(right["fine"])) <= 1
```

Expected: both jobs complete at 100%; five canonical Registry people; Furio's factual long block retained; approximately 10–12 chapters unless an indivisible block is warned; exactly one public 8–15 minute chapter; real Furio/Luigi URLs; accepted slides carry material/page; all endpoints are at or before Bunny duration; JSON/Markdown/TXT download successfully.

- [ ] **Step 6: Verificare non-regressione Academy e registrare evidenza**

Run before and after production validation: `git -C ../assoholding-platform status --short`

Expected: identical output before and after. Record only job ids, deployed commit, counts, duration, cost range, warning codes and timing comparison; do not record transcript text, signed media URLs or secrets.
