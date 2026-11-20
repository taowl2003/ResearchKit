'use strict';

// Asset candidates are now manually reviewed from their immutable submission.
// Opening a candidate must never start, list, or attach a retired Web job.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {tagName, textContent: '', value: '', hidden: false, disabled: false, children: [], className: '', dataset: {},
    append(...items) { items.forEach(item => { if (item) { item.parentNode = this; this.children.push(item); } }); },
    replaceChildren(...items) { this.children = []; this.append(...items); }, querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, remove() {}, click() {}, setAttribute() {}};
}
function find(root, predicate) { if (predicate(root)) return root; for (const child of root.children || []) { const found = find(child, predicate); if (found) return found; } return null; }
function load(fetch) {
  const ids = ['#notice', '#output', '#create', '#import', '#package-import', '#template-ref', '#editor', '#file-name', '#editor-state', '#save', '#compare-left', '#compare-right', '#compare', '#baseline-compare', '#component-access-nav', '#asset-candidates-nav', '#demo', '#download-data-references', '#submission-list', '#submission-more', '#submission-message', '#submission-compare-status', '#asset-message', '#asset-create', '#asset-candidate-list', '#asset-detail', '#asset-detail-title', '#asset-detail-content', '#asset-file-name', '#asset-file-preview'];
  const nodes = new Map(ids.map(id => [id, node(id)]));
  const context = {window: {__researchkitAppTest: true}, location: {hash: ''}, history: {replaceState() {}}, URLSearchParams,
    sessionStorage: {getItem: () => null, setItem() {}}, Headers: class {set() {}}, File: class {}, FormData: class {},
    URL: {createObjectURL: () => 'blob:test', revokeObjectURL() {}}, setTimeout() { return 1; }, clearTimeout() {},
    document: {querySelector: id => nodes.get(id), querySelectorAll: () => [], addEventListener() {}, dispatchEvent() {}, createElement: tag => node(tag), body: {append() {}}},
    fetch, confirm: () => true, CustomEvent: class {}};
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/review-batch.js', 'utf8'), context, {filename: 'review-batch.js'});
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/app.js', 'utf8'), context, {filename: 'app.js'});
  return {hooks: context.window.__researchkitAppTest, nodes};
}

const draft = {candidate_id: 'candidate-1', asset_id: 'rainfall.scale', kind: 'algorithm', name: 'rainfall.scale', version: '1.0.0', ref: null,
  status: 'draft', submission_id: 'submission-1', source_sha256: 'a'.repeat(64), candidate_sha256: 'b'.repeat(64), alias: 'scale',
  metadata: {inputs: {value: 'scalar@1.0.0'}, outputs: {value: 'scalar@1.0.0'}}, static_validation: {ok: true}, data_references: [], files: [{path: 'plugin.py', size: 4, sha256: 'c'.repeat(64)}]};

(async () => {
  const calls = [];
  const app = load(async (path, options = {}) => {
    calls.push({path, options});
    if (path === '/api/asset-candidates') return {ok: true, json: async () => [draft]};
    if (path === '/api/asset-candidates/candidate-1/review') return {ok: true, json: async () => ({...draft, status: 'reviewed', review_notes: 'checked'})};
    if (path === '/api/asset-candidates/candidate-1/publish') return {ok: true, json: async () => ({...draft, status: 'published', ref: 'rainfall.scale@1.0.0'})};
    if (path === '/api/catalog') return {ok: true, json: async () => ({templates: []})};
    if (path === '/api/component-access') return {ok: true, json: async () => []};
    if (path.startsWith('/api/asset-candidates/candidate-1/file?')) return {ok: true, json: async () => ({path: 'plugin.py', text: '<img src=x>'})};
    throw new Error(`unexpected ${path}`);
  });
  app.hooks.setMe({role: 'teacher'});
  await app.hooks.assetCandidates();
  app.hooks.setSelectedAsset('candidate-1'); app.hooks.renderAssetDetail(draft);
  const content = app.nodes.get('#asset-detail-content');
  assert.ok(find(content, item => item.textContent === '来源固定快照'));
  assert.equal(find(content, item => /专项测试|查看日志与结果|运行候选/.test(item.textContent)), null, 'candidate UI has no retired execution controls');
  const form = find(content, item => item.className === 'asset-review-form');
  const notes = find(form, item => item.tagName === 'textarea'); notes.value = 'checked manually';
  await form.onsubmit({preventDefault() {}});
  const review = calls.find(call => call.path.endsWith('/review'));
  assert.deepEqual(JSON.parse(review.options.body), {expected_sha256: 'b'.repeat(64), notes: 'checked manually', validation_run_id: null});
  assert.equal(calls.some(call => /\/api\/jobs|\/validations/.test(call.path)), false, 'manual review does not call retired execution APIs');
  const publish = find(content, item => item.textContent === '发布此版本'); await publish.onclick();
  assert.deepEqual(JSON.parse(calls.find(call => call.path.endsWith('/publish')).options.body), {expected_sha256: 'b'.repeat(64)});
  await app.hooks.openAssetFile('candidate-1', 'plugin.py');
  assert.equal(app.nodes.get('#asset-file-preview').textContent, '<img src=x>', 'fixed candidate files remain text previewed');
})().catch(error => { console.error(error); process.exitCode = 1; });
