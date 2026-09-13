# Academy Course Import Workflow Design

**Date:** 2026-09-13

## Goal

Extend Bunny Video Report from a per-video descriptive report into an internal,
auditable course-preparation workflow. The tool must synchronize the editorial
inventory from Google Sheets, match inventory rows to Bunny videos, produce one
factual intermediate JSON report per course, support human correction, and then
generate a validated `docs/IMPORT_CORSO.md` version 1 JSON file for the
Assoholding Academy.

The workflow must remain text-only. Source video, audio, transcripts, and slide
frames are temporary and must never be persisted.

## Product boundaries

- One output file represents one course.
- A course may contain one or more ordered Bunny videos.
- Rows in `Corsi premium - Master` remain separate courses. The Academy can
  later group those courses into a collection.
- The Google Sheet is an inventory and its row position is the primary course
  order. Bunny upload dates are informational only.
- The report pipeline records facts. The separate Academy generation step makes
  editorial decisions under a versioned prompt.
- No speaker identity, GUID, duration, role, organization, price band, or credit
  may be invented.
- Training credits are omitted and entered manually in the Academy.

## End-to-end flow

1. **Synchronize inventory.** Read the three tabs `Formazione`,
   `Corsi premium - Master`, and `Corsi Premium` from the configured Google
   Sheet. Preserve tab and row position.
2. **Match Bunny videos.** Use an explicit Bunny URL/GUID first. If it is absent
   or contains only `bunny`, propose a match from normalized titles only when it
   is unique and high-confidence. Ambiguous matches require review.
3. **Confirm the course.** The user reviews the proposed video membership and
   order before any paid analysis starts.
4. **Analyze.** Analyze every video independently, while building one course
   report. Speaker/time segments and slide changes are factual outputs.
5. **Review.** Present an editable, validated timeline. Critical verification
   items block Academy generation.
6. **Generate.** Send only the confirmed report to the Academy editorial model,
   validate the response against the Academy v1 contract, and save both JSON
   artifacts.
7. **Import externally.** The downloaded Academy JSON is suitable for
   `python manage.py importa_corso ...` in the Academy repository.

## Inventory synchronization

The production application needs a real Google Sheets integration; Codex's
development-time Google connector is not available to the deployed service.
Configuration therefore contains the spreadsheet ID and service-account
credentials. The service account is shared only on the inventory spreadsheet;
it needs editor access because confirmed matches may update the Link cell.

Each imported inventory course stores:

- source sheet title;
- one-based sheet row;
- course title exactly as displayed;
- expected speaker names;
- slide/material labels;
- explicit Bunny link/GUID when present;
- last synchronization time;
- current match state and confirmed Bunny video associations.

The sync updates source-owned fields but preserves confirmed local associations
unless the referenced video disappears. It never silently writes to Google
Sheets. When a confident missing-link match exists, the UI shows a proposal;
an authenticated, CSRF-protected user confirmation writes the full Bunny player
URL into the exact source row.

Title matching is normalization plus conservative similarity, not generative AI.
It folds whitespace, punctuation, accents, common prefixes such as `Webinar`,
and letter case. A proposal must be unique above the configured threshold and
must exceed the runner-up by a safety margin. Otherwise it remains unmatched.

## Intermediate report contract

The saved artifact is `report-intermedio.json` and uses this shape:

```json
{
  "versione": 1,
  "stato": "da_verificare",
  "corso": {
    "titolo": "Governance delle holding",
    "sinossi_corso": "Sintesi complessiva dell'intero corso.",
    "inventario": {"foglio": "Formazione", "ordine": 2}
  },
  "relatori": [
    {
      "nome": "Gaetano De Vito",
      "ruolo": "Presentatore",
      "organizzazione": "Assoholding",
      "confidenza": 0.98,
      "origine_nome": ["audio", "slide", "inventario"]
    }
  ],
  "video": [
    {
      "chiave": "v1",
      "guid": "7f254c4d-fe34-4fd3-a4cf-cda4f447e438",
      "titolo_bunny": "Governance delle holding",
      "durata_secondi": 5789,
      "ordine": 1,
      "interventi": [
        {
          "id": "v1-i001",
          "inizio": "0:00:00",
          "fine": "0:01:12",
          "tipo": "saluti",
          "relatori": ["Gaetano De Vito"],
          "titolo": "Apertura dell'incontro",
          "sintesi": "Saluti iniziali e presentazione dell'incontro.",
          "punti_chiave": [],
          "confidenza": 0.97
        }
      ],
      "slide": [
        {
          "inizio": "0:01:20",
          "titolo": "La governance della holding",
          "testo_principale": "Principi, organi e distribuzione delle responsabilità.",
          "confidenza": 0.96
        }
      ]
    }
  ],
  "verifiche_richieste": []
}
```

### Intermediate validation rules

- `stato` is `da_verificare` or `confermato`.
- Each video key is stable within the course (`v1`, `v2`, and so on).
- Times use `h:mm:ss`, exact to one second.
- Interventions cover each video continuously from zero through its duration,
  with no gaps or overlaps. Pauses, silence, and logistics are explicit
  timeline entries.
- `tipo` is one of `intervento`, `saluti`, `logistica`, `domande`, `pausa`,
  or `cambio_relatore`.
- `cambio_relatore` is only the brief handoff. The substantive contribution
  after the handoff is `intervento`.
- `relatori` is always an array when present. It is omitted when identity is not
  certain; generic names such as `Relatore 1` are forbidden.
- An intervention may contain multiple confirmed speakers.
- Each substantive intervention contains a short factual `sintesi` and three to
  seven `punti_chiave` grounded in speech or slides. Non-substantive entries may
  have an empty list.
- Course synopsis is global, not repeated as an editorial synopsis per segment.
- Each slide change contains exact `inizio`, detected `titolo`, up to about 500
  characters of `testo_principale`, and numeric `confidenza` from zero to one.
- Slide frames are not saved and no filename is emitted.
- Slide changes are evidence, never lesson boundaries.
- Names, roles, and organizations are emitted only when grounded in audio,
  visible content, inventory metadata, or a person registry.
- Critical verification items prevent `stato: confermato` and Academy output.

## Human review

The review page contains editable intervention rows with start, end, type,
title, speakers, summary, key points, and confidence. It also contains the slide
timeline and structured verification items. Clicking a timestamp opens or seeks
the Bunny player to that position without downloading the source.

Server-side validation is authoritative and checks time syntax, coverage,
duration, overlap, allowed types, speaker references, lengths, and critical
verification status. Client-side checks provide immediate feedback but cannot
bypass server validation. Saving a correction records the reviewed report in
SQLite. Confirmation is an explicit, authenticated, CSRF-protected action.

## Academy generation

The confirmed report is transformed into `import-academy.json` using the
versioned editorial rules from the Academy's `docs/IMPORT_CORSO.md`. The
Academy document is canonical. The deployed tool uses a versioned v1 contract
snapshot derived from that document so generation is reproducible. Contract
version and prompt version are stored with the result. Changes to the Academy
contract require a new snapshot and migration, not an in-place silent rewrite.

Editorial rules include:

- lessons are normally 8–40 minutes;
- modules contain 2–6 consecutive, thematically coherent lessons;
- greetings and introductions under two minutes are absorbed into the first
  lesson;
- logistics and pauses are excluded, and resulting timeline gaps are allowed;
- a speaker change on the same topic remains one lesson with all speakers,
  unless each part exceeds eight minutes and has its own title;
- questions under eight minutes extend the lesson that generated them;
  questions of at least eight minutes become `Domande e risposte`;
- slides provide evidence for titles, descriptions, and quizzes, but never
  become lessons;
- each module ends with a three-question quiz, each question having four
  answers, exactly one correct answer, and an explanation;
- `competenze` contains five to seven verb-led items;
- `profili` uses `Categoria | frase`;
- `presentazione` has three paragraphs;
- speaker names, GUIDs, and durations come only from the confirmed report;
- the hero is one representative clean lesson, normally 8–15 minutes, never a
  greeting. If no suitable lesson exists, generation is blocked for review.

The allowed `area` values are:

- `Fiscalità`
- `Governance`
- `Operazioni straordinarie`
- `Patrimonio`
- `Sostenibilità`
- `Trust`
- `Compliance`
- `Tecnologia e innovazione`
- `Family business`

If no area fits, the generator returns a critical verification item instead of
inventing a category.

Training credits are omitted. Price is computed from the total seconds included
in video lessons, after excluding pauses and logistics:

- up to 1 hour: EUR 97;
- over 1 and up to 2 hours: EUR 147;
- over 2 and up to 3 hours: EUR 197;
- over 3 and up to 4 hours: EUR 247.

The generator receives structured model output constraints. Its output is then
validated deterministically for schema shape, video/speaker references, lesson
times, non-overlap, allowed gaps, duration ranges, module sizes, quiz cardinality,
hero uniqueness, area, and price. One repair request may receive only the JSON
and machine validation errors. A second failure leaves the intermediate report
intact and blocks the Academy download.

## User interface

The home page gains three primary views:

1. `Inventario corsi`, ordered by Sheet tab and row;
2. `Da verificare`, containing course matches and reports that need action;
3. `Pronti per l'Academy`, containing confirmed reports and generated files.

An inventory row shows source tab/order, title, expected speakers, Bunny video
matches, workflow state, and the available next action. A secondary `Catalogo
Bunny` view retains direct access to all videos.

Progress continues to update automatically. All synchronization state, proposed
and confirmed matches, intermediate reports, corrections, contract versions,
and Academy JSON outputs persist in the Railway SQLite volume.

## Security, privacy, and failure behavior

- Every inventory, review, generation, download, and Sheet-write route requires
  the existing team session.
- Every mutation requires the existing CSRF/one-time confirmation controls.
- Google credentials, provider keys, transcript text, source URLs containing
  tokens, model diagnostics, and upstream response bodies never appear in logs
  or public errors.
- The Google account has access only to the inventory Sheet. Reads are automatic;
  writes happen only after a per-row confirmation.
- Provider timeouts and rate limits preserve the latest saved workflow state and
  present a retry action.
- A Sheet sync failure never deletes the previous local inventory snapshot.
- A Bunny disappearance marks the association invalid; it is not silently
  replaced by a fuzzy match.
- Legacy saved reports remain readable and exportable. They can be upgraded only
  by re-analysis because they do not contain intervention timelines.

## Verification and rollout

- Unit tests cover contracts, exact timeline validation, title matching, price
  bands, areas, and Academy validation.
- HTTP tests cover authenticated sync, proposal confirmation, review saves,
  confirmation, generation, downloads, CSRF, and safe errors.
- Pipeline tests prove that transcript/media/frame paths never enter persisted
  JSON and that temporary assets are cleaned.
- JavaScript tests cover inventory filters, editable timeline behavior, client
  validation, auto-progress, and stale-response isolation.
- Production rollout adds Google configuration without removing Bunny/OpenAI/
  AssemblyAI settings, migrates SQLite in place, deploys, checks health and UI,
  and performs a no-cost inventory synchronization before any paid video test.
