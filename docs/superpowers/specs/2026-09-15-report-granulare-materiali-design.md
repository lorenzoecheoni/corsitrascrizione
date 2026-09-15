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
  "relatori": ["Furio d'Andrea"],
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
  "relatori": ["Furio d'Andrea"],
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

- Furio d'Andrea:
  `https://www.assoholding.it/wp-content/uploads/2026/07/19072026_PP-Avv.-Furio-DAndrea_Webinar-22-luglio-2026.pptx`;
- Luigi Morra:
  `https://www.assoholding.it/wp-content/uploads/2026/07/Slide-Morra-Conferimenti-1.pptx`.

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
7. alias onorifici di Furio d'Andrea, Luigi Morra e Antonio Sibilia ricondotti
   a cinque persone totali nel video di riferimento;
8. caso omonimo che resta separato e genera `ALIAS_RELATORE_AMBIGUO`;
9. cella con hyperlink Google conservata come URL reale;
10. etichetta priva di URL/estensione che non diventa filename;
11. PPTX e PDF sintetici con associazione pagina verificata;
12. pagina ambigua che genera `SLIDE_NON_ABBINATA`;
13. serializzazione e validazione dell'intero report intermedio;
14. assenza di artefatti temporanei dopo successo, errore e cancellazione.

## Collaudo sul video Governance

Dopo i test locali e la pubblicazione:

1. rianalizzare il GUID `7f254c4d-fe34-4fd3-a4cf-cda4f447e438`;
2. verificare che le persone siano esattamente Vincenzo Manfredi, Gaetano De
   Vito, Furio d'Andrea, Antonio Sibilia e Luigi Morra;
3. verificare che nessun intervento usi `Avvocato`, `Dottor` o altri alias;
4. verificare che il blocco di Furio d'Andrea resti visibile come blocco lungo
   e produca capitoli distinti;
5. verificare una struttura attesa di circa 10–12 capitoli, preferibilmente da
   8–10 minuti, senza trasformare questa quantità in un vincolo artificiale;
6. verificare URL reali per i deck di Furio d'Andrea e Luigi Morra;
7. verificare `materiale` e `pagina` sulle slide abbinate;
8. verificare tutti i `CONFINE` e l'origine dei tagli;
9. validare gli export JSON, Markdown e testo;
10. lasciare invariati sia il corso pubblicato sia la bozza
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
- i capitoli sono normalmente 8–15 minuti, con preferenza 8–10 e nessun taglio
  forzato;
- i capitoli sopra 20 minuti hanno `INTERVENTO_LUNGO`;
- il video Governance espone cinque persone canoniche senza alias onorifici;
- nessun materiale fittizio viene esportato;
- le slide abbinate espongono materiale reale e pagina;
- l'elaborazione reale del video Governance termina con successo e gli export
  risultano validi e scaricabili;
- nessun corso Academy esistente viene modificato.
