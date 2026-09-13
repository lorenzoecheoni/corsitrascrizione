const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app/static/course_review.js', 'utf8');

function field(value) { return {value}; }
const fields = {
  inizio: field('0:00:00'), fine: field('0:10:00'), tipo: field('intervento'),
  relatori: field('Mario Rossi, Anna Bianchi'), titolo: field('<script>letterale</script>'),
  sintesi: field('Sintesi corretta.'), punti_chiave: field('Uno\nDue\nTre'), confidenza: field('0.9'),
};
const row = {
  dataset: {videoIndex: '0', interventionIndex: '0'},
  querySelector(selector) { return fields[selector.match(/data-field="([^"]+)/)[1]]; },
};
const form = {dataset: {}, handlers: {}, addEventListener(type, callback) {this.handlers[type] = callback;}};
const status = {textContent: '', className: ''};
const data = {textContent: JSON.stringify({
  versione: 1, stato: 'da_verificare', corso: {titolo: 'Corso', sinossi_corso: 'Sintesi'},
  relatori: [{nome: 'Mario Rossi', confidenza: .9, origine_nome: ['audio']},
             {nome: 'Anna Bianchi', confidenza: .9, origine_nome: ['audio']}],
  video: [{chiave: 'v1', guid: '00000000-0000-0000-0000-000000000001', titolo_bunny: 'Corso',
    durata_secondi: 600, ordine: 1, interventi: [{id: 'v1-i001', inizio: '0:00:00', fine: '0:10:00',
      tipo: 'intervento', relatori: ['Mario Rossi'], titolo: 'Titolo', sintesi: 'Sintesi',
      punti_chiave: ['Uno', 'Due', 'Tre'], confidenza: .9}], slide: []}], verifiche_richieste: [],
})};
const requests = [];

vm.runInNewContext(source, {
  document: {
    getElementById(id) { return {'course-review-form': form, 'course-report-data': data, 'review-status': status}[id] || null; },
    querySelectorAll(selector) { return selector === '[data-intervention-row]' ? [row] : []; },
  },
  fetch(url, options) { requests.push({url, options}); return Promise.resolve({ok: true, json: async () => ({ok: true})}); },
});

async function flush() { await new Promise(resolve => setImmediate(resolve)); }

(async () => {
  let prevented = false;
  await form.handlers.submit({preventDefault() {prevented = true;}});
  await flush();
  assert.equal(prevented, true);
  assert.equal(requests.length, 1);
  const payload = JSON.parse(requests[0].options.body);
  assert.equal(payload.video[0].interventi[0].titolo, '<script>letterale</script>');
  assert.deepEqual(payload.video[0].interventi[0].relatori, ['Mario Rossi', 'Anna Bianchi']);
  assert.deepEqual(payload.video[0].interventi[0].punti_chiave, ['Uno', 'Due', 'Tre']);
  assert.equal(requests[0].options.headers['X-CSRF-Token'], undefined, 'Token comes from form dataset only when present');
  assert.match(status.textContent, /salvate/i);

  fields.fine.value = '0:09:59';
  await form.handlers.submit({preventDefault(){}});
  assert.equal(requests.length, 1, 'Invalid final coverage is blocked before fetch');
  assert.match(status.textContent, /coprire/i);
  console.log('PASS: review literal updates and client timeline validation');
})().catch(error => { console.error(error); process.exitCode = 1; });
