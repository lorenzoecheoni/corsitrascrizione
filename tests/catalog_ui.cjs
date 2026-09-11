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
    handlers: {},
    addEventListener(type, callback) {
      this.handlers[type] = callback;
    },
  };
}

function setup() {
  const elements = new Map();
  const rows = [
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

  vm.runInNewContext(source, {
    document: {
      getElementById(id) { return elements.get(id) || null; },
      querySelectorAll(selector) {
        if (selector === '[data-video-row]') return rows;
        if (selector === '[data-video-select]') return selects;
        return [];
      },
    },
  });
  return {elements, rows, selects};
}

function fire(element, type) {
  element.handlers[type]();
}

const ui = setup();
const search = ui.elements.get('catalog-search');
const status = ui.elements.get('status-filter');
const collection = ui.elements.get('collection-filter');
const selectedOnly = ui.elements.get('selected-only');

assert.equal(ui.elements.get('selection-bar').hidden, true, 'The empty selection bar stays hidden');
assert.equal(ui.elements.get('selection-count').textContent, '0');
assert.equal(ui.elements.get('selection-duration').textContent, '00:00');

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

console.log('PASS: catalog filters and selected-only workflow');
