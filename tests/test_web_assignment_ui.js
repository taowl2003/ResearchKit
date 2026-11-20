'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {
    tagName, textContent: '', value: '', hidden: false, disabled: false, checked: false, type: '', children: [], className: '', dataset: {},
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    querySelectorAll(selector) {
      if (selector === 'input[type="checkbox"]') return collect(this, item => item.tagName === 'input' && item.type === 'checkbox');
      return [];
    },
  };
}
function collect(root, predicate, result = []) { if (predicate(root)) result.push(root); for (const child of root.children || []) collect(child, predicate, result); return result; }
function allText(root) { return [root.textContent || '', ...(root.children || []).map(allText)].join('\n'); }
function find(root, predicate) { if (predicate(root)) return root; for (const child of root.children || []) { const found = find(child, predicate); if (found) return found; } return null; }
function labelledControl(root, labelText) { const label = find(root, item => item.tagName === 'label' && item.textContent === labelText); return label?.children.find(item => ['input', 'select', 'textarea'].includes(item.tagName)); }
async function flush() { for (let index = 0; index < 6; index += 1) await Promise.resolve(); }

function load(role = 'teacher') {
  const ids = ['#resource-library-list', '#resource-library-message', '#assignment-message', '#assignment-form', '#assignment-dispatch',
    '#assignment-preview', '#assignment-preview-panel', '#assignment-preview-content', '#assignment-name', '#assignment-template',
    '#assignment-targets', '#assignment-instructions', '#assignment-composition', '#assignment-datasets', '#assignment-datasets-note'];
  const nodes = new Map(ids.map(id => [id, node(id)]));
  const calls = [], posts = [], listeners = [];
  let nextPreview, dispatchResponse = {submission_id: 'fixed-1', distributions: [{distribution_id: 'one'}, {distribution_id: 'two'}]}, uuid = 0;
  const context = {
    window: {}, document: {
      querySelector: id => nodes.get(id), createElement: tag => node(tag),
      dispatchEvent: event => listeners.push(event),
    }, JSON, Set, Map, Date, Math, globalThis: {crypto: {randomUUID: () => `00000000-0000-4000-8000-${String(++uuid).padStart(12, '0')}`}},
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/assignments.js', 'utf8'), context, {filename: 'assignments.js'});
  const hooks = context.window.ResearchKitAssignments;
  hooks.init({getMe: () => ({role}), api: async path => {
    calls.push(path);
    if (path === '/api/catalog') return {templates: [{ref: 'rain.case@1.0.0', name: '降雨案例', version: '1.0.0'}], plugins: [{ref: 'rain.scale@1.0.0', name: '倍率', version: '1.0.0', parameters: {factor: {required: true, default: 2}}}], contracts: [{id: 'scalar@1.0.0', type: 'number'}]};
    if (path === '/api/datasets') return [{dataset_id: 'rainfall-v1', name: '降雨数据', description: '<img src=x>'}];
    if (path === '/api/derived-datasets?limit=100') return {items: [{dataset_id: 'derived-v1', name: '处理结果'}]};
    if (path === '/api/storage') return {logical_bytes: 300, unique_bytes: 100, saved_bytes: 200, file_references: 3, unique_files: 1};
    throw new Error(`unexpected API ${path}`);
  }, post: (path, body) => {
    posts.push({path, body});
    if (path === '/api/assignments/preview') return nextPreview ? nextPreview.promise : Promise.resolve({template_ref: body.template_ref, composition: {steps: [{alias: 'scale', ref: 'rain.scale@1.0.0', parameters: {factor: 2}}]}, targets: body.targets, data_references: body.dataset_ids});
    if (path === '/api/assignments') return typeof dispatchResponse === 'function' ? dispatchResponse() : Promise.resolve(dispatchResponse);
    throw new Error(`unexpected post ${path}`);
  }});
  return {hooks, nodes, calls, posts, listeners, deferPreview() { let resolve; nextPreview = {promise: new Promise(value => { resolve = value; }), resolve}; return nextPreview; }, setDispatchResponse(value) { dispatchResponse = value; }, clearDeferredPreview() { nextPreview = null; }};
}

(async () => {
  const teacher = load(); await flush();
  assert.ok(teacher.calls.includes('/api/storage'), 'teachers see deduplicated storage overview');
  assert.match(allText(teacher.nodes.get('#resource-library-list')), /案例/);
  assert.match(allText(teacher.nodes.get('#resource-library-list')), /相同文件内容只保存一份/);
  teacher.hooks.enterAssignments(); await flush();
  assert.equal(teacher.nodes.get('#assignment-template').children[1].value, 'rain.case@1.0.0');
  assert.match(allText(teacher.nodes.get('#assignment-datasets')), /<img src=x>/, 'dataset text is never interpreted as HTML');
  const boxes = teacher.nodes.get('#assignment-datasets').querySelectorAll('input[type="checkbox"]'); boxes[0].checked = true;
  teacher.nodes.get('#assignment-name').value = '第一讲'; teacher.nodes.get('#assignment-template').value = 'rain.case@1.0.0';
  teacher.nodes.get('#assignment-targets').value = 'ly, test ly'; teacher.nodes.get('#assignment-instructions').value = '只读数据';
  await teacher.hooks.preview({preventDefault() {}});
  assert.equal(teacher.posts.at(-1).path, '/api/assignments/preview');
  assert.deepEqual(JSON.parse(JSON.stringify(teacher.posts.at(-1).body.targets)), ['ly', 'test']);
  assert.equal(teacher.posts.at(-1).body.composition, null, 'preview lets the server recover the exact case composition');
  assert.equal(teacher.nodes.get('#assignment-dispatch').disabled, false);
  assert.match(allText(teacher.nodes.get('#assignment-preview-content')), /rain.scale@1.0.0/);
  const addedRef = labelledControl(teacher.nodes.get('#assignment-preview-content'), '算法版本');
  const addedAlias = labelledControl(teacher.nodes.get('#assignment-preview-content'), '步骤别名');
  const addedInputs = labelledControl(teacher.nodes.get('#assignment-preview-content'), '输入绑定 JSON（必须明确填写）');
  addedRef.value = 'rain.scale@1.0.0'; addedRef.onchange(); addedAlias.value = 'second_scale'; addedInputs.value = '{"value":{"kind":"literal","value":3}}';
  const addAlgorithm = find(teacher.nodes.get('#assignment-preview-content'), item => item.tagName === 'button' && item.textContent === '加入任务编排');
  addAlgorithm.onclick();
  assert.equal(JSON.parse(teacher.nodes.get('#assignment-composition').value).steps.at(-1).alias, 'second_scale', 'an additional algorithm uses an explicit alias and input binding');
  await teacher.hooks.dispatch();
  assert.equal(teacher.posts.at(-1).path, '/api/assignments');
  assert.equal(teacher.posts.at(-1).body.composition.steps.at(-1).alias, 'second_scale');
  assert.equal(teacher.listeners.at(-1).type, 'researchkit:distributions-changed');
  assert.equal(teacher.nodes.get('#assignment-dispatch').disabled, true, 'a successful dispatch stays disabled until a fresh preview');

  const retry = load(); await flush(); retry.hooks.enterAssignments(); await flush();
  retry.nodes.get('#assignment-name').value = '可重试'; retry.nodes.get('#assignment-template').value = 'rain.case@1.0.0'; retry.nodes.get('#assignment-targets').value = 'test';
  await retry.hooks.preview({preventDefault() {}});
  retry.setDispatchResponse(() => Promise.reject(new Error('temporary network error')));
  await retry.hooks.dispatch(); const firstDispatch = retry.posts.at(-1).body;
  retry.setDispatchResponse({submission_id: 'retry-fixed', distributions: []}); await retry.hooks.dispatch();
  assert.equal(retry.posts.at(-1).body.request_id, firstDispatch.request_id, 'network retries use the same idempotency UUID');
  retry.nodes.get('#assignment-name').value = '字段已改'; retry.nodes.get('#assignment-name').oninput();
  assert.notEqual(retry.hooks._test.payload(false).request_id, firstDispatch.request_id, 'editing task fields creates a new UUID');

  const changed = load(); await flush(); changed.hooks.enterAssignments(); await flush();
  changed.nodes.get('#assignment-name').value = '改前'; changed.nodes.get('#assignment-template').value = 'rain.case@1.0.0'; changed.nodes.get('#assignment-targets').value = 'test';
  await changed.hooks.preview({preventDefault() {}}); const beforeChange = changed.hooks._test.payload(true).request_id;
  changed.nodes.get('#assignment-targets').value = 'test, ly'; changed.nodes.get('#assignment-targets').oninput();
  assert.notEqual(changed.hooks._test.payload(true).request_id, beforeChange, 'a changed field creates a new idempotency UUID before dispatch');

  const checkbox = load(); await flush(); checkbox.hooks.enterAssignments(); await flush();
  checkbox.nodes.get('#assignment-name').value = '数据变动'; checkbox.nodes.get('#assignment-template').value = 'rain.case@1.0.0'; checkbox.nodes.get('#assignment-targets').value = 'test';
  await checkbox.hooks.preview({preventDefault() {}}); checkbox.nodes.get('#assignment-datasets').onchange();
  assert.equal(checkbox.nodes.get('#assignment-preview-panel').hidden, true, 'changing a selected dataset invalidates the preview');
  assert.equal(checkbox.nodes.get('#assignment-dispatch').disabled, true);

  const manual = load(); await flush(); manual.hooks.enterAssignments(); await flush();
  manual.nodes.get('#assignment-name').value = '手工编排'; manual.nodes.get('#assignment-template').value = 'rain.case@1.0.0'; manual.nodes.get('#assignment-targets').value = 'test';
  await manual.hooks.preview({preventDefault() {}});
  manual.nodes.get('#assignment-composition').value = JSON.stringify({parameters: {}, steps: [{alias: 'scale', ref: 'rain.scale@1.0.0', parameters: {factor: 9}, inputs: {}}], draft_contracts: [], generate_entry: false});
  assert.equal(manual.hooks._test.payload(true).composition.steps[0].parameters.factor, 9, 'old parameter controls never overwrite explicitly edited JSON');
  assert.equal(manual.hooks._test.payload(true).algorithm_parameters, undefined, 'managed JSON is not mixed with legacy parameter overrides');

  const legacy = load(); await flush(); legacy.hooks.enterAssignments(); await flush();
  legacy.nodes.get('#assignment-name').value = '旧案例'; legacy.nodes.get('#assignment-template').value = 'rain.case@1.0.0'; legacy.nodes.get('#assignment-targets').value = 'test';
  legacy.clearDeferredPreview();
  // The backend may preserve an old case as entry code and return no managed composition.
  const delayedLegacy = legacy.deferPreview(); const legacyPreview = legacy.hooks.preview({preventDefault() {}}); await flush();
  delayedLegacy.resolve({template_ref: 'rain.case@1.0.0', composition: null, algorithm_bindings: {scale: {ref: 'rain.scale@1.0.0', parameters: {factor: 2}}}}); await legacyPreview;
  assert.equal(legacy.nodes.get('#assignment-composition').value, '', 'a legacy case does not retain stale managed JSON');
  assert.equal(legacy.hooks._test.payload(true).composition, null, 'legacy task creation sends no stale composition');

  const stale = load(); await flush(); stale.hooks.enterAssignments(); await flush();
  stale.nodes.get('#assignment-name').value = '旧任务'; stale.nodes.get('#assignment-template').value = 'rain.case@1.0.0'; stale.nodes.get('#assignment-targets').value = 'test';
  const deferred = stale.deferPreview(); const old = stale.hooks.preview({preventDefault() {}}); await flush();
  stale.nodes.get('#assignment-name').oninput();
  deferred.resolve({template_ref: 'rain.case@1.0.0', composition: {steps: [{alias: 'old', ref: 'old@1'}]}}); await old;
  assert.equal(stale.nodes.get('#assignment-preview-panel').hidden, true, 'late preview never overwrites edited task fields');

  const student = load('student'); await flush();
  student.hooks.enterAssignments(); await flush();
  assert.equal(student.calls.includes('/api/datasets'), false, 'students cannot load teacher task controls');
  assert.equal(student.posts.length, 0);
  process.stdout.write('web assignment UI tests passed\n');
})().catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
