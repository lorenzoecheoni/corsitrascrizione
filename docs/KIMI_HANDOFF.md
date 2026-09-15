# Handoff per Kimi — Bunny Video Report

Aggiornato: 16 settembre 2026.

> **STATO: i tre problemi del WIP sono risolti nel commit in testa al branch.**
> Prima del merge serve una nuova revisione indipendente del commit finale e la
> suite completa verde; restano vietati force-push, modifiche ai corsi Academy e
> analisi reali fuori dal GUID Governance.

## Repository e revisione

- Repository: `lorenzoecheoni/corsitrascrizione`
- Branch da usare: `feature/report-granulare-materiali`
- Commit applicativo verificato: `1bf98c5e8747b0dc1b247f864aef1d6e49a1a503`
- Base remota prima della feature: `e293c545676a582a60d89ec98c0afbd34c0c70c0`
- Spec: `docs/superpowers/specs/2026-09-15-report-granulare-materiali-design.md`
- Piano: `docs/superpowers/plans/2026-09-15-report-granulare-materiali.md`

Il branch non è stato unito in `main`, non è stato distribuito su Railway e non ha modificato corsi Academy.

## Ambito reale chiarito

- L'unico video da usare per la prova reale è **Governance**, GUID `7f254c4d-fe34-4fd3-a4cf-cda4f447e438`.
- Non eseguire analisi reali su altri video o corsi.
- I nomi di corsi/materiali presenti nei test sono esclusivamente fixture sintetiche locali: nessun altro corso reale è stato elaborato o modificato.
- Non è stata eseguita alcuna nuova analisi API a pagamento durante queste ultime correzioni.

## Stato dell'ultimo WIP

Dopo la revisione del commit `1bf98c5`, sono stati riprodotti tre ulteriori difetti. Il WIP locale ne corregge i casi diretti:

1. Collisione fra `Slide · Dottor Rossi` e `Slide · Elena Rossi` dopo la riconciliazione del report.
2. Due URL equivalenti (`/deck.pptx` e `/%64eck.pptx`) con metadati in conflitto.
3. Attribuzione impropria di `Slide · Dottor Morra` a Luigi quando il report documenta Luis Morra.

Le tre riproduzioni dirette passavano e il gruppo mirato aveva prodotto **788 test superati**, ma la revisione aveva trovato tre problemi aperti, **tutti risolti nel commit in testa al branch**:

1. **Riconciliazione a cascata dopo le omissioni — RISOLTO.** `_verified_material_result` ora itera a punto fisso: ricalcola la riconciliazione sul solo insieme superstite e ripete il controllo collisioni finché non emergono nuove ambiguità. L'export vede esattamente l'insieme stabile persistito e non può più rifiutarlo. Riprodotto da `test_material_reconciliation_iterates_to_a_stable_fixed_point`, che fallisce sul WIP e passa con la correzione.
2. **Qualifica del relatore persa — RISOLTO.** Il materiale persistito conserva il `relatore` originale: l'onorifico (`Dottor`, `Avvocato`…) continua ad alimentare il campo `ruolo` nell'export, che canonicalizza il nome visibile. Coperto da `test_persisted_material_honorific_still_feeds_the_exported_speaker_role`.
3. **Copertura del test di sicurezza — RISOLTO.** La riconciliazione speaker si attiva solo con un vero `AcademyContent`; il mock `SimpleNamespace` del test attraversa la fase materiali e raggiunge di nuovo la falla report controllata da `test_http_failed_job_has_safe_correlated_event[report-temporary_failure]`.

Risultato ampio dopo la correzione, in ambiente indipendente (Python 3.12): **1.736 passati, 4 esclusi, 0 falliti**. L'unico test escluso resta quello che richiede FFmpeg reale; il noto caso Python 3.13 non si manifesta su 3.12 e resta da ricontrollare sul container.

Prima del merge: rieseguire la revisione indipendente sul commit finale e la suite completa. Poi valgono i gate già elencati sotto (deploy Railway, due analisi reali Governance).

## Risultato implementato

- Report v1.1 per singolo video con durata ufficiale Bunny.
- Blocchi fattuali separati dai capitoli didattici; ogni capitolo indica il blocco sorgente.
- Confini basati su parola/silenzio, con evidenza compatta 5+5 parole per ogni passaggio interno.
- Segmenti `pausa` e `logistica` supportati all'inizio, fra blocchi e alla fine.
- Un solo capitolo pubblico di 8–15 minuti; mai saluti o logistica.
- ID deterministici `v1-bNNN` e `v1-iNNN`.
- Relatori riconciliati in modo conservativo. Forma canonica obbligatoria: `Furio D'Andrea` con apostrofo ASCII.
- Nomi simili ma distinti, come Fabio/Furio, Antonia/Antonio e Luis/Luigi, non vengono uniti.
- Materiali PPTX/PDF verificati, con associazione slide/materiale/pagina solo quando dimostrabile.
- Materiali ambigui o non persistibili vengono omessi con avviso non bloccante; nessun URL o file viene inventato.
- Export JSON, Markdown e TXT basato esclusivamente sui dati persistiti, senza rete o filesystem.
- Costo stimato incluso nel JSON.
- Video, audio, fotogrammi, deck, trascrizione completa, timing completi e URL firmati restano temporanei.
- Sdist e wheel hanno contenuti espliciti e non includono cartelle di lavoro, test, media o segreti.

## Ultime correzioni

Il commit `1bf98c5e8747b0dc1b247f864aef1d6e49a1a503` chiude sei difetti emersi dalla revisione globale:

1. Titoli materiale ambigui rilevati prima della persistenza, inclusa la convergenza `Dottor Morra`/`Luigi Morra`.
2. Identica grammatica delle sorgenti fra analisi, persistenza ed export.
3. Logistica iniziale e finale esportabile senza creare blocchi parlato fittizi.
4. Eliminato il fuzzy matching distruttivo dei nomi dei relatori.
5. Relazioni PPTX malformate trasformate in avviso materiale, senza far fallire il video.
6. Archivio sorgente ripulito tramite allowlist Hatch.

## Verifiche locali

- Suite mirata della correzione finale: **499 passed**.
- Regressione ampia: **1.338 passed**, 1 test FFmpeg escluso.
- Suite non-live completa: **1.670 passed**, 4 esclusi, 1 solo errore noto e preesistente.
- Errore noto: con Python 3.13 `traceback.format_exception` include la riga sorgente del test AssemblyAI contenente la stringa `private-video`. Non è una fuga del provider e i file interessati non sono stati modificati.
- `compileall`, `uv lock --check`, `git diff --check`, build offline, ispezione wheel/sdist e rebuild dal sdist: superati.
- FFmpeg/ffprobe non sono installati localmente; la prova reale nel container Railway resta obbligatoria.

## Passi minimi ancora necessari

1. Fare una revisione indipendente di `47f169fd..1bf98c5` confrontandola con `final-remediation-brief` ricostruibile dai sei punti sopra.
2. Se non emergono problemi, integrare il branch in `main` senza force-push.
3. Attendere il deploy Railway dell'esatto commit integrato e verificare `/healthz`, login, catalogo e FFmpeg nel container.
4. Analizzare due volte, sulla stessa revisione, il GUID Governance `7f254c4d-fe34-4fd3-a4cf-cda4f447e438`.
5. Scaricare JSON/Markdown/TXT e verificare:
   - completamento al 100%;
   - cinque persone canoniche senza doppioni;
   - blocco fattuale lungo di Furio conservato;
   - circa 10–12 capitoli, salvo un blocco realmente indivisibile segnalato;
   - esattamente un capitolo pubblico di 8–15 minuti;
   - materiali reali con pagina sulle slide accettate;
   - nessun tempo oltre la durata Bunny;
   - stessi ID fra i due report e confini con scarto massimo di un secondo.
6. Registrare soltanto commit, job ID, conteggi, durata, costo, codici avviso e differenze temporali. Non salvare trascrizioni o URL firmati.

## Vincoli operativi

- Non modificare il corso Academy pubblicato.
- Non modificare la bozza `/academy/governance-holding-report-15-09/`.
- Non stampare o committare variabili Railway, chiavi Bunny/OpenAI/AssemblyAI o password.
- Le credenziali condivise durante lo sviluppo dovrebbero essere ruotate al termine.
- Non eliminare questo branch finché deploy e due analisi reali non sono stati accettati.
