# Report intermedio Academy v1.1

## Obiettivo

Sostituire il JSON piatto del singolo report video con la busta intermedia
v1.1 descritta in `assoholding-platform/docs/IMPORT_CORSO.md`. Il tool continua
a elaborare e salvare ogni video Bunny come lavoro autonomo: il nuovo formato
non abbina più video e non compie scelte editoriali su moduli o lezioni.

Il download esistente `GET /jobs/{job_id}/report.json` diventa il download
canonico v1.1. I report già salvati vengono trasformati al momento del download,
senza nuova trascrizione e senza nuove richieste ai servizi di IA. Markdown e
TXT rimangono report leggibili per il controllo umano.

## Contratto esterno

Il formato si chiama v1.1, ma il campo resta `"versione": 1` per essere
compatibile con il contratto Academy esistente. La busta ha questa topologia:

```json
{
  "versione": 1,
  "stato": "verificato",
  "corso": {
    "titolo": "Titolo del video/corso",
    "sinossi_corso": "Sintesi complessiva"
  },
  "relatori": [],
  "video": [
    {
      "chiave": "v1",
      "guid": "...",
      "titolo_bunny": "...",
      "durata_secondi": 5789,
      "ordine": 1,
      "lingua": "italiano",
      "sinossi": "...",
      "interventi": [],
      "slide": [],
      "materiali": []
    }
  ],
  "verifiche_richieste": []
}
```

Ogni file prodotto dal flusso corrente contiene esattamente un elemento
`video`, con `chiave: "v1"` e `ordine: 1`. Gli interventi sono rinumerati in
ordine cronologico da `v1-i001`. `corso.titolo` usa il titolo suggerito dal
report, con ripiego sul titolo Bunny; `corso.sinossi_corso` usa la sinossi del
video. Non viene esportato il vecchio campo `incertezze`: le anomalie azionabili
sono convertite in `verifiche_richieste`.

## Modelli v1.1

### Relatore

Un relatore contiene:

- `nome` obbligatorio e non generico;
- `slug` solo quando presente nel Registro;
- `ruolo` come qualifica professionale o istituzionale;
- `organizzazione` separata dal ruolo;
- `confidenza` fra 0 e 1;
- `origine_nome` con uno o più valori tra `audio`, `slide`, `inventario`,
  `metadata`, `revisione`.

L'elenco è l'unione senza duplicati delle anagrafiche consolidate e dei nomi
presenti negli interventi. I nomi recuperati solo dalla timeline sono inclusi
con confidenza prudenziale e origine `audio`.

Il Registro incorporato per questa versione associa:

- Vincenzo Manfredi → `vincenzo-manfredi`;
- Gaetano De Vito → `gaetano-de-vito`;
- Antonio Sibilia → `antonio-sibilia`;
- Luigi Morra → `luigi-morra`.

Ogni altro nome produce un avviso `RELATORE_NON_NEL_REGISTRO`. Furio d'Andrea
rimane quindi privo di slug. Se la qualifica non è dimostrabile, viene omessa e
l'avviso chiede esplicitamente di completarla prima della creazione nel
Registro.

Ruolo e organizzazione vengono separati solo quando il testo contiene una
relazione esplicita come “Presidente di Ass Holding”. Le grafie `Ass Holding`,
`Asso Holding` e varianti di maiuscole vengono normalizzate in `Assoholding`.
Non si deducono organizzazioni da un nome o dal solo fatto che il webinar sia
pubblicato dall'associazione.

### Intervento

Ogni intervento contiene `id`, `inizio`, `fine`, `tipo`, `relatori`, `titolo`,
`sintesi`, `punti_chiave`, `accesso` e `confidenza`.

La trasformazione conserva esattamente inizio e fine del report analitico e
applica queste regole locali:

1. Un segmento con durata inferiore a 20 secondi non può avere tipo
   `intervento`. Se è già classificato come altro tipo, il tipo viene
   conservato. Se è ancora `intervento`, viene riclassificato in base al testo:
   `saluti` per saluti/ringraziamenti, `cambio_relatore` per presentazioni o
   passaggi di parola, `domande` per domande/risposte; un micro-turno parlato
   non altrimenti classificabile diventa `domande`, mentre un segmento senza
   relatore diventa `pausa`.
2. Ogni tipo diverso da `intervento` ha `punti_chiave: []`.
3. Un `intervento` di durata fra 20 secondi inclusi e 2 minuti esclusi resta
   separato e produce l'avviso `INTERVENTO_BREVE`.
4. Le sintesi che iniziano con una sola iniziale o con un'etichetta provider
   vengono normalizzate eliminando etichetta e verbo introduttivo e usando la
   forma `Tema trattato: …`. Il prompt di analisi viene aggiornato per chiedere
   sintesi che inizino direttamente dal contenuto e per vietare iniziali ed
   etichette provider.

Esattamente un intervento sostanziale riceve `accesso: "pubblico"`: il primo
`intervento` fra 8 e 15 minuti. Se non esiste, si sceglie il primo intervento
di almeno 8 minuti; se non esiste ancora, si sceglie l'intervento più lungo.
Saluti, logistica, domande, pause e cambi relatore non possono essere scelti.
Tutti gli altri segmenti hanno `accesso: "iscritti"`.

### Slide e materiali

Ogni slide contiene `inizio`, `titolo`, `testo_principale` (massimo 500
caratteri), `confidenza` e, solo quando dimostrabili, `materiale` e `pagina`.
Non si associa una slide a un file e non si inventa una pagina basandosi sul
solo titolo.

I materiali stanno dentro il video e contengono `titolo`, l'eventuale
`relatore`, una sola sorgente fra `url` e `file`, l'eventuale numero di
`pagine`, e `accesso` (`iscritti` per default). Sono usati solo i valori
espliciti dell'inventario associati al video mediante GUID o titolo esatto:

- una sorgente HTTP(S) diventa `url`;
- un nome/percorso non HTTP diventa `file`;
- se non esiste una sorgente, non si crea alcun materiale;
- campi non dimostrabili (`relatore`, `pagine`, collegamento slide/materiale)
  vengono omessi.

Una URL viene controllata con una richiesta limitata, seguendo i redirect. Un
errore di rete o una risposta non riuscita produce
`MATERIALE_NON_RAGGIUNGIBILE`. Un riferimento `file` produce lo stesso avviso,
perché il file dovrà essere fornito successivamente con `--materiali-da` e non
è disponibile sul server del tool.

## Verifiche richieste e stato

Le verifiche hanno `livello`, `codice`, e i campi contestuali opzionali
`video`, `intervento`, `campo`, `messaggio`. I soli codici ammessi sono:

- critici: `RELATORE_NON_IDENTIFICATO`, `TEMPI_INCOERENTI`;
- avvisi: `RELATORE_NON_NEL_REGISTRO`, `INTERVENTO_BREVE`,
  `CONFIDENZA_BASSA`, `MATERIALE_NON_RAGGIUNGIBILE`.

La generazione applica i controlli seguenti:

- un segmento parlato diverso da `pausa` e `logistica` senza relatore produce
  `RELATORE_NON_IDENTIFICATO`;
- inizio negativo, fine non successiva all'inizio, buco, sovrapposizione o
  fine oltre la durata producono `TEMPI_INCOERENTI`;
- ogni relatore privo di slug nel Registro produce
  `RELATORE_NON_NEL_REGISTRO`;
- gli interventi brevi definiti sopra producono `INTERVENTO_BREVE`;
- un intervento con confidenza inferiore a 0,8 o una slide inferiore a 0,7
  produce `CONFIDENZA_BASSA`;
- una sorgente materiale dichiarata ma non disponibile produce
  `MATERIALE_NON_RAGGIUNGIBILE`.

`stato` è `da_verificare` se esiste almeno una verifica critica; è
`verificato` quando non esistono criticità. Gli avvisi non bloccano lo stato
verificato. Le verifiche sono deduplicate mediante codice e contesto.

## Flusso applicativo

1. Il job continua a salvare l'`AcademyReport` analitico corrente, in modo da
   non invalidare i dati persistenti.
2. Al download JSON, un costruttore v1.1 riceve job, GUID e l'eventuale riga
   inventario abbinata.
3. Il costruttore riconcilia relatori, normalizza gli interventi, assegna
   accessi, converte slide e materiali, genera verifiche e calcola lo stato.
4. Il risultato viene validato da modelli Pydantic dedicati prima della
   serializzazione. Nessun campo extra è ammesso. La validazione strutturale
   ammette però una timeline semanticamente incoerente, perché tale anomalia
   deve poter essere esportata come verifica critica `TEMPI_INCOERENTI` invece
   di impedire la produzione del file da correggere.
5. Markdown/TXT usano i dati analitici per la lettura umana e mostrano anche
   relatori riconciliati; non diventano input per l'Academy.

L'indisponibilità temporanea di Google Sheets non blocca il download: il
report viene prodotto senza materiali inventario. La mancata disponibilità
del foglio non altera relatori, timeline o stato, perché il Registro minimo è
incorporato e il contenuto analitico è già persistito.

## Compatibilità e interfaccia

- `GET /jobs/{job_id}/report.json` cambia intenzionalmente contratto e nome
  file in `report-intermedio-{job_id}.json`.
- Non viene mantenuto un secondo endpoint per il vecchio JSON piatto.
- Il pulsante nella pagina del job viene etichettato “Download JSON v1.1”.
- Il job non viene rianalizzato quando si scarica il nuovo formato.
- I report salvati prima del rilascio beneficiano della trasformazione al
  momento del download.

## Test e criteri di accettazione

La modifica segue test-first e comprende:

1. validazione completa della topologia e rifiuto di campi/codici non ammessi;
2. singolo video sempre in lista con `v1`, ordine 1 e ID `v1-iNNN`;
3. riconciliazione di relatori mancanti, slug Registro, separazione ruolo/ente
   e normalizzazione “Assoholding”;
4. riclassificazione dei segmenti sotto 20 secondi, rimozione dei punti chiave
   e avvisi per 20–120 secondi;
5. selezione deterministica di un solo accesso pubblico;
6. controlli su timeline, identità e soglie di confidenza;
7. materiali espliciti, fonti non raggiungibili e assenza di associazioni
   inventate alle slide;
8. test dell'endpoint su un job già salvato, senza chiamare Bunny, AssemblyAI
   o OpenAI;
9. verifica in produzione sul report “Governance delle holding e conferimenti
   a realizzo controllato”, inclusi i cinque relatori, il segmento breve
   corretto, lo stato e la conformità JSON.
