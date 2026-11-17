// Run with: node --test tests/test_boilerexams_ui.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('normal Add course survives delayed and failed Boilerexams lookup', async () => {
  const nodes = new Map();
  function node(key) {
    if (!nodes.has(key)) nodes.set(key, { innerHTML: '', value: '', events: {},
      querySelector: node,
      addEventListener(name, fn) { this.events[name] = fn; } });
    return nodes.get(key);
  }
  const requests = [];
  const context = vm.createContext({
    document: { getElementById: node, querySelector: node, querySelectorAll: () => [], createElement: () => ({}) },
    window: { addEventListener() {} }, navigator: {},
    fetch: async (url) => {
      if (url === '/api/courses') return { ok: true, json: async () => ({ courses: [] }) };
      assert.equal(url, '/api/boilerexams');
      return new Promise((resolve, reject) => requests.push({ resolve, reject }));
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../src/study_deck/static/app.js'), 'utf8');
  // Exercise production form handlers and catalog rendering without starting the router.
  vm.runInContext(source.replace(/\nroute\(\);\s*$/, ''), context);
  vm.runInContext(`
    me = {name: '', max_deck_size: 100, brightspace: {}, genai: {configured: false}};
    initSeg = () => {};
    renderBlocks = () => renderStyleOption();
  `, context);
  await vm.runInContext('hostView()', context);
  assert.doesNotMatch(node('main').innerHTML, /Add Boilerexams course|boilerexamsPicker/);
  const form = node('#manualForm');
  form.manual = { value: 'phys 17200' };
  form.events.submit({ preventDefault() {}, currentTarget: form });
  assert.equal(vm.runInContext('setup.entries[0].kind', context), 'course');
  assert.match(node('#styleOption').innerHTML, /Checking for past exam/);
  requests[0].reject(new Error('Temporary network failure'));
  await new Promise(setImmediate);
  assert.match(node('#styleOption').innerHTML, /Could not check Boilerexams/);
  node('#retryBoilerexams').events.click();
  requests[1].resolve({ ok: true, json: async () => ({ enabled: true, courses: ['PHYS17200'] }) });
  await new Promise(setImmediate);
  assert.equal(vm.runInContext('onBoilerexams(setup.entries[0].name)', context), 'PHYS17200');
  assert.match(node('#styleOption').innerHTML, /Use past exam questions/);
  assert.match(node('#styleOption').innerHTML, /checked/);
  assert.equal(vm.runInContext('setup.entries.length', context), 1);
  // A failed server response also retries on the next ordinary Add course action.
  vm.runInContext('boilerexams = {enabled: true, courses: [], unreachable: true}', context);
  form.manual.value = 'MA 16100';
  form.events.submit({ preventDefault() {}, currentTarget: form });
  assert.equal(requests.length, 3);
  requests[2].resolve({ ok: true, json: async () => ({ enabled: true, courses: ['PHYS17200', 'MA16100'] }) });
  await new Promise(setImmediate);
  assert.match(node('#styleOption').innerHTML, /PHYS 17200, MA 16100/);
});
