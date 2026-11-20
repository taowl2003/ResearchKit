'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {
    tagName, id: '', textContent: '', value: '', type: '', checked: false, hidden: false, disabled: false,
    children: [], className: '', open: false, placeholder: '', dataset: {}, attributes: {},
    append(...items) { items.forEach(n => { n.parentNode = this; this.children.push(n); }); },
    replaceChildren(...items) { this.children = []; this.append(...items); },
    remove() {},
    click() {},
    addEventListener() {},
    querySelectorAll(selector) {
      const out = [];
      const walk = n => {
        if (selector === 'input[type="checkbox"]' && n.tagName === 'input' && n.type === 'checkbox') out.push(n);
        for (const c of n.children || []) walk(c);
      };
      walk(this);
      return out;
    },
    setAttribute(k, v) { this.attributes[String(k)] = String(v); },
    getAttribute(k) { return this.attributes[String(k)]; },
  };
}
function collect(root, predicate, values = []) {
  if (predicate(root)) values.push(root);
  for (const item of root.children || []) collect(item, predicate, values);
  return values;
}
async function flush() { for (let i = 0; i < 16; i += 1) await Promise.resolve(); await new Promise(resolve => setImmediate(resolve)); }

function setup({role = 'teacher'} = {}) {
  const ids = [
    '#assignment-message', '#assignment-result', '#assignment-form', '#assignment-dispatch',
    '#assignment-preview', '#assignment-preview-panel', '#assignment-preview-content',
    '#assignment-name', '#assignment-template', '#assignment-targets', '#assignment-instructions',
    '#assignment-algorithms', '#assignment-datasets', '#assignment-datasets-note',
    '#assignment-mode', '#assignment-mode-note', '#resource-library-list',
    '#resource-library-message', '#local-algorithms-list', '#local-algo-form',
    '#local-algo-name', '#local-algo-version', '#local-algo-tags', '#local-algo-description',
    '#local-algo-params', '#local-algo-inputs', '#local-algo-outputs', '#local-algo-code',
    '#local-algo-create', '#local-algo-message', '#resource-management-list', '#resource-management-message',
  ];
  const nodes = new Map(ids.map(id => [id, node(id)]));
  const posts = [];
  const apiCalls = [];
  const dispatched = [];
  let purgePanelCalls = 0;
  const mgmtNavNodes = ['template', 'algorithm', 'dataset'].map(mod => {
    const n = node('button');
    n.dataset = {page: 'resource-management', module: mod};
    return n;
  });
  const context = {
    window: {
      ResearchKitProjectPurge: {
        renderManagementPanel(purgeList, auditList) {
          purgePanelCalls += 1;
          purgeList.append(node('div'));
          auditList.append(node('div'));
        },
      },
    },
    document: {
      querySelector: id => nodes.get(id),
      querySelectorAll: selector => {
        if (selector.startsWith('#assignment-algorithms')) return nodes.get('#assignment-algorithms').querySelectorAll('input[type="checkbox"]');
        if (selector.startsWith('#assignment-datasets')) return nodes.get('#assignment-datasets').querySelectorAll('input[type="checkbox"]');
        if (selector === '[data-page="resource-management"]') return mgmtNavNodes;
        return [];
      },
      createElement: tag => node(tag),
      dispatchEvent(ev) { dispatched.push(ev); },
    },
    globalThis: {crypto: {randomUUID: () => 'req-v08'}},
    CustomEvent: class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } },
    Set, Map, Date, Math,
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/workflow.js', 'utf8'), context, {filename: 'workflow.js'});
  const hooks = context.window.ResearchKitAssignments;
  hooks.init({
    getMe: () => ({role, username: role === 'teacher' ? 'ly' : 'test'}),
    getProjects: () => [{project_id: 'proj-1', name: '我的研究项目', archived: false}],
    api: async path => {
      apiCalls.push(path);
      if (path === '/api/tasks') return {items: []};
      if (path === '/api/catalog') {
        return {
          templates: [{ref: 'rain.case@1.0.0', name: '降雨案例', version: '1.0.0', tags: ['水文']}],
          plugins: [
            {ref: 'algo.a@1.0.0', name: '网格重采样', version: '1.0.0', tags: ['水文', '预处理']},
            {ref: 'algo.b@1.0.0', name: '异常剔除', version: '1.0.0', tags: ['预处理', '质控']},
            {ref: 'algo.c@1.0.0', name: '深度学习推理', version: '1.0.0', tags: ['模型']},
          ],
        };
      }
      if (path === '/api/personal-resource-visibility') return {hidden_algorithms: [], hidden_datasets: []};
      if (path === '/api/datasets') return [
        {dataset_id: 'ds-1', name: '降雨数据集', tags: ['水文']},
        {dataset_id: 'ds-private', name: '学生私有切片', visibility: 'private', tags: ['私有']},
        {dataset_id: 'ds-review', name: '审核中私有切片', visibility: 'review', tags: ['审核']},
      ];
      if (path === '/api/derived-datasets?limit=100') return {items: []};
      if (path === '/api/local-algorithms') return [{id: 'local-1', name: '自研滤波', version: '1.0.0', description: '本地算法说明', entry: 'algo.py'}];
      if (path === '/api/component-access?include_inactive=true') return {items: []};
      if (path === '/api/derived-datasets/access') return {items: []};
      if (path === '/api/asset-candidates') return {items: []};
      if (path.startsWith('/api/asset-cleanup/check?kind=algorithm')) {
        return {can_delete: false, blocking_cases: [{name: '降雨案例', ref: 'rain.case@1.0.0'}]};
      }
      if (path.startsWith('/api/asset-cleanup/check?')) return {can_delete: true};
      throw new Error(`unexpected ${path}`);
    },
    post: async (path, body) => {
      posts.push({path, body});
      if (path === '/api/assignments/preview') return {template_ref: body.template_ref, algorithm_refs: body.algorithm_refs, dataset_ids: body.dataset_ids, targets: body.targets};
      if (path === '/api/assignments') return {task_id: 't-new', distributions: [{}]};
      if (path === '/api/asset-cleanup/direct' && body.kind === 'dataset') {
        return {deleted: true, platform_removed: true, physical_files_deleted: false,
          manual_cleanup: {path: '/home/share/Datasets/rainfall', message: '如确认不再需要，请由服务器管理员手动处理数据目录。'}};
      }
      return {ok: true};
    },
  });
  return {hooks, nodes, posts, apiCalls, mgmtNavNodes, dispatched, getPurgePanelCalls: () => purgePanelCalls};
}

(async () => {
  // 1. Multi-select AND tag intersection filtering & dispatch anti-double-click
  const teacherApp = setup({role: 'teacher'});
  await teacherApp.hooks.enterAssignments();
  await flush();

  const algoHost = teacherApp.nodes.get('#assignment-algorithms');
  const hydroChip = collect(algoHost, n => n.className.includes('tag-chip') && n.textContent === '水文')[0];
  assert.ok(hydroChip, 'tag chip rendered for algorithm filter');
  hydroChip.onclick();
  // After selecting 水文, only algo.a has 水文; 模型 has 0 intersection so its chip becomes disabled
  const modelChip = collect(algoHost, n => n.className.includes('tag-chip') && n.textContent === '模型')[0];
  assert.equal(modelChip.disabled, true, 'non-intersecting tag chip is disabled under AND filter');
  const rowsAfterFilter = collect(algoHost, n => n.tagName === 'details' && n.className.includes('resource-row'));
  assert.equal(rowsAfterFilter.length, 1);
  assert.match(collect(rowsAfterFilter[0], n => n.tagName === 'span').map(n => n.textContent).join(' '), /网格重采样/);

  teacherApp.nodes.get('#assignment-name').value = '单次分发测试';
  teacherApp.nodes.get('#assignment-template').value = 'rain.case@1.0.0';
  teacherApp.nodes.get('#assignment-targets').value = 'test';
  await teacherApp.hooks.preview({preventDefault() {}});
  const dispatchBtn = teacherApp.nodes.get('#assignment-dispatch');
  assert.equal(dispatchBtn.disabled, false);
  await dispatchBtn.onclick();
  assert.equal(dispatchBtn.textContent, '已分发', 'dispatch button shows 已分发 after completion');
  assert.equal(dispatchBtn.disabled, true, 'dispatch button locks after dispatch to prevent double-click');

  // 2. Alias binding in algorithmUse & personal local algorithms
  const studentApp = setup({role: 'student'});
  await studentApp.hooks.loadLibrary();
  await flush();
  const libList = studentApp.nodes.get('#resource-library-list');
  const useBoxes = collect(libList, n => n.className === 'resource-use');
  assert.ok(useBoxes.length >= 1);
  const firstUse = useBoxes[0];
  const projSelect = collect(firstUse, n => n.tagName === 'select')[0];
  const aliasInput = collect(firstUse, n => n.className === 'algo-alias-input')[0];
  const addBtn = collect(firstUse, n => n.tagName === 'button' && n.textContent === '加入项目')[0];
  assert.ok(aliasInput, 'alias input present on library algorithm card');
  projSelect.value = 'proj-1';
  aliasInput.value = 'scale_step';
  await addBtn.onclick();
  const bindReq = studentApp.posts.find(p => p.path === '/api/projects/proj-1/algorithm-materials');
  assert.ok(bindReq);
  assert.deepEqual(JSON.parse(JSON.stringify(bindReq.body)), {algorithm_refs: ['algo.a@1.0.0'], alias: 'scale_step', bind: true});

  // Local algorithm list and delete / alias bind
  const localHost = studentApp.nodes.get('#local-algorithms-list');
  const localRows = collect(localHost, n => n.tagName === 'details');
  assert.equal(localRows.length, 1);
  const localUse = collect(localRows[0], n => n.className === 'resource-use')[0];
  collect(localUse, n => n.tagName === 'select')[0].value = 'proj-1';
  collect(localUse, n => n.tagName === 'input')[0].value = 'local_filter';
  await collect(localUse, n => n.tagName === 'button' && n.textContent === '加入项目')[0].onclick();
  const localBindReq = studentApp.posts.at(-1);
  assert.deepEqual(JSON.parse(JSON.stringify(localBindReq.body)), {local_algorithm_id: 'local-1', alias: 'local_filter', bind: true});

  // 3. Tag filter persistence across tag switches
  const persistApp = setup({role: 'teacher'});
  await persistApp.hooks.enterAssignments();
  await flush();
  const pAlgoHost = persistApp.nodes.get('#assignment-algorithms');
  const pHydro = collect(pAlgoHost, n => n.className.includes('tag-chip') && n.textContent === '水文')[0];
  pHydro.onclick();
  const boxA = collect(pAlgoHost, n => n.tagName === 'input' && n.value === 'algo.a@1.0.0')[0];
  boxA.checked = true;
  boxA.onchange();
  collect(pAlgoHost, n => n.className.includes('tag-clear-btn'))[0].onclick();
  const pModel = collect(pAlgoHost, n => n.className.includes('tag-chip') && n.textContent === '模型')[0];
  pModel.onclick();
  assert.equal(collect(pAlgoHost, n => n.tagName === 'input' && n.value === 'algo.a@1.0.0').length, 0, 'filtered-out selection is hidden from DOM');
  const boxC = collect(pAlgoHost, n => n.tagName === 'input' && n.value === 'algo.c@1.0.0')[0];
  boxC.checked = true;
  boxC.onchange();
  persistApp.nodes.get('#assignment-name').value = '跨筛选测试';
  persistApp.nodes.get('#assignment-template').value = 'rain.case@1.0.0';
  persistApp.nodes.get('#assignment-targets').value = 'test';
  assert.deepEqual(JSON.parse(JSON.stringify(persistApp.hooks._test.payload().algorithm_refs)), ['algo.a@1.0.0', 'algo.c@1.0.0']);
  const summaryNode = collect(pAlgoHost, n => n.className.includes('selected-materials-summary'))[0];
  assert.ok(summaryNode.textContent.includes('algo.a@1.0.0') && summaryNode.textContent.includes('algo.c@1.0.0'));

  // 4. Management only renders the selected platform resource type.  It does
  // not read storage, render project purge, or expose force-cleanup controls.
  persistApp.hooks.setManagementModule('dataset');
  await persistApp.hooks.loadManagement();
  await flush();
  const mgmtHost = persistApp.nodes.get('#resource-management-list');
  assert.equal(persistApp.apiCalls.includes('/api/storage'), false, 'management must not request central storage statistics');
  assert.equal(persistApp.getPurgePanelCalls(), 0, 'management must not render the retired project-purge panel');
  assert.equal(collect(mgmtHost, n => /中央存储概览|关联数据集新版本|按项目清理/.test(n.textContent)).length, 0);
  assert.equal(collect(mgmtHost, n => n.textContent.includes('学生私有切片') || n.textContent.includes('审核中私有切片')).length, 0,
    'private/review data must never appear in platform data management');
  const dsCheckBtn = collect(mgmtHost, n => n.tagName === 'button' && n.textContent === '移除数据登记').at(-1);
  assert.ok(dsCheckBtn, 'dataset management renders the explicit registry-removal action');
  await dsCheckBtn.onclick();
  await flush();
  const removeConfirm = collect(mgmtHost, n => n.tagName === 'button' && n.textContent === '确认移除数据登记')[0];
  assert.ok(removeConfirm, 'no-reference dataset gets a second explicit confirmation');
  await removeConfirm.onclick();
  await flush();
  const managementMessage = persistApp.nodes.get('#resource-management-message').textContent;
  assert.match(managementMessage, /已移除平台数据登记：ds-1/);
  assert.match(managementMessage, /原始或派生数据文件仍保留/);
  assert.match(managementMessage, /\/home\/share\/Datasets\/rainfall/);
  assert.deepEqual(JSON.parse(JSON.stringify(persistApp.posts.find(p => p.path === '/api/asset-cleanup/direct').body)),
    {kind: 'dataset', ref: 'ds-1', confirmation: '永久清理'});

  persistApp.hooks.setManagementModule('algorithm');
  await persistApp.hooks.loadManagement();
  await flush();
  const algDeleteBtn = collect(mgmtHost, n => n.tagName === 'button' && n.textContent === '删除算法')[0];
  assert.ok(algDeleteBtn, 'algorithm management renders a direct delete action');
  await algDeleteBtn.onclick();
  await flush();
  assert.match(collect(mgmtHost, n => n.tagName === 'li').map(n => n.textContent).join(' '), /rain\.case@1\.0\.0/,
    'algorithm cleanup lists each blocking case ref');
  assert.equal(collect(mgmtHost, n => n.tagName === 'button' && n.textContent === '确认删除算法').length, 0,
    'blocked algorithms cannot be confirmed for deletion');
  const activeMgmtNav = persistApp.mgmtNavNodes.filter(n => n.getAttribute('aria-current') === 'page').map(n => n.dataset.module);
  assert.deepEqual(activeMgmtNav, ['algorithm'], 'exactly one management nav button has aria-current=page');
  assert.ok(persistApp.dispatched.some(ev => ev.type === 'researchkit:management-module-changed' && ev.detail?.module === 'algorithm'));

  const paramApp = setup({role: 'student'});
  await paramApp.hooks.loadLibrary();
  await flush();
  const firstUseBox = collect(paramApp.nodes.get('#resource-library-list'), n => n.className === 'resource-use')[0];
  collect(firstUseBox, n => n.tagName === 'select')[0].value = 'proj-1';
  collect(firstUseBox, n => n.className === 'algo-alias-input')[0].value = 'scaled_p';
  collect(firstUseBox, n => n.className === 'algo-params-input')[0].value = '{"factor": 5}';
  await collect(firstUseBox, n => n.tagName === 'button' && n.textContent === '加入项目')[0].onclick();
  const paramReq = paramApp.posts.find(p => p.path === '/api/projects/proj-1/algorithm-materials');
  assert.deepEqual(JSON.parse(JSON.stringify(paramReq.body)), {algorithm_refs: ['algo.a@1.0.0'], alias: 'scaled_p', bind: true, parameters: {factor: 5}});

  const localForm = paramApp.nodes.get('#local-algo-form');
  localForm.reset = () => {};
  paramApp.nodes.get('#local-algo-name').value = 'my.custom';
  paramApp.nodes.get('#local-algo-version').value = '1.0.0';
  paramApp.nodes.get('#local-algo-tags').value = '水文, 滤波';
  paramApp.nodes.get('#local-algo-description').value = '带参数规范的本地算法';
  paramApp.nodes.get('#local-algo-params').value = '{"threshold": {"type": "number", "default": 0.5}}';
  paramApp.nodes.get('#local-algo-inputs').value = '{"series": "scalar@1.0.0"}';
  paramApp.nodes.get('#local-algo-outputs').value = '{"filtered": "scalar@1.0.0"}';
  paramApp.nodes.get('#local-algo-code').value = 'def run(inputs, parameters, context):\n    return {}\n';
  await localForm.onsubmit({preventDefault() {}});
  const localCreateReq = paramApp.posts.find(p => p.path === '/api/local-algorithms');
  assert.deepEqual(JSON.parse(JSON.stringify(localCreateReq.body.parameters)), {threshold: {type: 'number', default: 0.5}});
  assert.deepEqual(JSON.parse(JSON.stringify(localCreateReq.body.inputs)), {series: 'scalar@1.0.0'});
  assert.deepEqual(JSON.parse(JSON.stringify(localCreateReq.body.outputs)), {filtered: 'scalar@1.0.0'});

  // 5. app.js role-separated delete/purge, unified review options & v0.8 4-module teacher nav
  function loadAppVm(role) {
    const appIds = [
      '#notice', '#output', '#create', '#import', '#package-import', '#template-ref', '#editor', '#save',
      '#compare-left', '#compare-right', '#compare', '#baseline-compare', '#component-access-nav', '#asset-candidates-nav',
      '#data-management-nav', '#retired-bin-nav', '#cleanup-bin-nav', '#teacher-nav-group', '#resource-management-nav',
      '#cases-management-nav', '#algorithms-management-nav', '#datasets-management-nav', '#distributed-tasks-nav', '#assignments-nav', '#projects-nav', '#demo',
      '#distributed-tasks', '#distributed-tasks-refresh',
      '#jobs-back', '#download-data-references', '#submission-list', '#submission-more', '#submission-message',
      '#submission-compare-status', '#submission-owner-label', '#submission-owner', '#asset-message', '#asset-create',
      '#asset-candidate-list', '#asset-detail', '#asset-detail-title', '#asset-detail-content', '#asset-file-name',
      '#asset-file-preview', '#component-access-list', '#component-access-message', '#project-list',
      '#execution-notice', '#distribution-intro', '#distribution-list', '#task-list', '#task-message',
      '#datasets-message', '#file-name', '#editor-state',
    ];
    const appNodes = new Map(appIds.map(id => [id, node(id)]));
    appNodes.get('#projects-nav').dataset = {page: 'projects'};
    appNodes.get('#cases-management-nav').dataset = {page: 'resource-management', module: 'template'};
    appNodes.get('#algorithms-management-nav').dataset = {page: 'resource-management', module: 'algorithm'};
    appNodes.get('#datasets-management-nav').dataset = {page: 'resource-management', module: 'dataset'};
    appNodes.get('#asset-candidates-nav').dataset = {page: 'asset-candidates'};
    appNodes.get('#distributed-tasks-nav').dataset = {page: 'distributed-tasks'};
    appNodes.get('#assignments-nav').hidden = true;
    appNodes.get('#distributed-tasks').hidden = true;
    appNodes.get('#resource-management-nav').hidden = true;
    const navPageNodes = [
      appNodes.get('#projects-nav'),
      appNodes.get('#cases-management-nav'),
      appNodes.get('#algorithms-management-nav'),
      appNodes.get('#datasets-management-nav'),
      appNodes.get('#asset-candidates-nav'),
      appNodes.get('#distributed-tasks-nav'),
    ];
    const docListeners = new Map();
    const apiCalls = [];
    const fetch = async (path, options = {}) => {
      apiCalls.push({path, options});
      if (path === '/api/me') return {ok: true, json: async () => ({username: role === 'teacher' ? 'ly' : 'alice', role, demo: false})};
      if (path === '/api/execution') return {ok: true, json: async () => ({enabled: false})};
      if (path === '/api/projects' || path.startsWith('/api/projects?')) {
        return {
          ok: true,
          json: async () => [
            {project_id: 'proj-1', name: '演示项目', path: 'projects/proj-1', files: [], archived: false, unavailable: false},
            {project_id: 'proj-2', name: '中央已清理项目', path: 'projects/proj-2', files: [], archived: false, unavailable: false, central_purged: true},
          ],
        };
      }
      if (path === '/api/projects/proj-1/delete-local') return {ok: true, json: async () => ({deleted: true})};
      if (path.startsWith('/api/review/submissions/sub-mod/release')) {
        return {
          ok: true,
          json: async () => ({
            submission: {id: 'sub-mod', version: '1.0.0', content_sha256: 'e'.repeat(64)},
            algorithms: [
              {alias: 'scale', name: 'hydro.scale', version: '1.0.0', ref: 'hydro.scale@1.0.0', upstream_ref: 'hydro.scale@1.0.0', modified_platform_algorithm: true, suggested_next_version: '1.0.1', is_local_plugin: false},
              {alias: 'local_step', name: 'local_step', version: '1.0.0', ref: 'local_step@1.0.0', is_local_plugin: true, modified_platform_algorithm: false},
            ],
            datasets: [
              {dataset_id: 'ds-priv-1', name: '私有切片', scope: 'private_derived', visibility: 'private', needs_resolution: true, tags: ['切片'], description: '私有切片说明'},
            ],
            existing_assets: {templates: [], algorithms: [], derived: []},
            evidence: [],
            publications: [],
          }),
        };
      }
      if (path.endsWith('/publish')) return {ok: true, json: async () => ({published: [{ref: 'case.mod@1.0.0'}]})};
      return {ok: true, json: async () => ({items: []})};
    };
    const ctx = {
      window: {
        __researchkitAppTest: true,
        ResearchKitProjectPurge: {configure() {}, open() {}},
        ResearchKitAssignments: {init() {}, loadManagement() {}, setManagementModule() {}},
      },
      location: {hash: '', pathname: '/'},
      history: {replaceState() {}},
      URLSearchParams,
      sessionStorage: {getItem: k => (k === 'researchkit-web-token' ? 'tok' : null), setItem() {}},
      Headers: class { set() {} },
      File: class {},
      FormData: class {},
      URL: {createObjectURL: () => 'blob:x', revokeObjectURL() {}},
      setTimeout() { return 1; },
      clearTimeout() {},
      document: {
        querySelector: id => appNodes.get(id),
        querySelectorAll: sel => {
          if (sel === '[data-page]') return navPageNodes;
          if (sel === '[data-page="resource-management"]') return navPageNodes.filter(n => n.dataset.page === 'resource-management');
          return [];
        },
        addEventListener(type, fn) {
          if (!docListeners.has(type)) docListeners.set(type, []);
          docListeners.get(type).push(fn);
        },
        dispatchEvent(ev) {
          (docListeners.get(ev?.type) || []).forEach(fn => fn(ev));
        },
        createElement: tag => node(tag),
        body: {append() {}},
      },
      fetch,
      confirm: () => true,
      CustomEvent: class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } },
      Set, Map, Date, Math,
    };
    vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/review-batch.js', 'utf8'), ctx);
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/app.js', 'utf8'), ctx, {filename: 'app.js'});
    return {hooks: ctx.window.__researchkitAppTest, nodes: appNodes, apiCalls, document: ctx.document, CustomEvent: ctx.CustomEvent};
  }

  const appTeacher = loadAppVm('teacher');
  await flush();
  assert.match(appTeacher.nodes.get('#notice').textContent, /v0\.8（IDE实验方案与产物 · 最新结果与中文案例 · 20261116）/);
  assert.equal(appTeacher.nodes.get('#assignments-nav').hidden, true, '#assignments-nav must stay hidden in v0.8');
  assert.equal(appTeacher.nodes.get('#resource-management-nav').hidden, true, '#resource-management-nav must stay hidden in v0.8');
  assert.equal(appTeacher.nodes.get('#retired-bin-nav').hidden, true, 'legacy retired-bin entrance remains hidden');
  assert.equal(appTeacher.nodes.get('#cleanup-bin-nav').hidden, true, 'legacy recycle-bin entrance remains hidden');
  assert.equal(appTeacher.nodes.get('#data-management-nav').hidden, true, 'dataset registration is only shown after entering 数据管理');
  assert.equal(appTeacher.nodes.get('#cases-management-nav').hidden, false);
  assert.equal(appTeacher.nodes.get('#algorithms-management-nav').hidden, false);
  assert.equal(appTeacher.nodes.get('#datasets-management-nav').hidden, false);
  assert.equal(appTeacher.nodes.get('#asset-candidates-nav').hidden, false);
  assert.equal(appTeacher.nodes.get('#distributed-tasks-nav').hidden, false, 'teachers see the separate distributed-task page');
  appTeacher.nodes.get('#distributed-tasks-nav').onclick();
  await flush();
  assert.equal(appTeacher.nodes.get('#distributed-tasks').hidden, false, 'teacher navigation opens the distributed-task page');
  assert.ok(appTeacher.apiCalls.some(call => call.path === '/api/tasks'), 'teacher navigation loads manager tasks');
  const tProjHost = appTeacher.nodes.get('#project-list');
  assert.equal(collect(tProjHost, n => n.textContent === '按项目清理...').length, 0, 'teacher personal cards must not expose 按项目清理');
  assert.ok(collect(tProjHost, n => n.textContent === '删除本地副本...').length >= 1, 'teacher sees 删除本地副本 on personal cards');
  assert.ok(collect(tProjHost, n => (n.textContent || '').includes('平台中央历史项目已清理')).length >= 1, 'teacher sees central_purged hint on personal card');

  const mgmtNavIds = ['#cases-management-nav', '#algorithms-management-nav', '#datasets-management-nav'];
  appTeacher.hooks.showPage('resource-management', 'algorithm');
  assert.deepEqual(
    mgmtNavIds.filter(id => appTeacher.nodes.get(id).getAttribute('aria-current') === 'page'),
    ['#algorithms-management-nav'],
    'showPage sets aria-current=page strictly on active module button'
  );
  appTeacher.hooks.showPage('resource-management', 'dataset');
  assert.deepEqual(
    mgmtNavIds.filter(id => appTeacher.nodes.get(id).getAttribute('aria-current') === 'page'),
    ['#datasets-management-nav']
  );
  appTeacher.document.dispatchEvent(new appTeacher.CustomEvent('researchkit:management-module-changed', {detail: {module: 'template'}}));
  assert.deepEqual(
    mgmtNavIds.filter(id => appTeacher.nodes.get(id).getAttribute('aria-current') === 'page'),
    ['#cases-management-nav']
  );

  const reviewHost = appTeacher.nodes.get('#asset-detail-content');
  await appTeacher.hooks.renderReviewDetail('sub-mod', reviewHost);
  await flush();
  assert.equal(collect(reviewHost, n => n.className === 'review-batch-resource').length, 4, 'case, modified algorithm, local algorithm and private dataset each have independent selection');
  assert.ok(collect(reviewHost, n => n.id === 'review-batch-preview').length, 'batch preview replaces the single-select form');

  const appStudent = loadAppVm('student');
  await flush();
  assert.equal(appStudent.nodes.get('#distributed-tasks-nav').hidden, true, 'students never see distributed-task navigation');
  appStudent.nodes.get('#distributed-tasks-nav').onclick();
  await flush();
  assert.equal(appStudent.nodes.get('#distributed-tasks').hidden, true, 'student role guard keeps the management page hidden');
  assert.equal(appStudent.apiCalls.filter(call => call.path === '/api/tasks').length, 0, 'students never request the manager task list');
  const sProjHost = appStudent.nodes.get('#project-list');
  assert.equal(collect(sProjHost, n => n.textContent === '按项目清理...').length, 0, 'student must never see按项目清理');
  const delLocalBtn = collect(sProjHost, n => n.textContent === '删除本地副本...')[0];
  assert.ok(delLocalBtn, 'student sees 删除本地副本');
  delLocalBtn.onclick();
  const confirmLocalBtn = collect(sProjHost, n => n.textContent === '确认删除本地副本')[0];
  assert.ok(confirmLocalBtn);
  await confirmLocalBtn.onclick();
  await flush();
  const delLocalCall = appStudent.apiCalls.find(c => c.path === '/api/projects/proj-1/delete-local');
  assert.ok(delLocalCall);
  assert.deepEqual(JSON.parse(delLocalCall.options.body), {confirmation: '确认删除本地副本'});

  console.log('v0.8 workflow UI tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
