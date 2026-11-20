'use strict';

// Minimal DOM harness for the package controls. No browser or npm dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node() {
  return {
    textContent: '', disabled: false, hidden: false, value: '', onclick: null, onsubmit: null,
    append(...children) { this.children.push(...children); }, children: [], replaceChildren(...children) { this.children = children; },
    remove() { this.removed = true; }, querySelector() { return this.button; },
  };
}

function load(fetch, session = {}) {
  const selectors = ['#notice', '#output', '#create', '#import', '#package-import', '#editor', '#save', '#compare-left', '#compare-right', '#compare', '#baseline-compare', '#component-access-nav', '#demo', '#jobs-back'];
  const nodes = new Map(selectors.map(selector => [selector, node()]));
  const downloads = [], revoked = [];
  class FakeFile { constructor(size) { this.size = size; } }
  class FakeFormData { constructor(form) { this.values = form.values; } get(key) { return this.values.get(key); } }
  const context = {
    window: {__researchkitAppTest: true},
    location: {hash: ''}, history: {replaceState() {}}, URLSearchParams,
    sessionStorage: {getItem: key => session[key] || null, setItem(key, value) { session[key] = value; }},
    Headers: class { constructor() {} set() {} }, File: FakeFile, FormData: FakeFormData,
    URL: {createObjectURL: () => 'blob:local-package', revokeObjectURL: value => revoked.push(value)}, setTimeout: callback => callback(),
    document: {
      querySelector: selector => nodes.get(selector), querySelectorAll: () => [], addEventListener() {},
      createElement: tag => { const value = node(); value.tagName = tag; value.click = () => downloads.push(value.href); return value; },
      body: {append(value) { value.appended = true; }}, dispatchEvent() {},
    }, fetch, confirm: () => true, CustomEvent: class {},
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/app.js', 'utf8'), context, {filename: 'app.js'});
  return {hooks: context.window.__researchkitAppTest, nodes, downloads, revoked, FakeFile};
}

(async () => {
  const failedExport = load(async () => ({ok: false, status: 403, json: async () => ({error: 'not allowed'})}));
  const failedButton = node();
  await failedExport.hooks.packageExport('submission-a', failedButton);
  assert.equal(failedButton.disabled, false);
  assert.deepEqual(failedExport.downloads, []);
  assert.equal(failedExport.nodes.get('#output').textContent, 'not allowed');

  const successfulExport = load(async (path, options) => {
    assert.equal(path, '/api/submissions/submission-a/package');
    assert.equal(options.headers.Authorization, 'Bearer ');
    return {ok: true, blob: async () => ({})};
  });
  const exportButton = node();
  await successfulExport.hooks.packageExport('submission-a', exportButton);
  assert.deepEqual(successfulExport.downloads, ['blob:local-package']);
  assert.equal(successfulExport.revoked[0], 'blob:local-package');
  assert.ok(!successfulExport.downloads[0].includes('Bearer'));
  assert.ok(successfulExport.hooks.sourceLabel({source: 'teacher-unassigned'}).includes('未指派'));
  assert.ok(successfulExport.hooks.sourceLabel({source: 'teacher-unassigned'}).includes('不能标为任务'));
  assert.deepEqual([...successfulExport.hooks.targetNames(' alice, bob, alice ,, ')], ['alice', 'bob']);

  const failedImport = load(async () => ({ok: false, status: 400, json: async () => ({error: 'bad zip'})}));
  const form = {values: new Map([['name', 'keep-name'], ['package', new failedImport.FakeFile(10)]]), resets: 0, reset() { this.resets += 1; }};
  const importButton = node();
  await failedImport.hooks.importPackage(form, importButton);
  assert.equal(form.resets, 0);
  assert.equal(importButton.disabled, false);
  assert.equal(failedImport.nodes.get('#output').textContent, 'bad zip');
})().catch(error => { console.error(error); process.exitCode = 1; });
