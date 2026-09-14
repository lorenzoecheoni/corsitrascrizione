# Intermediate Report v1.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat per-video JSON export with the Academy-compatible intermediate report v1.1 while preserving one autonomous report per Bunny video.

**Architecture:** Keep persisted `AcademyReport` jobs unchanged and build the v1.1 envelope deterministically at JSON download time. Add strict v1.1 Pydantic models, a pure conversion module with injected material reachability, and a narrow inventory lookup that supplies only explicit material sources; Markdown and TXT remain human-readable.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, httpx, pytest, Jinja2, Railway.

**Spec:** `docs/superpowers/specs/2026-09-14-intermediate-report-v1-1-design.md`

## Global Constraints

- The format is named v1.1 but serializes `"versione": 1`.
- Every current workflow export contains exactly one video: `chiave: "v1"`, `ordine: 1`.
- Do not group videos into courses or generate modules, lessons, or quizzes.
- Existing stored reports must export as v1.1 without Bunny, AssemblyAI, or OpenAI calls.
- Never invent a speaker, role, organization, material source, page, or slide/material association.
- `stato` is `da_verificare` iff at least one critical verification exists; warnings do not block `verificato`.
- All JSON models forbid extra fields and all output timestamps use exact `h:mm:ss` strings.
- Keep remote video content ephemeral and never log report content or provider secrets.

---

### Task 1: Define the strict v1.1 interchange models

**Files:**
- Create: `app/intermediate_models.py`
- Create: `tests/test_intermediate_models.py`

**Interfaces:**
- Produces: `IntermediateReportV11`, `IntermediateCourseV11`, `IntermediateSpeakerV11`, `IntermediateVideoV11`, `IntermediateInterventionV11`, `IntermediateSlideV11`, `IntermediateMaterialV11`, `VerificationRequestV11`.
- Produces: `format_hms(seconds: float) -> str` and `parse_hms(value: str) -> int`.
- Consumes: `ReportModel` and the existing `InterventionKind` literal from `app.models`.

- [ ] **Step 1: Write failing model-contract tests**

Create `tests/test_intermediate_models.py` with a minimal valid payload and assertions that the envelope has only `versione`, `stato`, `corso`, `relatori`, `video`, `verifiche_richieste`; the video is a list; timestamps round-trip as `0:00:06`; and intervention IDs start with `v1-i`.

```python
from copy import deepcopy
import pytest
from pydantic import ValidationError

from app.intermediate_models import IntermediateReportV11


def valid_payload():
    return {
        "versione": 1,
        "stato": "verificato",
        "corso": {"titolo": "Governance", "sinossi_corso": "Sintesi."},
        "relatori": [{
            "nome": "Gaetano De Vito", "slug": "gaetano-de-vito",
            "confidenza": .95, "origine_nome": ["audio"],
        }],
        "video": [{
            "chiave": "v1", "guid": "7f254c4d-fe34-4fd3-a4cf-cda4f447e438",
            "titolo_bunny": "Governance", "durata_secondi": 60, "ordine": 1,
            "lingua": "italiano", "sinossi": "Sintesi.",
            "interventi": [{
                "id": "v1-i001", "inizio": "0:00:00", "fine": "0:01:00",
                "tipo": "intervento", "relatori": ["Gaetano De Vito"],
                "titolo": "Apertura", "sintesi": "La governance viene introdotta.",
                "punti_chiave": ["Organi", "Deleghe", "Controlli"],
                "accesso": "pubblico", "confidenza": .9,
            }],
            "slide": [], "materiali": [],
        }],
        "verifiche_richieste": [],
    }


def test_v11_round_trips_exact_contract():
    model = IntermediateReportV11.model_validate(valid_payload())
    assert model.model_dump(mode="json", by_alias=True, exclude_none=True) == valid_payload()


@pytest.mark.parametrize("mutation", [
    lambda data: data.update(versione=1.1),
    lambda data: data.update(stato="confermato"),
    lambda data: data["video"].append(deepcopy(data["video"][0])),
    lambda data: data["video"][0]["interventi"][0].update(id="i001"),
    lambda data: data["verifiche_richieste"].append({
        "livello": "avviso", "codice": "CODICE_IGNOTO", "messaggio": "No",
    }),
])
def test_v11_rejects_values_outside_contract(mutation):
    data = valid_payload()
    mutation(data)
    with pytest.raises(ValidationError):
        IntermediateReportV11.model_validate(data)
```

- [ ] **Step 2: Run the model tests and verify RED**

Run: `uv run pytest -q tests/test_intermediate_models.py`

Expected: collection fails because `app.intermediate_models` does not exist.

- [ ] **Step 3: Implement the minimal strict models**

Create `app/intermediate_models.py`. Use `AliasChoices("start_seconds", "inizio")` and field serializers for timestamps. Define exact literals:

```python
VerificationCode = Literal[
    "RELATORE_NON_IDENTIFICATO", "TEMPI_INCOERENTI",
    "RELATORE_NON_NEL_REGISTRO", "INTERVENTO_BREVE",
    "CONFIDENZA_BASSA", "MATERIALE_NON_RAGGIUNGIBILE",
]
Access = Literal["pubblico", "iscritti"]
NameOrigin = Literal["audio", "slide", "inventario", "metadata", "revisione"]
```

`IntermediateReportV11.video` must have `min_length=1, max_length=1`. Its model validator must require key/order `v1`/`1`, unique speaker names, valid speaker references, and enforce the equivalence between a critical verification and `stato="da_verificare"`. `VerificationRequestV11` must reject a critical code with warning level and a warning code with critical level. `IntermediateInterventionV11` must require an ID matching `^v1-i[0-9]{3,}$`, require `fine > inizio`, allow timeline gaps/overlaps for later critical reporting, and require `punti_chiave == []` for every non-`intervento` type. `IntermediateMaterialV11` must require exactly one of `url` and `file`.

- [ ] **Step 4: Run the model tests and verify GREEN**

Run: `uv run pytest -q tests/test_intermediate_models.py`

Expected: all tests pass.

- [ ] **Step 5: Commit the model contract**

```bash
git add app/intermediate_models.py tests/test_intermediate_models.py
git commit -m "feat: define intermediate report v1.1 contract"
```

---

### Task 2: Build the one-video envelope and reconcile speakers

**Files:**
- Create: `app/intermediate_report.py`
- Create: `tests/test_intermediate_report.py`
- Modify: `app/reporting.py`

**Interfaces:**
- Consumes: `AcademyReport`, `IntermediateReportV11`, `UUID`, and optional material source strings.
- Produces: `build_intermediate_report(report: AcademyReport, guid: UUID, *, material_sources: Sequence[str] = (), material_url_checker: Callable[[str], bool] | None = None) -> IntermediateReportV11`.
- Produces: `registered_slug(name: str) -> str | None`, `split_role_organization(role: str | None) -> tuple[str | None, str | None]`, `reconcile_speakers(report: AcademyReport) -> list[ReconciledSpeaker]`, and `correct_speaker_name_mentions(value: str, canonical_names: Sequence[str]) -> str`.

- [ ] **Step 1: Write failing envelope and speaker tests**

Create a report based on `tests/fixtures/report.json`, with the formal speaker list containing only Vincenzo Manfredi and interventions naming all five target speakers. Assert:

```python
def test_builder_wraps_one_video_and_reconciles_registry_speakers():
    result = build_intermediate_report(report, TARGET_GUID)
    data = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert data["versione"] == 1
    assert data["corso"] == {
        "titolo": report.title, "sinossi_corso": report.synopsis,
    }
    assert [(item["chiave"], item["ordine"]) for item in data["video"]] == [("v1", 1)]
    assert [item["nome"] for item in data["relatori"]] == [
        "Vincenzo Manfredi", "Gaetano De Vito", "Furio d'Andrea",
        "Antonio Sibilia", "Luigi Morra",
    ]
    assert {item.get("slug") for item in data["relatori"]} == {
        "vincenzo-manfredi", "gaetano-de-vito", "antonio-sibilia",
        "luigi-morra", None,
    }
```

Add a role case with `Public Policy and Advocacy Director di Ass Holding` and assert `ruolo == "Public Policy and Advocacy Director"` and `organizzazione == "Assoholding"`. Assert Furio d'Andrea produces exactly one `RELATORE_NON_NEL_REGISTRO` warning whose message asks for the missing qualification when `ruolo` is absent.

- [ ] **Step 2: Run the builder tests and verify RED**

Run: `uv run pytest -q tests/test_intermediate_report.py -k 'wraps or registry or role'`

Expected: collection fails because `build_intermediate_report` is not defined.

- [ ] **Step 3: Implement registry, role splitting, and base envelope**

In `app/intermediate_report.py`, define the fixed registry using normalized name keys:

```python
_REGISTERED_SPEAKERS = {
    "vincenzo manfredi": "vincenzo-manfredi",
    "gaetano de vito": "gaetano-de-vito",
    "antonio sibilia": "antonio-sibilia",
    "luigi morra": "luigi-morra",
}
```

Reuse the reconciled-speaker behavior from `app.reporting` without importing a private renderer helper: rename the internal export dataclass to `ReconciledSpeaker`, expose `reconcile_speakers(report: AcademyReport) -> list[ReconciledSpeaker]`, and consume it from both Markdown rendering and the v1.1 builder. Preserve first appearance order, merge evidence/origins, and include timeline-only names with `origine_nome=["audio"]` and conservative `.65` confidence. Expose the existing near-name correction as `correct_speaker_name_mentions` and apply it to course/video titles, synopses, intervention titles, summaries, key points, and slide text so a canonical `Furio d'Andrea` cannot coexist with `Fulvio D'Andrea` in narrative fields.

Implement `split_role_organization` using a case-insensitive ` di (Ass Holding|Asso Holding|Assoholding)` suffix. Normalize only explicit organization text to `Assoholding`. Build the course and one-video shell, renumbering intervention IDs to `v1-iNNN` without altering times.

- [ ] **Step 4: Run speaker tests and verify GREEN**

Run: `uv run pytest -q tests/test_intermediate_report.py -k 'wraps or registry or role' tests/test_models_reporting.py`

Expected: all selected tests pass.

- [ ] **Step 5: Commit envelope and speaker reconciliation**

```bash
git add app/intermediate_report.py app/reporting.py tests/test_intermediate_report.py tests/test_models_reporting.py
git commit -m "feat: build v1.1 envelope and reconcile speakers"
```

---

### Task 3: Normalize short segments, summaries, access, and verification rules

**Files:**
- Modify: `app/intermediate_report.py`
- Modify: `app/prompts.py`
- Modify: `tests/test_intermediate_report.py`
- Modify: `tests/test_analysis.py`

**Interfaces:**
- Produces: `normalize_interventions(report: AcademyReport, video_key: str = "v1") -> list[IntermediateInterventionV11]`.
- Produces: `build_verifications(video: IntermediateVideoV11, speakers: Sequence[IntermediateSpeakerV11], material_failures: Sequence[str]) -> list[VerificationRequestV11]`.
- Produces: `choose_public_intervention(interventions: Sequence[IntermediateInterventionV11]) -> str | None`.

- [ ] **Step 1: Write failing tests for segment rules**

Add parameterized tests covering a 9-second `intervento`:

```python
@pytest.mark.parametrize(("title", "summary", "speakers", "expected"), [
    ("Ringraziamenti", "Grazie a tutti.", ["Vincenzo Manfredi"], "saluti"),
    ("Passaggio", "Passaggio della parola ad Antonio.", ["Vincenzo Manfredi"], "cambio_relatore"),
    ("Chiarimento", "Risposta sul regime fiscale.", ["Luigi Morra"], "domande"),
    ("Micro-turno", "Precisazione sul valore fiscale.", ["Luigi Morra"], "domande"),
    ("Silenzio", "Nessun parlato.", [], "pausa"),
])
def test_under_twenty_seconds_is_never_an_intervention(title, summary, speakers, expected):
    item = make_intervention(0, 9, titolo=title, sintesi=summary, relatori=speakers)
    result = normalize_interventions(make_report([item]))[0]
    assert result.tipo == expected
    assert result.punti_chiave == []
```

Add tests that 20 and 119 seconds remain `intervento` and generate `INTERVENTO_BREVE`, while 120 seconds does not. Add tests that every non-intervention loses key points. Add `F approfondisce il realizzo controllato.` and assert the result is `Tema trattato: il realizzo controllato.`.

- [ ] **Step 2: Write failing tests for access and all verification codes**

Build a timeline with substantive interventions of 6, 9, and 20 minutes and assert only the 9-minute intervention is public. Add fallback tests for first >=8 minutes and longest intervention. Test:

- missing speaker on a spoken segment → critical `RELATORE_NON_IDENTIFICATO`;
- a one-second gap, overlap, or final end beyond duration → critical `TEMPI_INCOERENTI`;
- intervention confidence `.79` and slide confidence `.69` → two `CONFIDENZA_BASSA` warnings;
- exact thresholds `.8` and `.7` → no confidence warning;
- critical checks → `da_verificare`; warnings only → `verificato`;
- repeated detections with the same code/video/intervention/field are deduplicated.

- [ ] **Step 3: Run the new rules tests and verify RED**

Run: `uv run pytest -q tests/test_intermediate_report.py -k 'twenty or short or access or verification or confidence or timeline or summary'`

Expected: failures show short segments remain `intervento`, access is absent, and verifications are not generated.

- [ ] **Step 4: Implement the deterministic normalizer and verification builder**

Implement keyword sets with casefolded, accent-insensitive matching. Apply the order `saluti`, `cambio_relatore`, `domande`, then fallback by presence of a named speaker. Always copy rather than mutate the persisted report.

Use this exact public-access ranking:

```python
duration = lambda item: item.end_seconds - item.start_seconds
eligible = [item for item in items if item.tipo == "intervento"]
preview = next((item for item in eligible if 480 <= duration(item) < 900), None)
preview = preview or next((item for item in eligible if duration(item) >= 480), None)
preview = preview or max(eligible, key=duration, default=None)
```

Generate verifications after normalization and material conversion. Compare consecutive starts/ends numerically and compare the final end to `durata_secondi`. Compute `stato` only after deduplication.

Update `WINDOW_PROMPT` and `CONSOLIDATION_PROMPT` to state: segments under 20 seconds may not be `intervento`; summaries begin with content and never with provider initials; `punti_chiave` occur only for `intervento`. Do not ask the model to assign access or Academy verifications; those remain deterministic application logic.

- [ ] **Step 5: Run focused and prompt tests and verify GREEN**

Run: `uv run pytest -q tests/test_intermediate_report.py tests/test_analysis.py`

Expected: all tests pass.

- [ ] **Step 6: Commit deterministic editorial normalization**

```bash
git add app/intermediate_report.py app/prompts.py tests/test_intermediate_report.py tests/test_analysis.py
git commit -m "feat: enforce v1.1 segment and verification rules"
```

---

### Task 4: Add explicit per-video materials from the inventory

**Files:**
- Modify: `app/inventory.py`
- Modify: `app/intermediate_report.py`
- Modify: `tests/test_inventory.py`
- Modify: `tests/test_intermediate_report.py`

**Interfaces:**
- Produces: `material_sources_for_video(courses: Sequence[InventoryCourse], video_id: UUID, title: str) -> list[str]`.
- Produces: `parse_material_source(value: str) -> IntermediateMaterialV11 | None`.
- Consumes: injected `material_url_checker(url: str) -> bool`.

- [ ] **Step 1: Write failing inventory-material matching tests**

Add tests proving materials are returned only for an explicit GUID match or an exact normalized-title match. A fuzzy proposal must not attach a material to a video. Preserve spreadsheet order and deduplicate identical strings.

```python
def test_material_sources_require_explicit_or_exact_video_match():
    assert material_sources_for_video(courses, target_id, "Governance delle holding") == [
        "Slide governance | https://example.test/governance.pdf",
        "dispensa.pdf",
    ]
    assert material_sources_for_video(courses, other_id, "Governance holding simile") == []
```

- [ ] **Step 2: Write failing parsing and reachability tests**

Using an injected checker, assert:

- `Slide governance | https://example.test/governance.pdf` yields title `Slide governance`, `url`, and `accesso: iscritti`;
- `dispensa.pdf` yields title `dispensa`, `file: dispensa.pdf`, and an unreachable-material warning;
- a failing URL checker yields `MATERIALE_NON_RAGGIUNGIBILE`;
- no source yields `materiali: []` and no invented slide `materiale` or `pagina` fields;
- `pagine` and `relatore` remain absent when not supplied by structured evidence.

- [ ] **Step 3: Run material tests and verify RED**

Run: `uv run pytest -q tests/test_inventory.py -k material tests/test_intermediate_report.py -k material`

Expected: failures show missing matching and parsing functions.

- [ ] **Step 4: Implement conservative material matching and parsing**

Reuse `normalize_title` and explicit GUID matching in `app.inventory`. Do not call fuzzy proposal logic. In `app.intermediate_report`, extract the first HTTP(S) URL from a cell; use preceding text stripped of `|`, `-`, and whitespace as title, otherwise use the URL path basename. For non-URL values use `Path(value).name` as `file` and `Path(value).stem` as title. Empty strings are discarded.

The default checker uses an `httpx.Client` with a five-second total timeout, redirect following, and a streamed GET so the body is not retained. Treat status 200–399 as reachable. Tests inject a pure checker and do not access the network.

- [ ] **Step 5: Run material and inventory tests and verify GREEN**

Run: `uv run pytest -q tests/test_inventory.py tests/test_intermediate_report.py`

Expected: all tests pass.

- [ ] **Step 6: Commit material support**

```bash
git add app/inventory.py app/intermediate_report.py tests/test_inventory.py tests/test_intermediate_report.py
git commit -m "feat: add explicit video materials to v1.1 reports"
```

---

### Task 5: Replace the JSON endpoint and update the download UI

**Files:**
- Modify: `app/web.py`
- Modify: `app/templates/job.html`
- Modify: `app/templates/home.html`
- Modify: `tests/test_web.py`
- Modify: `tests/job_ui.cjs`

**Interfaces:**
- Consumes: `build_intermediate_report`, `material_sources_for_video`, the stored `JobRecord`, and the authenticated inventory client.
- Produces: `GET /jobs/{job_id}/report.json` with v1.1 JSON and filename `report-intermedio-{job_id}.json`.

- [ ] **Step 1: Replace flat-export expectations with failing v1.1 endpoint tests**

Update `test_completed_job_downloads_and_page_escape_untrusted_content` and `test_video_json_export_serializes_each_intervention_with_exact_hms` to assert:

```python
payload = client.get(f"/jobs/{job.id}/report.json").json()
assert set(payload) == {
    "versione", "stato", "corso", "relatori", "video", "verifiche_richieste",
}
assert payload["video"][0]["chiave"] == "v1"
assert payload["video"][0]["ordine"] == 1
assert payload["video"][0]["interventi"][0]["id"] == "v1-i001"
assert "incertezze" not in payload
```

Configure the test inventory with explicit material fixtures and assert the endpoint does not call Bunny metadata, AssemblyAI, or OpenAI. Assert the `Content-Disposition` filename begins `report-intermedio-` and the page label is `Download JSON v1.1`.

- [ ] **Step 2: Run web and UI tests and verify RED**

Run: `uv run pytest -q tests/test_web.py -k 'downloads or json_export' && node --test tests/job_ui.cjs`

Expected: endpoint assertions fail because the old flat JSON is returned and the old button text remains.

- [ ] **Step 3: Implement endpoint conversion and best-effort inventory lookup**

In `json_report`, parse the canonical GUID as today, fetch inventory rows inside a fixed safe `try/except InventoryError`, call `material_sources_for_video`, build `IntermediateReportV11`, and serialize with `model_dump_json(indent=2, by_alias=True, exclude_none=True)`. If Sheets is unavailable, pass an empty material list and still return the report.

Change only JSON labels and filename. Keep Markdown/TXT routes and saved-report links functional. Ensure endpoint error responses remain fixed text and never include provider bodies or inventory cell contents.

- [ ] **Step 4: Run web and UI tests and verify GREEN**

Run: `uv run pytest -q tests/test_web.py tests/test_security.py && node --test tests/job_ui.cjs tests/catalog_ui.cjs`

Expected: all tests pass.

- [ ] **Step 5: Commit endpoint migration**

```bash
git add app/web.py app/templates/job.html app/templates/home.html tests/test_web.py tests/job_ui.cjs
git commit -m "feat: publish v1.1 report downloads"
```

---

### Task 6: Verify the complete contract and publish it

**Files:**
- Modify: `docs/superpowers/specs/2026-09-14-intermediate-report-v1-1-design.md` only if verification exposes a documented mismatch.
- Verify: all application and test files from Tasks 1–5.

**Interfaces:**
- Consumes: completed v1.1 implementation and Railway production configuration.
- Produces: a deployed, authenticated v1.1 export for the existing Governance report.

- [ ] **Step 1: Run the focused contract suite**

Run:

```bash
uv run pytest -q \
  tests/test_intermediate_models.py \
  tests/test_intermediate_report.py \
  tests/test_inventory.py \
  tests/test_models_reporting.py \
  tests/test_web.py \
  tests/test_security.py
```

Expected: zero failures.

- [ ] **Step 2: Run the full portable test suite and UI tests**

Run:

```bash
uv run pytest -q \
  --ignore=tests/test_media.py \
  --ignore=tests/test_end_to_end.py \
  --ignore=tests/test_assemblyai.py
node --test tests/job_ui.cjs tests/batch_ui.cjs tests/catalog_ui.cjs \
  tests/inventory_ui.cjs tests/course_review_ui.cjs
```

Expected: zero failures; only the existing platform-dependent skips and dependency deprecation warnings may remain.

- [ ] **Step 3: Build the distributable and inspect repository state**

Run:

```bash
uv build
git diff --check
git status --short
```

Expected: wheel and source distribution build successfully; no whitespace errors; only deliberate build artifacts ignored by Git.

- [ ] **Step 4: Commit final verification-only adjustments**

If Step 1–3 required a tracked correction, commit exactly those files:

```bash
git add app tests docs
git commit -m "test: verify intermediate report v1.1"
```

If no tracked correction was required, do not create an empty commit.

- [ ] **Step 5: Push and deploy the verified main branch**

Run:

```bash
git push origin main
railway up --detach --service bunny-video-report --environment production
railway deployment list --service bunny-video-report --environment production --limit 2
```

Expected: the new Railway deployment reaches `SUCCESS` and the previous deployment is removed only after the new health check passes.

- [ ] **Step 6: Smoke-test the existing Governance report without reanalysis**

Authenticate using `APP_PASSWORD` read in-process from `railway variables --json`; never print variables. Fetch the existing completed job
`ebd8ed92-9596-4b0b-b6d9-40af6e857ddc` and assert:

- response 200 and valid JSON;
- top-level fields exactly match v1.1;
- `stato` matches the presence or absence of critical verifications;
- one video, `v1`, order 1, correct GUID and 5789-second duration;
- intervention IDs are consecutive `v1-i001`… and timeline findings match verifications;
- the five known names appear once; the four registered names have slugs;
- Furio d'Andrea has no slug and has `RELATORE_NON_NEL_REGISTRO`;
- `Fulvio D'Andrea` and provider-initial summary openings are absent;
- no segment shorter than 20 seconds is `intervento` and every non-intervention has no key points;
- exactly one substantive intervention has `accesso: pubblico`;
- low-confidence and short-intervention warnings obey exact thresholds;
- `incertezze` is absent;
- the stored job timestamp is unchanged, proving no video reanalysis occurred.

- [ ] **Step 7: Check clean final state**

Run:

```bash
git status --porcelain=v1
git rev-parse --short HEAD
git ls-remote origin refs/heads/main
```

Expected: clean worktree and local/remote main at the same commit.
