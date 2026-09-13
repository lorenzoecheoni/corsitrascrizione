const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app/static/batch.js', 'utf8');

function element(initial = {}) {
  return {
    dataset: {}, textContent: '', hidden: false, value: 0, handlers: {},
    addEventListener(type, callback) { this.handlers[type] = callback; },
    ...initial,
  };
}

function row(id, state = 'processing') {
  const children = {
    '[data-job-message]': element(),
    '[data-job-label]': element(),
    '[data-job-progress]': element(),
  };
  return element({
    dataset: {batchJob: '', jobId: id, jobState: state},
    querySelector(selector) { return children[selector] || null; },
    children,
  });
}

function setup(states = ['processing', 'queued']) {
  const rows = states.map((state, index) => row(`job-${index + 1}`, state));
  const batch = element({dataset: {batchId: 'batch-1'}});
  const pollStatus = element({hidden: true});
  const events = {}, timers = new Map(), requests = [];
  let nextTimer = 0;
  vm.runInNewContext(source, {
    document: {
      getElementById(id) {
        if (id === 'batch') return batch;
        if (id === 'batch-poll-status') return pollStatus;
        return null;
      },
      querySelectorAll(selector) { return selector === '[data-batch-job]' ? rows : []; },
    },
    window: {addEventListener(type, callback) { events[type] = callback; }},
    setTimeout(callback, ms) {
      assert.equal(ms, 3000);
      timers.set(++nextTimer, callback);
      return nextTimer;
    },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      return new Promise((resolve, reject) => requests.push({url, options, resolve, reject}));
    },
  });
  return {rows, batch, pollStatus, events, timers, requests};
}

function fireTimer(ui) {
  const next = ui.timers.entries().next().value;
  assert.ok(next, 'Expected a scheduled poll');
  const [id, callback] = next;
  ui.timers.delete(id);
  return callback();
}

function respond(request, jobs, status = 200) {
  request.resolve({
    ok: status >= 200 && status < 300,
    status,
    async json() { return {id: 'batch-1', jobs}; },
  });
}

async function flush() { await new Promise(resolve => setImmediate(resolve)); }

(async () => {
  assert.equal(setup(['completed', 'failed']).timers.size, 0, 'Terminal batches do not poll');

  const ui = setup();
  assert.equal(ui.timers.size, 1, 'Active batch schedules automatic polling');
  const stale = fireTimer(ui);
  assert.equal(ui.requests[0].url, '/api/batches/batch-1');
  assert.equal(ui.requests[0].options.cache, 'no-store');
  ui.events.pagehide();
  ui.events.pageshow({persisted: true});
  assert.equal(ui.requests.length, 2, 'BFCache restoration polls immediately');
  respond(ui.requests[1], [
    {id: 'job-1', state: 'processing', progress: 75, message: 'Analisi'},
    {id: 'job-2', state: 'queued', progress: 0, message: 'In coda'},
  ]);
  await flush();
  respond(ui.requests[0], [
    {id: 'job-1', state: 'processing', progress: 25, message: 'Vecchio'},
    {id: 'job-2', state: 'queued', progress: 0, message: 'In coda'},
  ]);
  await stale;
  assert.equal(ui.rows[0].children['[data-job-progress]'].value, 75, 'Stale response cannot regress progress');
  assert.equal(ui.rows[0].children['[data-job-label]'].textContent, '75%');
  assert.equal(ui.rows[0].children['[data-job-message]'].textContent, 'Analisi');
  assert.equal(ui.timers.size, 1, 'Only one timer remains after BFCache restoration');

  const final = fireTimer(ui);
  respond(ui.requests[2], [
    {id: 'job-1', state: 'completed', progress: 100, message: 'Completato'},
    {id: 'job-2', state: 'cancelled', progress: 0, message: 'Annullato'},
  ]);
  await final;
  assert.equal(ui.timers.size, 0, 'Polling stops when every job is terminal');

  const retry = setup(['processing']);
  const failedPoll = fireTimer(retry);
  retry.requests[0].reject(new Error('offline'));
  await failedPoll;
  assert.equal(retry.pollStatus.hidden, false);
  assert.match(retry.pollStatus.textContent, /Nuovo tentativo/);
  assert.equal(retry.timers.size, 1, 'Transient network errors retry automatically');

  for (const [status, expected] of [[401, /Accesso scaduto/], [404, /Gruppo non disponibile/]]) {
    const stopped = setup(['processing']);
    const request = fireTimer(stopped);
    respond(stopped.requests[0], [], status);
    await request;
    assert.match(stopped.pollStatus.textContent, expected);
    assert.equal(stopped.timers.size, 0, `${status} stops polling`);
  }

  console.log('PASS: automatic batch polling, retries, terminal stop and BFCache isolation');
})().catch(error => { console.error(error); process.exitCode = 1; });
