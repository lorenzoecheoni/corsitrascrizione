# Bunny Video Report — analisi a blocchi

## Contesto e causa verificata

La pipeline corrente completa correttamente estrazione e trascrizione, ma invia
l'intera trascrizione di un video da 1–4 ore a una singola richiesta strutturata
OpenAI. Una prova controllata equivalente a 1 ora e 36 minuti ha riprodotto un
errore HTTP 429 dopo tutti i tentativi previsti. Il limite è quello dei token per
minuto del progetto, non il credito disponibile.

Attendere o ripetere la stessa richiesta non rende il flusso affidabile. La
pipeline deve imporre un limite di dimensione a ogni chiamata AI e non deve mai
dipendere dalla possibilità di elaborare un intero video in una sola richiesta.

## Obiettivo

Produrre, anche per video di quattro ore, un report testuale composto da:

- titolo e durata;
- sinossi breve;
- relatori, eventuale presentatore o moderatore, ruoli ed evidenze;
- sequenza dei cambi slide con timestamp, titolo e testo leggibile;
- incertezze esplicite;
- esportazione Markdown, testo semplice e PDF tramite stampa.

La trascrizione resta un dato tecnico temporaneo e non viene mostrata nel report.
Video, audio, fotogrammi e trascrizione non vengono conservati dopo il lavoro.

## Approccio scelto

L'analisi usa una pipeline map-reduce seriale e limitata:

1. i fotogrammi candidati sono classificati in piccoli gruppi;
2. i segmenti della trascrizione sono divisi in finestre temporali;
3. ogni finestra produce soltanto evidenze sui relatori e appunti per la sinossi;
4. una richiesta finale piccola consolida le evidenze già sintetizzate;
5. il backend inserisce deterministicamente le slide rilevate e valida il report.

Non vengono richiesti un livello OpenAI superiore, Batch API, database o nuovi
servizi esterni.

## Contratti intermedi

### Analisi visiva

Ogni richiesta contiene al massimo 25 fotogrammi JPEG a dettaglio basso. Per ogni
timestamp il modello restituisce esattamente uno dei seguenti risultati:

- `slide`: vera slide o schermata di presentazione;
- `camera_change`: cambio di inquadratura senza cambio slide;
- `uncertain`: contenuto non classificabile con sufficiente confidenza.

Per `slide` sono ammessi soltanto titolo e testo realmente leggibili. Le slide
sono ordinate per timestamp e non vengono rigenerate dalla sintesi finale.

### Analisi della trascrizione

La trascrizione viene divisa in finestre di massimo 10 minuti, senza spezzare un
segmento diarizzato. Se una singola finestra supera 12.000 caratteri viene
ulteriormente divisa mantenendo gli offset originali. Ogni richiesta riceve solo:

- titolo e durata del video;
- intervallo temporale della finestra;
- segmenti con timestamp, etichetta vocale e testo;
- mapping locale delle etichette vocali.

Il risultato intermedio contiene:

- lingua rilevata nella finestra;
- appunti sintetici per la sinossi;
- persone o voci citate, ruolo solo se esplicito e relative evidenze;
- incertezze.

Non produce capitoli, trascrizione riformattata o testo editoriale esteso.

### Consolidamento finale

La richiesta finale riceve esclusivamente metadati essenziali, risultati intermedi
e slide già classificate; non riceve la trascrizione originale né immagini. La
dimensione dell'input viene verificata localmente prima dell'invio e non può
superare 30.000 caratteri. Se gli appunti eccedono il limite, il backend conserva
prima evidenze di identità e ruolo, poi comprime gli appunti della sinossi.

Il modello consolida nomi e ruoli soltanto quando le evidenze lo consentono.
Persone omonime con evidenze incompatibili non vengono unite. Voci non identificate
rimangono etichette generiche e non vengono attribuite biometricamente tra blocchi.

## Modello del report

Il report pubblico viene ridotto ai dati richiesti. I campi obbligatori sono:

- `title`;
- `duration_seconds`;
- `detected_language`;
- `synopsis`;
- `speakers`;
- `slides`;
- `uncertainties`.

Ogni relatore contiene identificativo, nome visualizzato, ruolo opzionale,
confidenza ed evidenze. Ogni slide contiene timestamp, titolo opzionale, contenuto
leggibile e confidenza. Costi e utilizzo API restano metadati applicativi, non dati
generati dal modello.

Le pagine e gli export non mostrano più descrizione estesa, prerequisiti,
obiettivi, capitoli, interventi, argomenti, keyword o takeaway. Questa riduzione è
intenzionale: diminuisce costo e fragilità e rispetta il requisito di ottenere
soltanto relatori, sinossi e cambi slide.

## Controllo dei limiti e tentativi

- Nessuna richiesta testuale supera una finestra o il limite di caratteri sopra
  definito.
- Le chiamate vengono eseguite in serie per non sommare il consumo nello stesso
  intervallo TPM.
- Dopo ogni richiesta testuale viene rispettato l'eventuale reset indicato dagli
  header OpenAI.
- Gli errori 429 seguono `Retry-After` quando presente e non ripetono
  immediatamente la medesima richiesta.
- Gli errori esposti distinguono fase visiva, blocco testuale e consolidamento,
  senza includere contenuti o risposte del fornitore.
- Un blocco fallito interrompe il lavoro: non viene pubblicato un report parziale
  spacciato per completo.

## Stato, avanzamento e pulizia

L'interfaccia mostra un progresso monotono nelle fasi:

1. estrazione temporanea;
2. trascrizione;
3. analisi slide, con numero del gruppo;
4. analisi relatori e sinossi, con numero del blocco;
5. consolidamento;
6. export e pulizia.

La directory temporanea continua a essere rimossa in un blocco `finally` dopo
successo, errore o annullamento. Gli oggetti intermedi esistono soltanto nella
memoria del worker e scompaiono al riavvio del processo.

## Compatibilità e migrazione

Le rotte di catalogo, selezione, stato lavoro e download restano invariate. Cambia
il contenuto del report e il contratto interno dell'analizzatore. I lavori già
falliti non sono recuperabili perché, per requisito, i loro media e la loro
trascrizione non vengono conservati.

La stima preventiva viene aggiornata per includere più richieste piccole; deve
restare prudente e dichiarata come stima. L'aumento del numero di richieste non
implica un nuovo abbonamento e non richiede servizi diversi da Bunny Stream,
OpenAI API, Railway e FFmpeg già configurati.

## Sicurezza

- `store=False` su tutte le richieste Responses API.
- Nessun contenuto, URL firmato o segreto nei log.
- Nessun riconoscimento biometrico di voce o volto.
- Nomi e ruoli ammessi solo con introduzione, sottopancia, slide o metadati.
- Nessuna scrittura verso la libreria Bunny.
- Nessun file persistente aggiunto oltre agli export scaricati esplicitamente
  dall'utente.

## Verifica e criteri di accettazione

### Test automatici

- una trascrizione di quattro ore genera più finestre e nessun payload supera i
  limiti definiti;
- un segmento a cavallo del confine non viene perso o duplicato;
- 101 fotogrammi producono cinque chiamate visive da massimo 25 elementi;
- la sintesi finale non contiene trascrizione originale né immagini;
- nomi privi di evidenza diventano `Relatore N`;
- slide e timestamp provengono soltanto dal passaggio visivo;
- fallimenti di un blocco riportano la fase corretta e puliscono i temporanei;
- Markdown, testo e pagina stampabile contengono relatori, sinossi e slide;
- l'intera suite non espone segreti o contenuti nei log.

### Prova reale

Il video “Governance delle holding e conferimenti a realizzo controllato” deve:

1. completare tutte le fasi senza errore 429;
2. produrre una sinossi breve;
3. elencare i relatori con ruoli solo quando supportati;
4. elencare i cambi slide in ordine con timestamp e titoli leggibili;
5. offrire download Markdown e testo e stampa PDF;
6. lasciare vuoto lo spazio temporaneo al termine.

Il nuovo tentativo reale viene avviato soltanto dopo test automatici, prova API con
payload equivalente e pubblicazione riuscita, per evitare ulteriori elaborazioni
inutili a pagamento.
