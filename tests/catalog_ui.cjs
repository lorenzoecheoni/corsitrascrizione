const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app/static/catalog.js', 'utf8');

function createElement({dataset = {}, value = '', checked = false, hidden = false} = {}) {
  return {
    dataset,
    value,
    checked,
    hidden,
    textContent: '',
    attributes: {},
    handlers: {},
    addEventListener(type, callback) {
      this.handlers[type] = callback;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) { return this.attributes[name]; },
  };
}

function setup(size = 3, storageOptions = {}) {
  const elements = new Map();
  const storageWrites = [];
  let rows = [
    createElement({dataset: {
      videoRow: '', title: 'Corso Python', description: 'Fondamenti pratici',
      status: 'ready', collection: 'academy', duration: '61',
    }}),
    createElement({dataset: {
      videoRow: '', title: 'Sicurezza applicativa', description: 'Firewall e policy',
      status: 'processing', collection: 'academy', duration: '120',
    }}),
    createElement({dataset: {
      videoRow: '', title: 'Design system', description: 'Interfacce accessibili',
      status: 'ready', collection: 'creative', duration: '360',
    }}),
  ];
  if (size !== 3) rows = Array.from({length: size}, (_, index) => createElement({dataset: {
    title: `Video ${index + 1}`, description: '', status: 'ready', collection: 'academy', duration: '60',
  }}));
  const selects = rows.map(row => createElement({dataset: {videoSelect: ''}}));
  elements.set('catalog-search', createElement());
  elements.set('status-filter', createElement({value: 'all'}));
  elements.set('collection-filter', createElement({value: 'all'}));
  elements.set('selected-only', createElement());
  elements.set('select-visible', createElement());
  elements.set('clear-selection', createElement());
  elements.set('selection-count', createElement());
  elements.set('selection-duration', createElement());
  elements.set('selection-bar', createElement({hidden: true}));
  elements.set('catalog-form', createElement());
  elements.set('analyse-selection', createElement());
  elements.set('selection-status', createElement());
  elements.set('catalog-grid', createElement({dataset: {view: 'cards'}}));
  elements.set('catalog-view-cards', createElement());
  elements.set('catalog-view-list', createElement());

  const localStorage = {
    getItem(key) {
      if (storageOptions.throwOnGet) throw new Error('storage unavailable');
      return storageOptions.storedValue ?? null;
    },
    setItem(key, value) {
      if (storageOptions.throwOnSet) throw new Error('storage unavailable');
      storageWrites.push([key, value]);
    },
  };

  vm.runInNewContext(source, {
    document: {
      getElementById(id) { return elements.get(id) || null; },
      querySelectorAll(selector) {
        if (selector === '[data-video-row]') return rows;
        if (selector === '[data-video-select]') return selects;
        return [];
      },
    },
    localStorage,
  });
  return {elements, rows, selects, storageWrites};
}

function fire(element, type) {
  const event = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
  element.handlers[type](event);
  return event;
}

assert.doesNotThrow(() => vm.runInNewContext(source, {
  document: {
    getElementById() { return null; },
    querySelectorAll() { return []; },
  },
}), 'Catalog script ignores dashboard states without catalog controls');

const ui = setup();
const search = ui.elements.get('catalog-search');
const status = ui.elements.get('status-filter');
const collection = ui.elements.get('collection-filter');
const selectedOnly = ui.elements.get('selected-only');

assert.equal(ui.elements.get('selection-bar').hidden, true, 'The empty selection bar stays hidden');
assert.equal(ui.elements.get('selection-count').textContent, '0');
assert.equal(ui.elements.get('selection-duration').textContent, '00:00');
assert.equal(ui.elements.get('catalog-grid').dataset.view, 'cards');
assert.equal(ui.elements.get('catalog-view-cards').getAttribute('aria-pressed'), 'true');
assert.equal(ui.elements.get('catalog-view-list').getAttribute('aria-pressed'), 'false');

fire(ui.elements.get('catalog-view-list'), 'click');
assert.equal(ui.elements.get('catalog-grid').dataset.view, 'list');
assert.equal(ui.elements.get('catalog-view-cards').getAttribute('aria-pressed'), 'false');
assert.equal(ui.elements.get('catalog-view-list').getAttribute('aria-pressed'), 'true');
assert.deepEqual(ui.storageWrites, [['bunny-video-report:catalog-view', 'list']]);

const restored = setup(3, {storedValue: 'list'});
assert.equal(restored.elements.get('catalog-grid').dataset.view, 'list', 'Saved list view is restored');
assert.doesNotThrow(() => {
  const unavailable = setup(3, {throwOnGet: true, throwOnSet: true});
  fire(unavailable.elements.get('catalog-view-list'), 'click');
  assert.equal(unavailable.elements.get('catalog-grid').dataset.view, 'list');
}, 'Catalog view remains usable when browser storage is unavailable');

search.value = 'pYtHoN';
fire(search, 'input');
assert.deepEqual(ui.rows.map(row => row.hidden), [false, true, true], 'Search is case-insensitive over titles');

search.value = '';
fire(search, 'input');
status.value = 'ready';
fire(status, 'change');
collection.value = 'creative';
fire(collection, 'change');
assert.deepEqual(ui.rows.map(row => row.hidden), [true, true, false], 'Status and collection filters combine');

status.value = 'all';
collection.value = 'all';
fire(collection, 'change');
ui.selects[1].checked = true;
fire(ui.selects[1], 'change');
selectedOnly.checked = true;
fire(selectedOnly, 'change');
assert.deepEqual(ui.rows.map(row => row.hidden), [true, false, true], 'Selected-only hides unchecked rows');

selectedOnly.checked = false;
fire(selectedOnly, 'change');
assert.equal(ui.elements.get('catalog-grid').dataset.view, 'list', 'Filtering does not reset list view');
search.value = 'corso';
fire(search, 'input');
fire(ui.elements.get('select-visible'), 'click');
assert.deepEqual(ui.selects.map(select => select.checked), [true, true, false], 'Select visible preserves hidden rows');
assert.equal(ui.elements.get('selection-count').textContent, '2');
assert.equal(ui.elements.get('selection-duration').textContent, '03:01');
assert.equal(ui.elements.get('selection-bar').hidden, false);

fire(ui.elements.get('clear-selection'), 'click');
assert.deepEqual(ui.selects.map(select => select.checked), [false, false, false], 'Clear selection resets every checkbox');
assert.equal(ui.elements.get('selection-count').textContent, '0');
assert.equal(ui.elements.get('selection-duration').textContent, '00:00');
assert.equal(ui.elements.get('selection-bar').hidden, true);

for (const size of [51, 50]) {
  const large = setup(size);
  fire(large.elements.get('select-visible'), 'click');
  assert.equal(large.selects.filter(select => select.checked).length, 50, `${size} visible videos stop at 50`);
  assert.equal(large.elements.get('selection-count').textContent, '50');
  assert.equal(large.elements.get('selection-duration').textContent, '50:00');
  assert.equal(large.elements.get('analyse-selection').disabled, false);
  assert.match(large.elements.get('selection-status').textContent, /50/);
  assert.equal(fire(large.elements.get('catalog-form'), 'submit').defaultPrevented, false);
  if (size === 51) {
    large.selects[50].checked = true;
    fire(large.selects[50], 'change');
    assert.equal(large.selects[50].checked, false, 'Individual selection cannot become the 51st video');
    large.selects[50].checked = true;
    assert.equal(fire(large.elements.get('catalog-form'), 'submit').defaultPrevented, true,
      'Submit guard blocks even an invalid selection introduced without change events');
    assert.equal(large.elements.get('analyse-selection').disabled, true);
    assert.match(large.elements.get('selection-status').textContent, /50/);
  }
}

const remaining = setup(51);
remaining.selects.slice(0, 49).forEach(select => { select.checked = true; });
fire(remaining.selects[48], 'change');
remaining.elements.get('catalog-search').value = 'Video 5';
fire(remaining.elements.get('catalog-search'), 'input');
fire(remaining.elements.get('select-visible'), 'click');
assert.equal(remaining.selects[49].checked, true, 'Visible selection fills the final free slot');
assert.equal(remaining.selects[50].checked, false, 'Visible selection respects hidden selected videos');
assert.equal(remaining.selects.filter(select => select.checked).length, 50);
fire(remaining.elements.get('clear-selection'), 'click');
assert.equal(fire(remaining.elements.get('catalog-form'), 'submit').defaultPrevented, true);
assert.equal(remaining.elements.get('analyse-selection').disabled, true);

console.log('PASS: catalog filters, selection workflow and accessible 50/51 limit');
