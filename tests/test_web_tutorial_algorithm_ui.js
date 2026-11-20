'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {
    tagName, textContent: '', value: '', type: '', checked: false, hidden: false, disabled: false,
    children: [], className: '', dataset: {},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    querySelectorAll() { return []; },
  };
}

function setup() {
  const ids = ['#local-algo-form', '#local-algo-name', '#local-algo-version', '#local-algo-tags',
    '#local-algo-description', '#local-algo-params', '#local-algo-inputs', '#local-algo-outputs',
    '#local-algo-code', '#local-algo-create', '#local-algo-message', '#local-algorithms-list'];
  const nodes = new Map(ids.map(id => [id, node(id)]));
  nodes.get('#local-algo-version').value = '1.0.0';
  const posts = [], apiCalls = [];
  const context = {
    window: {},
    document: {querySelector: id => nodes.get(id), querySelectorAll: () => [], createElement: tag => node(tag), dispatchEvent() {}},
    globalThis: {crypto: {randomUUID: () => 'blank-request'}}, Set, Map, Date, Math,
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/workflow.js', 'utf8'), context, {filename: 'workflow.js'});
  const hooks = context.window.ResearchKitAssignments;
  hooks.init({
    getMe: () => ({role: 'student', username: 'test'}),
    api: async path => { apiCalls.push(path); if (path === '/api/local-algorithms') return []; throw new Error(`unexpected ${path}`); },
    post: async (path, body) => { posts.push({path, body}); return {ok: true}; },
  });
  return {nodes, posts, apiCalls};
}

(async () => {
  const app = setup();
  assert.equal(app.nodes.has('#local-algo-tutorial'), false, 'the blank creation form has no tutorial selector');
  assert.equal(app.nodes.has('#local-algo-tutorial-fill'), false, 'the blank creation form has no fill button');
  assert.equal(app.nodes.get('#local-algo-code').value, '', 'plugin code starts blank');
  assert.equal(app.nodes.get('#local-algo-description').value, '', 'description starts blank');

  app.nodes.get('#local-algo-name').value = 'blank.algorithm';
  const form = app.nodes.get('#local-algo-form');
  form.reset = () => {};
  await form.onsubmit({preventDefault() {}});
  const created = app.posts.find(call => call.path === '/api/local-algorithms');
  assert.ok(created, 'blank local algorithms keep the existing creation endpoint');
  assert.equal(created.body.name, 'blank.algorithm');
  assert.equal(created.body.plugin_py, '', 'the frontend submits no tutorial implementation');
  assert.equal('parameters' in created.body, false);
  assert.equal('inputs' in created.body, false);
  assert.equal('outputs' in created.body, false);
  assert.equal(app.apiCalls.some(path => String(path).includes('tutorial-algorithms')), false, 'blank creation never fetches tutorial data');

  const index = fs.readFileSync('core/researchkit/web_static/index.html', 'utf8');
  assert.doesNotMatch(index, /local-algo-tutorial|教学示例|填入示例/);
  assert.match(index, /代码默认留空。填写算法名称等信息后创建基本接口骨架/);
  console.log('blank local algorithm UI tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
