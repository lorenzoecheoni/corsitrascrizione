# Handoff per Kimi — Bunny Video Report

Aggiornato: 16 settembre 2026.

## Repository e revisione

- Repository: `lorenzoecheoni/corsitrascrizione`
- Branch da usare: `feature/report-granulare-materiali`
- Commit applicativo verificato: `1bf98c5e8747b0dc1b247f864aef1d6e49a1a503`
- Base remota prima della feature: `e293c545676a582a60d89ec98c0afbd34c0c70c0`
- Spec: `docs/superpowers/specs/2026-09-15-report-granulare-materiali-design.md`
- Piano: `docs/superpowers/plans/2026-09-15-report-granulare-materiali.md`

Il branch non è stato unito in `main`, non è stato distribuito su Railway e non ha modificato corsi Academy.

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
