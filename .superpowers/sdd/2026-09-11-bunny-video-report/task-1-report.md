# Task 1 report — Fondazione del progetto, configurazione e autenticazione

## Implementazione

- Creato il packaging Python 3.12 con le dipendenze applicative e di test richieste.
- Creato un ambiente virtuale locale `.venv` con Python 3.12 tramite `uv`; nessuna dipendenza e' stata installata globalmente.
- Aggiunta `Settings`, con caricamento da `.env`, campi segreti esclusi da `repr`, e supporto per l'iniezione nei test.
- Aggiunta autenticazione HTTP Basic condivisa: sono accettate esclusivamente le credenziali `team` e la password configurata, confrontate in tempo costante.
- Aggiunta app factory `create_app(settings: Settings | None = None)`. Non esiste un'applicazione di produzione istanziata al momento dell'import, per cui l'import di `app.main` non richiede segreti d'ambiente.
- Aggiunte la pagina iniziale protetta, le risorse statiche e la route pubblica `/healthz`.
- Aggiunti i test di accesso richiesti. Il primo test run e' fallito come previsto con `ModuleNotFoundError: No module named 'app'` prima dell'implementazione.

## Verifica

- Focused: `.venv/bin/python -m pytest tests/test_auth.py -q` — 3 passed.
- Suite completa: `.venv/bin/python -m pytest -q` — 3 passed.
- Controllo whitespace: `git diff --check` — nessun errore.

I due warning della suite provengono dalle API deprecate di FastAPI/Starlette TestClient nelle versioni installate; non sono causati dal codice applicativo.

## Self-review

- `/healthz` non ha dipendenze di autenticazione; `/` richiede Basic Auth e restituisce `WWW-Authenticate: Basic` in caso di errore.
- Password e API key non compaiono in template o log; gli attributi sensibili di `Settings` non compaiono nella rappresentazione dell'oggetto.
- Il task non introduce database, persistenza, video o audio.
- Conservata la regola `.worktrees/` nel `.gitignore` radice, senza modifiche.
