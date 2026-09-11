# Bunny Video Report

Applicazione interna per trasformare video Bunny Stream in report Academy in italiano: descrizione, destinatari, obiettivi, relatori con evidenze e incertezze, interventi, capitoli, slide e stima dei costi. Anche quando pubblicata online rimane uno strumento a uso interno del team, protetto da HTTP Basic Auth e HTTPS.

## Avvio locale

Richiede Python 3.12 o successivo e FFmpeg/FFprobe nel `PATH`. Installare FFmpeg con il gestore pacchetti del sistema (su Debian/Ubuntu: `sudo apt-get install ffmpeg`; su macOS con Homebrew: `brew install ffmpeg`).

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
cp .env.example .env
chmod 600 .env
# Compilare .env con un editor locale, senza incollare segreti nel terminale.
.venv/bin/python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

Aprire `http://127.0.0.1:8000`. Utente Basic Auth: `team`; password: `APP_PASSWORD`. Il processo legge `.env` dalla directory corrente; le variabili dell'ambiente hanno precedenza. Non attivare access log, debug con variabili locali o logging dei corpi delle richieste. Nessuna chiamata remota viene effettuata all'import del modulo; `create_app` è una factory e `build_services(settings)` costruisce le dipendenze sostituibili.

## Configurazione

| Variabile | Valore richiesto |
| --- | --- |
| `BUNNY_LIBRARY_ID` | ID numerico positivo della libreria autorizzata |
| `BUNNY_STREAM_API_KEY` | Chiave API Stream read-only della stessa libreria |
| `BUNNY_CDN_HOSTNAME` | Hostname CDN della libreria, senza `https://`, porta o percorso |
| `BUNNY_TOKEN_AUTH_KEY` | Chiave token CDN opzionale; lasciare vuota se token authentication è disabilitata |
| `OPENAI_API_KEY` | Chiave OpenAI del progetto autorizzato alla trascrizione e analisi |
| `APP_PASSWORD` | Password lunga e casuale condivisa esclusivamente con il team |

`BUNNY_SAMPLE_VIDEO_URL` serve soltanto al test live. `RUN_LIVE_BUNNY=1` abilita esplicitamente quel test a pagamento. `TEMP_ROOT`, opzionale, seleziona una directory temporanea già esistente e scrivibile dal processo. Non inserire `.env` nel repository, nell'immagine o nei report diagnostici; il file di esempio contiene soltanto segnaposto.

La chiave Bunny deve avere permessi di sola lettura. L'applicazione legge i metadati con GET e il flusso HLS; non crea, modifica o elimina video, capitoli, sottotitoli o configurazioni Bunny. Audio e frame selezionati vengono inviati a OpenAI per l'analisi; le chiamate Responses usano `store=False`. Questo non equivale a una garanzia sulla conservazione lato fornitore: applicare le policy del proprio account.

## Video e utilizzo

Sono accettati link HTTPS nei seguenti formati, esclusivamente per la libreria e il CDN configurati:

- `https://iframe.mediadelivery.net/embed/LIBRARY_ID/VIDEO_UUID`
- `https://player.mediadelivery.net/embed/LIBRARY_ID/VIDEO_UUID`
- `https://CDN_HOSTNAME/VIDEO_UUID/playlist.m3u8`

Eventuali query del link incollato vengono scartate. Il playback viene ricostruito usando la configurazione del server e, se presente, la chiave token CDN. Link dashboard, URL arbitrari e video di altre librerie non sono supportati. DRM e restrizioni referrer che impediscono l'accesso server sono esclusi: non vengono aggirati. La compatibilità dei token e delle protezioni della propria libreria deve essere verificata con il test live.

Il target operativo è costituito da video di **1-4 ore**; i video brevi sono accettati per il collaudo e quelli oltre quattro ore vengono rifiutati. Serve audio decodificabile. Si elabora un video alla volta; gli altri rimangono in coda. Inserire il link nel form, seguire l'avanzamento e riaprire l'URL del lavoro nella stessa sessione del server. Dal report si possono scaricare **Markdown (.md)** e **testo (.txt)**, copiare il contenuto oppure scegliere **Stampa / Salva PDF** nel browser. Il PDF usa la stampa browser, non un generatore server.

Nomi e ruoli richiedono evidenze testuali o visive; in caso di dubbio compaiono etichette generiche e incertezze. L'identità dei relatori non viene dedotta biometricamente dalla voce. Il report richiede revisione umana prima dell'uso editoriale.

## Dati temporanei e riavvio

Audio e frame sono salvati in directory temporanee per la durata del lavoro e rimossi al completamento, errore o annullamento cooperativo. Il transcript resta soltanto in memoria durante la pipeline. I report e la coda restano nella memoria del singolo processo: **un riavvio perde tutti i lavori e report**, e i vecchi URL restituiscono 404. Scaricare gli export prima della manutenzione; i file scaricati sul computer dell'utente restano finché l'utente li elimina.

L'annullamento attende che la fase in corso riconosca la richiesta e pulisca i file; una chiamata remota già in corso può terminare prima dell'annullamento. Durante l'arresto ordinato il worker può finire i lavori già accodati. Prima di riavviare, annullare o attendere tutti i lavori. Un arresto forzato, un crash o la perdita dell'host possono impedire la pulizia: usare storage temporaneo effimero, senza backup, e rimuovere le sole directory residue `bunny-video-*` quando nessun worker è attivo. Non promettere pulizia Python dopo SIGKILL.

## Docker e pubblicazione online

```sh
docker build -t bunny-video-report:local .
docker run --rm bunny-video-report:local ffmpeg -version
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 bunny-video-report:local
```

L'immagine include FFmpeg, template e asset statici, esegue l'app come `appuser` non root e avvia `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log`. I segreti entrano soltanto a runtime attraverso `--env-file` o il secret manager del servizio scelto; non usare build argument o `ENV` nel Dockerfile per le credenziali. La directory di build è filtrata da `.dockerignore`.

Il provider non è ancora scelto. Per pubblicare, configurare un reverse proxy con certificato TLS valido davanti alla porta privata 8000 e usare sempre **HTTPS** per l'accesso remoto: Basic Auth trasporta credenziali e non cifra il traffico. Impedire l'accesso pubblico diretto alla porta del container e inoltrare gli header proxy soltanto da proxy fidati. Disabilitare anche sul proxy il logging di header Authorization, body, query e URL completi sensibili.

Usare **esattamente una replica e un processo Uvicorn**, senza `--workers` multipli, autoscaling o rolling overlap: queue e report sono in memoria e non sono condivisi fra processi. Configurare il provider senza sospensione automatica durante i lavori, con risorse CPU/RAM e spazio temporaneo adeguati ai video lunghi. Misurare il fabbisogno sul primo video reale; non è stato ancora calibrato.

`GET /healthz` è pubblico e restituisce `{"status":"ok"}`; tutte le altre rotte, inclusi asset e download, richiedono autenticazione. L'healthcheck prova che il processo HTTP risponde, non che Bunny/OpenAI siano raggiungibili o che le credenziali siano corrette. Il Dockerfile include questa verifica di salute.

## Costi e collaudo

La stima iniziale è **$0.40-$0.70 per ora di video**, da ricalibrare sul primo video reale usando usage API e traffico Bunny. Non è un preventivo: modello, numero di frame, retry, contenuti e tariffe possono cambiare il costo. Il traffico mostrato è una stima dai byte dei pacchetti di input FFmpeg con margine del 20%, non traffico di rete misurato. Le costanti iniziali sono in `app/costs.py`; aggiornarle soltanto sulla base di misure reali insieme a questo README.

Suite offline, senza credenziali reali né accesso ai servizi:

```sh
.venv/bin/python -m pytest -m 'not live' -q
```

Lo smoke `tests/test_end_to_end.py` genera un video sintetico con FFmpeg, usa confini Bunny/OpenAI finti e attraversa form, worker, schema, download e pulizia temporanea entro 15 secondi. Verifica anche autenticazione, healthcheck e perdita del lavoro al riavvio. FFmpeg e FFprobe devono essere disponibili nel `PATH`.

Il test live è **opt-in e a pagamento**. Compilare in `.env` le variabili obbligatorie e `BUNNY_SAMPLE_VIDEO_URL` con un video autorizzato; fornire anche la chiave token se necessaria. Impostare `RUN_LIVE_BUNNY=1` nel file e caricarlo come dati, senza eseguirlo come script shell:

```sh
.venv/bin/python -c 'from dotenv import load_dotenv; load_dotenv(".env"); import pytest; raise SystemExit(pytest.main(["tests/test_live_bunny.py", "-m", "live", "-q", "--tb=no"]))'
```

Se l'opt-in o una variabile obbligatoria manca, il test viene saltato. Non usare `--showlocals`, debugger o registrazioni HTTP con credenziali reali. Il test non stampa URL, transcript, report o segreti e non effettua modifiche su Bunny. Lasciare `RUN_LIVE_BUNNY=0` al termine.

Prima della messa online: eseguire la build Docker e il controllo FFmpeg sopra; provare prima un video breve e poi uno di almeno un'ora; verificare almeno dieci timestamp fra interventi, capitoli e slide; controllare presentatore, assenza del moderatore e due relatori. Provare annullamento e riavvio, aprire MD/TXT e controllare visivamente la stampa PDF. Registrare costo e durata del collaudo senza conservare contenuti sensibili nei log.

Nel primo ambiente di sviluppo non è disponibile Docker/Podman/Buildah: sono verificabili la suite offline e il contenuto della wheel, ma build ed esecuzione del container richiedono un host con runtime. I test live, i costi e la precisione su video reali restano da verificare con le credenziali del team.
