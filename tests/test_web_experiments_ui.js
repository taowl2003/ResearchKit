'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.parentNode = null; this._text = ''; this._value = ''; this._index = 0; this.className = ''; this.disabled = false; this.checked = false; this.attributes = []; this.classList = {add: c => { this.className += ` ${c}`; }}; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set value(value) { if (this.tagName === 'select') this._index = this.children.findIndex(c => c.value === value); else this._value = String(value); }
  get value() { return this.tagName === 'select' ? (this.children[this._index]?.value || '') : this._value; }
  get options() { return this.children; }
  set selectedIndex(value) { this._index = value; }
  get selectedIndex() { return this._index; }
  append(...nodes) { nodes.forEach(n => { if (typeof n === 'string') { const text = new Node('#text'); text.textContent = n; n = text; } n.parentNode = this; this.children.push(n); }); }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(n => n !== this); }
  querySelector(tag) { return all(this, n => n !== this && n.tagName === tag)[0] || null; }
  click() { return this.onclick?.(); }
}
function all(root, predicate) { return [root, ...root.children.flatMap(n => all(n, () => true))].filter(predicate); }
const tag = (root, name) => all(root, n => n.tagName === name);
const button = (root, label) => all(root, n => n.tagName === 'button' && n.textContent === label)[0];
function field(root, label) { return all(root, n => n.tagName === 'label' && n.children[0]?.textContent === label)[0]?.children[1]; }
const change = (node, value) => { node.value = value; node.oninput?.(); };
const tick = () => new Promise(resolve => setImmediate(resolve));
const raw = fs.readFileSync('core/researchkit/web_static/experiments.js', 'utf8');
const appRaw = fs.readFileSync('core/researchkit/web_static/app.js', 'utf8');
const data = {
  has_runner: true, defaults: {case_parameters: {limit: 10, enabled: false}, algorithm_parameters: {scale: {factor: 2}}}, algorithm_rules: {},
  schemes: [{id: 'baseline', sha256: 'a'.repeat(64), document: {schema: 'researchkit.experiment/v1', name: '基线', description: '原方案', case_parameters: {}, algorithm_parameters: {scale: {factor: 2}}}}],
  runs: [
    {scheme_id: 'baseline', batch_id: 'old', record: {status: 'succeeded', parameters: {case: {factor: 2}}, summary: {accuracy: 0.8, error: {rmse_m: 10}}}, files: [{path: 'results/baseline/old/summary.json', size: 100}]},
    {scheme_id: 'baseline', batch_id: 'new', record: {status: 'succeeded', parameters: {case: {factor: 3}}, summary: {accuracy: 0.9, error: {rmse_m: 8}}}, files: [{path: 'results/baseline/new/summary.json', size: 100}]},
    {scheme_id: 'running', batch_id: 'active', record: {status: 'running'}, files: [], revision: 'b'.repeat(64)},
  ],
  result_files: [{path: 'results/legacy.txt', size: 10}], warnings: [],
};
function setup() {
  const calls = [], fetched = [], paths = [], doc = {createElement: tag => new Node(tag), body: new Node('body')};
  const context = vm.createContext({window: {}, document: doc, URLSearchParams, URL: {createObjectURL: () => 'blob:local', revokeObjectURL: () => {}}, setTimeout: f => f(), confirm: () => true, console});
  vm.runInContext(raw, context);
  const ui = context.window.ResearchKitExperiments.mount({
    api: async path => { paths.push(path); return JSON.parse(JSON.stringify(data)); },
    post: async (path, body) => { calls.push({path, body: JSON.parse(JSON.stringify(body))}); return {}; },
    fetchFile: async path => { fetched.push(path); return {ok: false, status: 403, headers: {get: () => 'text/plain'}, text: async () => 'SECRET ERROR BODY'}; },
  });
  return {ui, context, calls, fetched, paths};
}
(async () => {
  const test = setup(), host = new Node('div');
  await test.ui.renderProject(host, {project_id: 'p-1'});
  assert.match(host.textContent, /accuracy: 0.8 → 0.9/);
  assert.match(host.textContent, /error.rmse_m: 10 → 8/);
  assert.match(host.textContent, /case.factor: 2 → 3/);
  assert.match(host.textContent, /其他已有产物/);
  assert.equal(button(all(host, n => n.className === 'experiment-run')[2], '删除此批次产物').disabled, true);
  const form = tag(host, 'form')[0];
  change(field(form, '方案 ID'), 'new-scheme'); change(field(form, '方案名称'), '第二方案'); change(field(form, '说明'), '更改倍率');
  change(field(form, 'factor'), '5'); field(form, 'enabled').checked = true; field(form, 'enabled').onchange();
  await button(form, '保存方案').onclick();
  assert.equal(test.calls.length, 1);
  assert.deepEqual(Object.keys(test.calls[0].body).sort(), ['document', 'expected_sha256', 'id']);
  assert.deepEqual(Object.keys(test.calls[0].body.document).sort(), ['algorithm_parameters', 'case_parameters', 'description', 'name', 'schema']);
  assert.equal(test.calls[0].body.document.algorithm_parameters.scale.factor, 5);
  assert.equal(test.calls[0].body.document.case_parameters.enabled, true);
  assert.equal(test.calls[0].body.expected_sha256, null);
  assert.match(host.textContent, /已保存方案：new-scheme/);
  const invalidForm = tag(host, 'form')[0]; change(field(invalidForm, '方案 ID'), 'invalid'); change(field(invalidForm, '方案名称'), 'bad'); change(field(invalidForm, 'factor'), 'oops');
  await button(invalidForm, '保存方案').onclick(); assert.equal(test.calls.length, 1, 'invalid scalar must never silently save stale JSON');
  button(host, '编辑').onclick();
  const editing = tag(host, 'form').at(-1); assert.equal(field(editing, '方案 ID').disabled, true);
  change(field(editing, 'factor'), '7'); await button(editing, '保存方案').onclick();
  assert.equal(test.calls[1].body.expected_sha256, 'a'.repeat(64));
  button(host, '复制').onclick(); const copy = tag(host, 'form').at(-1);
  assert.equal(field(copy, '方案 ID').value, 'baseline-copy');
  const advanced = tag(copy, 'textarea').find(node => node.rows === 10);
  const advancedValue = JSON.parse(advanced.value); advancedValue.case_parameters.limit = 77;
  advanced.value = JSON.stringify(advancedValue); advanced.oninput();
  change(field(copy, '方案名称'), '复杂参数方案');
  assert.equal(field(copy, 'factor').disabled, true);
  await button(copy, '保存方案').onclick(); assert.equal(test.calls[2].body.expected_sha256, null);
  assert.equal(test.calls[2].body.document.case_parameters.limit, 77, 'renaming after JSON editing must retain advanced parameters');


  const readonly = new Node('div'); await test.ui.renderSubmission(readonly, 'fixed-1', {review: true});
  assert.equal(tag(readonly, 'form').length, 0); assert.equal(button(readonly, '删除此批次产物'), undefined);
  assert.equal(button(readonly, '保存方案'), undefined);
  button(readonly, '预览').onclick(); await tick();
  assert.match(test.fetched[0], /^\/api\/review\/submissions\/fixed-1\/result-file\?path=/);
  assert.doesNotMatch(test.fetched[0], /token|Bearer/);
  assert.match(readonly.textContent, /预览失败：HTTP 403/); assert.doesNotMatch(readonly.textContent, /SECRET ERROR BODY/);
  button(readonly, '下载').onclick(); await tick(); assert.match(test.fetched.at(-1), /download=true/);

  // Execute the actual app submission selector with a minimal DOM, including
  // unsorted runs and a large artifact. This catches choosing the oldest batch.
  const output = new Node('div'), previewCalls = [];
  const rows = [{path: 'results/s/old/a.json', size: 10}, {path: 'results/s/new/b.json', size: 20}, {path: 'results/s/new/model.pt', size: 3 * 1024 * 1024}, {path: 'results/f/failed/partial.csv', size: 5}];
  const submissionContext = vm.createContext({dirty: false, Set, Map, document: {createElement: tag => new Node(tag)}, output,
    text: (n, value) => {n.textContent = value;}, card: () => new Node('article'), heading: (n, value) => {const h = new Node('h3');h.textContent=value;n.append(h);},
    paragraph: (n, value) => {const p = new Node('p');p.textContent=value;n.append(p);}, span: value => {const n = new Node('span');n.textContent=value;return n;},
    button: (label, action) => {const n = new Node('button');n.textContent=label;n.onclick=action;return n;}, displayBytes: n => `${n} B`,
    api: async () => ({runs: [{scheme_id: 's', batch_id: 'old', record: {status: 'succeeded', started_at: '2026-01-01'}, files: [rows[0]]}, {scheme_id: 's', batch_id: 'new', record: {status: 'succeeded', started_at: '2026-01-02'}, files: [rows[1], rows[2]]}], result_files: rows}),
    post: async (path, payload) => {previewCalls.push({path,payload}); return {};}, renderSubmissionConfirmation: () => {},
  });
  vm.runInContext(appRaw.slice(appRaw.indexOf('  async function submit('), appRaw.indexOf("  document.querySelector('#create').onsubmit")), submissionContext);
  await submissionContext.submit('p-1', 'minor');
  const checks = tag(output, 'input'); assert.deepEqual(checks.map(n => n.checked), [false,true,false,false]);
  assert.match(output.textContent, /将保存 1 个产物/);
  checks[3].checked=true; checks[3].onchange();
  await button(output, '确认产物选择并预览版本').onclick();
  assert.deepEqual(Array.from(previewCalls[0].payload.result_paths), [rows[3].path, rows[1].path].sort());
  // Maintenance reuses the existing revision-checked delete endpoint. Keep the
  // newest success and running rows; update only the recognized standard helper.
  data.helper_update_available = true;
  data.runs[0].record.started_at = '2026-01-01T00:00:00Z'; data.runs[0].revision = 'c'.repeat(64);
  data.runs[1].record.started_at = '2026-01-02T00:00:00Z'; data.runs[1].revision = 'd'.repeat(64);
  const maintenance = setup(), maintained = new Node('div');
  await maintenance.ui.renderProject(maintained, {project_id:'own-project'});
  await button(maintained, '启用最新结果保存方式').onclick();
  assert.equal(maintenance.calls[0].path, '/api/projects/own-project/experiments/update-helper');
  assert.deepEqual(maintenance.calls[0].body, {});
  await button(maintained, '整理旧产物').onclick();
  assert.equal(maintenance.calls.length, 2);
  assert.equal(maintenance.calls[1].path, '/api/projects/own-project/experiment-results/delete');
  assert.deepEqual(maintenance.calls[1].body, {scheme_id:'baseline',batch_id:'old',
    confirmation:'baseline/old',expected_revision:'c'.repeat(64)});
  assert.match(maintained.textContent, /已整理 1 个旧批次/);
  assert.doesNotMatch(raw, /Bearer |sessionStorage|location\.hash/);
  console.log('Experiment editing, CAS, copying, invalid inputs, artifact comparison, readonly access, errors and selected submission tests passed');
})().catch(error => {console.error(error); process.exitCode=1;});
