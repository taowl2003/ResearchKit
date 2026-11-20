'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {tagName, textContent: '', value: '', hidden: false, disabled: false, type: '', className: '', dataset: {}, children: [],
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    querySelector() { return null; }};
}
function find(root, predicate) { if (predicate(root)) return root; for (const child of root.children || []) { const found = find(child, predicate); if (found) return found; } return null; }
function all(root, predicate, result = []) { if (predicate(root)) result.push(root); for (const child of root.children || []) all(child, predicate, result); return result; }
function textOf(root) { return [root.textContent || '', ...(root.children || []).map(textOf)].join('\n'); }
function button(root, label) { return find(root, value => value.tagName === 'button' && value.textContent === label); }
function flush() { return new Promise(resolve => setImmediate(resolve)); }

function load(api, role = 'teacher') {
  const ids = ['#retired-bin', '#cleanup-bin', '#retired-bin-list', '#cleanup-bin-list', '#retired-bin-message', '#cleanup-bin-message', '#retired-bin-pagination', '#cleanup-bin-pagination', '#retired-bin-detail', '#cleanup-bin-detail', '#retired-bin-kind', '#cleanup-bin-kind', '#retired-bin-query', '#cleanup-bin-query'];
  const nodes = new Map(ids.map(id => [id, node(id)])); nodes.get('#retired-bin').hidden = false; nodes.get('#cleanup-bin').hidden = true;
  const context = {window: {}, URLSearchParams, encodeURIComponent, JSON, setTimeout(fn) { context.timer = fn; return 1; }, clearTimeout() {},
    document: {querySelector: id => nodes.get(id), createElement: tag => node(tag)},
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/cleanup.js', 'utf8'), context, {filename: 'cleanup.js'});
  context.window.ResearchKitCleanup.configure({api, post: (path, body) => api(path, {method: 'POST', body: JSON.stringify(body)}), getMe: () => ({role})});
  return {hooks: context.window.ResearchKitCleanup, nodes, context};
}

(async () => {
  const calls = [], entries = [
    {kind: 'algorithm', ref: 'test.scale@1.0.0', name: '<img src=x>', state: 'retired', revision: 'a', source: 'web'},
    {kind: 'dataset', ref: 'test-data', name: '测试数据', state: 'trashed', revision: 'b', source: 'raw'},
  ];
  const app = load(async (path, options = {}) => {
    calls.push({path, options});
    if (path.startsWith('/api/asset-bin?')) { const trashed = path.includes('state=trashed'); return {items: trashed ? [entries[1]] : [entries[0]], total: 1, offset: 0, limit: 50}; }
    if (path === '/api/asset-lifecycle') return {state: 'active'};
    if (path === '/api/asset-cleanup/preview') return {plan_token: 'plan', can_execute: false, items: [{...entries[1], can_delete: false, blockers: [{label: '固定提交', suggestion: '先删除测试固定提交'}], advisories: [], estimated_bytes: 12}], order: [{kind: 'dataset', ref: 'test-data'}], total_bytes: 12, notes: ['不删除个人项目']};
    throw new Error(`unexpected ${path}`);
  });
  app.hooks.enter('retired'); await flush();
  assert.match(textOf(app.nodes.get('#retired-bin-list')), /<img src=x>/, 'names render as text');
  await button(app.nodes.get('#retired-bin-list'), '打开').onclick();
  assert.equal(button(app.nodes.get('#retired-bin-detail'), '恢复').textContent, '恢复');
  await button(app.nodes.get('#retired-bin-detail'), '恢复').onclick();
  assert.equal(JSON.parse(calls.find(value => value.path === '/api/asset-lifecycle').options.body).action, 'restore');

  app.nodes.get('#retired-bin').hidden = true; app.nodes.get('#cleanup-bin').hidden = false;
  app.hooks.enter('trashed'); await flush();
  const choice = find(app.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox'); choice.checked = true; choice.onchange();
  assert.match(button(app.nodes.get('#cleanup-bin-pagination'), '预览永久清理（已选 1 项）').textContent, /已选 1/);
  await button(app.nodes.get('#cleanup-bin-pagination'), '预览永久清理（已选 1 项）').onclick();
  const preview = calls.find(value => value.path === '/api/asset-cleanup/preview');
  assert.deepEqual(JSON.parse(preview.options.body), {items: [{kind: 'dataset', ref: 'test-data'}]});
  assert.match(textOf(app.nodes.get('#cleanup-bin-detail')), /先删除测试固定提交/);
  assert.match(textOf(app.nodes.get('#cleanup-bin-detail')), /不会顺带删除个人项目/);
  assert.equal(button(app.nodes.get('#cleanup-bin-detail'), '永久清理所选回收站资产'), null, 'blocked plans never render permanent delete');

  const recoveryCalls = [];
  const failedEntry = {...entries[1], cleanup_status: 'failed', request_id: 'failed-request', cleanup_error: 'temporary storage error'};
  const recovering = load(async (path, options = {}) => {
    recoveryCalls.push({path, options});
    if (path.startsWith('/api/asset-bin?')) return {items: [failedEntry], total: 1, offset: 0, limit: 50};
    if (path === '/api/asset-cleanup/failed-request') return {...failedEntry, status: 'failed', error: 'temporary storage error'};
    if (path === '/api/asset-cleanup/failed-request/retry') return {request_id: 'failed-request', status: 'completed'};
    throw new Error(`unexpected ${path}`);
  });
  recovering.nodes.get('#retired-bin').hidden = true; recovering.nodes.get('#cleanup-bin').hidden = false;
  recovering.hooks.enter('trashed'); await flush();
  const failedChoice = find(recovering.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox');
  assert.equal(failedChoice.disabled, true, 'a failed request remains locked to its original confirmed batch');
  await button(recovering.nodes.get('#cleanup-bin-list'), '打开').onclick();
  assert.ok(button(recovering.nodes.get('#cleanup-bin-detail'), '查看清理进度'));
  assert.ok(button(recovering.nodes.get('#cleanup-bin-detail'), '重试这次清理'));
  await button(recovering.nodes.get('#cleanup-bin-detail'), '查看清理进度').onclick();
  assert.ok(recoveryCalls.some(value => value.path === '/api/asset-cleanup/failed-request'));
  await button(recovering.nodes.get('#cleanup-bin-detail'), '重试这次清理').onclick();
  assert.deepEqual(JSON.parse(recoveryCalls.find(value => value.path.endsWith('/retry')).options.body), {}, 'retry has no asset list and cannot expand the confirmed batch');
  assert.match(recovering.nodes.get('#cleanup-bin-message').textContent, /永久清理已完成/, 'an immediately completed retry updates the list and completion message');

  const runningEntry = {...entries[1], cleanup_status: 'running', request_id: 'running-request'};
  const running = load(async path => {
    if (path.startsWith('/api/asset-bin?')) return {items: [runningEntry], total: 1, offset: 0, limit: 50};
    throw new Error(`unexpected ${path}`);
  });
  running.nodes.get('#retired-bin').hidden = true; running.nodes.get('#cleanup-bin').hidden = false;
  running.hooks.enter('trashed'); await flush();
  const runningChoice = find(running.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox');
  assert.equal(runningChoice.disabled, true, 'in-progress cleanup assets cannot be selected again');
  await button(running.nodes.get('#cleanup-bin-list'), '打开').onclick();
  assert.equal(button(running.nodes.get('#cleanup-bin-detail'), '恢复'), null, 'in-progress cleanup assets cannot be restored');

  const batchEntries = Array.from({length: 31}, (_, index) => ({kind: 'dataset', ref: `batch-${index}`, name: `批次 ${index}`, state: 'trashed', revision: String(index), source: 'raw'}));
  const capped = load(async path => {
    if (path.startsWith('/api/asset-bin?')) return {items: batchEntries, total: batchEntries.length, offset: 0, limit: 50};
    throw new Error(`unexpected ${path}`);
  });
  capped.nodes.get('#retired-bin').hidden = true; capped.nodes.get('#cleanup-bin').hidden = false;
  capped.hooks.enter('trashed'); await flush();
  const cappedChoices = all(capped.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox');
  cappedChoices.slice(0, 30).forEach(choice => { choice.checked = true; choice.onchange(); });
  cappedChoices[30].checked = true; cappedChoices[30].onchange();
  assert.equal(cappedChoices[30].checked, false, 'the 31st item is refused in the browser');
  assert.match(capped.nodes.get('#cleanup-bin-message').textContent, /每批最多选择 30 项/);

  let resolveStalePreview;
  const stalePreview = load(path => {
    if (path.startsWith('/api/asset-bin?')) return Promise.resolve({items: [entries[1]], total: 1, offset: 0, limit: 50});
    if (path === '/api/asset-cleanup/preview') return new Promise(resolve => { resolveStalePreview = resolve; });
    throw new Error(`unexpected ${path}`);
  });
  stalePreview.nodes.get('#retired-bin').hidden = true; stalePreview.nodes.get('#cleanup-bin').hidden = false;
  stalePreview.hooks.enter('trashed'); await flush();
  const staleChoice = find(stalePreview.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox'); staleChoice.checked = true; staleChoice.onchange();
  const pendingPreview = button(stalePreview.nodes.get('#cleanup-bin-pagination'), '预览永久清理（已选 1 项）').onclick();
  await flush(); staleChoice.checked = false; staleChoice.onchange();
  resolveStalePreview({plan_token: 'stale', can_execute: true, items: [{...entries[1], can_delete: true, blockers: [], advisories: []}], order: [], total_bytes: 0, notes: []});
  await pendingPreview;
  assert.equal(button(stalePreview.nodes.get('#cleanup-bin-detail'), '永久清理所选回收站资产'), null, 'a stale preview cannot restore an old confirmation form after selection changes');

  const executeCalls = [];
  const executable = load(async (path, options = {}) => {
    executeCalls.push({path, options});
    if (path.startsWith('/api/asset-bin?')) return {items: [entries[1]], total: 1, offset: 0, limit: 50};
    if (path === '/api/asset-cleanup/preview') return {plan_token: 'ready-plan', can_execute: true, items: [{...entries[1], can_delete: true, blockers: [], advisories: [], estimated_bytes: 64}], order: [{kind: 'dataset', ref: 'test-data'}], total_bytes: 64, notes: []};
    if (path === '/api/asset-cleanup/execute') return {request_id: 'request-1', status: 'queued'};
    if (path === '/api/asset-cleanup/request-1') return {request_id: 'request-1', status: 'completed'};
    throw new Error(`unexpected ${path}`);
  });
  executable.nodes.get('#retired-bin').hidden = true; executable.nodes.get('#cleanup-bin').hidden = false;
  executable.hooks.enter('trashed'); await flush();
  const executableChoice = find(executable.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox'); executableChoice.checked = true; executableChoice.onchange();
  await button(executable.nodes.get('#cleanup-bin-pagination'), '预览永久清理（已选 1 项）').onclick();
  const confirmation = find(executable.nodes.get('#cleanup-bin-detail'), value => value.tagName === 'input'); confirmation.value = '永久清理';
  await find(executable.nodes.get('#cleanup-bin-detail'), value => value.tagName === 'form').onsubmit({preventDefault() {}});
  assert.deepEqual(JSON.parse(executeCalls.find(value => value.path === '/api/asset-cleanup/execute').options.body), {items: [{kind: 'dataset', ref: 'test-data'}], plan_token: 'ready-plan', confirmation: '永久清理'});
  assert.ok(executeCalls.some(value => value.path === '/api/asset-cleanup/request-1'), 'queued cleanup is polled while its page remains open');

  let failedExecution = false;
  const failsImmediately = load(async path => {
    if (path.startsWith('/api/asset-bin?')) return {items: [{...entries[1], ...(failedExecution ? {cleanup_status: 'failed', request_id: 'failed-now', cleanup_error: 'storage unavailable'} : {})}], total: 1};
    if (path === '/api/asset-cleanup/preview') return {plan_token: 'failed-plan', can_execute: true, items: [{...entries[1], can_delete: true, blockers: [], advisories: []}], order: [], total_bytes: 0, notes: []};
    if (path === '/api/asset-cleanup/execute') { failedExecution = true; return {request_id: 'failed-now', status: 'failed', error: 'storage unavailable'}; }
    throw new Error(`unexpected ${path}`);
  });
  failsImmediately.nodes.get('#retired-bin').hidden = true; failsImmediately.nodes.get('#cleanup-bin').hidden = false;
  failsImmediately.hooks.enter('trashed'); await flush();
  const failureChoice = find(failsImmediately.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox'); failureChoice.checked = true; failureChoice.onchange();
  await button(failsImmediately.nodes.get('#cleanup-bin-pagination'), '预览永久清理（已选 1 项）').onclick();
  find(failsImmediately.nodes.get('#cleanup-bin-detail'), value => value.tagName === 'input').value = '永久清理';
  await find(failsImmediately.nodes.get('#cleanup-bin-detail'), value => value.tagName === 'form').onsubmit({preventDefault() {}});
  assert.match(failsImmediately.nodes.get('#cleanup-bin-message').textContent, /storage unavailable/);
  assert.equal(find(failsImmediately.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox').disabled, true, 'a failed execute refreshes its reserved item immediately');
  await button(failsImmediately.nodes.get('#cleanup-bin-list'), '打开').onclick();
  assert.ok(button(failsImmediately.nodes.get('#cleanup-bin-detail'), '重试这次清理'), 'the failed batch can be retried without leaving and reentering the page');

  let resolveLateStatus;
  const late = load((path, options = {}) => {
    if (path.startsWith('/api/asset-bin?')) return Promise.resolve({items: [entries[1]], total: 1, offset: 0, limit: 50});
    if (path === '/api/asset-cleanup/preview') return Promise.resolve({plan_token: 'late-plan', can_execute: true, items: [{...entries[1], can_delete: true, blockers: [], advisories: [], estimated_bytes: 1}], order: [], total_bytes: 1, notes: []});
    if (path === '/api/asset-cleanup/execute') return Promise.resolve({request_id: 'late-request', status: 'queued'});
    if (path === '/api/asset-cleanup/late-request') return new Promise(resolve => { resolveLateStatus = resolve; });
    throw new Error(`unexpected ${path} ${options.method || 'GET'}`);
  });
  late.nodes.get('#retired-bin').hidden = true; late.nodes.get('#cleanup-bin').hidden = false;
  late.hooks.enter('trashed'); await flush();
  const lateChoice = find(late.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox'); lateChoice.checked = true; lateChoice.onchange();
  await button(late.nodes.get('#cleanup-bin-pagination'), '预览永久清理（已选 1 项）').onclick();
  find(late.nodes.get('#cleanup-bin-detail'), value => value.tagName === 'input').value = '永久清理';
  const pendingExecution = find(late.nodes.get('#cleanup-bin-detail'), value => value.tagName === 'form').onsubmit({preventDefault() {}});
  await flush();
  late.hooks.leave();
  resolveLateStatus({request_id: 'late-request', status: 'completed'});
  await pendingExecution;
  assert.doesNotMatch(late.nodes.get('#cleanup-bin-message').textContent, /永久清理已完成/, 'a late polling response after leaving does not update the old page');

  let resolveLateExecute, lateExecuteStatusCalls = 0;
  const lateExecute = load((path, options = {}) => {
    if (path.startsWith('/api/asset-bin?')) return Promise.resolve({items: [entries[1]], total: 1, offset: 0, limit: 50});
    if (path === '/api/asset-cleanup/preview') return Promise.resolve({plan_token: 'late-execute-plan', can_execute: true, items: [{...entries[1], can_delete: true, blockers: [], advisories: []}], order: [], total_bytes: 0, notes: []});
    if (path === '/api/asset-cleanup/execute') return new Promise(resolve => { resolveLateExecute = resolve; });
    if (path === '/api/asset-cleanup/late-execute-request') { lateExecuteStatusCalls += 1; return Promise.resolve({status: 'completed'}); }
    throw new Error(`unexpected ${path} ${options.method || 'GET'}`);
  });
  lateExecute.nodes.get('#retired-bin').hidden = true; lateExecute.nodes.get('#cleanup-bin').hidden = false;
  lateExecute.hooks.enter('trashed'); await flush();
  const lateExecuteChoice = find(lateExecute.nodes.get('#cleanup-bin-list'), value => value.tagName === 'input' && value.type === 'checkbox'); lateExecuteChoice.checked = true; lateExecuteChoice.onchange();
  await button(lateExecute.nodes.get('#cleanup-bin-pagination'), '预览永久清理（已选 1 项）').onclick();
  find(lateExecute.nodes.get('#cleanup-bin-detail'), value => value.tagName === 'input').value = '永久清理';
  const pendingLateExecute = find(lateExecute.nodes.get('#cleanup-bin-detail'), value => value.tagName === 'form').onsubmit({preventDefault() {}});
  lateExecute.hooks.leave();
  resolveLateExecute({request_id: 'late-execute-request', status: 'queued'});
  await pendingLateExecute;
  assert.equal(lateExecuteStatusCalls, 0, 'a late execute response after leaving cannot start polling in a different page');

  let studentCalls = 0;
  const student = load(async () => { studentCalls += 1; throw new Error('students must not load bins'); }, 'student');
  student.hooks.enter('trashed'); await flush();
  assert.equal(studentCalls, 0, 'students do not request the teacher-only recycle-bin endpoints');
  process.stdout.write('web cleanup UI tests passed\n');
})().catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
