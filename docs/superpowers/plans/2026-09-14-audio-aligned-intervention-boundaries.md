# Audio-Aligned Intervention Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Produce contiguous intervention boundaries verified against audio silence, persist compact five-word evidence, and emit one CONFINE warning for every internal pair.

**Architecture:** Extend the existing AssemblyAI response parser with ephemeral word timestamps and the existing FFmpeg pass with ephemeral silence intervals. A new pure boundary module combines complete semantic utterance groups with those two evidence sources before AcademyReport persistence; the v1.1 exporter renders only compact boundary evidence and rejects historical reports that lack it.

**Tech Stack:** Python 3.12+, Pydantic 2, FastAPI, HTTPX, AssemblyAI pre-recorded REST, FFmpeg silencedetect, SQLite, pytest, Node test runner, Railway.

**Spec:** docs/superpowers/specs/2026-09-14-audio-aligned-intervention-boundaries-design.md

## Global Constraints

- Do not store video, audio, full transcripts, word arrays, silence arrays, provider bodies, or FFmpeg diagnostics.
- Do not log transcript words, provider bodies, material cells, Bunny secrets, or temporary paths.
- Use AssemblyAI word timestamps from the existing transcription request; do not add a provider or subscription.
- Detect silence during the existing FFmpeg source read; do not add a second Bunny download.
- Never use a slide timestamp as an intervention boundary.
- Preserve complete utterances and semantic reasoning; never split a sentence, example, explanation, or answer.
- Keep a moderator in a separate saluti, domande, or cambio_relatore segment.
- Preserve a contiguous whole-second timeline: every internal fine equals the next inizio.
- Long pause (at least 2 seconds): one-second speech margins; short pause: midpoint; no pause: previous last-word end.
- Emit exactly len(interventi) - 1 CONFINE warnings, excluding absolute video start and end.
- CONFINE is always livello avviso, attaches to the next exported intervention, and names the previous exported ID.
- A missing boundary input or incomplete audit fails closed with fixed application text.
- Historical reports remain readable for Markdown/TXT but their JSON returns HTTP 409 until reanalysis.
- The intermediate envelope stays versione 1 and strictly one video v1.
- Tests must follow RED then GREEN; production deployment waits for a clean whole-branch review.

---

### Task 1: Preserve AssemblyAI word timing ephemerally

**Files:**
- Modify: app/transcription.py
- Modify: app/assemblyai.py
- Modify: app/analysis_chunks.py
- Modify: tests/test_assemblyai.py
- Modify: tests/test_analysis_chunks.py
- Modify: tests/test_transcription.py

**Interfaces:**
- Produces: TranscriptWord(text, start_seconds, end_seconds, diarization_label, confidence).
- Produces: TranscriptSegment.source_utterance_id and TranscriptSegment.words.
- Consumes: AssemblyAI utterances[i].words[j].
- Preserves: TranscriptWindow.to_payload without any words member.

- [ ] **Step 1: Add failing AssemblyAI word-contract tests**

Add a word fixture and a completed response whose expected in-memory values are exact:

    def word(text: str, start: int, end: int, speaker: str = "A") -> dict:
        return {
            "text": text,
            "start": start,
            "end": end,
            "confidence": 0.97,
            "speaker": speaker,
        }

    def test_assemblyai_preserves_ordered_word_timing_in_memory():
        result = AssemblyAITranscriber._parse_result({
            "status": "completed",
            "audio_duration": 3,
            "language_code": "it",
            "text": "La governance evolve.",
            "utterances": [{
                "speaker": "A",
                "start": 500,
                "end": 2500,
                "text": "La governance evolve.",
                "words": [
                    word("La", 500, 700),
                    word("governance", 800, 1500),
                    word("evolve.", 1600, 2500),
                ],
            }],
        }, 3)
        segment = result.original_segments[0]
        assert segment.source_utterance_id == "assembly-u000001"
        assert [(item.text, item.start_seconds, item.end_seconds) for item in segment.words] == [
            ("La", .5, .7), ("governance", .8, 1.5), ("evolve.", 1.6, 2.5),
        ]

Parameterize missing/empty words, reversed or unordered word times, word outside utterance, mismatched speaker, empty text, non-finite confidence, and confidence outside 0 through 1. Require TranscriptionError code response and safe exception/log text.

- [ ] **Step 2: Verify RED**

    uv run pytest -q tests/test_assemblyai.py -k word tests/test_transcription.py

Expected: failure because word evidence and source_utterance_id are absent.

- [ ] **Step 3: Implement strict ephemeral word parsing**

Add:

    class TranscriptWord(BaseModel):
        text: str = Field(min_length=1, repr=False)
        start_seconds: float = Field(ge=0, allow_inf_nan=False)
        end_seconds: float = Field(ge=0, allow_inf_nan=False)
        diarization_label: str = Field(min_length=1)
        confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

        @model_validator(mode="after")
        def ordered_interval(self) -> "TranscriptWord":
            if self.end_seconds <= self.start_seconds:
                raise ValueError("Intervallo parola non valido")
            return self

Extend TranscriptSegment with source_utterance_id: str = "" and words: list[TranscriptWord] using a default factory and repr=False. In AssemblyAI parsing, number utterances from one, namespace IDs as assembly-uNNNNNN, validate every word against its utterance and label, and preserve provider order. Never synthesize words from text. The OpenAI fallback keeps words empty; Task 4 rejects that path for conforming reports.

- [ ] **Step 4: Prove words never enter AI payloads**

Serialize each window segment using:

    segment.model_dump(mode="json", exclude={"words"})

Use sentinel PRIVATE-WORD-EVIDENCE and assert it is absent from window and repair payloads. Preserve source_utterance_id. If bounding splits a segment, every piece keeps the same source ID; original words remain in memory only.

- [ ] **Step 5: Run focused regressions and commit**

    uv run pytest -q tests/test_assemblyai.py tests/test_transcription.py tests/test_analysis_chunks.py tests/test_analysis.py
    git add app/transcription.py app/assemblyai.py app/analysis_chunks.py tests/test_assemblyai.py tests/test_transcription.py tests/test_analysis_chunks.py
    git commit -m "feat: retain ephemeral word timing evidence"

Expected: all tests pass; AssemblyAI cancellation and deletion remain green.

---

### Task 2: Detect speech silence in the existing FFmpeg pass

**Files:**
- Modify: app/media.py
- Modify: tests/test_media.py
- Modify: tests/test_pipeline.py

**Interfaces:**
- Produces: SilenceInterval(start_seconds, end_seconds).
- Produces: MediaArtifacts.silence_intervals.
- Produces: private _SilenceEvents.consume(line) and finish(duration_seconds).
- Preserves: one source read and no audio files in extract_visual.

- [ ] **Step 1: Write failing silence parser tests**

    def test_silence_events_pair_and_close_end_of_file():
        events = media._SilenceEvents()
        events.consume("[silencedetect @ 0x1] silence_start: 2.25")
        events.consume("[silencedetect @ 0x1] silence_end: 4.75 | silence_duration: 2.5")
        events.consume("[silencedetect @ 0x1] silence_start: 9")
        assert events.finish(10) == [
            media.SilenceInterval(2.25, 4.75),
            media.SilenceInterval(9, 10),
        ]

Add end-without-start, duplicate start, reversed pair, non-finite values, overlap, beyond-duration, and malformed silence diagnostic tests. Only fixed MediaError text may escape.

- [ ] **Step 2: Verify RED**

    uv run pytest -q tests/test_media.py -k silence

Expected: failure because silence parsing does not exist.

- [ ] **Step 3: Implement collection and FFmpeg wiring**

Add a frozen SilenceInterval with finite, nonnegative, ordered validation. Add silence_intervals to MediaArtifacts with a default factory. Replace fast-path audio packet copy with:

    command.extend([
        "-map", "0:a:0", "-vn",
        "-af", "silencedetect=noise=-45dB:d=0.15",
        "-f", "null", os.devnull,
    ])

Parse only exact silencedetect events and finalize against verified FFmpeg progress duration. Return compact intervals and no audio chunks.

- [ ] **Step 4: Prove one-pass and privacy behavior**

Update visual-only extraction to assert one source URL, exact filter, no copy codec, no m4a/audio.csv, and expected compact intervals. Add real synthetic tone, 2.4-second silence, tone, 0.8-second silence, tone; assert intervals within 150 ms and no retained audio. Test cancellation and safe malformed diagnostics.

- [ ] **Step 5: Run regressions and commit**

    uv run pytest -q tests/test_media.py tests/test_pipeline.py
    git add app/media.py tests/test_media.py tests/test_pipeline.py
    git commit -m "feat: detect speech pauses during media scan"

Expected: all tests pass on a host with FFmpeg. The same media suite must pass in the deploy image.

---

### Task 3: Align semantic interventions to audio-safe boundaries

**Files:**
- Create: app/boundaries.py
- Create: tests/test_boundaries.py
- Modify: app/models.py
- Modify: app/analysis_chunks.py
- Modify: tests/test_analysis_chunks.py
- Modify: tests/test_models_reporting.py

**Interfaces:**
- Produces: BoundaryEvidence persisted in AcademyReport.boundaries.
- Produces: SemanticIntervention internal handoff from AI grouping.
- Produces: BoundaryAlignment(interventions, boundaries).
- Produces: align_intervention_boundaries(duration_seconds, semantic_groups, silence_intervals).
- Produces: has_complete_boundary_evidence(report).

- [ ] **Step 1: Write failing model/completeness tests**

Use this exact valid shape:

    evidence = BoundaryEvidence(
        previous_intervention_id="i001",
        next_intervention_id="i002",
        boundary_seconds=14,
        words_before=["la", "governance", "si", "chiude", "qui"],
        words_after=["passiamo", "ora", "al", "tema", "fiscale"],
        pause_before=True,
        pause_after=True,
        rule="long_pause",
    )

Add boundaries: list[BoundaryEvidence] = Field(default_factory=list) to AcademyContent so AnalysisResult and AcademyReport inherit the same contract. Test strict integer seconds, zero-to-five non-empty words, distinct IDs, exact rule literal, and historical AcademyReport boundaries defaulting to empty. Test completeness against missing, duplicate, reordered, wrong-second, and nonconsecutive evidence.

- [ ] **Step 2: Write RED alignment tests**

Cover:
- last word 10.2, next word 13.0, measured silence 10.3 to 12.9: previous ends 11, generated pause 11 to 12, next starts 12;
- short silence 20.2 to 21.4: direct midpoint boundary 21;
- no silence and previous last word end 30.4: direct boundary 30;
- integer ties choose the earlier valid second;
- first segment starts 0 and last ends at rounded duration;
- all neighbors are adjacent and boundary count equals interventions minus one.

    uv run pytest -q tests/test_boundaries.py

Expected: import failure because app.boundaries is absent.

- [ ] **Step 3: Implement the pure aligner**

Create immutable handoffs:

    @dataclass(frozen=True)
    class SemanticIntervention:
        tipo: InterventionKind
        relatori: tuple[str, ...]
        titolo: str
        sintesi: str
        punti_chiave: tuple[str, ...]
        confidenza: float
        segments: tuple[TranscriptSegment, ...]

    @dataclass(frozen=True)
    class BoundaryAlignment:
        interventions: list[Intervention]
        boundaries: list[BoundaryEvidence]

Implement isolated helpers for first/last word, matching measured silence, whole-second selection, pause insertion, final IDs, and evidence. This module must not import or accept frames/slides. Long silence creates one pause segment only when at least one whole second remains after both margins. Reject wordless spoken groups, empty/reversed segments, nonadjacency, or incomplete evidence.

- [ ] **Step 4: Protect semantic completeness and moderator behavior**

Test same source_utterance_id pieces merging across adjacent windows; one continued example over three utterances remains one segment; moderator between speakers gets its own segment with two boundaries and the first before the moderator's first word. Test zero, one, four, five, and more than five words, verbatim punctuation, and pause flags.

- [ ] **Step 5: Refactor materialization**

Change the interface to:

    def materialize_interventions(
        duration_seconds: float,
        windows: Sequence[TranscriptWindow],
        analyses: Sequence[WindowAnalysis],
        speaker_names: Mapping[str, str],
        silence_intervals: Sequence[SilenceInterval],
    ) -> BoundaryAlignment:

Build SemanticIntervention values from complete selected segments, merge shared source utterances, then call the pure aligner. Remove the old timestamp rounding/gap-filler fallback.

- [ ] **Step 6: Run regressions and commit**

    uv run pytest -q tests/test_boundaries.py tests/test_analysis_chunks.py tests/test_models_reporting.py
    git add app/boundaries.py app/models.py app/analysis_chunks.py tests/test_boundaries.py tests/test_analysis_chunks.py tests/test_models_reporting.py
    git commit -m "feat: align intervention cuts to speech pauses"

---

### Task 4: Integrate fail-closed alignment into analysis and persistence

**Files:**
- Modify: app/analysis.py
- Modify: app/pipeline.py
- Modify: app/prompts.py
- Modify: app/logging_config.py
- Modify: tests/test_analysis.py
- Modify: tests/test_pipeline.py
- Modify: tests/test_jobs.py
- Modify: tests/test_end_to_end.py

**Interfaces:**
- Consumes: MediaArtifacts.silence_intervals, word-bearing TranscriptionResult, BoundaryAlignment.
- Produces: AnalysisResult.boundaries and persisted AcademyReport.boundaries.
- Produces: fixed pipeline code boundary with message Verifica audio dei confini non riuscita; riprova.

- [ ] **Step 1: Write failing integration tests**

Use a fast-path fixture with two word-bearing utterances and one measured silence. Assert Analyzer receives intervals, returns adjacent interventions/evidence, and Pipeline persists only compact evidence. Parameterize empty words, unavailable silence collection, incomplete semantic group, nonadjacent output, and pipeline without fast_transcriber. Require fixed user text and safe phase/code logs.

- [ ] **Step 2: Verify RED**

    uv run pytest -q tests/test_analysis.py tests/test_pipeline.py -k "boundary or silence or word"

Expected: failure because analysis and pipeline do not pass/persist evidence.

- [ ] **Step 3: Strengthen semantic prompts**

Add this literal requirement to WINDOW_PROMPT:

    Raggruppa tutte le utterance che completano la stessa frase, esempio,
    spiegazione, risposta o linea di ragionamento. Non creare mai un confine nel
    mezzo di questi elementi. Se interviene il moderatore, assegna le sue
    utterance a un segmento autonomo saluti, domande o cambio_relatore: non
    accodarle all'intervento precedente. I timestamp finali sono calcolati
    localmente dall'audio; non usare i cambi di slide per dividere gli interventi.

Test the prompt. Do not ask the model for silence positions or CONFINE items.

- [ ] **Step 4: Wire the analyzer and pipeline**

Add required keyword-only silence_intervals to analyze and analyze_fast. Use the new materializer and pass alignment.interventions and alignment.boundaries into AnalysisResult. Map missing/invalid boundary evidence to AnalysisError boundaries at stage boundary and then to the fixed PipelineError text. Preserve immediate deletion of transcript after AcademyReport construction.

- [ ] **Step 5: Prove persistence and privacy**

Round-trip a completed report through JobStore. Assert compact boundaries survive. Assert SQLite bytes, report JSON, logs, exceptions, and output omit sentinel full transcript text, word arrays, audio paths, FFmpeg diagnostics, and provider bodies.

- [ ] **Step 6: Run regressions and commit**

    uv run pytest -q tests/test_analysis.py tests/test_pipeline.py tests/test_jobs.py tests/test_end_to_end.py
    git add app/analysis.py app/pipeline.py app/prompts.py app/logging_config.py tests/test_analysis.py tests/test_pipeline.py tests/test_jobs.py tests/test_end_to_end.py
    git commit -m "feat: persist verified audio boundary evidence"

Expected: all offline tests pass and no live provider is called.

---

### Task 5: Export CONFINE warnings and gate historical JSON

**Files:**
- Modify: app/intermediate_models.py
- Modify: app/intermediate_report.py
- Modify: app/web.py
- Modify: app/templates/job.html
- Modify: app/templates/home.html
- Modify: tests/test_intermediate_models.py
- Modify: tests/test_intermediate_report.py
- Modify: tests/test_web.py
- Modify: tests/job_ui.cjs

**Interfaces:**
- Adds: CONFINE to VerificationCode as warning-only.
- Produces: build_boundary_verifications(report, stored_to_exported_ids).
- Consumes: complete AcademyReport.boundaries.
- Produces: fixed HTTP 409 for incomplete historical JSON.

- [ ] **Step 1: Write failing model/message tests**

Build four segments and three boundary records. Require:

    checks = [item for item in payload["verifiche_richieste"] if item["codice"] == "CONFINE"]
    assert [item["intervento"] for item in checks] == ["v1-i002", "v1-i003", "v1-i004"]
    assert [item["campo"] for item in checks] == ["inizio", "inizio", "inizio"]
    assert "Confine 0:00:11; termina v1-i001." in checks[0]["messaggio"]
    assert "Prima: «la governance si chiude qui»." in checks[0]["messaggio"]
    assert "Dopo: «passiamo ora al tema fiscale»." in checks[0]["messaggio"]

Add fewer-than-five, zero-word, before-pause, after-pause, and both-pause cases. CONFINE with livello critico must fail validation. Distinct pairs may not deduplicate.

- [ ] **Step 2: Verify RED**

    uv run pytest -q tests/test_intermediate_models.py tests/test_intermediate_report.py -k confine

Expected: CONFINE is rejected or absent.

- [ ] **Step 3: Implement ID mapping and exact rendering**

Share this chronological mapping:

    stored_to_exported_ids = {
        stored.id: f"v1-i{index:03d}"
        for index, stored in enumerate(ordered_interventions, start=1)
    }

Validate evidence completeness before build. Emit video v1, intervention next exported ID, campo inizio, exact Italian message, before/after clauses, and pause markers. Append expected CONFINE warnings without changing stato.

- [ ] **Step 4: Gate historical reports and update UI**

Before inventory or network work:

    if not has_complete_boundary_evidence(job.report):
        raise HTTPException(
            409,
            "Rianalisi necessaria per verificare i confini sull’audio",
        )

Test zero inventory/Bunny/AssemblyAI/OpenAI/material-checker calls. Markdown/TXT stay 200. Historical job/home cards show Rianalisi necessaria and link to the catalog flow; conforming reports retain Download JSON v1.1.

- [ ] **Step 5: Run regressions and commit**

    uv run pytest -q tests/test_intermediate_models.py tests/test_intermediate_report.py tests/test_web.py tests/test_security.py
    node --test tests/job_ui.cjs tests/catalog_ui.cjs
    git add app/intermediate_models.py app/intermediate_report.py app/web.py app/templates/job.html app/templates/home.html tests/test_intermediate_models.py tests/test_intermediate_report.py tests/test_web.py tests/job_ui.cjs
    git commit -m "feat: export auditable intervention boundaries"

Expected: new JSON has exactly N minus one warnings and historical JSON returns fixed 409.

---

### Task 6: Verify, publish, and reanalyze Governance

**Files:**
- Verify: all application and test files from Tasks 1 through 5.
- Modify: production/test files only when a reproduced defect has a failing test.

**Interfaces:**
- Consumes: completed boundary-aware pipeline and Railway configuration.
- Produces: deployed conforming Governance report and authenticated v1 JSON.

- [ ] **Step 1: Run focused contract suites**

    uv run pytest -q tests/test_assemblyai.py tests/test_transcription.py tests/test_media.py tests/test_boundaries.py tests/test_analysis_chunks.py tests/test_analysis.py tests/test_pipeline.py tests/test_models_reporting.py tests/test_intermediate_models.py tests/test_intermediate_report.py tests/test_jobs.py tests/test_web.py tests/test_security.py

Expected: zero failures. FFmpeg integration is mandatory before publishing.

- [ ] **Step 2: Run full portable/UI/build checks**

    unset RUN_LIVE_BUNNY RUN_LIVE_SYNTHETIC_ANALYSIS
    uv run pytest -q --ignore=tests/test_assemblyai.py
    node --test tests/job_ui.cjs tests/batch_ui.cjs tests/catalog_ui.cjs tests/inventory_ui.cjs tests/course_review_ui.cjs
    uv build
    git diff --check
    git status --porcelain=v1

Expected: no failures, clean tracked state, and no generated lockfile.

- [ ] **Step 3: Complete mandatory whole-branch review**

Review every plan commit against the spec. Fix Critical/Important findings with failing regression tests and re-review. Confirm privacy, cancellation, one source read, semantic grouping, boundary arithmetic, historical migration, and exact warning cardinality.

- [ ] **Step 4: Integrate, push, and deploy**

After explicit integration approval and a green merged suite:

    git push origin main
    railway up --detach --service bunny-video-report --environment production
    railway deployment list --service bunny-video-report --environment production --limit 2 --json

Wait for SUCCESS while the previous healthy instance remains active.

- [ ] **Step 5: Prove historical gating**

Authenticate with APP_PASSWORD read in-process without printing it. For historical Governance job ebd8ed92-9596-4b0b-b6d9-40af6e857ddc, require Markdown/TXT 200, JSON 409 fixed reanalysis text, and unchanged stored timestamp.

- [ ] **Step 6: Start exactly one authorized Governance reanalysis**

Create one new job for Bunny GUID 7f254c4d-fe34-4fd3-a4cf-cda4f447e438 through authenticated catalog confirmation. Record the ID and poll only it with bounded backoff. Never create an automatic second paid job after failure.

- [ ] **Step 7: Validate real output**

Download new JSON twice and require byte stability plus unchanged job timestamp. Validate exact v1 envelope, duration 5789, five expected speakers/four slugs, contiguous whole-second timeline, exactly len(interventi)-1 CONFINE warnings, correct previous/next IDs and seconds, words or pause markers, all segment types, no slide-derived cuts without audio support, and no transcript/audio/provider diagnostic leakage. JSON downloads must not spend transcription or AI credit.

- [ ] **Step 8: Check final state**

    git status --porcelain=v1
    git rev-parse HEAD
    git ls-remote origin refs/heads/main
    railway deployment list --service bunny-video-report --environment production --limit 1 --json

Expected: clean local/remote main equality and deployed SUCCESS.
