# Persistent reports, compact catalog, and live progress

**Date:** 2026-09-13  
**Status:** Approved for implementation

## Goal

Make the internal Bunny Video Report tool easier to browse and reliable across
Railway restarts by adding a compact catalog view, automatic progress updates,
and persistent textual reports.

The application must continue to avoid retaining video, audio, slide images, or
transcripts. Only job metadata, batch membership, and the final structured text
report are persistent.

## User experience

### Catalog display

The catalog keeps the existing card view and adds a compact list view. A
two-option control labelled `Schede` and `Elenco` switches between them without
reloading the page. The selected view is stored in browser `localStorage` and is
restored on later visits. If storage is unavailable, the page continues to work
and defaults to cards.

The list view presents one video per row with the existing selection control, a
small thumbnail when available, title, duration, Bunny status, collection, and
upload date. Search, filters, selected-only mode, the 50-video limit, selection
duration, and bulk selection operate identically in both views. The layout
collapses cleanly on narrow screens.

### Live progress

The individual job page keeps its existing automatic polling. Copy that model to
the batch page through one authenticated read-only batch-status endpoint, rather
than one request per job. The page polls approximately every three seconds,
updates each message, percentage, and progress bar in place, and stops once all
jobs are terminal. A small connection warning appears after transient failures
and polling retries automatically. Authentication expiry and missing records
produce explicit messages instead of silently stopping.

The existing `Aggiorna stato` action remains available as an `Aggiorna ora`
fallback. A disabled aggregate progress bar is not required: per-job progress is
the actionable view and avoids implying that queued long videos have equal work
weight.

### Persistent archive

The catalog page contains an archive of saved completed reports, ordered newest
first, with title, completion date, and links to open, download TXT, and download
Markdown. Recent non-completed jobs remain visible separately so running and
failed work is not confused with saved reports.

Completed reports have a delete action protected by the existing authenticated
session and CSRF token. The interface asks for browser confirmation and clearly
states that deletion removes only the report and local job record, never the
Bunny video. A deleted report returns 404 from its former report and download
URLs.

## Persistence design

Use the Python standard library `sqlite3`; no additional database service or API
subscription is required. `JobStore` becomes a SQLite-backed, thread-safe store
while retaining its current public interface wherever practical.

The database contains:

- `jobs`: UUID, canonical Bunny source URL, source title, state, progress,
  application-authored status message, timestamps, final report JSON, and safe
  error text;
- `batches`: UUID and creation timestamp;
- `batch_jobs`: ordered batch-to-job membership.

All writes use transactions. Foreign keys are enabled. The final report is
serialized from `AcademyReport` as validated JSON and validated again when read.
Queries return defensive Pydantic copies as the current in-memory store does.
Database exceptions must not expose paths, SQL, or stored content to users.

The database path comes from `DATABASE_PATH`. The local default is a file under
an application data directory suitable for development; tests pass an isolated
temporary path or use an explicit in-memory database. Production sets
`DATABASE_PATH=/data/bunny-video-report.sqlite3` and mounts a Railway persistent
volume at `/data`.

Only one application replica and one worker remain supported. SQLite is not used
to authorize horizontal scaling.

## Restart behavior

On startup, completed, failed, and cancelled jobs retain their state. Jobs found
in `queued` or `processing` state cannot be resumed safely because temporary
media and in-flight provider calls are intentionally not retained. They are
atomically changed to `failed` with progress preserved and a fixed message that
the server restarted and the analysis must be relaunched.

Batch records remain accessible. Deleted jobs are removed from batch membership;
an empty batch may remain as a valid historical group with no jobs.

Session cookies remain ephemeral and may be invalidated by a server restart.
After logging in again, the persistent reports are visible in the archive.

## Security and data retention

- Never store Bunny API keys, provider keys, cookies, confirmation tokens, raw
  transcripts, prompt payloads, media URLs containing signed query parameters,
  video/audio files, or slide frames.
- Store only canonical Bunny embed URLs already constructed by the server.
- Keep reports without an automatic expiry until a team member deletes them.
- Deletion affects only the local SQLite records and uses a transaction.
- Continue returning `Cache-Control: no-store` on authenticated pages and APIs.
- Database and volume contents must not be committed, embedded into Docker
  images, or printed in logs.

## API and rendering changes

Add `GET /api/batches/{batch_id}` returning only the batch identifier and the
safe JSON form of its jobs. `source_url` remains excluded. For completed jobs,
the endpoint does not need to include full report text; the job link is enough.

Add a CSRF-protected `POST /jobs/{job_id}/delete` for completed report deletion.
Non-completed jobs return a conflict response rather than being silently
removed.

The existing job-status endpoint and report downloads read from the persistent
store without changing their URLs.

## Failure handling

- A temporary batch polling error does not change server-side job state.
- Invalid or corrupt stored report JSON is treated as unavailable and logged
  without leaking the stored body; other reports remain accessible.
- SQLite uses a busy timeout and short transactions so UI reads do not block the
  worker's progress updates for meaningful periods.
- Store initialization fails fast with a safe configuration error if the parent
  directory is absent or unwritable.
- Deleting an unknown job returns 404; deleting a running, queued, failed, or
  cancelled job returns 409.

## Verification

Implementation is test-driven and must cover:

- persistence across two `JobStore` instances using the same database;
- recovery of interrupted jobs on startup;
- report schema validation and defensive copies;
- batch ordering and cleanup after deletion;
- archive ordering and separation from recent active/failed jobs;
- authenticated batch-status API and safe JSON fields;
- CSRF-protected completed-report deletion and rejection of invalid states;
- catalog view toggle, local-storage fallback, filters, and selections in both
  views;
- batch polling updates, terminal stop, retry, authentication expiry, and
  browser back/forward cache handling;
- existing offline Python and JavaScript suites;
- production smoke verification after the Railway volume and database variable
  are configured.

Deployment verification must not run a paid video analysis. It checks login,
catalog rendering, persisted report availability, batch status, and application
health. A new paid real-video run requires separate explicit authorization.

## Deployment

1. Add a Railway persistent volume mounted at `/data` for the existing service.
2. Set `DATABASE_PATH=/data/bunny-video-report.sqlite3`.
3. Deploy the tested revision with one replica and one Uvicorn process.
4. Verify health, login, catalog views, archive access, downloads, and live batch
   polling.
5. Restart the service once and confirm a completed report remains available
   after logging in again.

The deployment does not add a new API subscription. It consumes a very small
amount of Railway volume storage because only textual JSON and metadata are
retained.
