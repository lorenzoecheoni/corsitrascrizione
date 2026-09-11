const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/static/job.js', 'utf8');

function setup(state = 'processing') {
  const elements = new Map(), events = {}, timers = new Map(), requests = [];
  let nextTimer = 0, copied, printed = 0;
  const element = id => {
    if (!elements.has(id)) elements.set(id, {dataset: {}, textContent: '', hidden: false, handlers: {},
      addEventListener(type, callback) {this.handlers[type] = callback;}});
    return elements.get(id);
  };
  element('job').dataset = {jobId: 'test-job', state};
  vm.runInNewContext(source, {
    document: {getElementById: element},
    navigator: {clipboard: {async writeText(text) {copied = text;}}},
    window: {addEventListener(type, callback) {events[type] = callback;}, print() {printed++;}},
    setTimeout(callback, ms) {assert.equal(ms, 3000); timers.set(++nextTimer, callback); return nextTimer;},
    clearTimeout(id) {timers.delete(id);},
    fetch(url, options) {return new Promise(resolve => requests.push({url, options, resolve}));},
  });
  return {element, events, timers, requests, copied: () => copied, printed: () => printed};
}
function fireTimer(ui) {
  const [id, callback] = ui.timers.entries().next().value;
  ui.timers.delete(id); return callback();
}
function respond(request, progress, state = 'processing') {
  request.resolve({ok: true, status: 200, async json() {
    return {state, progress, message: 'Working', report_text: '<script>literal text</script>', error: null};
  }});
}
async function flush() {await new Promise(resolve => setImmediate(resolve));}

(async () => {
  const ui = setup();
  const stale = fireTimer(ui);
  ui.events.pagehide();
  ui.events.pageshow({persisted: true});
  assert.equal(ui.requests.length, 2);
  respond(ui.requests[1], 80);
  await flush();
  respond(ui.requests[0], 20);
  await stale;
  assert.equal(ui.element('job-progress').value, 80, 'Stale response regressed progress after BFCache');
  assert.equal(ui.timers.size, 1, 'BFCache created overlapping polling timers');
  const final = fireTimer(ui);
  respond(ui.requests[2], 100, 'completed');
  await final;
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.element('cancel-form').hidden, true);
  assert.equal(ui.element('report-section').hidden, false);
  assert.equal(ui.element('report-text').textContent, '<script>literal text</script>');
  await ui.element('copy-report').handlers.click();
  assert.equal(ui.copied(), '<script>literal text</script>');
  ui.element('print-report').handlers.click();
  assert.equal(ui.printed(), 1);
  for (const state of ['completed', 'failed', 'cancelled']) assert.equal(setup(state).timers.size, 0);
  console.log('PASS: BFCache stale response isolation, single timer, terminal stop, literal rendering, copy, print');
})().catch(error => {console.error(error); process.exitCode = 1;});
