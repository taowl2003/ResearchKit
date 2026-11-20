'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {tagName, textContent: '', value: '', type: '', checked: false, hidden: false, disabled: false, children: [], className: '',
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; }};
}
function collect(root, predicate, values = []) { if (predicate(root)) values.push(root); for (const item of root.children || []) collect(item, predicate, values); return values; }
async function flush() { for (let index = 0; index < 5; index += 1) await Promise.resolve(); }

function load() {
  const ids = ['#assignment-message', '#assignment-result', '#assignment-form', '#assignment-dispatch', '#assignment-preview', '#assignment-preview-panel', '#assignment-preview-content', '#assignment-name', '#assignment-template', '#assignment-targets', '#assignment-instructions', '#assignment-algorithms', '#assignment-datasets', '#assignment-datasets-note', '#assignment-mode', '#assignment-mode-note'];
  const nodes = new Map(ids.map(id => [id, node(id)]));
  nodes.get('#assignment-algorithms').querySelectorAll = selector => selector === 'input[type="checkbox"]' ? collect(nodes.get('#assignment-algorithms'), item => item.tagName === 'input' && item.type === 'checkbox') : [];
  nodes.get('#assignment-datasets').querySelectorAll = selector => selector === 'input[type="checkbox"]' ? collect(nodes.get('#assignment-datasets'), item => item.tagName === 'input' && item.type === 'checkbox') : [];
  const posts = [];
  const context = {window: {}, document: {querySelector: id => nodes.get(id), querySelectorAll: selector => {
    if (selector.startsWith('#assignment-algorithms')) return collect(nodes.get('#assignment-algorithms'), item => item.tagName === 'input' && item.type === 'checkbox');
    if (selector.startsWith('#assignment-datasets')) return collect(nodes.get('#assignment-datasets'), item => item.tagName === 'input' && item.type === 'checkbox');
    return [];
  }, createElement: tag => node(tag), dispatchEvent() {}},
    globalThis: {crypto: {randomUUID: () => 'request-1'}}, CustomEvent: class {}, Set, Map, Date, Math};
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/workflow.js', 'utf8'), context, {filename: 'workflow.js'});
  const hooks = context.window.ResearchKitAssignments;
  hooks.init({getMe: () => ({role: 'teacher'}), api: async path => {
    if (path === '/api/tasks') return {items: []};
    if (path === '/api/catalog') return {templates: [{ref: 'rain.case@1.0.0', name: '降雨案例', version: '1.0.0'}], plugins: [{ref: 'rain.scale@1.0.0', name: '倍率', version: '1.0.0'}]};
    if (path === '/api/datasets') return [{dataset_id: 'rainfall-v1', name: '降雨数据'}];
    if (path === '/api/derived-datasets?limit=100') return {items: []};
    throw new Error(`unexpected ${path}`);
  }, post: async (path, body) => { posts.push({path, body}); if (path === '/api/assignments') return {task_id:'task-created',distributions:[{},{}]}; return {template_ref: body.template_ref, algorithm_refs: body.algorithm_refs, dataset_ids: body.dataset_ids, targets: body.targets, source_algorithms: ['rain.base@1.0.0'], source_data: ['rainfall-base']}; }});
  return {hooks, nodes, posts};
}

(async () => {
  const app = load(); await app.hooks.enterAssignments(); await flush();
  app.nodes.get('#assignment-name').value = '练习一'; app.nodes.get('#assignment-template').value = 'rain.case@1.0.0'; app.nodes.get('#assignment-targets').value = 'ly, test';
  collect(app.nodes.get('#assignment-algorithms'), item => item.tagName === 'input')[0].checked = true;
  collect(app.nodes.get('#assignment-datasets'), item => item.tagName === 'input')[0].checked = true;
  await app.hooks.preview({preventDefault() {}});
  const request = app.posts.at(-1);
  assert.equal(request.path, '/api/assignments/preview');
  assert.deepEqual(JSON.parse(JSON.stringify(request.body)), {request_id: 'request-1', include_baseline_algorithms:true, name: '练习一', template_ref: 'rain.case@1.0.0', algorithm_refs: ['rain.scale@1.0.0'], dataset_ids: ['rainfall-v1'], targets: ['ly', 'test'], instructions: ''});
  assert.equal(Object.hasOwn(request.body, 'composition'), false, 'task creation never sends composition');
  assert.match(collect(app.nodes.get('#assignment-preview-content'), item => /不会自动写入 case.py/.test(item.textContent))[0].textContent, /不会自动写入 case.py/);
  await app.nodes.get('#assignment-dispatch').onclick();
  assert.equal(app.posts.at(-1).path, '/api/assignments', 'the visible dispatch button invokes the formal task endpoint');
  assert.equal(app.nodes.get('#assignment-result').hidden,false);
  assert.match(app.nodes.get('#assignment-result').textContent,/分发成功.*练习一.*2 个接收记录.*ly、test.*task-created/);
  assert.equal(app.nodes.get('#assignment-preview-panel').hidden,true,'successful dispatch keeps its outcome visible after collapsing the preview');

  const index = fs.readFileSync('core/researchkit/web_static/index.html', 'utf8');
  const appSource = fs.readFileSync('core/researchkit/web_static/app.js', 'utf8');
  assert.match(index, /额外算法工具（可多选）/);
  assert.doesNotMatch(index, /重新生成 case\.py|调整任务编排 JSON/);
  assert.doesNotMatch(index, /共享权限<\/button>/);
  assert.match(appSource, /\/api\/tasks\?personal=true/);
  assert.match(appSource, /\/api\/projects\/\$\{encodeURIComponent\(projectId\)\}\/task-materials/);
  app.nodes.get('#assignment-mode').value = 'resources'; app.nodes.get('#assignment-mode').onchange();
  assert.equal(app.nodes.get('#assignment-template').disabled, true, 'resource packages do not require a hidden base case');
  await app.hooks.preview({preventDefault() {}});
  assert.equal(app.posts.at(-1).body.template_ref, '');
  assert.equal(app.posts.at(-1).body.algorithm_refs[0], 'rain.scale@1.0.0');
  assert.ok(collect(app.nodes.get('#assignment-preview-content'), item => /资源包：不含基础案例/.test(item.textContent)).length);
  collect(app.nodes.get('#assignment-algorithms'), item => item.tagName === 'input')[0].checked = false;
  collect(app.nodes.get('#assignment-datasets'), item => item.tagName === 'input')[0].checked = false;
  const count = app.posts.length;
  await app.hooks.preview({preventDefault() {}});
  assert.equal(app.posts.length, count, 'an empty resource package is rejected before dispatch');
  assert.match(app.nodes.get('#assignment-message').textContent, /至少选择一个算法或数据集/);
  app.nodes.get('#assignment-mode').value = 'project'; app.nodes.get('#assignment-mode').onchange();
  assert.equal(app.nodes.get('#assignment-template').disabled, false);
  assert.equal(app.nodes.get('#assignment-template').required, true);
  const pendingApp=load();await pendingApp.hooks.enterAssignments();
  pendingApp.nodes.get('#assignment-name').value='等待确认';pendingApp.nodes.get('#assignment-template').value='rain.case@1.0.0';pendingApp.nodes.get('#assignment-targets').value='ly,test';
  await pendingApp.hooks.preview({preventDefault(){}});
  let complete,dispatchCalls=0;
  pendingApp.hooks._test.state.post=async()=>{dispatchCalls+=1;return new Promise(resolve=>{complete=resolve;});};
  const running=pendingApp.hooks.dispatch();await pendingApp.hooks.dispatch();
  assert.equal(dispatchCalls,1,'a second click during dispatch cannot send a duplicate request');
  assert.match(pendingApp.nodes.get('#assignment-result').textContent,/正在分发/);
  complete({task_id:'new-task',recipients:[{username:'ly'},{username:'test'}]});await running;
  assert.match(pendingApp.nodes.get('#assignment-result').textContent,/分发成功/);
  pendingApp.hooks._test.state.post=async()=>{throw new Error('服务拒绝本次请求');};
  await pendingApp.hooks.dispatch();
  assert.match(pendingApp.nodes.get('#assignment-result').textContent,/分发未完成.*服务拒绝/);
  assert.match(pendingApp.nodes.get('#assignment-result').className,/error/,'a failed request cannot leave a previous green success message');
  console.log('workflow UI tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
