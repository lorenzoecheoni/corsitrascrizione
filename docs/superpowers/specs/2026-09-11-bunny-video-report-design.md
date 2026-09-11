# Bunny Video Report — specifica di progettazione

## Obiettivo

Realizzare un piccolo strumento web interno, indipendente da AssoCrowd e dagli altri prodotti del workspace, che riceve il link di un video Bunny Stream e restituisce un report testuale utile a creare una scheda corso per un'Academy.

Il tool non modifica la libreria Bunny e non conserva video, audio o fotogrammi. I media vengono elaborati esclusivamente in una directory temporanea e rimossi al termine, anche in caso di errore. Il report resta disponibile solo nella memoria del processo fino al riavvio dell'applicazione e può essere copiato o scaricato dall'utente.

## Utenti e perimetro

- Uso interno da parte di un piccolo team.
- Video Bunny Stream da 1 a 4 ore.
- Video in italiano o in altre lingue rilevabili dal servizio di trascrizione.
- Uno o più relatori, con o senza moderatore/presentatore.
- Relazioni congiunte, interviste e passaggi di parola.
- Link Bunny pubblici o protetti con la normale token authentication.
- Media Cage DRM e flussi che non consentono accesso legittimo al media sono esclusi dalla prima versione.
- Nessuna creazione di corsi, quiz o contenuti direttamente dentro un LMS.

## Risultato prodotto

Il report contiene:

1. titolo Bunny e titolo didattico suggerito;
2. durata totale;
3. lingua o lingue rilevate;
4. sinossi breve pronta per una scheda corso;
5. descrizione estesa;
6. pubblico consigliato e prerequisiti deducibili, marcati come suggerimenti;
7. obiettivi formativi e risultati attesi;
8. elenco dei relatori, con possibile nome, ruolo e livello di affidabilità;
9. cronologia degli interventi per relatore;
10. capitoli didattici suggeriti con timestamp, titolo e sintesi;
11. sequenza dei cambi slide rilevati, con timestamp, titolo o contenuto principale;
12. argomenti principali, parole chiave e takeaway;
13. avvertenze sui dati incerti;
14. stima del costo dell'elaborazione.

Il nome di un relatore viene ricavato solo da evidenze presenti nel contenuto: introduzioni pronunciate, sottopancia, slide, titolo o metadati. Non viene eseguito riconoscimento biometrico. Quando l'identità non è sufficientemente supportata, il report usa etichette come «Relatore 1».

## Esperienza utente

La home presenta un campo per il link Bunny e un pulsante **Analizza video**. Dopo la convalida mostra titolo, durata e costo stimato, quindi inserisce il lavoro in una coda a singola esecuzione.

La pagina del lavoro mostra questi stati:

- in coda;
- lettura metadati;
- analisi audio;
- riconoscimento relatori;
- rilevamento slide;
- generazione report;
- completato oppure errore.

L'utente può chiudere e riaprire la pagina del lavoro finché il processo applicativo resta attivo. Al termine può copiare il report, scaricarlo come Markdown o testo semplice e usare la stampa del browser per produrre un PDF. La prima versione non genera DOCX e non mantiene uno storico permanente.

## Architettura raccomandata

### Applicazione

- Python 3.12 o successivo.
- FastAPI per API e pagine web.
- HTML, CSS e JavaScript essenziali, senza framework frontend.
- Un esecutore in background con un solo worker e una coda in memoria.
- Registro dei lavori e report in memoria; nessun database.
- Dockerfile e avvio locale documentato.

La scelta di un solo processo e un solo worker riduce dipendenze e complessità. Non vengono introdotti Redis, Celery, code gestite o storage esterno. Un riavvio interrompe i lavori e cancella i report non scaricati: è un compromesso esplicito della versione minima.

### Servizi esterni

1. **Bunny Stream API**, in sola lettura, per metadati e accesso autorizzato al flusso.
2. **OpenAI Audio Transcriptions API** con `gpt-4o-transcribe-diarize` per testo, timestamp e distinzione delle voci.
3. **OpenAI Responses API** con `gpt-5.6-luna`, `store: false` e output strutturato per interpretare fotogrammi, associare identità e ruoli, proporre capitoli e generare il report.
4. **FFmpeg**, eseguito localmente dal tool, per decodifica, compressione audio e rilevamento iniziale dei cambi scena.

Non viene usato Bunny Transcribe AI: scriverebbe didascalie e metadati nella libreria, non offre da solo l'analisi richiesta e costa attualmente 0,10 USD per minuto.

## Configurazione

Le credenziali vengono lette soltanto da variabili d'ambiente:

```text
BUNNY_LIBRARY_ID
BUNNY_STREAM_API_KEY
BUNNY_CDN_HOSTNAME
BUNNY_TOKEN_AUTH_KEY       # opzionale, solo con token authentication
OPENAI_API_KEY
APP_PASSWORD               # password condivisa per l'accesso interno
```

La chiave Bunny deve essere read-only quando la configurazione dell'account lo consente. Nessuna chiave compare nel codice, nei log o nel report.

## Flusso di elaborazione

### 1. Validazione e metadati

Il parser accetta gli URL Bunny Stream previsti, compresi gli embed URL, estrae l'identificativo della libreria e il GUID del video e rifiuta host arbitrari. L'app verifica che la libreria estratta coincida con `BUNNY_LIBRARY_ID`.

Il client Bunny richiama soltanto endpoint GET. Recupera almeno titolo, durata, stato di encoding, risoluzioni, descrizione, didascalie e capitoli eventualmente già presenti.

### 2. Accesso al flusso

L'app costruisce o recupera la playlist HLS dal solo hostname CDN configurato. Se la libreria usa token authentication, genera un token a scadenza breve con la chiave configurata. La prima versione non tenta di aggirare DRM, restrizioni di referrer non autorizzate o controlli di accesso.

### 3. Estrazione temporanea

Un processo FFmpeg legge una variante a risoluzione ridotta, sufficiente per distinguere struttura e testo delle slide. Dal medesimo flusso produce:

- audio mono compresso per la trascrizione;
- fotogrammi candidati nei cambi scena;
- timestamp dei candidati.

I file sono creati sotto una directory temporanea univoca. Il numero di fotogrammi viene ridotto tramite confronto percettivo e distanza temporale minima, eliminando duplicati, dissolvenze e variazioni minori.

### 4. Trascrizione e relatori

L'audio viene suddiviso in blocchi inferiori al limite API di 25 MB, preferendo pause naturali e mantenendo gli offset temporali. Ogni blocco usa `response_format="diarized_json"` e `chunking_strategy="auto"`.

Le etichette vocali vengono riconciliate tra blocchi usando, quando disponibili, brevi riferimenti audio dei relatori già rilevati, entro il limite API di quattro riferimenti noti. Se i relatori sono più di quattro o il collegamento tra blocchi non è affidabile, il sistema conserva etichette distinte e delega al passaggio testuale la sola associazione supportata da evidenze.

### 5. Rilevamento delle slide

Il rilevamento ha due livelli:

1. FFmpeg e confronto percettivo individuano localmente i cambi visivi senza costi AI;
2. `gpt-5.6-luna` classifica in piccoli batch i fotogrammi candidati, distinguendo un vero cambio slide da cambio inquadratura, webcam, animazione o transizione.

Per ogni cambio confermato vengono prodotti timestamp, titolo leggibile e breve descrizione. I timestamp sono indicativi e possono avere alcuni secondi di tolleranza.

### 6. Sintesi finale

La trascrizione diarizzata, i metadati Bunny e l'analisi visiva vengono passati al modello in forma compatta. Il modello restituisce JSON conforme a uno schema rigido. Il backend valida lo schema, calcola il costo stimato dai dati di utilizzo disponibili e rende il report Markdown.

### 7. Pulizia

Un blocco di pulizia eseguito sempre rimuove directory temporanea, audio e fotogrammi dopo il completamento o l'errore. I buffer non necessari vengono liberati prima della generazione finale del report.

## Sicurezza e riservatezza

- Autenticazione interna con password condivisa configurata via ambiente.
- HTTPS obbligatorio quando il servizio è esposto fuori dalla rete locale.
- Allowlist degli host Bunny e blocco degli URL arbitrari per prevenire SSRF.
- Timeout, limite massimo di quattro ore e limite alla dimensione temporanea.
- Segreti filtrati dai log e dagli errori mostrati all'utente.
- Nessuna chiamata Bunny che modifica video, didascalie, capitoli o metadati.
- `store: false` per le richieste Responses API.
- La cancellazione locale non equivale a Zero Data Retention del fornitore: il trattamento dei dati da parte di OpenAI segue le condizioni del progetto API configurato. Se è necessario ZDR, deve essere abilitato e verificato nell'account OpenAI.

## Costi stimati

Ai prezzi pubblici verificati l'11 settembre 2026:

- Bunny Stream: da 0,005 USD/GB per il traffico Volume; l'analisi a risoluzione ridotta dovrebbe incidere circa 0,002–0,01 USD per ora di video.
- `gpt-4o-transcribe-diarize`: tariffazione a token, con 2,50 USD per milione di token audio in input e 10 USD per milione di token in output. Per il budget iniziale si assume circa 0,36–0,50 USD per ora; il valore va calibrato sul primo video reale.
- `gpt-5.6-luna`: 0,20 USD per milione di token in input e 1,20 USD per milione di token in output. Con fotogrammi preselezionati e report compatto si stima 0,01–0,08 USD per ora.
- Totale prudente: 0,40–0,70 USD per ogni ora di video, escluso l'eventuale costo fisso del server.

L'interfaccia mostra sia la stima preventiva sia l'utilizzo effettivamente restituito dalle API. Il valore monetario resta una stima applicativa e non sostituisce la fattura dei fornitori.

L'esecuzione su un computer già disponibile non comporta costi di hosting aggiuntivi. Un eventuale piccolo VPS costituisce un costo mensile separato e non è necessario per il primo rilascio.

## Errori e recupero

- URL non valido: errore immediato prima di contattare servizi esterni.
- Video assente, non pronto o non appartenente alla libreria: messaggio specifico.
- Token scaduto: rigenerazione una volta, poi errore esplicito.
- Errore temporaneo Bunny o OpenAI: massimo tre tentativi con attesa progressiva.
- Fallimento di un blocco audio o visivo: il lavoro fallisce senza produrre un report potenzialmente incompleto.
- Impossibilità di identificare un relatore: non è un errore; viene usata un'etichetta generica con bassa affidabilità.
- Assenza di slide: il report dichiara che non sono stati rilevati cambi slide.
- Errore o annullamento: pulizia obbligatoria dei file temporanei.

## Struttura logica del codice

- `app/main.py`: applicazione FastAPI e composizione delle dipendenze.
- `app/config.py`: variabili d'ambiente e validazione.
- `app/jobs.py`: coda, stati e avanzamento dei lavori.
- `app/bunny.py`: parsing link, metadati e accesso HLS.
- `app/media.py`: FFmpeg, chunk audio, scene detection e pulizia.
- `app/transcription.py`: client di diarizzazione e unione dei blocchi.
- `app/analysis.py`: analisi visiva, risoluzione relatori e report strutturato.
- `app/report.py`: rendering Markdown e testo semplice.
- `app/templates/` e `app/static/`: interfaccia minima.
- `tests/`: test automatici.

Ogni modulo espone interfacce piccole ed è testabile con client finti, senza richiedere credenziali reali.

## Verifica e criteri di accettazione

### Test automatici

- parsing di URL Bunny validi e rifiuto di host estranei;
- accesso ai metadati con risposte Bunny simulate;
- firma dei link protetti con esempi deterministici;
- divisione audio entro 25 MB e corretta gestione degli offset;
- fusione dei segmenti diarizzati;
- deduplicazione dei fotogrammi;
- validazione dello schema del report;
- download Markdown e testo;
- pulizia dei file temporanei dopo successo, errore e annullamento;
- assenza di segreti nei log.

### Prova integrata

Con credenziali fornite tramite `.env`, un video Bunny reale deve:

1. essere riconosciuto dal link;
2. mostrare titolo e durata corretti;
3. completare l'analisi senza modificare la libreria;
4. distinguere almeno i principali passaggi di voce;
5. associare nomi solo quando esistono evidenze;
6. produrre capitoli e una sinossi utilizzabili;
7. indicare i cambi slide con timestamp ragionevoli;
8. consentire copia e download del report;
9. lasciare vuota la directory temporanea;
10. mostrare costo stimato e utilizzo API.

La qualità semantica viene verificata manualmente sul primo video e usata per tarare soglia di cambio scena, numero massimo di fotogrammi e prompt. Non viene promessa identificazione certa di persone non nominate né precisione al fotogramma dei cambi slide.

## Fonti tecniche e di prezzo

- Bunny Get Video: https://docs.bunny.net/reference/video_getvideo
- Bunny Stream pricing: https://bunny.net/pricing/stream/
- Bunny Transcribe AI: https://bunny.net/stream/transcribe-ai/
- OpenAI file transcription: https://developers.openai.com/api/docs/guides/speech-to-text
- OpenAI diarization model: https://developers.openai.com/api/docs/models/gpt-4o-transcribe-diarize
- OpenAI GPT-5.6 Luna: https://developers.openai.com/api/docs/models/gpt-5.6-luna
- OpenAI API pricing: https://developers.openai.com/api/docs/pricing

