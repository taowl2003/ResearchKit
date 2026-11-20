'use strict';
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
function all(root, predicate) { const result = []; const walk = n => { if (predicate(n)) result.push(n); (n.children || []).forEach(walk); }; walk(root); return result; }
function node(tagName = '') {
  return {tagName, value: '', textContent: '', children: [], disabled: false, hidden: false, checked: false, events: {},
    append(...items) { items.forEach(n => { n.parentNode = this; this.children.push(n); }); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    addEventListener(type, cb) { this.events[type] = cb; }, setAttribute() {},
    querySelectorAll() { return all(this, n => ['input', 'select', 'textarea'].includes(n.tagName)); }};
}
function setup(detail, post, onPublished) {
  const context = {window: {}, document: {createElement: node}};
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/review-batch.js', 'utf8'), context);
  const host = node('section'); context.window.ResearchKitReviewBatch.render(host, detail, {submissionId: 'fixed-1', post, onPublished});
  const resourceRows = all(host, n => n.className === 'review-batch-resource');
  const preview = () => all(host, n => n.id === 'review-batch-preview')[0];
  const confirm = () => all(host, n => n.id === 'review-batch-confirm')[0];
  const field = (row, title) => all(row, n => n.tagName === 'label' && n.children[0]?.textContent === title)[0].children[1];
  const choose = row => { const check = all(row, n => n.type === 'checkbox')[0]; check.checked = true; check.onchange?.(); };
  const fill = (row, t = '测试', d = '资源说明') => { field(row, '标签（逗号分隔；新版本可留空继承）').value = t; field(row, '说明（新版本可留空继承）').value = d; };
  return {host, resourceRows, preview, confirm, choose, fill, field};
}
const detail = {submission: {content_sha256: 'a'.repeat(64), project: {name: '多资源案例'}},
  algorithms: [{alias: 'one', name: 'local.one', version: '1.0.0', is_local_plugin: true}, {alias: 'two', name: 'local.two', version: '2.0.0', is_local_plugin: true}],
  datasets: [{dataset_id: 'data-one', name: '处理结果一', needs_resolution: true}, {dataset_id: 'data-two', name: '处理结果二', needs_resolution: true}]};
(async () => {
  const calls = []; let finish;
  const app = setup(detail, async (path, payload) => { calls.push({path, payload}); return new Promise(resolve => { finish = resolve; }); }, async () => { throw new Error('刷新失败'); });
  app.preview().onclick(); assert.equal(app.confirm(), undefined, 'empty selection cannot preview');
  app.resourceRows.slice(1).forEach(app.choose); app.preview().onclick(); assert.equal(app.confirm(), undefined, 'each new resource needs its own metadata');
  app.resourceRows.slice(1).forEach(row => app.fill(row)); app.preview().onclick();
  const running = app.confirm().onclick(); await app.confirm().onclick();
  assert.equal(calls.length, 1, 'double click dispatches only one atomic batch');
  assert.equal(calls[0].path, '/api/review/submissions/fixed-1/publish-batch');
  const items = calls[0].payload.items;
  assert.equal(items.length, 4); assert.deepEqual(Array.from(items, r => r.kind), ['algorithm', 'algorithm', 'derived', 'derived']);
  assert.deepEqual(Array.from(items.slice(0, 2), r => r.alias), ['one', 'two']);
  assert.deepEqual(Array.from(items.slice(2), r => r.dataset_id), ['data-one', 'data-two']);
  assert.equal(calls[0].payload.expected_sha256, 'a'.repeat(64));
  finish({ok: true, published: [{ref: 'local.one@1.0.0'}, {ref: 'local.two@2.0.0'}, {dataset_id: 'data-one'}, {dataset_id: 'data-two'}]}); await running;
  assert.ok(all(app.host, n => n.className === 'action-result success' && /入库成功.*local.two.*data-two.*暂未刷新/.test(n.textContent)).length);
  await app.confirm().onclick(); assert.equal(calls.length, 1, 'successful batch cannot be republished');
  const mixed = setup({...detail, algorithms: [...detail.algorithms, {alias: 'changed', name: 'platform.alg', version: '1.0.0', upstream_ref: 'platform.alg@1.0.0', suggested_next_version: '1.0.1', modified_platform_algorithm: true}], existing_assets: {algorithms: [{ref: 'platform.alg@1.0.0'}]}}, async () => ({ok: false, failures: [{error: '版本冲突'}]}));
  mixed.choose(mixed.resourceRows[0]); mixed.fill(mixed.resourceRows[0]);
  const forced = mixed.resourceRows[3];
  assert.equal(all(forced, n => n.type === 'checkbox')[0].checked, true); assert.equal(all(forced, n => n.type === 'checkbox')[0].disabled, true);
  mixed.preview().onclick(); assert.ok(mixed.confirm()); await mixed.confirm().onclick();
  assert.ok(all(mixed.host, n => /入库失败.*版本冲突/.test(n.textContent)).length);
  assert.equal(mixed.field(mixed.resourceRows[0], '说明（新版本可留空继承）').value, '资源说明', 'failed atomic publish preserves edits');
  assert.equal(all(forced, n => n.type === 'checkbox')[0].disabled, true, 'forced algorithm stays protected after failure');
  mixed.field(mixed.resourceRows[0], '说明（新版本可留空继承）').value = '修改后的说明';
  await mixed.confirm().onclick(); assert.ok(all(mixed.host, n => /选项已变化/.test(n.textContent)).length, 'editing a preview requires a fresh preview');
  const html = fs.readFileSync('core/researchkit/web_static/index.html', 'utf8'), css = fs.readFileSync('core/researchkit/web_static/app.css', 'utf8');
  assert.doesNotMatch(html, /submission-owner/); assert.match(css, /\[hidden\]\{display:none!important\}/);
  console.log('Batch review selection, atomic request, metadata, forced version, retry and visibility tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
