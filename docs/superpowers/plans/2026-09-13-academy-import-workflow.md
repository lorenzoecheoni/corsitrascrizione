# Academy Import Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the synchronized, reviewable course-report workflow that emits one factual intermediate JSON and one validated Academy v1 JSON per course.

**Architecture:** Preserve the existing per-video worker and SQLite job store, but extend its report with exact intervention timelines. Add focused inventory, course-workflow, and Academy-contract modules around it; a course orchestrates one or more existing video jobs, then review and generation operate only on persisted structured JSON.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLite, httpx, OpenAI Responses structured output, Jinja2, browser JavaScript, Google Sheets CSV/API, pytest, Node test runner.

**Spec:** `docs/superpowers/specs/2026-09-13-academy-import-workflow-design.md`

## Global Constraints

- One persisted report and final file per course; a course may contain multiple ordered Bunny videos.
- Never persist source media, audio, complete transcripts, slide frames, or provider diagnostics.
- Intermediate interventions cover every second of every video without gaps or overlaps.
- Unknown people are omitted and surfaced as critical verification items; never emit `Relatore N` in the intermediate contract.
- Google Sheet row position is the primary inventory order.
- Google writes require explicit per-row confirmation and existing session/CSRF protection.
- Academy JSON generation is blocked until the intermediate report is confirmed and has no critical verification item.
- Price bands are EUR 97/147/197/247 for up to 1/2/3/4 hours of included lesson time; credits are omitted.
- Existing saved reports and the direct Bunny catalog remain usable.

---

### Task 1: Define strict intermediate and Academy contracts

**Files:**
- Create: `app/course_models.py`
- Modify: `app/models.py`
- Create: `tests/test_course_models.py`

**Interfaces:**
- Produces: `format_hms(seconds: float) -> str`, `parse_hms(value: str) -> int`, `Intervention`, `IntermediateCourseReport`, `AcademyImport`, `validate_academy_import(report, source) -> list[str]`, and `academy_price(included_seconds: int) -> Decimal`.
- Consumes: existing `ReportModel`, `Confidence`, `SlideChange`, and `SpeakerProfile`.

- [ ] **Step 1: Write failing contract tests**

Add tests that prove exact `h:mm:ss` parsing, numeric confidence bounds, the six intervention kinds, multi-speaker arrays, three-to-seven key points on substantive interventions, complete per-video coverage, omission of generic speakers, price boundary behavior, Academy video/speaker references, lesson bounds, module/quiz cardinality, allowed area values, and hero uniqueness.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `.venv/bin/pytest -q tests/test_course_models.py`

Expected: collection fails because `app.course_models` and `Intervention` do not exist.

- [ ] **Step 3: Implement the contracts and pure validators**

Use Pydantic models with `extra="forbid"`. Store analysis timestamps as bounded numeric seconds in `Intervention`; serialize intermediate timestamps through explicit conversion into `h:mm:ss`. Keep defaults on the new `AcademyReport.interventions` field so historical report JSON remains readable.

- [ ] **Step 4: Run model and legacy report tests**

Run: `.venv/bin/pytest -q tests/test_course_models.py tests/test_models_reporting.py tests/test_jobs.py`

Expected: all pass, including loading `tests/fixtures/report.json` without interventions.

- [ ] **Step 5: Commit the contract layer**

```bash
git add app/course_models.py app/models.py tests/test_course_models.py
git commit -m "feat: define academy course report contracts"
```

### Task 2: Produce exact intervention timelines during video analysis

**Files:**
- Modify: `app/analysis_chunks.py`
- Modify: `app/analysis.py`
- Modify: `app/prompts.py`
- Modify: `app/pipeline.py`
- Modify: `app/reporting.py`
- Modify: `tests/test_analysis_chunks.py`
- Modify: `tests/test_analysis.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_models_reporting.py`

**Interfaces:**
- Consumes: `Intervention` from Task 1 and existing `TranscriptWindow`/`TranscriptSegment`.
- Produces: `WindowInterventionDraft`, `materialize_interventions(duration_seconds: float, windows: Sequence[TranscriptWindow], analyses: Sequence[WindowAnalysis], speaker_names: Mapping[str, str]) -> list[Intervention]`, and `AnalysisResult.interventions` covering the complete video duration.

- [ ] **Step 1: Write failing window-partition tests**

Cover a single speaker, a speaker handoff, joint speakers, questions, logistics,
silence between transcript segments, pre-roll/post-roll, invalid AI partitions,
and exact one-second rounding. Assert every transcript segment index appears in
one AI group and the materialized result starts at zero, ends at duration, and
has adjacent boundaries.

- [ ] **Step 2: Verify focused failures**

Run: `.venv/bin/pytest -q tests/test_analysis_chunks.py -k intervention`

Expected: failures for missing draft/materialization types.

- [ ] **Step 3: Extend window structured output and prompts**

Add stable local segment indexes to window payloads. Ask the model to return a
partition of those indexes plus `tipo`, diarization labels, factual title,
summary, key points, and numeric confidence. Validate partitions before
materialization; retry through the existing safe repair boundary.

- [ ] **Step 4: Build a whole-video timeline locally**

Convert index groups into exact numeric ranges. Use deterministic adjacent
boundaries, zero for the first start, and metadata duration for the final end.
Resolve supported names from explicit window evidence and provider mappings;
omit generic or unsupported names and add structured uncertainty messages.

- [ ] **Step 5: Use complete transcript windows in both transcription modes**

Keep AssemblyAI as the fast transcription path, but analyze its complete
globally diarized segments through bounded windows instead of the existing
sample-only report payload. Preserve the bounded final consolidation for the
global synopsis and speaker list. Do not expose or store window transcript text.

- [ ] **Step 6: Update text rendering without adding transcript output**

Render intervention ranges, type, speakers, title, summary, and key points in
Markdown/TXT. Keep complete transcript text absent.

- [ ] **Step 7: Run analysis and pipeline regression tests**

Run: `.venv/bin/pytest -q tests/test_analysis_chunks.py tests/test_analysis.py tests/test_pipeline.py tests/test_models_reporting.py`

Expected: all pass; cancellation and temporary-file cleanup remain green.

- [ ] **Step 8: Commit intervention analysis**

```bash
git add app/analysis_chunks.py app/analysis.py app/prompts.py app/pipeline.py app/reporting.py tests/test_analysis_chunks.py tests/test_analysis.py tests/test_pipeline.py tests/test_models_reporting.py
git commit -m "feat: detect exact course interventions"
```

### Task 3: Synchronize and match the Google inventory

**Files:**
- Create: `app/inventory.py`
- Create: `app/course_store.py`
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Create: `tests/test_inventory.py`
- Create: `tests/test_course_store.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_deployment.py`

**Interfaces:**
- Produces: `InventoryClient.fetch() -> list[InventoryCourse]`, `InventoryClient.update_link(course, url)`, `propose_matches(courses, bunny_videos)`, and `CourseStore` persistence methods.
- Consumes: public CSV exports for configured sheet GIDs, optional service-account JSON for writes, existing Bunny catalog models, and the existing SQLite database path.

- [ ] **Step 1: Write failing CSV and matching tests**

Fixture the three real header variants. Verify row order, blank rows, multiline
speaker cells, explicit GUID extraction, literal `bunny`, empty links, accent/
punctuation/prefix normalization, unique high-confidence proposals, runner-up
safety margins, and ambiguous non-matches.

- [ ] **Step 2: Verify inventory failures**

Run: `.venv/bin/pytest -q tests/test_inventory.py`

Expected: missing inventory module.

- [ ] **Step 3: Implement read synchronization and optional confirmed writes**

Read each configured GID through the public CSV export with bounded timeouts and
safe error categories. Add optional Google service-account authentication for
the Sheets values API so only an explicit Link-cell update can write. Never log
credential JSON, tokens, row contents, or upstream bodies.

- [ ] **Step 4: Write failing persistence/restart tests**

Verify sync upserts source fields, keeps source sheet/order, preserves confirmed
video associations, never replaces disappeared confirmations by fuzzy matches,
and leaves the last snapshot intact on fetch failure.

- [ ] **Step 5: Implement focused course/inventory tables**

Use a separate `CourseStore` connection to the same SQLite path with WAL,
foreign keys, and explicit schema initialization. Store inventory rows,
proposals, ordered confirmed video GUIDs, analysis job IDs, intermediate JSON,
confirmation state, Academy JSON, contract version, and prompt version.

- [ ] **Step 6: Wire configuration and services**

Add default spreadsheet ID `1yw2K-1cH5goQER1tP3KItvluXMv7N_H5RoratcWZoZo`,
three stable tab/GID mappings, optional service-account JSON, conservative
matching threshold/margin, and HTTP client lifecycle. Invalid secrets must fail
as safe configuration errors only when a write is attempted.

- [ ] **Step 7: Run inventory/store/deployment tests**

Run: `.venv/bin/pytest -q tests/test_inventory.py tests/test_course_store.py tests/test_deployment.py`

Expected: all pass.

- [ ] **Step 8: Commit synchronized inventory**

```bash
git add app/inventory.py app/course_store.py app/config.py app/main.py pyproject.toml .env.example tests/test_inventory.py tests/test_course_store.py tests/conftest.py tests/test_deployment.py
git commit -m "feat: synchronize academy course inventory"
```

### Task 4: Orchestrate one course across one or more video jobs

**Files:**
- Create: `app/courses.py`
- Modify: `app/jobs.py`
- Modify: `app/web.py`
- Modify: `tests/test_jobs.py`
- Create: `tests/test_courses.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces: `build_intermediate_report(course, ordered_jobs) -> IntermediateCourseReport`, course analysis preview/start/status routes, and stable association between a course run and its jobs.
- Consumes: confirmed inventory associations, existing job runner, extended `AcademyReport.interventions`, and Task 1 models.

- [ ] **Step 1: Write failing course-assembly tests**

Verify one and multiple video reports, stable `v1`/`v2` keys, ordered videos,
global synopsis, deduplicated supported speakers, slide conversion, generic-name
omission, structured critical verifications, and no transcript/media fields.

- [ ] **Step 2: Verify course failures**

Run: `.venv/bin/pytest -q tests/test_courses.py`

Expected: missing course assembly module.

- [ ] **Step 3: Implement assembly and course-run persistence**

Create one batch of existing jobs from the confirmed video list. Store job IDs
in course/video order. Once all jobs complete, assemble and persist exactly one
intermediate course report. A failed/cancelled child leaves the course retryable
without discarding successful saved child reports.

- [ ] **Step 4: Add signed analysis preview/start routes**

Show ordered videos, total duration, and provider cost estimate. Bind the signed
confirmation to the course ID and current ordered GUID list, consume it once,
then submit jobs. Reject stale associations or a changed inventory snapshot.

- [ ] **Step 5: Add safe course status JSON**

Return course state and child progress without source URLs, report bodies,
credentials, or provider errors. Reuse automatic polling behavior.

- [ ] **Step 6: Run course orchestration regressions**

Run: `.venv/bin/pytest -q tests/test_courses.py tests/test_jobs.py tests/test_web.py tests/test_selection.py`

Expected: all pass.

- [ ] **Step 7: Commit course orchestration**

```bash
git add app/courses.py app/jobs.py app/web.py tests/test_courses.py tests/test_jobs.py tests/test_web.py
git commit -m "feat: analyze ordered videos as one course"
```

### Task 5: Add the inventory and review interface

**Files:**
- Create: `app/templates/inventory.html`
- Create: `app/templates/course.html`
- Create: `app/templates/course_review.html`
- Create: `app/static/inventory.js`
- Create: `app/static/course_review.js`
- Modify: `app/templates/base.html`
- Modify: `app/templates/home.html`
- Modify: `app/static/app.css`
- Modify: `app/web.py`
- Create: `tests/inventory_ui.cjs`
- Create: `tests/course_review_ui.cjs`
- Modify: `tests/test_web.py`
- Modify: `tests/test_security.py`

**Interfaces:**
- Produces: inventory sync/match pages, authenticated match/link mutations, editable report API, confirmation action, and intermediate JSON download.
- Consumes: `CourseStore`, `InventoryClient`, `IntermediateCourseReport`, Bunny metadata, existing sessions and CSRF middleware.

- [ ] **Step 1: Write failing HTTP and browser tests**

Cover three workflow tabs, Sheet ordering, status filters, explicit sync,
proposal confirmation, write-link confirmation, no silent write, video reorder,
timestamp player links, literal rendering, intervention edits, client feedback,
server validation, critical-warning block, CSRF rejection, and JSON download.

- [ ] **Step 2: Verify UI failures**

Run: `.venv/bin/pytest -q tests/test_web.py tests/test_security.py -k 'inventory or course or review' && node --test tests/inventory_ui.cjs tests/course_review_ui.cjs`

Expected: missing routes/templates/scripts.

- [ ] **Step 3: Implement inventory workflow pages**

Make `Inventario corsi` the primary entry while retaining `Catalogo Bunny` as a
secondary view. Show source tab/order, expected speakers, proposed/confirmed
videos, status, and next action. Synchronization is explicit and preserves the
previous snapshot on errors.

- [ ] **Step 4: Implement report review API and page**

Render editable fields as literal text. Send the complete Pydantic-shaped JSON
body with the CSRF header, validate and save atomically, and show structured
field errors. Timestamp links target the configured Bunny player and never
download media.

- [ ] **Step 5: Implement explicit confirmation and download**

Server-side confirmation requires a valid full-coverage report and zero
critical items. Download `report-intermedio-<course-id>.json` with UTF-8 JSON,
`nosniff`, no-store, and attachment headers.

- [ ] **Step 6: Run UI and security regression tests**

Run: `.venv/bin/pytest -q tests/test_web.py tests/test_security.py tests/test_auth.py && node --test tests/catalog_ui.cjs tests/job_ui.cjs tests/batch_ui.cjs tests/inventory_ui.cjs tests/course_review_ui.cjs`

Expected: all pass.

- [ ] **Step 7: Commit the review workflow**

```bash
git add app/templates app/static app/web.py tests/test_web.py tests/test_security.py tests/inventory_ui.cjs tests/course_review_ui.cjs
git commit -m "feat: review academy course reports"
```

### Task 6: Generate and validate the Academy import JSON

**Files:**
- Create: `app/academy.py`
- Create: `app/academy_prompt.py`
- Modify: `app/course_store.py`
- Modify: `app/main.py`
- Modify: `app/web.py`
- Modify: `app/templates/course_review.html`
- Create: `tests/test_academy.py`
- Modify: `tests/test_course_store.py`
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces: `AcademyGenerator.generate(source, cancellation_event=None) -> AcademyImport`, deterministic validation/one-repair behavior, generation route, and `import-academy.json` download.
- Consumes: confirmed `IntermediateCourseReport`, existing OpenAI client/rate gate patterns, Academy v1 Pydantic contract, allowed areas, and price calculator.

- [ ] **Step 1: Write failing generator tests**

Stub structured Responses calls. Verify prompt separation from untrusted report
data, `store=False`, fixed model configuration, source-only speakers/GUIDs/
durations, lesson rules, slide evidence use, one repair request containing only
JSON plus machine errors, and safe terminal failure.

- [ ] **Step 2: Verify generator failures**

Run: `.venv/bin/pytest -q tests/test_academy.py`

Expected: missing Academy generator.

- [ ] **Step 3: Implement the versioned editorial generator**

Use the exact approved prompt in a dedicated constant with `CONTRACT_VERSION=1`
and `PROMPT_VERSION=1`. Parse into `AcademyImport`, deterministically overwrite
price from included seconds, omit credits, and validate against the confirmed
source before saving.

- [ ] **Step 4: Add generation persistence and routes**

Permit generation only from `stato: confermato`. Save Academy JSON and versions
atomically. A failure keeps the confirmed intermediate file. Add retry and
download controls, never returning upstream diagnostics.

- [ ] **Step 5: Run generator/store/web tests**

Run: `.venv/bin/pytest -q tests/test_academy.py tests/test_course_store.py tests/test_web.py tests/test_security.py`

Expected: all pass.

- [ ] **Step 6: Commit Academy generation**

```bash
git add app/academy.py app/academy_prompt.py app/course_store.py app/main.py app/web.py app/templates/course_review.html tests/test_academy.py tests/test_course_store.py tests/test_web.py
git commit -m "feat: generate validated academy imports"
```

### Task 7: Complete compatibility, documentation, and production rollout

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `tests/test_end_to_end.py`
- Modify: `tests/test_revision_security.py`
- Modify: `tests/test_smoke_deadline.py`

**Interfaces:**
- Consumes: the complete course workflow.
- Produces: documented configuration and verified production deployment.

- [ ] **Step 1: Add a no-network end-to-end course fixture**

Exercise sync fixture, explicit match, signed multi-video analysis, job
completion, intermediate assembly, correction, confirmation, Academy generation,
both downloads, SQLite restart, and absence of transcript/media paths.

- [ ] **Step 2: Run the complete offline verification suite**

Run: `.venv/bin/pytest -q --ignore=tests/test_media.py -k 'not real_ffmpeg'`

Run: `.venv/bin/pytest -q tests/test_media.py -k 'not extract_single_input_audio_scene_timestamps_and_byte_estimate and not invalid_source_error_does_not_expose_url_or_upstream_diagnostics and not oversized_chunk_is_split_locally_without_a_second_source_read and not hls_timestamps_are_relative_to_video_start_and_segments_read_once and not long_audio_segments_keep_offsets_and_stay_within_transcription_target'`

Run: `node --test tests/*.cjs`

Expected: all selected tests pass; five local FFmpeg-dependent tests remain
deselected because FFmpeg is supplied by the Railway image.

- [ ] **Step 3: Update operator documentation**

Document Sheet sync, optional service-account JSON, per-row write confirmation,
matching thresholds, course review, contract/prompt versions, pricing, downloads,
data retention, legacy reports, and rollback behavior.

- [ ] **Step 4: Commit the completed workflow**

```bash
git add README.md .env.example tests/test_end_to_end.py tests/test_revision_security.py tests/test_smoke_deadline.py
git commit -m "docs: document academy import workflow"
```

- [ ] **Step 5: Verify the branch before publishing**

Run: `git diff --check && git status --short --branch && git log --oneline -10`

Expected: clean working tree on `main`, commits in task order, no unstaged files.

- [ ] **Step 6: Publish and deploy**

Push `main`, configure only the new Google variables required for the selected
read/write mode, deploy to the existing Railway service and persistent volume,
then wait for `SUCCESS`.

- [ ] **Step 7: Perform no-cost production verification**

Check `/healthz`, authenticated inventory sync, course counts/order, proposed
matches, catalog fallback, saved report visibility, and downloads for fixture-
free existing data. Do not start a paid video analysis without a separate
explicit user instruction.
