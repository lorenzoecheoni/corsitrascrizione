# Bunny Video Report

Applicazione interna per trasformare video Bunny Stream in report Academy: titolo suggerito, breve sinossi, presentatori/moderatori/relatori con evidenze e incertezze, timestamp e titoli dei cambi slide, più stima dei costi. Anche quando pubblicata online rimane uno strumento a uso interno del team, protetto da login web, cookie di sessione e HTTPS.

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

Aprire `http://127.0.0.1:8000/login`. L'utente è fisso: `team`; la password è `APP_PASSWORD`. Un accesso valido rilascia un cookie firmato, `HttpOnly` e `SameSite=Strict`, valido 12 ore; su HTTPS è anche `Secure`. Logout e ogni azione che crea, annulla, conferma o elimina un lavoro richiedono CSRF. Un riavvio può invalidare cookie e conferme in memoria, ma non elimina i report completati salvati nel database.

Il processo legge `.env` dalla directory corrente; le variabili dell'ambiente hanno precedenza. Non attivare access log, debug con variabili locali o logging dei corpi delle richieste. Nessuna chiamata remota viene effettuata all'import del modulo; `create_app` è una factory e `build_services(settings)` costruisce le dipendenze sostituibili.

## Configurazione

| Variabile | Valore richiesto |
| --- | --- |
| `BUNNY_LIBRARY_ID` | ID numerico positivo della libreria autorizzata |
| `BUNNY_STREAM_API_KEY` | Chiave API Stream read-only della stessa libreria, ottenuta dalle impostazioni della libreria Bunny |
| `BUNNY_CDN_HOSTNAME` | Hostname CDN della libreria, senza `https://`, porta o percorso |
| `BUNNY_TOKEN_AUTH_KEY` | Chiave token CDN opzionale; lasciare vuota se token authentication è disabilitata |
| `OPENAI_API_KEY` | Chiave OpenAI del progetto autorizzato alla trascrizione e analisi |
| `ASSEMBLYAI_API_KEY` | Richiesta per generare nuovi report: abilita trascrizione globale, word timing, diarizzazione e identificazione relatori |
| `ASSEMBLYAI_REGION` | `eu` (predefinito) per endpoint europeo, oppure `global` |
| `APP_PASSWORD` | Password lunga e casuale condivisa esclusivamente con il team |
| `DATABASE_PATH` | File SQLite dei report; in produzione Railway usare `/data/bunny-video-report.sqlite3` su volume persistente |
| `MATERIAL_ALLOWED_HOSTS` | `www.assoholding.it` per impostazione predefinita; eventuali altri hostname esatti, separati da virgole, senza URL, porte o wildcard |
| `MATERIAL_LIBRARY_DIR` | Facoltativa: cartella con i deck PDF/PPTX scaricati a mano da Drive; le etichette del foglio (es. `Slide Furio D'Andrea`) vengono abbinate ai nomi dei file e il report espone solo il nome del deck, pronto per `--materiali-da` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Facoltativa: JSON del service account con cui è condiviso il foglio inventario; con esso la lettura avviene via API e recupera anche gli hyperlink delle celle — i link video nascosti dietro testi come `bunny` e gli URL dei deck dietro le etichette delle slide |
| `BUNNY_STORAGE_ZONE` | Facoltativa: nome della Bunny Storage Zone su cui ricaricare i deck verificati; assente (o incompleta la tripletta), il report mantiene gli URL sorgente |
| `BUNNY_STORAGE_API_KEY` | Chiave Read+Write della storage zone (non la chiave dell'account) |
| `BUNNY_STORAGE_PULL_HOSTNAME` | Hostname CDN della pull zone collegata alla zona, es. `academy-decks.b-cdn.net`; è l'host che finisce negli URL dei materiali del report |
| `BUNNY_STORAGE_REGION` | Regione della zona: `de` (predefinito, Falkenstein), `ny`, `la`, `sg`, `syd`, `uk`, `se`, `br`, `jh` |

`BUNNY_SAMPLE_VIDEO_URL` serve soltanto al test live. `RUN_LIVE_BUNNY=1` abilita esplicitamente quel test a pagamento. `TEMP_ROOT`, opzionale, seleziona una directory temporanea già esistente e scrivibile dal processo. Non inserire `.env` nel repository, nell'immagine o nei report diagnostici; il file di esempio contiene soltanto segnaposto.

La chiave Stream va creata o recuperata nelle impostazioni della **libreria** configurata e deve essere limitata alla lettura. Non usare una chiave di un'altra libreria né una chiave con permessi di scrittura. `OPENAI_API_KEY` richiede fatturazione o crediti per la **OpenAI API**: è separata da un eventuale abbonamento ChatGPT. Anche AssemblyAI usa un account API separato, pay-as-you-go; non serve un piano mensile obbligatorio. Senza `ASSEMBLYAI_API_KEY` catalogo e report storici restano disponibili, ma un nuovo report fallisce chiuso con il messaggio di verifica audio prima di avviare trascrizione o analisi OpenAI a pagamento.

La chiave Bunny deve avere permessi di sola lettura. L'applicazione legge i metadati con GET e il media CDN; non crea, modifica o elimina video, capitoli, sottotitoli o configurazioni Bunny. In modalità veloce AssemblyAI legge direttamente l'MP4 alla risoluzione minima disponibile, mentre l'app salva temporaneamente soltanto i frame candidati delle slide. La trascrizione AssemblyAI viene cancellata tramite API prima che il report sia rilasciato. Le immagini selezionate e il solo campione testuale compatto vengono inviati a OpenAI con `store=False`. Le policy e i tempi tecnici di cancellazione lato fornitori restano quelli dei rispettivi account e contratti.

## Catalogo, selezione e utilizzo

Dopo il login si apre direttamente il catalogo della sola libreria Bunny configurata. Ogni video è un lavoro autonomo: non viene abbinato a corsi, inventari o fogli esterni. La dashboard rilegge il catalogo a ogni apertura o aggiornamento e non lo salva: il limite operativo è **10.000 video per caricamento**; una libreria più grande richiede paginazione lato server prima di poter proseguire. Ricerca, filtri e selezione agiscono nel browser sui dati visualizzati. Il controllo `Schede / Elenco` cambia la densità del catalogo e ricorda la preferenza nel browser.

Selezionare da **1 a 50 video** e scegliere `Analizza selezionati`. Il server rilegge i metadati Bunny e mostra durata e stima totale prima di creare alcun lavoro. Solo `Conferma e genera report` accoda un lavoro indipendente per ogni video; più di 50 report richiedono gruppi successivi. La conferma firmata scade dopo dieci minuti. Un errore di un video non deve bloccare gli altri lavori del gruppo.

Sono accettati link HTTPS nei seguenti formati, esclusivamente per la libreria e il CDN configurati:

- `https://iframe.mediadelivery.net/embed/LIBRARY_ID/VIDEO_UUID`
- `https://player.mediadelivery.net/embed/LIBRARY_ID/VIDEO_UUID`
- `https://CDN_HOSTNAME/VIDEO_UUID/playlist.m3u8`

Eventuali query del link incollato vengono scartate. Il playback viene ricostruito usando la configurazione del server e, se presente, la chiave token CDN. Link dashboard, URL arbitrari e video di altre librerie non sono supportati. DRM e restrizioni referrer che impediscono l'accesso server sono esclusi: non vengono aggirati. La compatibilità dei token e delle protezioni della propria libreria deve essere verificata con il test live.

Il form autenticato mostra prima il titolo originale Bunny, la durata e la stima del costo; soltanto la conferma accoda il lavoro. Il worker rilegge i metadati e verifica nuovamente la disponibilità: gli stati Bunny 3 (Finished) e 4 (Resolution finished) sono accettati solo con risoluzioni disponibili. Descrizione, capitoli e didascalie esistenti sono usati come evidenze nell’analisi.

Con AssemblyAI attivo viene usato l'MP4 Bunny alla risoluzione minima disponibile, al massimo 720p: AssemblyAI ne legge l'audio e FFmpeg rileva localmente i cambi visivi e le pause senza scrivere file audio. Il percorso HLS legacy può ancora validare ed estrarre media, ma non produce un nuovo report perché non raccoglie nello stesso passaggio la misura dei silenzi necessaria ai confini verificati. Se mancano varianti ridotte, abilitare una risoluzione SD nella codifica Bunny. Le risorse devono restare nella directory autorizzata sul CDN configurato. Il ritorno a una slide precedente conserva il nuovo timestamp.

I limiti configurabili sono `MEDIA_RUNTIME_SECONDS=21600` (sei ore per estrazione, incluse le operazioni locali), `MEDIA_INACTIVITY_SECONDS=120` e `MEDIA_MAX_WORKSPACE_BYTES=2000000000` (2 GB). Un controllo circa ogni 100 ms interrompe e raccoglie il processo quando supera uno dei limiti; brevi superamenti della soglia di spazio fra due controlli sono possibili. I file vengono rimossi prima del lavoro successivo. Gli errori temporanei Bunny hanno al massimo tre tentativi; un errore di accesso al playback protetto rigenera il token una sola volta, ripartendo in una directory pulita. Questa ripartenza può rileggere media già scaricato nel tentativo fallito.

Il target operativo è costituito da video di **1-4 ore**; i video brevi sono accettati per il collaudo e quelli oltre quattro ore vengono rifiutati. Serve audio decodificabile. Si elabora un video alla volta; gli altri rimangono in coda. La pagina del singolo lavoro e quella del gruppo aggiornano automaticamente stato e barre ogni tre secondi; `Aggiorna ora` resta disponibile come controllo manuale. Ogni report può essere scaricato come **JSON (.json)**, **Markdown (.md)** o **testo (.txt)**, copiato oppure stampato/salvato come PDF dal browser.

Il JSON usa il contratto intermedio Academy v1.1: la busta mantiene `"versione": 1` e contiene un solo video con `chiave: "v1"` e `ordine: 1`. Include GUID Bunny, titoli, lingua, sinossi, relatori con evidenze, blocchi fattuali di parlato (`blocchi_parlato`), capitoli in `interventi`, slide, materiali verificati e `costo_stimato` in USD. Gli identificativi cronologici sono stabili (`v1-b001`, `v1-i001`, ecc.). `inizio`, `fine` e gli indizi visivi sono espressi in `h:mm:ss`.

La durata autorevole proviene soltanto da `length` di Bunny, normalizzato per difetto al secondo intero. Durate FFmpeg o del provider di trascrizione non la sostituiscono; blocchi, capitoli e slide non possono superarla. Ogni blocco conserva il parlato fattuale anche quando è lungo: i suoi capitoli lo coprono interamente, senza buchi o sovrapposizioni. Pause e logistica fra blocchi restano segmenti espliciti. Un cambio slide può suggerire una chiusura tematica; il taglio resta verificato sulle parole e sul silenzio, con motivazione editoriale in `confine_inizio` e prova compatta `CONFINE`.

I capitoli didattici preferiscono 8–10 minuti, puntano a 8–15 e tollerano fino a 20; un'unità indivisibile rimane lunga con un avviso. Solo il primo capitolo `intervento` idoneo di 480–900 secondi ha `accesso: "pubblico"`; gli altri sono `iscritti`. Se manca un candidato idoneo, il nuovo export segnala la necessità di rianalisi. Il tool propone capitoli, ma non crea corsi, moduli, lezioni o quiz nell'Academy.

I download JSON/Markdown/TXT dei nuovi report `analysis_profile: 2` usano esclusivamente i dati persistiti: non rileggono Bunny o l'inventario, non scaricano nuovamente materiali e non ricalcolano l'analisi. I report storici di profilo 1 restano leggibili e scaricabili in Markdown/TXT; il loro JSON restituisce `409 Rianalisi necessaria per il formato granulare`. Un profilo 2 malformato restituisce `409 Report granulare non valido: rianalisi necessaria` in tutti e tre i formati. Gli avvisi validi restano visibili negli export.

Nomi e ruoli richiedono evidenze testuali o visive; in caso di dubbio compaiono etichette generiche e incertezze. L'identità dei relatori non viene dedotta biometricamente dalla voce. Il report richiede revisione umana prima dell'uso editoriale.

Le persone riconosciute usano nome e slug del Registro. In particolare la grafia canonica è esattamente `Furio D'Andrea` (apostrofo ASCII), con slug `furio-dandrea`, anche nei riferimenti di blocchi, capitoli, sintesi e materiali (`Slide · Furio D'Andrea`). Varianti di apostrofo, maiuscole e onorifici convergono su quella voce; la qualifica resta nel ruolo. Un cognome senza nome viene unito solo quando la corrispondenza è univoca.

## Materiali verificati e limiti

Il lavoro può leggere URL reali dall'inventario, hyperlink Google Sheets quando il service account è configurato, sorgenti curate per GUID e file PDF/PPTX realmente disponibili. Un'etichetta come `Slide relatore` non diventa un file. Se `MATERIAL_LIBRARY_DIR` è configurata, l'etichetta viene però abbinata ai deck scaricati a mano in quella cartella: il confronto usa i token del nome (cognomi, titoli, numeri) dopo aver normalizzato accenti e apostrofi, richiede un unico miglior candidato e fallisce chiuso sui pareggi. Il materiale abbinato conserva il titolo canonico dell'etichetta e il JSON v1.1 esporta in `file` soltanto il nome del deck, mai il percorso locale. I PPTX vengono letti con `zipfile`/XML nell'ordine della presentazione; i PDF con la dipendenza dichiarata `pypdf>=6,<7`, inclusa in `uv.lock`. L'estrazione legge il testo: non aggiunge OCR alle pagine PDF composte soltanto da immagini.

Il recupero remoto ammette solo HTTPS sulla porta 443 e gli host esatti di `MATERIAL_ALLOWED_HOSTS=www.assoholding.it`. Rifiuta credenziali, query e frammenti negli URL, indirizzi IP e destinazioni private. Ogni redirect (massimo cinque) ripete i controlli DNS e di host; la connessione usa gli indirizzi pubblici verificati e non usa proxy, cookie o credenziali implicite. Il contenuto dei documenti non viene eseguito. Collegamenti esterni, azioni attive, macro, allegati e PDF cifrati non sono supportati.

Limiti applicativi per il trattamento dei materiali:

| Risorsa | Limite |
| --- | --- |
| Sorgenti per lavoro | 32 |
| File scaricato o locale | 50 MiB |
| Download / estrazione / abbinamento | 20 secondi per fase, in processi controllati |
| Pagine per deck | 1.000 |
| Elementi archivio PPTX | 10.000 |
| Dati decompressi PPTX o stream PDF decodificati | 100 MiB complessivi per deck |
| Singolo XML o stream PDF decodificato | 10 MiB |
| Testo estratto | 100.000 caratteri per pagina; 2.000.000 per deck |
| Processo di lavoro | 15 secondi CPU; 512 MiB di spazio di indirizzamento su Linux; limite memoria non disponibile in modo affidabile su macOS |

Il decoder PDF ammette soltanto filtri testuali gestiti e limitati (Flate, LZW, ASCII85, ASCIIHex e RunLength). Non decodifica le immagini per estrarre testo e blocca decoder esterni come JBIG2, inclusi gli stream oggetto; dati o filtri non supportati causano un errore controllato del materiale. L'abbinamento titolo/OCR–pagina richiede punteggio almeno 0,55, margine almeno 0,10 sul secondo candidato e ordine delle pagine non decrescente. Se non c'è evidenza sufficiente, `materiale` e `pagina` restano assenti.

Le verifiche aggiuntive sono avvisi e non bloccano gli export:

- `INTERVENTO_LUNGO`: capitolo didattico oltre 20 minuti conservato senza un taglio forzato.
- `SLIDE_NON_ABBINATA`: slide senza materiale e pagina sufficientemente certi.
- `ALIAS_RELATORE_AMBIGUO`: alias anagrafico non risolto in modo univoco.

`MATERIALE_NON_RAGGIUNGIBILE` segnala una sorgente assente, non accessibile o non elaborabile. Un materiale fallito non interrompe il report; cancellazione ed errori interni del programma conservano il loro comportamento di errore. Solo `RELATORE_NON_IDENTIFICATO` e `TEMPI_INCOERENTI` rendono il report `da_verificare`.

## Dati temporanei, persistenza e riavvio

Il database SQLite conserva soltanto metadati sicuri dei lavori, appartenenza ai gruppi e JSON strutturato dei report finali: blocchi, capitoli, sintesi, metadati materiali/pagine, evidenze compatte dei confini e consumo API. Non conserva deck, testo integrale dei deck, video, audio, immagini, transcript o word timing; non contiene chiavi, cookie, token o prompt. Le prove `CONFINE` conservano al massimo cinque parole per lato, senza la sequenza completa dei tempi. Il catalogo Bunny non viene memorizzato. In modalità veloce l'app scrive frame e deck temporanei; nel fallback scrive anche segmenti audio temporanei. Tutti vengono rimossi al completamento, errore o annullamento cooperativo. Il transcript resta soltanto in memoria durante la pipeline; l'artefatto AssemblyAI viene cancellato via API in uscita.

I report completati restano nell'archivio senza scadenza e sopravvivono ai riavvii quando `DATABASE_PATH` si trova sul volume persistente. L'eliminazione autenticata rimuove esclusivamente il record locale e non chiama mai Bunny. Un lavoro trovato in coda o in elaborazione all'avvio non può essere ripreso senza i file temporanei: viene marcato come fallito con l'indicazione di rilanciare l'analisi. Dopo un riavvio può essere necessario effettuare nuovamente il login.

L'annullamento attende che la fase in corso riconosca la richiesta e pulisca i file; una chiamata remota già in corso può terminare prima dell'annullamento. Durante l'arresto ordinato il worker può finire i lavori già accodati. Prima di riavviare, annullare o attendere tutti i lavori. Un arresto forzato, un crash o la perdita dell'host possono impedire la pulizia: usare storage temporaneo effimero, senza backup, e rimuovere le sole directory residue `bunny-video-*` quando nessun worker è attivo. Non promettere pulizia Python dopo SIGKILL.

## Docker e pubblicazione online

```sh
docker build -t bunny-video-report:local .
docker run --rm bunny-video-report:local ffmpeg -version
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 bunny-video-report:local
```

L'immagine include FFmpeg, template e asset statici, esegue l'app come `appuser` non root e avvia `uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --no-access-log --proxy-headers --forwarded-allow-ips 127.0.0.1,100.0.0.0/8`. Gli header del proxy sono accettati solo da loopback e dalla rete proxy Railway configurata, così l'origine HTTPS viene verificata correttamente anche dietro il proxy. I segreti entrano soltanto a runtime attraverso `--env-file` o il secret manager del servizio scelto; non usare build argument o `ENV` nel Dockerfile per le credenziali. La directory di build è filtrata da `.dockerignore`.

La distribuzione prevista è Railway. Per pubblicare, configurare il dominio con TLS valido e usare sempre **HTTPS** per l'accesso remoto, così il cookie di sessione viene emesso con `Secure`. Impedire l'accesso pubblico diretto alla porta del container e inoltrare gli header proxy soltanto da proxy fidati. Disabilitare anche sul proxy il logging di cookie, body, query e URL completi sensibili.

Su Railway aggiungere al servizio un volume persistente montato in `/data` e impostare `DATABASE_PATH=/data/bunny-video-report.sqlite3`. Il file e i relativi file SQLite `-wal`/`-shm` non devono entrare nel repository o nell'immagine. Questo non richiede un nuovo abbonamento API: usa soltanto una quantità minima dello storage Railway già associato al servizio.

Usare **esattamente una replica e un processo Uvicorn**, senza `--workers` multipli, autoscaling o rolling overlap: il database SQLite e il singolo worker non autorizzano l'elaborazione concorrente fra repliche. Configurare il provider senza sospensione automatica durante i lavori, con risorse CPU/RAM e spazio temporaneo adeguati ai video lunghi.

`GET /healthz`, `/login` e gli asset statici sono pubblici; dashboard, batch, lavori, download e API richiedono una sessione. L'healthcheck prova che il processo HTTP risponde, non che Bunny/OpenAI siano raggiungibili o che le credenziali siano corrette. Il Dockerfile include questa verifica di salute.

## Costi e collaudo

Le tariffe seguenti sono riferimenti indicativi, non un preventivo né una garanzia di fatturazione: piani, valute, tassazione, regioni, modelli e condizioni commerciali possono cambiare. Prima di impegnare budget, verificare sempre le pagine ufficiali e la fattura del proprio account.

- **Bunny Stream:** richiede un account Bunny e una libreria che contenga i video. Il minimo dell'account è indicativamente **$1/mese**; lo standard encoding è incluso. Come riferimento, lo storage Stream a Francoforte è **$0,01/GB** e la CDN standard in Europa/Nord America è **$0,01/GB**. L'app non usa Bunny AI Transcription. Consultare [prezzi Bunny](https://bunny.net/pricing).
- **AssemblyAI (modalità veloce consigliata):** account API pay-as-you-go, senza piano mensile obbligatorio. Universal-3.5 Pro costa **$0,21/ora**, diarizzazione **$0,02/ora** e Speaker Identification low effort **$0,02/ora**: totale applicativo iniziale **$0,25/ora di video**. L'app usa l'endpoint UE per impostazione predefinita e cancella ogni transcript al termine. Consultare [prezzi AssemblyAI](https://www.assemblyai.com/pricing/) e [data retention](https://www.assemblyai.com/docs/data-retention-and-model-training).
- **OpenAI API:** fatturazione e crediti API sono separati da ChatGPT. In modalità veloce OpenAI classifica i frame candidati e riceve una sola richiesta testuale compatta per sinossi e relatori. Il fallback senza AssemblyAI usa anche `gpt-4o-transcribe-diarize` a blocchi. Consultare [prezzi OpenAI API](https://developers.openai.com/api/docs/pricing).
- **Railway:** il Trial assegna indicativamente **$5 una tantum** per al massimo **30 giorni**; il piano Free offre **$1 di credito/mese**. Hobby costa **$5/mese**, include $5 di risorse e il consumo eccedente viene addebitato. Consultare [piani Railway](https://docs.railway.com/pricing/plans) e [prezzi Railway](https://railway.com/pricing).

La stima iniziale della modalità veloce è **$0,29-$0,43 per ora di video**, più il piccolo traffico Bunny; il fallback OpenAI resta circa **$0,40-$0,70/ora**. Va ricalibrata sul primo video reale usando usage API e traffico Bunny. Non è un preventivo: modello, numero di frame, retry, contenuti e tariffe possono cambiare il costo. Il traffico mostrato è una stima dai byte dei pacchetti di input FFmpeg con margine del 20%, non traffico di rete misurato. Le costanti iniziali sono in `app/costs.py`.

Il report conserva separatamente titolo Bunny e titolo didattico suggerito. Nei download e nella pagina compaiono i contatori numerici restituiti da trascrizione e Responses per ogni tentativo, inclusi batch, retry e riparazioni quando disponibili. I contatori mancanti sono dichiarati; la stima usa quelli disponibili e aggiunge una quota da durata quando non è possibile calcolare un tentativo. Non sostituisce la fattura. Il traffico di un tentativo FFmpeg fallito non è misurabile dalla sintesi finale ed è escluso dalla stima di banda.

Suite offline, senza credenziali reali né accesso ai servizi (fornire FFmpeg e FFprobe nel `PATH`):

```sh
PATH='/percorso/a/ffmpeg:'"$PATH" .venv/bin/python -m pytest -m 'not live' -q
```

Lo smoke `tests/test_end_to_end.py` genera un video sintetico con FFmpeg, usa confini Bunny/OpenAI finti e attraversa form, worker, schema, download e pulizia temporanea entro 15 secondi. Verifica anche autenticazione, healthcheck e persistenza del report dopo il riavvio. FFmpeg e FFprobe devono essere disponibili nel `PATH`.

## Analisi rapida e verifica prima della pubblicazione

Con AssemblyAI l'intero video viene diarizzato in un solo lavoro, così una stessa voce mantiene un'identità globale. L'app analizza poi l'intera sequenza testuale in finestre cronologiche limitate, senza ridurla a un solo campione, e consolida sinossi e identità in un payload separato e limitato. Tutti i cambi slide classificati restano nel report locale. Senza AssemblyAI resta disponibile la stessa analisi a finestre, alimentata dalla trascrizione OpenAI a blocchi.

Viene usato un database SQLite esclusivamente per report testuali e stato dei lavori; non viene introdotto storage video o del transcript. Per la modalità veloce servono Bunny Stream read-only, OpenAI API, AssemblyAI API e Railway. Non è richiesto un nuovo abbonamento mensile: AssemblyAI è pay-as-you-go, ma richiede account, chiave e credito/fatturazione propri. Credito e limiti API restano distinti.

Prima di pubblicare, eseguire la suite disponibile nell'ambiente locale privo di FFmpeg/FFprobe:

```sh
.venv/bin/pytest -q -m 'not live' --ignore=tests/test_media.py -k 'not test_form_to_report_with_real_ffmpeg_and_ephemeral_cleanup'
.venv/bin/python -m compileall -q app tests
git diff --check
```

I test esclusi che richiedono FFmpeg e FFprobe vanno eseguiti nell'immagine Docker/Railway, dove i due programmi sono installati. L'immagine di runtime non include test o `pytest`; senza modificarla, costruirla e montare il checkout in sola lettura, creando le dipendenze di test soltanto nel venv effimero del container:

```sh
docker build -t bunny-video-report:local .
docker run --rm -v "$(pwd)":/src:ro -w /src bunny-video-report:local sh -c \
  'python -m venv --system-site-packages /tmp/test-venv && \
   /tmp/test-venv/bin/pip install --no-cache-dir ".[test]" && \
   PYTHONDONTWRITEBYTECODE=1 /tmp/test-venv/bin/python -m pytest -q -m "not live" -p no:cacheprovider'
```

Con le variabili runtime Railway già configurate, la prova live esplicita usa 96 segmenti sintetici da un minuto (1 ora e 36 minuti), non chiama Bunny e non legge media reali. Richiede **entrambi** `RUN_LIVE_SYNTHETIC_ANALYSIS=1` e `OPENAI_API_KEY`; la sola chiave non la abilita:

```sh
RUN_LIVE_SYNTHETIC_ANALYSIS=1 railway run .venv/bin/pytest -q -m live tests/test_live_diagnostics.py -k chunked_long_report
```

La prova percorre finestre e consolidamento reali senza immagini, con una parola temporizzata per ogni token del testo sintetico. Verifica limiti di payload, profilo 2, copertura dei blocchi, capitoli, confini e limite temporale Bunny; non dipende dalla densità delle finestre o dalla lunghezza della prosa generata. Stampa solo stato, richieste, token, durata, tempo trascorso e conteggi del risultato. Non stampa chiavi, payload, trascrizione, testo del modello o corpi di risposta. Non rilanciare il video reale “Governance delle holding e conferimenti a realizzo controllato” senza conferma esplicita: trascrizione e analisi generano un nuovo costo API.

Il test live è **opt-in e a pagamento**. Compilare in `.env` le variabili obbligatorie e `BUNNY_SAMPLE_VIDEO_URL` con un video autorizzato; fornire anche la chiave token se necessaria. Impostare `RUN_LIVE_BUNNY=1` nel file e caricarlo come dati, senza eseguirlo come script shell:

```sh
.venv/bin/python -c 'from dotenv import load_dotenv; load_dotenv(".env"); import pytest; raise SystemExit(pytest.main(["tests/test_live_bunny.py", "-m", "live", "-q", "--tb=no"]))'
```

Se l'opt-in o una variabile obbligatoria (compresa `ASSEMBLYAI_API_KEY`) manca, il test viene saltato. Il collaudo usa un database e uno spazio temporaneo dedicati: verifica la durata letta da Bunny, la copertura fattuale, gli ID stabili su export ripetuti, un'anteprima pubblica idonea, l'assenza di dati transitori e i download autenticati JSON/Markdown/TXT. Un report malformato fa fallire il test. Questo singolo lavoro non prova la ripetibilità fra due analisi complete: quel confronto richiede due collaudi reali autorizzati sulla stessa revisione. Non usare `--showlocals`, debugger o registrazioni HTTP con credenziali reali. Il test non stampa URL, transcript, report o segreti e non effettua modifiche su Bunny. Lasciare `RUN_LIVE_BUNNY=0` al termine.

Prima della messa online: eseguire la build Docker e il controllo FFmpeg sopra; provare prima un video breve e poi uno di almeno un'ora; verificare almeno dieci timestamp fra interventi, capitoli e slide; controllare presentatore, assenza del moderatore e due relatori. Provare annullamento e riavvio, aprire MD/TXT e controllare visivamente la stampa PDF. Registrare costo e durata del collaudo senza conservare contenuti sensibili nei log.

Lo smoke test offline gira in un processo isolato con limite esterno di dodici secondi e fino a due secondi per arresto/raccolta dei processi. Il supervisore elimina i media anche se il worker o il suo shutdown restano bloccati. Il controllo JavaScript è eseguibile con `node tests/job_ui.cjs` e include il ripristino della pagina dalla cache del browser.

Nel primo ambiente di sviluppo non è disponibile Docker/Podman/Buildah: sono verificabili la suite offline e il contenuto della wheel, ma build ed esecuzione del container richiedono un host con runtime. I test live, i costi e la precisione su video reali restano da verificare con le credenziali del team.
