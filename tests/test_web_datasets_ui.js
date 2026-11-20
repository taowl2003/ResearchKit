'use strict';

// Pure selection/manifest behavior is exercised through the real app hooks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node() {
  return {textContent: '', hidden: false, disabled: false, children: [], dataset: {},
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    querySelector() { return this.button; }, remove() {}};
}

const ids = ['#notice', '#output', '#create', '#import', '#package-import', '#editor', '#save',
  '#compare-left', '#compare-right', '#compare', '#baseline-compare', '#component-access-nav', '#demo',
  '#jobs-back', '#download-data-references'];
const nodes = new Map(ids.map(id => [id, node()]));
const context = {
  window: {__researchkitAppTest: true}, location: {hash: ''}, history: {replaceState() {}}, URLSearchParams,
  sessionStorage: {getItem: () => null, setItem() {}}, Headers: class { set() {} }, File: class {}, FormData: class {},
  URL: {createObjectURL() { return 'blob:test'; }, revokeObjectURL() {}}, setTimeout() { return 1; }, clearTimeout() {},
  document: {querySelector: id => nodes.get(id), querySelectorAll: () => [], addEventListener() {}, dispatchEvent() {}, createElement: () => node(), body: {append() {}}},
  fetch: async () => { throw new Error('dataset codec must not fetch'); }, confirm: () => true, CustomEvent: class {},
};
vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/app.js', 'utf8'), context, {filename: 'app.js'});
const hooks = context.window.__researchkitAppTest;

const chosen = hooks.dataReferences([{
  dataset_id: 'rainfall-2026-v1', path: 'daily/2026-01.csv', size: 12,
  sha256: 'a'.repeat(64), host_path: '/srv/private/rainfall/daily/2026-01.csv',
}]);
assert.deepEqual(JSON.parse(JSON.stringify(chosen)), [{
  dataset_id: 'rainfall-2026-v1', path: 'daily/2026-01.csv', size: 12, sha256: 'a'.repeat(64),
}]);
assert.ok(!JSON.stringify(chosen).includes('/srv/private'));
assert.equal(hooks.runnerDatasetPath(chosen[0]), '/datasets/rainfall-2026-v1/daily/2026-01.csv');
assert.deepEqual(JSON.parse(JSON.stringify(hooks.dataReferences([]))), []);

assert.throws(() => hooks.dataReferences([{
  dataset_id: 'large-v1', path: 'blob.bin', size: 512 * 1024 * 1024 + 1, sha256: 'b'.repeat(64),
}]), /512 MiB/);
assert.throws(() => hooks.dataReferences(Array.from({length: 129}, (_, index) => ({
  dataset_id: 'many-v1', path: `part-${index}`, size: 1, sha256: 'c'.repeat(64),
}))), /128/);
assert.throws(() => hooks.dataReferences(Array.from({length: 5}, (_, index) => ({
  dataset_id: 'total-v1', path: `part-${index}`, size: 512 * 1024 * 1024, sha256: 'd'.repeat(64),
}))), /2 GiB/);
