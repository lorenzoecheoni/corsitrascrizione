# Report granulare, blocchi di parlato e materiali verificabili

Data: 15 settembre 2026

## Obiettivo

Il tool deve descrivere fedelmente il video e, nello stesso report intermedio,
fornire unità editoriali utilizzabili come lezioni. I due livelli non devono
essere confusi:

- il **blocco di parlato** rappresenta ciò che accade realmente nel video, per
  esempio un intervento continuativo di 38 minuti dello stesso relatore;
- il **capitolo** è una suddivisione editoriale interna al blocco, proposta in
  corrispondenza di una chiusura di tema verificata sull'audio;
- una slide è un indizio utile per cercare un cambio di tema, mai un confine
  automatico e mai una lezione autonoma.

La misura editoriale preferita è 8–10 minuti, l'intervallo obiettivo è 8–15
minuti e il tetto ordinario è 20 minuti. I 40 minuti ammessi dall'Academy
restano esclusivamente una tolleranza d'importazione. Quando non esiste un
taglio accettabile, il capitolo rimane lungo e il report lo segnala, senza
forzare un taglio dentro una frase, un esempio o una risposta.

## Ambito

Il cambiamento riguarda il tool Bunny Video Report e il suo report intermedio.
Non modifica né il corso Academy pubblicato né la bozza
`/academy/governance-holding-report-15-09/`.

Sono inclusi:

1. segmentazione della trascrizione su parole e frasi reali;
2. conservazione dei blocchi originali e produzione di capitoli granulari;
3. confini audio contigui e verificabili;
4. normalizzazione anagrafica dei relatori e separazione della qualifica;
5. acquisizione di URL materiali reali;
6. lettura transitoria di PPTX e PDF e associazione slide-materiale-pagina;
7. nuove verifiche dedicate ai capitoli lunghi e alle slide non abbinate;
8. aggiornamento della documentazione del formato intermedio.

Non sono inclusi:

- creazione o modifica di corsi Academy;
- salvataggio persistente di video, audio, trascrizioni, fotogrammi o deck;
- una lezione per ogni slide;
- creazione artificiale di file o URL mancanti;
- modifica del corso Academy pubblicato o della bozza di confronto.

## Problemi riscontrati nel flusso attuale

### Granularità insufficiente

AssemblyAI può restituire una singola utterance lunga molti minuti. Il codice
attuale divide una utterance lunga usando porzioni proporzionali di testo e
tempo, invece di assegnare a ogni porzione il relativo sottoinsieme di parole
con timestamp reali. Di conseguenza il modello vede poche unità editoriali e
tende a ricomporle in quattro grandi interventi.

Inoltre il prompt corrente dice esplicitamente di non usare i cambi di slide
per dividere gli interventi. Questa regola resta valida nel significato
“nessun taglio automatico sulla slide”, ma deve diventare “la slide indica dove
cercare un cambio di tema e una chiusura audio”.

### Alias dei relatori

La riconciliazione attuale gestisce grafie equivalenti, accenti e l'alias
Furio/Fulvio d'Andrea, ma non separa titoli onorifici e qualifiche. Per questo
nomi come `Avvocato Furio D'Andrea`, `Dottor Morra` e `Dottor Sibilia` possono
diventare persone distinte e gli interventi possono continuare a riferirsi
all'alias.

### Materiali e pagine

L'inventario è letto attraverso l'esportazione CSV di Google Sheets. Il CSV
conserva il testo visibile della cella ma può perdere il collegamento associato
al testo. Inoltre qualsiasi testo senza URL viene oggi interpretato come un
nome file, anche quando è soltanto un'etichetta come `Slide Luigi Morra`.

Le slide rilevate nel video non vengono confrontate con le pagine dei deck;
perciò i campi `materiale` e `pagina` restano vuoti.

## Contratto del report intermedio

Il formato resta il report intermedio Academy v1.1: il nome del contratto è
v1.1, mentre la busta conserva `"versione": 1` per compatibilità con l'import
Academy. Le nuove informazioni sono additive. I report già archiviati
continuano a essere leggibili; per ottenere blocchi e capitoli un vecchio
video deve essere rianalizzato.

### Durata autorevole

`video[].durata_secondi` deriva esclusivamente da `length` restituito dalla
libreria Bunny. Durate lette da FFmpeg, playlist, traccia audio o provider di
trascrizione servono soltanto alla diagnostica e non possono sostituirla. Il
contratto Academy usa secondi interi: il tool applica una sola normalizzazione
conservativa al secondo intero senza mai arrotondare oltre la durata Bunny e
usa lo stesso valore come limite massimo di blocchi, capitoli e slide.

Nessun `fine` può superare `durata_secondi`. Una discrepanza sub-secondo, come
248 millisecondi aggiuntivi misurati nella traccia audio, viene assorbita dal
clamp finale e non modifica la durata ufficiale esportata.

### Blocchi di parlato

Ogni video aggiunge `blocchi_parlato`, in ordine cronologico. Un blocco è una
sequenza reale e continua con la stessa funzione editoriale e gli stessi
relatori principali. Comprende anche saluti, logistica, domande e passaggi di
parola, che restano fatti del video anche se l'Academy li assorbirà o escluderà.

Esempio:

```json
{
  "id": "v1-b003",
  "inizio": "0:14:22",
  "fine": "0:52:15",
  "tipo": "intervento",
  "relatori": ["Furio D’Andrea"],
  "titolo": "Governance delle holding",
  "sinossi": "Il blocco affronta poteri societari, decisioni assembleari e direttive della holding."
}
```

I blocchi non vengono spezzati per rispettare una durata editoriale: sono il
livello fattuale che consente di ricostruire il webinar originale.

### Capitoli/interventi granulari

La lista `interventi` continua a essere la linea del tempo operativa usata per
creare le lezioni. Ogni elemento aggiunge:

- `blocco`: identificativo del blocco di origine;
- `capitolo_numero`: posizione del capitolo nel blocco;
- `capitoli_blocco`: numero totale di capitoli del blocco;
- `sinossi`: descrizione specifica del capitolo, non copia della sinossi del
  blocco;
- `confine_inizio`, assente soltanto sul primo elemento assoluto.

Esempio:

```json
{
  "id": "v1-i006",
  "blocco": "v1-b003",
  "capitolo_numero": 2,
  "capitoli_blocco": 4,
  "inizio": "0:22:49",
  "fine": "0:32:11",
  "tipo": "intervento",
  "relatori": ["Furio D’Andrea"],
  "titolo": "Le decisioni assembleari",
  "sinossi": "Il capitolo esamina competenze dei soci, quorum e modalità decisionali alternative.",
  "confine_inizio": {
    "motivo_editoriale": "slide_e_tema",
    "regola_audio": "short_pause",
    "slide_indizio": "0:22:49"
  }
}
```

`motivo_editoriale` ammette:

- `inizio_blocco`;
- `cambio_tema`;
- `slide_e_tema`;
- `cambio_relatore`.

`regola_audio` conserva la prova applicata dal motore dei confini:

- `long_pause`;
- `short_pause`;
- `no_pause`.

Il cambio slide non è sufficiente, da solo, a creare un capitolo. Il valore
`slide_e_tema` richiede anche una reale chiusura del tema. `slide_indizio`
riporta il timestamp visivo che ha fatto cercare il taglio; il timestamp del
capitolo è sempre il confine audio verificato.

### Slide e materiali

Ogni slide rilevata continua a contenere inizio, titolo, testo principale e
confidenza. Quando il deck è raggiungibile, deve contenere anche:

- `materiale`: titolo esatto del materiale presente in `materiali`;
- `pagina`: indice uno-based della pagina o slide del deck.

Un materiale viene emesso solo quando contiene un URL HTTP(S) reale oppure un
file con nome ed estensione realmente disponibili. Una semplice etichetta non
diventa mai un file.

Per il video Governance il registro iniziale userà gli URL ufficiali già
presenti nell'import Academy esistente:

- Furio D’Andrea:
  `https://www.assoholding.it/wp-content/uploads/2026/07/19072026_PP-Avv.-Furio-DAndrea_Webinar-22-luglio-2026.pptx`;
- Luigi Morra:
  `https://www.assoholding.it/wp-content/uploads/2026/07/Slide-Morra-Conferimenti-1.pptx`.

I titoli destinati alla piattaforma sono rispettivamente
`Slide · Furio D’Andrea` e `Slide · Luigi Morra`. L'etichetta dell'inventario
può servire a trovare la sorgente, ma non viene copiata automaticamente come
titolo pubblico.

Il registro locale è una fonte curata per i corsi già migrati; per i corsi
successivi prevalgono URL o hyperlink presenti nel foglio inventario.

## Segmentazione su parole reali

### Atomi di trascrizione

Ogni utterance del provider viene trasformata in atomi cronologici usando il
sottoinsieme reale di `TranscriptWord`. Un atomo:

- termina preferibilmente alla fine di una frase;
- non supera circa 90 secondi salvo assenza di punteggiatura;
- possiede testo ricostruito dalle proprie parole;
- usa inizio e fine della prima e ultima parola reali;
- non duplica parole in altri atomi;
- conserva l'identificativo dell'utterance sorgente per audit.

La suddivisione proporzionale per caratteri viene eliminata dal percorso
AssemblyAI.

### Costruzione dei blocchi

Il modello riceve atomi sufficientemente piccoli e identifica i blocchi
fattuali: relatore, funzione e continuità del discorso. I micro-segmenti di
cambio relatore restano autonomi. I blocchi non sono vincolati a 20 minuti.

### Costruzione dei capitoli

Per ogni blocco sostanziale il modello propone chiusure tematiche tra atomi. Il
motore locale sceglie i tagli con questo ordine di priorità:

1. frase, esempio o risposta conclusi;
2. cambio di tema esplicito;
3. pausa audio disponibile;
4. cambio slide vicino, usato come indizio aggiuntivo;
5. durata risultante vicina a 8–10 minuti.

Sono accettabili capitoli didattici di tipo `intervento` fra 8 e 15 minuti. Un
capitolo fra 15 e 20 minuti è ammesso quando evita un taglio debole. Sopra 20
minuti il motore cerca un altro taglio. Se nessun candidato rispetta la
chiusura del discorso, mantiene il capitolo lungo e aggiunge
`INTERVENTO_LUNGO`.

Queste durate non si applicano ai segmenti fattuali `saluti`, `logistica`,
`domande`, `pausa` e `cambio_relatore`: restano granulari con la loro durata
reale e saranno assorbiti, esclusi o accorpati soltanto dall'IA dell'Academy
secondo le regole editoriali già definite.

Il motore non taglia mai a forza soltanto per rispettare la durata.

### Copertura temporale e accesso

I capitoli collegati allo stesso blocco formano una partizione completa del
blocco: il primo inizia con il blocco, l'ultimo finisce con il blocco e ogni
fine interna coincide con l'inizio successivo. Non sono ammessi buchi o
sovrapposizioni all'interno del blocco.

Fra due blocchi un intervallo è ammesso solo quando la timeline lo dichiara
esplicitamente come `logistica` o `pausa`. Un intervallo non classificato
produce `TEMPI_INCOERENTI`, quindi rende il report `da_verificare`.

Nell'intero report esiste esattamente un capitolo didattico con
`accesso: "pubblico"`. Deve essere di tipo `intervento`, durare da 8 a 15
minuti e non contenere soltanto saluti. Tutti gli altri capitoli e segmenti
hanno `accesso: "iscritti"`. Il capitolo pubblico è il candidato unico per
anteprima gratuita e hero; il report non assegna `pubblico` a un ripiego fuori
misura.

### Identificatori e ripetibilità

Gli identificatori vengono assegnati soltanto dopo la normalizzazione finale
della timeline, in ordine cronologico e con tie-break deterministici:
`v1-b001` per i blocchi e `v1-i001` per i segmenti/capitoli. Configurazione del
modello, prompt, soglie, ordinamenti e scelta fra candidati equivalenti sono
versionati e deterministici.

Due analisi complete dello stesso GUID e della stessa revisione Bunny, con la
stessa versione del motore, devono produrre gli stessi identificatori, lo
stesso numero di capitoli e confini che differiscono al massimo di un secondo.
Il collaudo esegue realmente entrambe le analisi e confronta i JSON
normalizzati; il requisito non viene affidato soltanto a test sintetici.

### Costo nel JSON

Ogni elemento di `video` aggiunge `costo_stimato`, derivato dallo stesso
`CostEstimate` salvato dal lavoro:

```json
{
  "valuta": "USD",
  "minimo": 0.31,
  "massimo": 0.45,
  "banda_bunny": 0.02,
  "trascrizione": 0.25,
  "analisi": 0.07,
  "criterio": "Stima da contatori API disponibili; non è una fattura."
}
```

Gli importi sono stime, non valori di fatturazione, ma il JSON deve consentire
di leggerli senza accedere al pannello del tool.

## Confini audio

Restano valide le regole già concordate:

- pausa di almeno 2 secondi: margine di 1 secondo dopo l'ultima parola;
- pausa più breve: metà pausa per lato;
- nessuna pausa: confine alla fine dell'ultima parola, con fine e inizio
  coincidenti;
- il taglio non cade dentro una frase, un esempio o una risposta;
- quando interviene il moderatore, il confine precede le sue parole;
- tutti i segmenti sono contigui e non sovrapposti;
- una voce `CONFINE` accompagna ogni passaggio interno con le ultime cinque
  parole prima e le prime cinque dopo.

La nuova `confine_inizio` spiega perché è stato proposto il taglio; la verifica
`CONFINE` continua a dimostrare dove è stato collocato sull'audio.

## Normalizzazione dei relatori

La riconciliazione segue criteri deterministici:

1. rimuove prefissi onorifici e professionali dal nome (`avv.`, `avvocato`,
   `dott.`, `dottor`, `dottore`, `prof.`, `professore` e forme femminili);
2. conserva il prefisso come qualifica nel campo `ruolo` quando non esiste una
   qualifica più specifica;
3. abbina il nome ripulito a una voce completa del Registro, del foglio o delle
   evidenze esplicite;
4. un alias con solo cognome, come `Dottor Morra`, viene unito soltanto quando
   esiste una sola persona compatibile;
5. in caso di due persone compatibili non effettua il merge e produce una
   verifica;
6. unisce evidenze, qualifiche e organizzazioni senza perdere informazioni;
7. riscrive tutti i riferimenti negli interventi, nei blocchi, nei titoli e
   nelle sintesi verso il nome anagrafico canonico.

Quando una persona ha sia una qualifica professionale sia una funzione nel
video, il ruolo le conserva entrambe in forma leggibile, per esempio
`Avvocato; moderatore`.

Per ogni persona riconosciuta nel Registro il report usa sia il nome canonico
del Registro sia il relativo `slug`; lo slug non viene ricostruito dal nome.
Nel video Governance, in particolare, la voce è `Furio D’Andrea` con apostrofo
tipografico e `slug: "furio-dandrea"`. Tutti i riferimenti interni usano la
stessa grafia per evitare la creazione di doppioni durante l'import.

## Recupero e analisi dei materiali

### Fonti

Ordine di precedenza:

1. URL scritto esplicitamente nella cella del foglio;
2. hyperlink associato alla cella, letto tramite Google Sheets API quando il
   service account è configurato;
3. registro curato per GUID dei corsi già migrati;
4. filename reale con estensione riconosciuta, solo se disponibile al tool.

Un testo senza URL e senza estensione non è una sorgente valida.

### Estrazione transitoria

Il deck viene scaricato soltanto nello spazio temporaneo del singolo lavoro e
viene eliminato alla fine, come audio e fotogrammi. Sono supportati:

- PPTX: testo di ogni slide e numero slide tramite gli XML del pacchetto;
- PDF: testo di ogni pagina e numero pagina.

Non vengono eseguite macro, collegamenti incorporati o istruzioni contenute nei
documenti.

### Abbinamento

Titolo e testo OCR della slide video vengono confrontati con titolo e testo di
ogni pagina del deck. L'abbinamento richiede:

- punteggio minimo;
- margine minimo rispetto alla seconda pagina candidata;
- coerenza cronologica delle pagine, salvo ritorni espliciti nel video.

Un abbinamento incerto non viene inventato: i campi restano assenti e viene
prodotta una verifica `SLIDE_NON_ABBINATA`.

## Verifiche aggiuntive

Oltre ai codici già esistenti vengono introdotti:

- `INTERVENTO_LUNGO` — avviso; capitolo sopra 20 minuti mantenuto perché privo
  di un taglio semanticamente e acusticamente accettabile;
- `SLIDE_NON_ABBINATA` — avviso; slide rilevata senza materiale/pagina
  sufficientemente certi;
- `ALIAS_RELATORE_AMBIGUO` — avviso; titolo o cognome non consentono un merge
  anagrafico univoco.

`MATERIALE_NON_RAGGIUNGIBILE` continua a indicare URL/file reali ma non
accessibili. Un'etichetta priva di sorgente non viene emessa come materiale e
genera lo stesso avviso con messaggio esplicito “sorgente reale assente”.

`INTERVENTO_LUNGO` non è mai critico e non blocca stato, download o import: lo
schema Academy tollera capitoli fino a 40 minuti e la decisione di un eventuale
taglio manuale resta al destinatario del report.

## Sicurezza e persistenza

- Video, audio, trascrizione parola-per-parola, fotogrammi e deck restano
  temporanei e vengono cancellati anche in caso di errore.
- Nel database rimangono soltanto blocchi, capitoli, sintesi, slide, materiali
  verificati, prove sintetiche dei confini e consumo API.
- Log e messaggi pubblici non contengono testo della trascrizione, URL firmati,
  chiavi o risposte complete dei provider.
- Il recupero dei materiali mantiene limiti di dimensione, timeout, redirect e
  protezioni SSRF già applicate alle sorgenti remote.

## Strategia di test

Lo sviluppo procede per test-first:

1. utterance lunga suddivisa in atomi con parole uniche e timestamp reali;
2. conservazione di un blocco da 38 minuti con più capitoli collegati;
3. obiettivo 8–10, intervallo 8–15 e tetto 20 senza tagli forzati;
4. fallback `INTERVENTO_LUNGO` quando non esiste una chiusura valida;
5. slide usata come indizio ma confine collocato sul silenzio vicino;
6. sinossi distinta per ogni capitolo;
7. alias onorifici di Furio D’Andrea, Luigi Morra e Antonio Sibilia ricondotti
   a cinque persone totali nel video di riferimento;
8. caso omonimo che resta separato e genera `ALIAS_RELATORE_AMBIGUO`;
9. cella con hyperlink Google conservata come URL reale;
10. etichetta priva di URL/estensione che non diventa filename;
11. PPTX e PDF sintetici con associazione pagina verificata;
12. pagina ambigua che genera `SLIDE_NON_ABBINATA`;
13. serializzazione e validazione dell'intero report intermedio;
14. durata Bunny conservata e clamp di tutti i tempi al suo secondo finale;
15. un solo accesso pubblico, didattico e compreso fra 8 e 15 minuti;
16. copertura completa di ogni blocco e classificazione degli intervalli fra
    blocchi;
17. costo stimato serializzato nel JSON;
18. stabilità di identificatori e confini su due analisi equivalenti;
19. assenza di artefatti temporanei dopo successo, errore e cancellazione.

## Collaudo sul video Governance

Dopo i test locali e la pubblicazione:

1. rianalizzare il GUID `7f254c4d-fe34-4fd3-a4cf-cda4f447e438`;
2. verificare che le persone siano esattamente Vincenzo Manfredi, Gaetano De
   Vito, Furio D’Andrea, Antonio Sibilia e Luigi Morra, con nomi e slug del
   Registro;
3. verificare che nessun intervento usi `Avvocato`, `Dottor` o altri alias;
4. verificare che il blocco di Furio D’Andrea resti visibile come blocco lungo
   e produca capitoli distinti;
5. verificare una struttura attesa di circa 10–12 capitoli, preferibilmente da
   8–10 minuti, senza trasformare questa quantità in un vincolo artificiale;
6. verificare URL reali per i deck di Furio D’Andrea e Luigi Morra;
7. verificare `materiale` e `pagina` sulle slide abbinate;
8. verificare tutti i `CONFINE` e l'origine dei tagli;
9. validare gli export JSON, Markdown e testo;
10. eseguire una seconda analisi completa con la stessa versione del motore e
    verificare stessi identificatori e confini entro un secondo;
11. verificare un solo capitolo pubblico da 8–15 minuti e il costo nel JSON;
12. lasciare invariati sia il corso pubblicato sia la bozza
    `/academy/governance-holding-report-15-09/`.

Il nuovo report servirà per creare una terza versione Academy e confrontare le
tre strutture. Solo dopo il confronto umano il nuovo schema diventerà il
riferimento per i corsi successivi.

## Criteri di accettazione

Il lavoro è accettato quando:

- il report conserva sia i blocchi fattuali sia i capitoli editoriali;
- ogni capitolo riferisce il proprio blocco e ha una sinossi specifica;
- ogni taglio interno ha motivazione editoriale, regola audio e verifica
  contestuale;
- la durata esportata proviene da Bunny e nessun tempo la supera;
- ogni blocco è coperto integralmente dai propri capitoli e ogni intervallo fra
  blocchi è dichiarato come logistica o pausa;
- i capitoli sono normalmente 8–15 minuti, con preferenza 8–10 e nessun taglio
  forzato;
- i capitoli sopra 20 minuti hanno `INTERVENTO_LUNGO`;
- esiste esattamente un capitolo pubblico didattico da 8–15 minuti;
- il video Governance espone cinque persone canoniche senza alias onorifici,
  con nomi e slug del Registro;
- nessun materiale fittizio viene esportato;
- le slide abbinate espongono materiale reale e pagina;
- il costo stimato è presente nel JSON;
- due analisi equivalenti mantengono identificatori uguali e confini entro un
  secondo;
- l'elaborazione reale del video Governance termina con successo e gli export
  risultano validi e scaricabili;
- nessun corso Academy esistente viene modificato.
