# Persistent Reports, Compact Catalog, and Live Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist textual reports across Railway restarts, add a compact catalog list view, and update batch progress automatically.

**Architecture:** Replace the volatile `JobStore` internals with transactional SQLite while keeping its current domain interface. Add narrowly scoped authenticated routes for batch status and completed-report deletion, then enhance the existing server-rendered pages with dependency-free JavaScript for polling and view switching. Production stores the database on a Railway volume mounted at `/data`; media and transcripts remain ephemeral.

**Tech Stack:** Python 3.12, standard-library `sqlite3`, FastAPI, Pydantic v2, Jinja2, browser JavaScript, CSS, pytest, Node `vm` tests, Railway.

**Spec:** `docs/superpowers/specs/2026-09-13-persistent-reports-list-autorefresh-design.md`

## Global Constraints

- Retain only canonical Bunny source metadata and final structured report JSON; never persist video, audio, slide frames, transcripts, credentials, cookies, or prompt bodies.
- Keep reports without automatic expiry until an authenticated team member explicitly deletes them.
- Use exactly one application replica, one Uvicorn process, and one report worker.
- Keep all existing authenticated route URLs and report download URLs stable.
- Do not run a paid real-video analysis during implementation or deployment verification.
- Every behavior change follows a witnessed red-green TDD cycle.

---

### Task 1: SQLite-backed job and batch store

**Files:**
- Modify: `tests/test_jobs.py`
- Modify: `app/jobs.py`

**Interfaces:**
- Consumes: existing `JobRecord`, `BatchRecord`, `JobState`, and `AcademyReport` models.
- Produces: `JobStore(database_path: str | Path = ":memory:")`, existing `create`, `create_batch`, `get`, `get_batch`, `list_recent`, and `update` behavior, plus `list_completed() -> list[JobRecord]`, `list_uncompleted(limit: int = 20) -> list[JobRecord]`, and `delete_completed(job_id: UUID) -> None`.

- [ ] **Step 1: Write failing persistence and recovery tests**

Add tests that use `tmp_path / "reports.sqlite3"`. The first store creates and completes a job with the fixture report and a two-job batch. A second store opened on the same path must recover the completed report and ordered batch. A separately persisted queued and processing job must reopen as failed with preserved progress and the fixed safe restart error. Mutating loaded nested models must not alter later reads. Deleting a completed report removes its batch membership while preserving an empty batch; deleting any non-completed state is rejected. Corrupt report JSON is returned as an unavailable report with the fixed safe error `Report salvato non leggibile`, does not expose stored content in logs, and does not prevent valid reports from being listed.

```python
def test_sqlite_store_persists_completed_report_and_batch(tmp_path, report):
    path = tmp_path / "reports.sqlite3"
    first = JobStore(path)
    batch, jobs = first.create_batch([("first", "Primo"), ("second", "Secondo")])
    first.update(jobs[0].id, state=JobState.PROCESSING, progress=75)
    first.update(jobs[0].id, state=JobState.COMPLETED, report=report)

    reopened = JobStore(path)

    assert reopened.get(jobs[0].id).report == report
    assert reopened.get_batch(batch.id).job_ids == batch.job_ids


def test_sqlite_store_marks_interrupted_jobs_failed_on_reopen(tmp_path):
    path = tmp_path / "reports.sqlite3"
    store = JobStore(path)
    queued = store.create("queued")
    processing = store.create("processing")
    store.update(processing.id, state=JobState.PROCESSING, progress=50)

    reopened = JobStore(path)

    assert reopened.get(queued.id).state == JobState.FAILED
    assert reopened.get(processing.id).progress == 50
    assert reopened.get(processing.id).error == "Elaborazione interrotta dal riavvio; avvia nuovamente l’analisi"
```

- [ ] **Step 2: Run the focused tests and witness the expected failure**

Run: `.venv/bin/pytest tests/test_jobs.py -q`

Expected: the persistence test fails because reopening a store currently creates an empty in-memory dictionary, and the new constructor/list/delete methods do not yet exist.

- [ ] **Step 3: Implement transactional SQLite storage**

Create the three tables during initialization, enable foreign keys and a 5-second busy timeout, and serialize Pydantic values with `model_dump_json()` / `model_validate_json()`. Keep the existing lock and transition validation. Use parameterized SQL exclusively and commit mutations atomically. On file-backed store startup, update `queued` and `processing` records to `failed` with the safe restart message. Preserve `source_url` internally while relying on the Pydantic exclusion for public dumps.

`list_completed()` selects every completed record newest first. `list_uncompleted()` selects only queued, processing, failed, and cancelled records newest first. `delete_completed()` verifies the row exists and is completed, deletes it in a transaction, and raises `KeyError` for missing IDs or `ValueError` for non-completed states. Foreign-key cascade removes batch membership. A row decoder catches only report validation/JSON failures, logs identifiers and a fixed error code without the stored body, and returns the completed record with `report=None` and `error="Report salvato non leggibile"`; list operations continue decoding subsequent rows.

- [ ] **Step 4: Run job-store tests and refactor only while green**

Run: `.venv/bin/pytest tests/test_jobs.py -q`

Expected: all tests pass, including existing concurrent runner and state-transition coverage.

- [ ] **Step 5: Commit the storage slice**

```bash
git add app/jobs.py tests/test_jobs.py
git commit -m "feat: persist report jobs in sqlite"
```

### Task 2: Configure persistent storage safely

**Files:**
- Modify: `tests/test_deployment.py`
- Modify: `tests/test_end_to_end.py`
- Modify: `app/config.py`
- Modify: `app/main.py`
- Modify: `.env.example`
- Modify: `.gitignore`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `JobStore(database_path)` from Task 1.
- Produces: `Settings.database_path: str`, default `bunny-video-report.sqlite3`; `build_services()` supplies it to `JobStore`.

- [ ] **Step 1: Write failing configuration and restart tests**

Update the end-to-end restart assertion so a completed job created with an isolated temporary `DATABASE_PATH` remains accessible from a newly created app after re-login. Add a configuration test that the default path is `bunny-video-report.sqlite3` and a test that an absent parent directory causes safe application construction failure without leaking the configured path in the exception message.

- [ ] **Step 2: Run the focused tests and witness the expected failure**

Run: `.venv/bin/pytest tests/test_deployment.py tests/test_end_to_end.py -q -k 'database or restart'`

Expected: failures because `Settings` and `build_services` do not yet pass a database path and the old restart test expects 404.

- [ ] **Step 3: Wire the database setting into application startup**

Add `database_path: str = "bunny-video-report.sqlite3"` to `Settings`, construct `JobStore(settings.database_path)`, list `DATABASE_PATH=` in `.env.example`, and ignore `*.sqlite3`, `*.sqlite3-shm`, and `*.sqlite3-wal`. Convert storage initialization errors at `create_app` into the existing safe `RuntimeError("Configurazione applicazione non valida; verificare le variabili richieste")` boundary.

Set a unique `DATABASE_PATH` from the autouse `tests/conftest.py` fixture using its per-test `tmp_path`, so every application test is isolated while multiple app instances inside the same test deliberately share the file. Tests explicitly checking the in-memory store may continue to instantiate `JobStore()`.

- [ ] **Step 4: Run focused configuration and restart tests**

Run: `.venv/bin/pytest tests/test_deployment.py tests/test_end_to_end.py -q`

Expected: all tests pass and the restart flow reopens the completed report.

- [ ] **Step 5: Commit configuration changes**

```bash
git add .env.example .gitignore app/config.py app/main.py tests/conftest.py tests/test_deployment.py tests/test_end_to_end.py
git commit -m "feat: configure persistent report database"
```

### Task 3: Saved-report archive, batch status API, and safe deletion

**Files:**
- Modify: `tests/test_web.py`
- Modify: `app/web.py`
- Modify: `app/templates/home.html`
- Modify: `app/templates/job.html`
- Modify: `app/static/app.css`

**Interfaces:**
- Consumes: `JobStore.list_completed()` and `JobStore.delete_completed()` from Task 1.
- Produces: `GET /api/batches/{batch_id}` safe status payload and CSRF-protected `POST /jobs/{job_id}/delete`.

- [ ] **Step 1: Write failing route and rendering tests**

Create completed, processing, and failed records through the real store. Assert the dashboard lists the completed report under `Report salvati` with open/TXT/Markdown links, while the recent-work area contains only non-completed work. Assert the batch endpoint preserves batch order and excludes `source_url`, report text, and secrets. Assert completed deletion redirects to `/`, removes the job, and does not invoke Bunny; missing IDs return 404, non-completed states return 409, and missing/invalid CSRF returns 403.

- [ ] **Step 2: Run the focused tests and witness the expected failure**

Run: `.venv/bin/pytest tests/test_web.py -q -k 'archive or batch_status or delete_report'`

Expected: 404 for missing routes and absent archive markup.

- [ ] **Step 3: Implement archive rendering and routes**

Pass `saved_reports=store.list_completed()` and `recent_jobs=store.list_uncompleted()` from both success and Bunny-error dashboard branches. Render archive links and safe dates. Add the batch JSON route using each job's public `model_dump(mode="json", exclude={"report"})`. Add the delete POST route; map missing to 404, invalid state to 409, and redirect successful deletions to `/`.

Place the delete form only on completed report pages. Its button carries a `data-confirm-delete` message explaining that Bunny is untouched; submit without JavaScript remains possible because authentication and CSRF protect the operation.

- [ ] **Step 4: Run route tests and the existing security suite**

Run: `.venv/bin/pytest tests/test_web.py tests/test_auth.py tests/test_security.py tests/test_revision_security.py -q`

Expected: all pass with no secret or source URL exposed.

- [ ] **Step 5: Commit archive and API changes**

```bash
git add app/web.py app/templates/home.html app/templates/job.html app/static/app.css tests/test_web.py
git commit -m "feat: add persistent report archive"
```

### Task 4: Automatic batch progress

**Files:**
- Create: `tests/batch_ui.cjs`
- Create: `app/static/batch.js`
- Modify: `tests/test_web.py`
- Modify: `app/templates/batch.html`
- Modify: `app/static/app.css`

**Interfaces:**
- Consumes: `GET /api/batches/{batch_id}` from Task 3.
- Produces: batch DOM contract `data-batch-id`, `data-job-id`, `data-job-state`, `[data-job-message]`, `[data-job-label]`, and `[data-job-progress]`.

- [ ] **Step 1: Write a failing Node behavior test and HTML contract test**

Build a small DOM/fetch/timer harness like `tests/job_ui.cjs`. Verify the initial 3-second timer, in-place progress update, automatic retry after a network failure, explicit 401/404 messages, one timer after BFCache restoration, stale-response rejection, and polling stop only after every job is terminal. The server-rendering test verifies that `batch.js` is loaded only by the batch page and all required data attributes exist.

- [ ] **Step 2: Run tests and witness the expected failure**

Run: `node tests/batch_ui.cjs`

Expected: failure because `app/static/batch.js` does not exist.

- [ ] **Step 3: Implement dependency-free polling and accessible status updates**

Poll `/api/batches/${batch.dataset.batchId}` with `cache: "no-store"` every 3000 ms. Render messages and progress with `textContent`/numeric properties only. Use generation counters plus `pagehide`/`pageshow` to reject stale responses and avoid overlapping timers. Stop when all returned states belong to `completed`, `failed`, or `cancelled`. Keep `Aggiorna ora` as a normal link and replace the instructional copy with live-update wording.

- [ ] **Step 4: Run the Node, web, and existing job UI tests**

Run: `node tests/batch_ui.cjs && node tests/job_ui.cjs && .venv/bin/pytest tests/test_web.py -q`

Expected: all commands pass.

- [ ] **Step 5: Commit live batch progress**

```bash
git add app/static/batch.js app/static/app.css app/templates/batch.html tests/batch_ui.cjs tests/test_web.py
git commit -m "feat: update batch progress automatically"
```

### Task 5: Compact catalog list view

**Files:**
- Modify: `tests/catalog_ui.cjs`
- Modify: `tests/test_web.py`
- Modify: `app/templates/home.html`
- Modify: `app/static/catalog.js`
- Modify: `app/static/app.css`

**Interfaces:**
- Consumes: existing `[data-video-row]` and `[data-video-select]` catalog DOM.
- Produces: `#catalog-view-cards`, `#catalog-view-list`, `data-view="cards|list"` on the fieldset, and local-storage key `bunny-video-report:catalog-view`.

- [ ] **Step 1: Write failing view-toggle tests**

Extend the JavaScript harness with `classList`, `setAttribute`, and controlled `localStorage`. Assert the default `cards` view, a click to `list`, persistence under the exact key, restoration of `list`, accessibility state through `aria-pressed`, and graceful fallback when `getItem` or `setItem` throws. Existing filter and selection assertions must still pass in list mode. Add server HTML assertions for the labelled two-button control and list metadata.

- [ ] **Step 2: Run tests and witness the expected failure**

Run: `node tests/catalog_ui.cjs && .venv/bin/pytest tests/test_web.py -q -k 'catalog'`

Expected: the new assertions fail because the toggle and `data-view` behavior are absent.

- [ ] **Step 3: Implement the toggle and responsive list CSS**

Add the two buttons next to the catalog filters. `catalog.js` validates stored values, applies `data-view`, updates `aria-pressed`, and catches storage exceptions. In list mode use a single-column fieldset; each card becomes a row with a fixed small thumbnail, selection control, body, title, duration, status, collection, and upload date. At widths below 600 px the row becomes a compact two-column or stacked layout without horizontal scrolling.

- [ ] **Step 4: Run catalog tests and accessibility-adjacent web checks**

Run: `node tests/catalog_ui.cjs && .venv/bin/pytest tests/test_web.py tests/test_selection.py -q`

Expected: all pass and the existing 50/51 selection boundaries remain enforced.

- [ ] **Step 5: Commit list view**

```bash
git add app/templates/home.html app/static/catalog.js app/static/app.css tests/catalog_ui.cjs tests/test_web.py
git commit -m "feat: add compact catalog list view"
```

### Task 6: Documentation, complete verification, and Railway release

**Files:**
- Modify: `README.md`
- Modify: `Dockerfile` only if the volume path needs an explicit writable directory; prefer the Railway mount.

**Interfaces:**
- Consumes: all completed feature slices.
- Produces: documented `DATABASE_PATH`, restart semantics, report deletion policy, catalog toggle, live batch polling, and Railway volume setup.

- [ ] **Step 1: Update operational documentation**

Replace statements that reports are volatile. Document that only final structured reports and job metadata persist, active work becomes failed after a restart, login may still be required again, database files must not be committed, and production mounts `/data` with `DATABASE_PATH=/data/bunny-video-report.sqlite3`. Retain all warnings about ephemeral media and no real-video test without authorization.

- [ ] **Step 2: Run the full offline verification suite**

Run:

```bash
.venv/bin/pytest -q -m 'not live' --ignore=tests/test_media.py -k 'not test_form_to_report_with_real_ffmpeg_and_ephemeral_cleanup'
.venv/bin/pytest tests/test_media.py -q -m 'not live'
node tests/catalog_ui.cjs
node tests/job_ui.cjs
node tests/batch_ui.cjs
.venv/bin/python -m compileall -q app tests
git diff --check
```

Expected: zero failures and zero formatting errors. If the host still lacks FFmpeg, record only the exact media-test exclusions and run those tests in the production image before release.

- [ ] **Step 3: Commit documentation**

```bash
git add README.md Dockerfile
git commit -m "docs: describe persistent reports"
```

- [ ] **Step 4: Prepare and attach Railway persistence**

Use Railway's volume command to add one volume to the existing `bunny-video-report` service at `/data`, then set `DATABASE_PATH=/data/bunny-video-report.sqlite3`. Resolve the exact service and environment from read-only Railway status before any mutation. Do not print secret variable values.

- [ ] **Step 5: Publish and verify without paid provider calls**

Push the verified branch to the production-tracked GitHub branch, wait for Railway deployment health, then check `/healthz`, authenticated catalog rendering, archive, batch status, and downloads. Create only local/test fixture reports for persistence verification; do not select or analyze a real Bunny video. Restart once only after confirming no job is active, then verify the saved fixture/report remains accessible after re-login.

- [ ] **Step 6: Inspect final repository and deployment state**

Run `git status --short --branch`, `git log -1 --oneline`, `railway status`, and the read-only Railway volume listing. Report the deployed commit, exact automated test counts, persistent mount state, and any verification that could not be performed.
