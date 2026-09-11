# Catalog Login Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rendere Bunny Video Report una dashboard interna con login HTML, catalogo completo Bunny, selezione multipla con conferma dei costi e coda dei soli video scelti.

**Architecture:** Il backend FastAPI continua a essere stateless rispetto ai media e conserva sessioni, conferme, lavori e batch soltanto nella memoria del singolo processo. Un client Bunny read-only recupera e valida tutte le pagine del catalogo; il browser filtra e seleziona i record già renderizzati, mentre il server rilegge i metadati prima di firmare una conferma e accodare i lavori in serie.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, httpx, Jinja2, JavaScript senza framework, CSS, pytest/respx, Node `vm`, Docker/Railway.

**Spec:** `docs/superpowers/specs/2026-09-11-catalog-login-redesign.md`

## Global Constraints

- Conservare una sola replica, un processo Uvicorn e un worker di elaborazione.
- Non aggiungere database, storage persistente, salvataggio del catalogo o analisi automatica non confermata.
- Accettare da 1 a 50 video per conferma e al massimo 10.000 video per caricamento catalogo.
- Usare soltanto richieste Bunny GET con la Stream API key della libreria configurata.
- Non registrare password, cookie, chiavi, URL firmati, risposte provider, transcript o contenuti multimediali.
- Mantenere `store=False` per OpenAI Responses e la pulizia effimera già collaudata.
- Rendere ogni nuova azione mutante dipendente da sessione valida e CSRF valido.
- Il cookie dura dodici ore, è `HttpOnly`, `SameSite=Strict` e `Secure` in produzione HTTPS.
- Conservare compatibilità mobile, tastiera, stampa PDF e download Markdown/TXT.

## File Structure

- `app/auth.py`: emissione/verifica della sessione firmata e verifica constant-time delle credenziali.
- `app/bunny.py`: modelli catalogo, paginazione read-only e costruzione sicura delle miniature.
- `app/selection.py`: firma e verifica della selezione di UUID, indipendente dalle rotte HTTP.
- `app/jobs.py`: titoli sicuri dei lavori, batch in memoria e lista lavori recenti.
- `app/main.py`: chiave di sessione e middleware che distingue rotte pubbliche, HTML e API.
- `app/web.py`: login/logout, dashboard, anteprima aggregata, creazione batch e pagine di stato.
- `app/templates/base.html`: shell condivisa, navigazione e metadati.
- `app/templates/login.html`: form di accesso compatibile con browser integrato.
- `app/templates/home.html`: catalogo e selezione multipla.
- `app/templates/selection_preview.html`: riepilogo e conferma del gruppo.
- `app/templates/batch.html`: panoramica dei lavori creati.
- `app/templates/job.html`, `app/templates/preview.html`: migrazione alla shell condivisa e nuovo stile.
- `app/static/app.css`: design system responsive della dashboard.
- `app/static/catalog.js`: ricerca, filtri e stato della selezione nel browser.
- `tests/test_auth.py`, `tests/test_bunny.py`, `tests/test_jobs.py`, `tests/test_web.py`: contratti backend.
- `tests/test_selection.py`: contratti del token aggregato.
- `tests/catalog_ui.cjs`: comportamento reale dello script catalogo con DOM controllato.
- `README.md`: accesso, catalogo, credenziali e costi dei servizi.

---

### Task 1: Sessione web e login compatibile

**Files:**
- Modify: `app/auth.py`
- Modify: `app/main.py`
- Modify: `app/web.py`
- Create: `app/templates/login.html`
- Modify: `tests/test_auth.py`

**Interfaces:**
- Produces: `SESSION_COOKIE: str`, `issue_session(secret: bytes, *, now: int | None = None) -> str`, `session_is_valid(value: str | None, secret: bytes, *, now: int | None = None) -> bool`, `credentials_are_valid(username: str, password: str, expected_password: str) -> bool`.
- Produces: `GET /login`, `POST /login`, `POST /logout`.
- Consumes: `request.app.state.csrf_token`, `request.app.state.session_key`, `Settings.app_password`.

- [ ] **Step 1: Scrivere i test fallenti di sessione e login**

  Sostituire i contratti Basic Auth in `tests/test_auth.py` con casi che verificano: redirect `/` → `/login`, login errato senza cookie, login valido con cookie, accesso con cookie, cookie scaduto/manomesso, `401` su `/api/jobs/unknown`, logout POST con CSRF e invalidazione del cookie. Esempio del contratto principale:

  ```python
  def test_html_login_issues_session_and_unlocks_home(client):
      login = client.post("/login", data={
          "username": "team", "password": "team-secret",
          "csrf_token": client.app.state.csrf_token,
      }, follow_redirects=False)
      assert login.status_code == 303
      assert login.headers["location"] == "/"
      assert "bvr_session=" in login.headers["set-cookie"]
      assert "HttpOnly" in login.headers["set-cookie"]
      assert client.get("/").status_code == 200
  ```

- [ ] **Step 2: Verificare il RED**

  Run: `.venv/bin/python -m pytest tests/test_auth.py -q`

  Expected: FAIL perché `/login` non esiste e `/` restituisce ancora `401` JSON.

- [ ] **Step 3: Implementare il token di sessione minimo**

  In `app/auth.py`, firmare il payload `team:<expiry>` con `hmac.digest(secret, payload, "sha256")`, codificare payload e firma con Base64 URL-safe senza padding, imporre `SESSION_TTL_SECONDS = 43_200` e confrontare la firma con `secrets.compare_digest`. Ogni parsing invalido deve restituire `False`, senza eccezioni o dettagli.

- [ ] **Step 4: Implementare rotte e middleware**

  Creare `app.state.session_key = secrets.token_bytes(32)`. Rendere pubblici `/healthz`, `/login` e `/static/*`; mantenere il controllo CSRF su `POST /login`. Per HTML senza sessione restituire `RedirectResponse('/login', 303)`, per `/api/*` restituire `JSONResponse({'detail': 'Richiesta non autorizzata'}, 401)`. Il login valido imposta il cookie per dodici ore; `secure=True` quando `request.url.scheme == 'https'` o `X-Forwarded-Proto` è `https`. Il logout cancella il cookie.

- [ ] **Step 5: Verificare il GREEN e la regressione sicurezza**

  Run: `.venv/bin/python -m pytest tests/test_auth.py tests/test_security.py tests/test_revision_security.py -q`

  Expected: PASS.

- [ ] **Step 6: Commit**

  ```bash
  git add app/auth.py app/main.py app/web.py app/templates/login.html tests/test_auth.py
  git commit -m "feat: replace browser auth with signed web sessions"
  ```

---

### Task 2: Catalogo Bunny paginato e miniature sicure

**Files:**
- Modify: `app/bunny.py`
- Modify: `tests/test_bunny.py`

**Interfaces:**
- Produces: `BunnyCatalogVideo` con `video_id`, `title`, `duration_seconds`, `status`, `description`, `uploaded_at`, `collection_id`, `thumbnail_file_name`, `thumbnail_url`.
- Produces: `BunnyCatalog` con `videos: list[BunnyCatalogVideo]` e `total_items: int`.
- Produces: `BunnyClient.list_videos(*, max_items: int = 10_000) -> BunnyCatalog` e `BunnyClient.build_thumbnail_url(video_id: str | UUID, filename: str) -> str`.
- Consumes: `Settings.bunny_library_id`, `bunny_stream_api_key`, `bunny_cdn_hostname`, `bunny_token_auth_key`.

- [ ] **Step 1: Scrivere i test fallenti della paginazione**

  Aggiungere fixture complete Bunny con `totalItems`, `currentPage`, `itemsPerPage` e `items`. Verificare due pagine da 100, arresto sull'ultima pagina, ordine conservato, richiesta `AccessKey`, `follow_redirects=False`, `trust_env=False` e assenza di chiamate oltre il totale.

  ```python
  @respx.mock
  def test_list_videos_reads_every_page_once(settings):
      first = respx.get(CATALOG_URL, params={"page": 1, "itemsPerPage": 100}).respond(
          200, json=catalog_page([catalog_item(VIDEO_ID)], total=2, page=1, per_page=1))
      second = respx.get(CATALOG_URL, params={"page": 2, "itemsPerPage": 100}).respond(
          200, json=catalog_page([catalog_item(OTHER_VIDEO_ID)], total=2, page=2, per_page=1))
      result = BunnyClient(settings).list_videos()
      assert [str(video.video_id) for video in result.videos] == [VIDEO_ID, OTHER_VIDEO_ID]
      assert first.called and second.called
  ```

- [ ] **Step 2: Scrivere i test fallenti dei confini**

  Coprire catalogo vuoto, duplicati, pagina incoerente, oltre 10.000 elementi, UUID/durata/data/nome miniatura invalidi, 401/403, 429, 5xx, timeout, non-JSON e redirect. Verificare che segreti e corpo upstream non compaiano in eccezioni o log.

- [ ] **Step 3: Verificare il RED**

  Run: `.venv/bin/python -m pytest tests/test_bunny.py -q`

  Expected: FAIL perché `BunnyCatalogVideo` e `list_videos` non esistono.

- [ ] **Step 4: Implementare modelli, GET condivisa e paginazione**

  Estrarre una funzione privata del client che esegue un solo GET e riusa la classificazione sicura degli errori esistente. In `list_videos`, richiedere pagine da 100, validare contatori interi non negativi, rifiutare UUID duplicati e fermarsi quando gli elementi raccolti raggiungono `totalItems`. Oltre il limite sollevare `BunnyResponseError("Catalogo Bunny troppo grande; usa la paginazione")`.

- [ ] **Step 5: Implementare URL miniatura**

  Accettare solo filename ASCII che rispettano `^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`, vietare `..` e costruire il percorso esclusivamente con hostname e UUID già validati. Riutilizzare la firma HMAC directory-scoped quando è presente la token key; esporre il token firmato, mai la chiave.

- [ ] **Step 6: Verificare il GREEN**

  Run: `.venv/bin/python -m pytest tests/test_bunny.py -q`

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add app/bunny.py tests/test_bunny.py
  git commit -m "feat: read and validate the complete Bunny catalog"
  ```

---

### Task 3: Conferme multiple, batch e lavori recenti

**Files:**
- Create: `app/selection.py`
- Create: `tests/test_selection.py`
- Modify: `app/jobs.py`
- Modify: `tests/test_jobs.py`

**Interfaces:**
- Produces: `sign_selection(video_ids: list[UUID], key: bytes, *, now: int | None = None) -> str` e `verify_selection(token: str, key: bytes, *, now: int | None = None) -> list[UUID]`.
- Produces: `BatchRecord(id: UUID, job_ids: list[UUID], created_at: datetime)`.
- Produces: `JobStore.create(source_url: str, *, source_title: str = "") -> JobRecord`, `JobStore.create_batch(items: list[tuple[str, str]]) -> tuple[BatchRecord, list[JobRecord]]`, `JobStore.get_batch(batch_id: UUID) -> BatchRecord`, `JobStore.list_recent(limit: int = 20) -> list[JobRecord]`.

- [ ] **Step 1: Scrivere i test fallenti del token selezione**

  Verificare round-trip di UUID unici in ordine, scadenza a dieci minuti, firma manomessa, payload malformato, zero elementi, duplicati e più di cinquanta UUID. Usare orari e UUID letterali derivati a mano.

- [ ] **Step 2: Verificare il RED selezione**

  Run: `.venv/bin/python -m pytest tests/test_selection.py -q`

  Expected: ERROR di import perché `app.selection` non esiste.

- [ ] **Step 3: Implementare `app/selection.py`**

  Serializzare `<expiry>|<uuid>,<uuid>` in Base64 URL-safe e aggiungere una firma HMAC-SHA256 separata da `.`. Il verificatore deve imporre lunghezza massima 4.096 byte, scadenza, cardinalità 1–50, unicità e UUID canonici; ogni errore deve diventare `ValueError("Conferma non valida o scaduta")`.

- [ ] **Step 4: Scrivere i test fallenti di batch e lavori recenti**

  In `tests/test_jobs.py`, verificare che `create_batch` crei un batch e lavori distinti nell'ordine fornito, che `source_title` sia visibile ma `source_url` resti escluso dal dump, che `get_batch` restituisca copie e che `list_recent` ordini dal più recente senza superare il limite.

- [ ] **Step 5: Verificare il RED dei batch**

  Run: `.venv/bin/python -m pytest tests/test_jobs.py -q`

  Expected: FAIL perché le nuove API del `JobStore` non esistono.

- [ ] **Step 6: Implementare batch e lista thread-safe**

  Aggiungere `source_title: str = ""` a `JobRecord`, una mappa privata `_batches`, creazione atomica sotto lo stesso `RLock`, copie profonde in uscita e validazione di `limit` fra 1 e 100. Non modificare le transizioni del worker.

- [ ] **Step 7: Verificare il GREEN**

  Run: `.venv/bin/python -m pytest tests/test_selection.py tests/test_jobs.py -q`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add app/selection.py app/jobs.py tests/test_selection.py tests/test_jobs.py
  git commit -m "feat: add signed selections and volatile job batches"
  ```

---

### Task 4: Dashboard, conferma aggregata e pagina batch

**Files:**
- Modify: `app/web.py`
- Modify: `app/templates/home.html`
- Create: `app/templates/selection_preview.html`
- Create: `app/templates/batch.html`
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces: `GET /` catalogo e lavori recenti; `POST /selections/preview`; `POST /batches`; `GET /batches/{batch_id}`.
- Consumes: `BunnyClient.list_videos`, `read_metadata`, `sign_selection`, `verify_selection`, `JobStore.create_batch`, `SingleWorkerRunner.submit`.

- [ ] **Step 1: Scrivere i test fallenti della dashboard**

  Con client autenticato tramite login reale, sostituire solo il confine Bunny remoto e verificare rendering di due video completi, totale e durata; catalogo vuoto; messaggi sicuri per `BunnyAuthError`, `BunnyRateLimitError` e `BunnyServerError`; lavori recenti senza `source_url`.

- [ ] **Step 2: Scrivere i test fallenti della selezione HTTP**

  Verificare zero, uno, cinquanta e cinquantuno `video_ids`; UUID invalidi; rimozione duplicati vietata; rilettura metadati; durata/costo totale; token hidden firmato; token alterato/scaduto; creazione batch e submit di ogni lavoro; un errore del runner su un elemento non deve annullare i lavori già creati ma deve produrre una risposta sicura.

  ```python
  def test_selected_videos_are_rechecked_then_queued(client):
      preview = client.post("/selections/preview", data=[
          ("video_ids", VIDEO_ID), ("video_ids", OTHER_VIDEO_ID),
          ("csrf_token", client.app.state.csrf_token),
      ])
      token = extract_hidden(preview.text, "confirmation")
      created = client.post("/batches", data={
          "confirmation": token, "csrf_token": client.app.state.csrf_token,
      }, follow_redirects=False)
      assert created.status_code == 303
      batch = client.get(created.headers["location"])
      assert VIDEO_TITLE in batch.text and OTHER_VIDEO_TITLE in batch.text
  ```

- [ ] **Step 3: Verificare il RED**

  Run: `.venv/bin/python -m pytest tests/test_web.py -q`

  Expected: FAIL perché dashboard e rotte batch non esistono.

- [ ] **Step 4: Implementare dashboard e mapping errori**

  `home` chiama `list_videos`, calcola conteggi in applicazione e passa un messaggio sicuro al template in caso di provider non configurato/non disponibile. Non reinvia eccezioni o response body. `source_url` non viene renderizzato.

- [ ] **Step 5: Implementare anteprima e creazione batch**

  Validare 1–50 UUID con `UUID`, rifiutare duplicati, chiamare `read_metadata` per ciascuno, calcolare `estimate_cost(sum(duration), 0)` e firmare la selezione. Alla conferma ricostruire esclusivamente URL embed canonici, creare il batch, inviare tutti i job al worker e reindirizzare a `/batches/{id}`.

- [ ] **Step 6: Implementare template funzionali minimi**

  In `home.html` usare un solo form con checkbox `video_ids`; in `selection_preview.html` mostrare lista, totale e token; in `batch.html` mostrare stato e collegamento di ogni lavoro. Mantenere escaping Jinja e token CSRF su ogni POST.

- [ ] **Step 7: Verificare il GREEN e i flussi esistenti**

  Run: `.venv/bin/python -m pytest tests/test_web.py tests/test_end_to_end.py -q`

  Expected: PASS.

- [ ] **Step 8: Commit**

  ```bash
  git add app/web.py app/templates/home.html app/templates/selection_preview.html app/templates/batch.html tests/test_web.py
  git commit -m "feat: select Bunny videos and create reviewed batches"
  ```

---

### Task 5: Interazione catalogo e redesign completo

**Files:**
- Create: `app/templates/base.html`
- Modify: `app/templates/login.html`
- Modify: `app/templates/home.html`
- Modify: `app/templates/selection_preview.html`
- Modify: `app/templates/batch.html`
- Modify: `app/templates/job.html`
- Modify: `app/templates/preview.html`
- Modify: `app/static/app.css`
- Create: `app/static/catalog.js`
- Create: `tests/catalog_ui.cjs`
- Modify: `tests/test_web.py`

**Interfaces:**
- Produces DOM catalogo: `#catalog-search`, `#status-filter`, `#collection-filter`, `#selected-only`, `#select-visible`, `#clear-selection`, `[data-video-row]`, `[data-video-select]`, `#selection-count`, `#selection-duration`, `#selection-bar`.
- Consumes gli attributi server-side `data-title`, `data-description`, `data-status`, `data-collection`, `data-duration`.

- [ ] **Step 1: Scrivere l'harness JavaScript fallente**

  `tests/catalog_ui.cjs` carica `catalog.js` con `vm`, crea tre righe controllate e verifica: ricerca case-insensitive, filtro stato/collezione, mostra solo selezionati, seleziona visibili senza toccare righe nascoste, azzera selezione, conteggio e durata, barra nascosta quando il conteggio è zero.

- [ ] **Step 2: Verificare il RED JavaScript**

  Run: `node tests/catalog_ui.cjs`

  Expected: FAIL perché `app/static/catalog.js` non esiste.

- [ ] **Step 3: Implementare `catalog.js`**

  Scrivere funzioni pure interne per normalizzare testo, calcolare visibilità e sommare durata; collegare eventi `input`/`change`/`click`; impostare `hidden` sulle righe e aggiornare testi tramite `textContent`. Non usare `innerHTML`, storage browser o chiamate remote.

- [ ] **Step 4: Verificare il GREEN JavaScript**

  Run: `node tests/catalog_ui.cjs`

  Expected: `PASS: catalog filters and selected-only workflow`.

- [ ] **Step 5: Scrivere i test HTML fallenti**

  In `tests/test_web.py` verificare struttura semantica, label associate, miniature con `loading="lazy"`, pulsante submit disabilitabile, navigazione/logout e presenza dello script soltanto nella dashboard. Non fare asserzioni su colori o stringhe CSS private.

- [ ] **Step 6: Creare shell e visual design**

  Estrarre `base.html`; applicare fondo blu-notte `#081525`, superfici `#f7f9fc`, testo `#142033`, accento corallo `#ff6b5f`, raggi 18–24px, ombre leggere e corpo minimo 16px. Desktop: catalogo a griglia con controlli compatti e barra selezione sticky; mobile: una colonna, controlli e azioni a tutta larghezza. Conservare focus visibile, contrasto, `prefers-reduced-motion` e regole stampa del report.

- [ ] **Step 7: Migrare tutte le pagine e verificare il GREEN**

  Estendere `base.html` nei template, usare miniature Bunny come unico contenuto fotografico e mantenere le azioni esistenti di download/copia/stampa/annullamento.

  Run: `.venv/bin/python -m pytest tests/test_web.py tests/test_auth.py -q && node tests/catalog_ui.cjs && node tests/job_ui.cjs`

  Expected: PASS per Python e entrambi gli harness Node.

- [ ] **Step 8: Commit**

  ```bash
  git add app/templates app/static/app.css app/static/catalog.js tests/test_web.py tests/catalog_ui.cjs
  git commit -m "feat: redesign the internal Bunny catalog workspace"
  ```

---

### Task 6: Documentazione, verifica completa e pubblicazione

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `docs/superpowers/plans/2026-09-11-catalog-login-redesign.md`

**Interfaces:**
- Consumes: tutte le API e rotte dei task precedenti.
- Produces: istruzioni operative aggiornate e versione Railway verificata.

- [ ] **Step 1: Aggiornare la documentazione operativa**

  Sostituire Basic Auth con login/cookie, documentare catalogo e limite 50, precisare che Bunny Stream API key proviene dalle impostazioni della libreria e che OpenAI API billing è separato da ChatGPT. Riportare come riferimenti verificati: Bunny Stream minimo mensile e tariffe storage/CDN, OpenAI diarized transcription e Luna per token, Railway Trial/Hobby e billing a consumo. Conservare l'avvertenza che le tariffe possono cambiare.

- [ ] **Step 2: Eseguire la suite completa con FFmpeg**

  Run: `PATH='/Users/lorenzo/.Trash/bunny-video-report-sdd-20260911-01/tools:'$PATH .venv/bin/python -m pytest -m 'not live' -q`

  Expected: tutti i test offline PASS e un solo test live deselected.

- [ ] **Step 3: Eseguire i test browser senza provider**

  Run: `node tests/catalog_ui.cjs && node tests/job_ui.cjs`

  Expected: due righe PASS, nessun errore.

- [ ] **Step 4: Verificare artefatto Python e Dockerfile**

  Run: `.venv/bin/python -m build --wheel`

  Expected: wheel creata e contenente template/static. Ispezionare `Dockerfile` e lasciare la build reale a Railway se Docker locale non è disponibile.

- [ ] **Step 5: Commit della documentazione**

  ```bash
  git add README.md .env.example docs/superpowers/plans/2026-09-11-catalog-login-redesign.md
  git commit -m "docs: explain catalog access and provider costs"
  ```

- [ ] **Step 6: Revisionare il branch e integrare `main`**

  Eseguire `git diff --check main...HEAD`, revisionare sicurezza/privacy e poi integrare con fast-forward soltanto dopo suite verde. Fare push di `main` a `origin` per attivare il deploy Railway.

- [ ] **Step 7: Attendere e verificare Railway**

  Controllare che il deployment del nuovo commit sia `SUCCESS`. Verificare:

  ```text
  GET /healthz                     -> 200 {"status":"ok"}
  GET /                            -> 303 /login senza cookie
  GET /login                       -> 200 HTML
  POST /login credenziali valide   -> 303 / e Set-Cookie
  GET / con cookie                 -> 200 dashboard o messaggio Bunny sicuro
  ```

- [ ] **Step 8: Collaudo provider con credenziali reali**

  Inserire i valori reali nel secret manager Railway, senza commit o output terminale. Verificare il catalogo completo; selezionare un video breve; controllare costo, coda, report ed export. Solo dopo il video breve, provare un contenuto reale da 1–4 ore e registrare durata/costo senza contenuti sensibili.

