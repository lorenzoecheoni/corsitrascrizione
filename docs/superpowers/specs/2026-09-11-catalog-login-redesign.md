# Catalogo Bunny, login web e selezione multipla

## Obiettivo

Trasformare Bunny Video Report in una dashboard interna utilizzabile anche nel browser integrato. L'applicazione deve leggere l'intero catalogo della singola libreria Bunny configurata, permettere al team di scegliere uno o più video e avviare l'analisi AI soltanto dopo una conferma esplicita del costo complessivo. Il tool non crea, modifica o elimina contenuti su Bunny e non conserva video, audio o frame oltre il tempo necessario alla singola elaborazione.

## Ambito

La nuova versione comprende:

- un login HTML al posto della Basic Auth del browser;
- una dashboard responsive con catalogo Bunny, ricerca, filtri e selezione multipla;
- lettura paginata di tutti i video della libreria configurata;
- anteprima aggregata di durata e costo per i soli video selezionati;
- accodamento seriale dei video confermati e panoramica dei lavori correnti;
- rinnovo visivo delle pagine di catalogo, conferma e report;
- aggiornamento della documentazione operativa e dei costi esterni richiesti.

Restano esclusi database, salvataggio permanente dei report, analisi automatica non confermata, modifica dei metadati Bunny e accesso a più librerie.

## Esperienza utente

### Accesso

La rotta `/login` mostra un form normale con utente fisso `team` e password `APP_PASSWORD`. Dopo un accesso valido il server rilascia un cookie di sessione firmato, `HttpOnly` e `SameSite=Strict`, con scadenza di dodici ore; in produzione HTTPS il cookie è anche `Secure`, mentre lo sviluppo locale HTTP è ammesso soltanto su loopback. La firma usa un segreto casuale in memoria: un riavvio invalida le sessioni esistenti, coerentemente con la natura effimera dell'applicazione. Il logout è un'azione POST protetta da CSRF.

Le richieste HTML non autenticate vengono reindirizzate al login; le rotte JSON continuano a restituire `401`. `/healthz`, la pagina di login e gli asset necessari al login rimangono pubblici. Il confronto della password resta constant-time e i messaggi non distinguono utente e password errati.

### Dashboard

Dopo il login l'utente vede subito la superficie di lavoro, non una pagina promozionale. La direzione visiva è un pannello editoriale contemporaneo: fondo blu-notte, superfici chiare ad alto contrasto, accento corallo, tipografia leggibile e una testata compatta con stato della libreria. Non servono immagini decorative; miniature e dati dei video sono il contenuto visivo principale.

La dashboard mostra:

- totale dei video rilevati e durata complessiva;
- ricerca locale per titolo e descrizione;
- filtri per stato Bunny e collezione, quando disponibile;
- schede o righe responsive con miniatura, titolo, durata, data di caricamento, stato, descrizione breve e collezione;
- selezione individuale, seleziona visibili e azzera selezione;
- barra persistente con numero di video selezionati, durata e azione `Analizza selezionati`;
- una sezione lavori recenti, letta dalla memoria del processo.

Il catalogo viene recuperato da Bunny a ogni apertura o aggiornamento manuale e non viene salvato. Per evitare richieste o memoria senza limiti, il client usa pagine da 100 elementi, segue i contatori restituiti da Bunny, rifiuta risposte incoerenti e applica un limite operativo documentato di 10.000 video per caricamento. Se la libreria supera il limite, la pagina segnala che occorre introdurre paginazione server-side prima di proseguire.

### Selezione e conferma

`Analizza selezionati` invia soltanto gli UUID scelti. Il server accetta da uno a cinquanta video per conferma, li valida, rilegge da Bunny i metadati correnti e costruisce una conferma firmata e con scadenza breve. La pagina di riepilogo mostra ogni video, la durata totale, il costo stimato minimo/massimo e il numero di lavori che verranno accodati. Se servono più di cinquanta report, l'utente può creare ulteriori gruppi dopo la prima conferma.

Solo `Conferma e genera report` crea i lavori. Ogni video diventa un `JobRecord` indipendente e il worker esistente li elabora uno alla volta. Dopo la conferma, una pagina di coda mostra tutti i lavori creati con stato, avanzamento e collegamento al report. Un errore su un video non blocca gli altri.

## Integrazione Bunny

`BunnyClient.list_videos()` usa esclusivamente `GET https://video.bunnycdn.com/library/{libraryId}/videos` con header `AccessKey`. Parametri di pagina, numero di elementi e ricerca sono costruiti dall'applicazione, mai copiati da URL esterni. Redirect, proxy d'ambiente e corpi di errore non vengono inoltrati o registrati.

Ogni elemento del catalogo viene validato in un modello dedicato. Campi malformati causano un errore applicativo sicuro; valori opzionali mancanti ricevono default espliciti. La miniatura viene costruita esclusivamente dal CDN configurato, dall'UUID validato e dal nome file Bunny validato. Se la libreria richiede token authentication, il server genera un URL directory-scoped e temporaneo con la chiave già prevista; la chiave non raggiunge mai il browser.

Le credenziali necessarie restano `BUNNY_LIBRARY_ID`, `BUNNY_STREAM_API_KEY`, `BUNNY_CDN_HOSTNAME` e, solo se attiva sulla libreria, `BUNNY_TOKEN_AUTH_KEY`. Il tool mantiene accesso di sola lettura.

## Sicurezza e privacy

- Tutte le azioni che cambiano stato richiedono sessione valida e token CSRF.
- Gli UUID selezionati vengono ricontrollati lato server e non autorizzano librerie diverse.
- Il token di conferma aggregato è firmato, scade dopo dieci minuti e ha dimensione massima controllata.
- Non vengono registrati cookie, password, chiavi, URL firmati, transcript, miniature o corpi delle risposte dei fornitori.
- Audio e frame restano in storage effimero e vengono rimossi con il comportamento già collaudato.
- Le chiamate OpenAI Responses continuano con `store=False`; il transcript non viene scritto su disco dall'applicazione.

## Errori e stati vuoti

La pagina distingue messaggi sicuri per configurazione Bunny mancante, autorizzazione negata, limite richieste, indisponibilità temporanea, catalogo vuoto e risposta non valida. Il catalogo non mostra stack trace o dettagli del fornitore. Se non sono ancora configurate credenziali reali, il login funziona ma la dashboard presenta una richiesta chiara di configurazione senza tentare analisi.

La selezione resta vuota al ricaricamento, così nessun video può essere analizzato accidentalmente. Un catalogo vuoto offre il solo aggiornamento manuale. Durante il recupero e l'invio sono mostrati stati di attesa accessibili; controlli e messaggi sono utilizzabili da tastiera e su schermi piccoli.

## Verifica

Lo sviluppo segue test-first. I test devono coprire:

- redirect HTML al login, `401` JSON, cookie valido/scaduto/manomesso e logout;
- protezione CSRF delle nuove azioni;
- paginazione Bunny, catalogo vuoto, 401/403, 429, errori 5xx e payload non validi;
- nessuna perdita di chiavi o dati upstream nei log e nelle risposte;
- validazione delle miniature e degli UUID;
- selezione singola e multipla, anteprima aggregata, conferma scaduta o alterata;
- accodamento seriale e indipendenza degli errori fra lavori;
- ricerca, filtri, seleziona visibili e ripristino della UI nel test JavaScript;
- regressione completa della pipeline, degli export e della pulizia FFmpeg.

Prima della pubblicazione si eseguono la suite offline completa con FFmpeg, il test JavaScript, la build/deploy Railway e verifiche HTTP reali di login, catalogo protetto e healthcheck. Il collegamento reale a Bunny e OpenAI viene collaudato soltanto dopo l'inserimento delle credenziali del team, iniziando da un video breve selezionato esplicitamente.

## Distribuzione e servizi

La distribuzione rimane su Railway, una replica e un solo worker Uvicorn. La prova gratuita attuale è sufficiente per la preview, ma per continuità operativa servirà un piano Railway attivo quando i crediti terminano. Non vengono aggiunti database o storage persistente.

Per l'uso completo servono soltanto:

- l'account Bunny Stream già contenente i video;
- un progetto OpenAI API con fatturazione/crediti API abilitati, separati dall'eventuale abbonamento ChatGPT;
- Railway per mantenere online il container.

Non sono necessari Bunny Premium Encoding, Bunny AI Transcription, un database, un servizio di file storage o un altro abbonamento AI. I costi correnti e i collegamenti ufficiali saranno riportati nel rilascio e mantenuti come stime, non come garanzie di fatturazione.
