const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app/static/inventory.js', 'utf8');

function element(dataset = {}) {
  return {
    dataset, hidden: false, value: '', textContent: '', attributes: {}, handlers: {},
    addEventListener(type, callback) { this.handlers[type] = callback; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
  };
}

const rows = [
  element({state: 'non_abbinato', search: 'governance delle holding mario rossi'}),
  element({state: 'da_verificare', search: 'trust anna bianchi'}),
  element({state: 'pronto_academy', search: 'fiscalita luca verdi'}),
];
const buttons = [element({inventoryFilter: 'all'}), element({inventoryFilter: 'review'}), element({inventoryFilter: 'ready'})];
const search = element();
const status = element();
const inventory = element();

vm.runInNewContext(source, {
  document: {
    getElementById(id) { return {inventory, 'inventory-search': search, 'inventory-status': status}[id] || null; },
    querySelectorAll(selector) {
      if (selector === '[data-course-row]') return rows;
      if (selector === '[data-inventory-filter]') return buttons;
      return [];
    },
  },
});

buttons[1].handlers.click();
assert.deepEqual(rows.map(row => row.hidden), [false, false, true]);
assert.equal(buttons[1].attributes['aria-pressed'], 'true');

buttons[0].handlers.click();
search.value = 'fiscalita';
search.handlers.input();
assert.deepEqual(rows.map(row => row.hidden), [true, true, false]);
assert.match(status.textContent, /1/);

console.log('PASS: inventory state and text filters');
