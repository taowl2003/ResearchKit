'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {
    tagName, textContent: '', value: '', type: '', checked: false, hidden: false, disabled: false,
    children: [], className: '', dataset: {}, open: false, placeholder: '',
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    querySelector() { return this.button; },
    querySelectorAll() { return []; },
    reset() {}, remove() {}, click() {}, setAttribute() {},
  };
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) {
    const value = find(child, predicate);
    if (value) return value;
  }
  return null;
}
function findAll(root, predicate, out = []) {
  if (predicate(root)) out.push(root);
  for (const child of root.children || []) findAll(child, predicate, out);
  return out;
}

function load(fetch) {
  const ids = [
    '#notice', '#output', '#create', '#import', '#package-import', '#template-ref', '#editor',
    '#file-name', '#editor-state', '#save', '#compare-left', '#compare-right', '#compare',
    '#baseline-compare', '#demo', '#jobs-back', '#task-list', '#task-message', '#teacher-task-list',
    '#teacher-task-message', '#project-list', '#project-query', '#projects', '#distributions',
    '#project-include-archived', '#distribution-list', '#distribution-intro',
    '#project-dataset-references-list', '#dataset-project-state', '#dataset-project',
  ];
  const nodes = new Map(ids.map(id => [id, node(id)]));
  const context = {
    window: {__researchkitAppTest: true},
    location: {hash: ''},
    history: {replaceState() {}},
    URLSearchParams,
    sessionStorage: {getItem: () => null, setItem() {}},
    Headers: class {set() {}},
    File: class {},
    FormData: class {},
    URL: {createObjectURL() { return 'blob:test'; }, revokeObjectURL() {}},
    setTimeout() { return 1; },
    clearTimeout() {},
    document: {
      querySelector: id => nodes.get(id),
      querySelectorAll: () => [],
      addEventListener() {},
      dispatchEvent() {},
      createElement: tag => node(tag),
      body: {append() {}},
    },
    fetch,
    confirm: () => true,
    CustomEvent: class {},
    Intl,
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/app.js', 'utf8'), context, {filename: 'app.js'});
  return {hooks: context.window.__researchkitAppTest, nodes};
}

(async () => {
  const requests = [];
  let tasksOnServer = [{
    task_id: 'task-1',
    kind: 'formal',
    name: '降雨练习',
    instructions: '保持入口',
    recipients: [{username: 'test'}, {username: 'alice', project_id: 'started-1'}],
    materials: [
      {kind: 'template', ref: 'rain.case@1.0.0'},
      {kind: 'algorithm', ref: 'rain.scale@1.0.0'},
      {kind: 'dataset', ref: 'rainfall-v1'},
    ],
  }];
  const app = load(async (path, options = {}) => {
    requests.push({path, options});
    if (path === '/api/tasks') return {ok: true, json: async () => ({items: tasksOnServer})};
    if (path === '/api/tasks?personal=true') return {ok: true, json: async () => ({items: [{task_id: 'task-1', kind: 'formal', name: '降雨练习'}]})};
    if (path === '/api/tasks/task-1/materials?personal=true') {
      return {
        ok: true,
        json: async () => ({
          task_id: 'task-1',
          supplements: [{supplement_id: 'supp-1', instructions: '额外数据只读使用。'}],
          materials: [
            {material_id: 'base', kind: 'template', ref: 'rain.case@1.0.0'},
            {material_id: 'tool', kind: 'algorithm', ref: 'rain.scale@1.0.0'},
            {material_id: 'data', kind: 'dataset', ref: 'rainfall-v1'},
          ],
        }),
      };
    }
    if (path === '/api/tasks/task-1/delete') {
      tasksOnServer = [];
      return {ok: true, json: async () => ({deleted: true})};
    }
    return {ok: true, json: async () => ({})};
  });

  app.hooks.setMe({role: 'teacher', username: 'ly'});
  const retry = {};
  const firstRetry = app.hooks.idempotentRequest(retry, {submission_id: 'fixed-1', targets: ['ly'], instructions: '试用'});
  const sameRetry = app.hooks.idempotentRequest(retry, {submission_id: 'fixed-1', targets: ['ly'], instructions: '试用'});
  const changedRetry = app.hooks.idempotentRequest(retry, {submission_id: 'fixed-1', targets: ['test'], instructions: '试用'});
  assert.equal(firstRetry.request_id, sameRetry.request_id);
  assert.notEqual(firstRetry.request_id, changedRetry.request_id);

  await app.hooks.tasks();
  assert.ok(requests.some(item => item.path === '/api/tasks?personal=true'), 'LY opens personal task records by default');
  await app.hooks.tasks('manager');

  const groupSummary = find(app.nodes.get('#teacher-task-list'), item => item.tagName === 'summary' && item.textContent === '接收者：test · 1 个任务');
  assert.ok(groupSummary, 'teacher view groups assigned tasks by recipient');
  const recipientGroup = app.nodes.get('#teacher-task-list').children[0];
  assert.equal(recipientGroup.open, false, 'recipient groups start collapsed');
  assert.equal(find(app.nodes.get('#teacher-task-list'), item => /撤销来源|补充资源/.test(item.textContent)), null, 'legacy grant revoke and supplement UI are removed');
  const delBtn = find(app.nodes.get('#teacher-task-list'), item => item.textContent === '删除任务');
  assert.ok(delBtn, 'teacher task card renders delete button');
  delBtn.onclick();
  assert.equal(requests.filter(item => item.path === '/api/tasks/task-1/delete').length, 0, 'opening delete confirmation does not call the API');
  assert.ok(find(app.nodes.get('#teacher-task-list'), item => /全部接收人：test（未开始）、alice（已开始）/.test(item.textContent)));
  const confirmBtn = find(app.nodes.get('#teacher-task-list'), item => item.textContent === '确认删除任务');
  assert.ok(confirmBtn, 'delete requires a second, inline confirmation');
  const cancelBtn = find(app.nodes.get('#teacher-task-list'), item => item.textContent === '取消');
  cancelBtn.onclick();
  assert.equal(requests.filter(item => item.path === '/api/tasks/task-1/delete').length, 0, 'cancelling does not delete the task');
  delBtn.onclick();
  await confirmBtn.onclick();
  const deleteReq = requests.find(item => item.path === '/api/tasks/task-1/delete');
  assert.equal(deleteReq.options.method, 'POST');
  assert.match(app.nodes.get('#teacher-task-message').textContent, /已删除任务：降雨练习.*授权保留/);
  assert.ok(find(app.nodes.get('#teacher-task-list'), item => item.textContent === '当前没有已布置任务。'));
  assert.equal(requests.filter(item => /asset|cleanup|purge/.test(item.path)).length, 0, 'task deletion never invokes asset cleanup');

  const refreshFailureRequests = [];
  let refreshFailureTasks = [{task_id: 'refresh-failure', name: '刷新失败练习', recipients: [{username: 'test'}]}];
  const refreshFailure = load(async (path, options = {}) => {
    refreshFailureRequests.push({path, options});
    if (path === '/api/tasks') return {ok: true, json: async () => ({items: refreshFailureTasks})};
    if (path === '/api/tasks/refresh-failure/delete') {
      refreshFailureTasks = [];
      return {ok: true, json: async () => ({deleted: true})};
    }
    if (path === '/api/tasks?personal=true') return {ok: true, json: async () => ({items: []})};
    if (path === '/api/distributions') return {ok: false, status: 503, json: async () => ({error: '旧分发读取失败'})};
    return {ok: true, json: async () => ({})};
  });
  refreshFailure.hooks.setMe({role: 'teacher', username: 'ly'});
  await refreshFailure.hooks.tasks('manager');
  const refreshDelete = find(refreshFailure.nodes.get('#teacher-task-list'), item => item.textContent === '删除任务');
  refreshDelete.onclick();
  const refreshConfirm = find(refreshFailure.nodes.get('#teacher-task-list'), item => item.textContent === '确认删除任务');
  await refreshConfirm.onclick();
  assert.equal(refreshFailureRequests.filter(item => item.path === '/api/tasks/refresh-failure/delete').length, 1, 'a successful delete is submitted exactly once');
  assert.match(refreshFailure.nodes.get('#teacher-task-message').textContent, /已删除任务：刷新失败练习.*列表刷新失败，请刷新重试/);
  await refreshConfirm.onclick();
  assert.equal(refreshFailureRequests.filter(item => item.path === '/api/tasks/refresh-failure/delete').length, 1, 'a failed refresh never retries an already successful delete');

  const legacyRequests = [];
  const legacy = load(async (path, options = {}) => {
    legacyRequests.push({path, options});
    if (path === '/api/tasks') return {ok: true, json: async () => ({items: [{task_id: 'legacy-1', name: '早期资源包', legacy_distribution: true, recipients: [{username: 'test', project_id: 'project-1'}]}]})};
    if (path === '/api/tasks/legacy-1/delete') return {ok: true, json: async () => ({deleted: true})};
    if (path === '/api/tasks?personal=true' || path === '/api/distributions') return {ok: true, json: async () => ({items: []})};
    return {ok: true, json: async () => ({})};
  });
  legacy.hooks.setMe({role: 'teacher', username: 'ly'});
  await legacy.hooks.tasks('manager');
  assert.ok(find(legacy.nodes.get('#teacher-task-list'), item => /旧版分发 · 早期资源包/.test(item.textContent)));
  assert.ok(find(legacy.nodes.get('#teacher-task-list'), item => /该用户开始状态：已开始/.test(item.textContent)));
  const legacyDelete = find(legacy.nodes.get('#teacher-task-list'), item => item.textContent === '删除任务');
  legacyDelete.onclick();
  await find(legacy.nodes.get('#teacher-task-list'), item => item.textContent === '确认删除任务').onclick();
  assert.equal(legacyRequests.find(item => item.path === '/api/tasks/legacy-1/delete').options.method, 'POST', 'legacy distributions use the task delete endpoint');
  assert.equal(legacyRequests.filter(item => /asset|cleanup|purge/.test(item.path)).length, 0, 'legacy deletion does not depend on source assets');

  app.hooks.setMe({role: 'student', username: 'test'});
  await app.hooks.tasks('personal');
  const host = node('article');
  await app.hooks.taskMaterials({task_id: 'task-1'}, host);
  assert.ok(find(host, item => /案例 · rain.case@1.0.0/.test(item.textContent)));
  assert.ok(find(host, item => /算法 · rain.scale@1.0.0/.test(item.textContent)));
  assert.ok(find(host, item => /数据 · rainfall-v1/.test(item.textContent)));
  assert.ok(find(host, item => /额外数据只读使用/.test(item.textContent)));
  assert.ok(requests.some(item => item.path === '/api/tasks/task-1/materials?personal=true'));
  const whole = find(app.nodes.get('#task-list'), item => item.textContent === '开始任务');
  assert.ok(whole);

  const claimRequests = [];
  let claimed = false;
  const own = {project_id: 'own-1', name: '我领取的项目', path: 'projects/own-1', source: 'teacher-assigned', files: []};
  const claiming = load(async (path, options = {}) => {
    claimRequests.push({path, options});
    if (path === '/api/tasks?personal=true') {
      return {
        ok: true,
        json: async () => ({
          items: [
            {task_id: 'whole-1', name: '完整基线', distribution_id: 'dist-1', claim_status: claimed ? 'claimed' : 'pending', project_id: claimed ? 'own-1' : null},
            {task_id: 'tools-1', name: '算法资源包', delivery_mode: 'resources'},
          ],
        }),
      };
    }
    if (path === '/api/tasks/whole-1/claim') { claimed = true; return {ok: true, json: async () => ({project: own})}; }
    if (path === '/api/projects/own-1/task-materials') return {ok: true, json: async () => ({project_id: 'own-1'})};
    if (path === '/api/projects') return {ok: true, json: async () => [own]};
    if (path === '/api/distributions') return {ok: true, json: async () => [{distribution_id: 'dist-1', target_uid: 12, target_username: 'test', status: 'pending'}]};
    throw new Error(`unexpected ${path}`);
  });
  claiming.hooks.setMe({uid: 12, role: 'student', username: 'test'});
  await claiming.hooks.tasks('personal');
  await claiming.hooks.distributions();
  assert.equal(find(claiming.nodes.get('#distribution-list'), item => item.textContent === '领取到我的工作区'), null);
  assert.equal(find(claiming.nodes.get('#task-list'), item => item.textContent === '新建空项目并领取'), null);
  assert.ok(find(claiming.nodes.get('#task-list'), item => item.textContent === '添加材料到项目'));
  const primary = find(claiming.nodes.get('#task-list'), item => item.textContent === '开始任务');
  await primary.onclick();
  assert.equal(claimRequests.filter(item => item.path === '/api/tasks/whole-1/claim').length, 1);
  assert.deepEqual(JSON.parse(claimRequests.find(item => item.path === '/api/tasks/whole-1/claim').options.body), {});
  assert.ok(find(claiming.nodes.get('#task-list'), item => item.textContent === '打开项目'));
  assert.equal(claiming.nodes.get('#projects').hidden, false);
  assert.equal(claiming.nodes.get('#project-query').value, 'own-1');
  const addToTask = find(claiming.nodes.get('#task-list'), item => item.textContent === '添加材料到本任务项目');
  await addToTask.onclick();
  assert.equal(claimRequests.filter(item => item.path === '/api/tasks/whole-1/claim').length, 1);
  assert.deepEqual(JSON.parse(claimRequests.find(item => item.path === '/api/projects/own-1/task-materials').options.body), {task_id: 'whole-1'});

  // Student view of a teacher-deleted task: unclaimed blocks claim and shows dismiss button
  const deletedReqs = [];
  let studentDeletedItems = [
    {task_id: 'task-del', name: '已撤回练习', claim_status: 'pending', deleted_by_teacher: true},
  ];
  const deletedApp = load(async (path, options = {}) => {
    deletedReqs.push({path, options});
    if (path === '/api/tasks?personal=true') return {ok: true, json: async () => ({items: studentDeletedItems})};
    if (path === '/api/tasks/task-del/dismiss') {
      studentDeletedItems = [];
      return {ok: true, json: async () => ({dismissed: true})};
    }
    return {ok: true, json: async () => ({})};
  });
  deletedApp.hooks.setMe({uid: 12, role: 'student', username: 'test'});
  await deletedApp.hooks.tasks('personal');
  assert.equal(find(deletedApp.nodes.get('#task-list'), item => item.textContent === '开始任务'), null, 'deleted unclaimed task cannot be started');
  assert.ok(find(deletedApp.nodes.get('#task-list'), item => /该布置任务已被教师删除/.test(item.textContent)));
  const dismissBtn = find(deletedApp.nodes.get('#task-list'), item => item.textContent === '删除记录');
  assert.ok(dismissBtn, 'student gets dismiss button for teacher-deleted task');
  await dismissBtn.onclick();
  assert.equal(deletedReqs.find(r => r.path === '/api/tasks/task-del/dismiss').options.method, 'POST');
  assert.ok(find(deletedApp.nodes.get('#task-list'), item => item.textContent.includes('当前没有任务或补充材料')));

  // Stale dataset badge & remove reference button, plus review test-copy button
  const miscReqs = [];
  const valueWithStale = {sha256: 'sha-1', datasets: [{dataset_id: 'drv-stale', name: '旧切片', stale: true, exists: false}]};
  const staleApp = load(async (path, options = {}) => {
    miscReqs.push({path, options});
    if (path === '/api/projects/proj-1/datasets') {
      const removed = miscReqs.some(r => r.path === '/api/projects/proj-1/stale-datasets/remove');
      return {ok: true, json: async () => (removed ? {project_id: 'proj-1', sha256: 'sha-2', datasets: []} : {project_id: 'proj-1', sha256: 'sha-1', datasets: valueWithStale.datasets})};
    }
    if (path === '/api/projects/proj-1/stale-datasets/remove') return {ok: true, json: async () => ({removed: true})};
    if (path === '/api/review/submissions/sub-1/release') {
      return {ok: true, json: async () => ({submission: {submission_id: 'sub-1', project_name: '降雨案例', version: 'v1', content_sha256: 'abc'}, algorithms: []})};
    }
    if (path === '/api/review/submissions/sub-1/test-copy') return {ok: true, json: async () => ({project_id: 'test-proj-1', name: '[测试] 降雨案例-v1'})};
    if (path === '/api/projects') return {ok: true, json: async () => []};
    return {ok: true, json: async () => ({})};
  });
  staleApp.hooks.setMe({role: 'teacher', username: 'ly'});
  staleApp.nodes.get('#dataset-project').value = 'proj-1';
  await staleApp.nodes.get('#dataset-project').onchange();
  const staleRmBtn = find(staleApp.nodes.get('#project-dataset-references-list'), item => item.textContent === '[该数据集已被管理员清理 · 点击移除引用]');
  assert.ok(staleRmBtn, 'stale dataset renders remove-reference button');
  await staleRmBtn.onclick();
  const rmReq = miscReqs.find(r => r.path === '/api/projects/proj-1/stale-datasets/remove');
  assert.equal(rmReq.options.method, 'POST');
  assert.deepEqual(JSON.parse(rmReq.options.body), {dataset_id: 'drv-stale', expected_sha256: 'sha-1'});

  const reviewHost = node('section');
  await staleApp.hooks.renderReviewDetail('sub-1', reviewHost);
  const testCopyBtn = find(reviewHost, item => item.textContent === '复制到 ly 工作区用 IDE 测试');
  assert.ok(testCopyBtn, 'review detail renders test-copy button for ly');
  await testCopyBtn.onclick();
  assert.equal(miscReqs.find(r => r.path === '/api/review/submissions/sub-1/test-copy').options.method, 'POST');
  assert.ok(find(reviewHost, item => /已复制为测试项目.*降雨案例-v1/.test(item.textContent)));

  const versions = load(async () => ({
    ok: true,
    json: async () => ({
      items: [
        {task_id: 'version-1', family_id: 'family', task_version: 1, name: '同一任务'},
        {task_id: 'version-2', family_id: 'family', task_version: 2, name: '同一任务'},
      ],
    }),
  }));
  versions.hooks.setMe({role: 'student', username: 'test'});
  await versions.hooks.tasks('personal');
  assert.equal(versions.nodes.get('#task-list').children.length, 1);
  const versionGroup = versions.nodes.get('#task-list').children[0];
  assert.equal(versionGroup.children.filter(item => item.className === 'task-version').length, 2);
  assert.ok(find(versionGroup, item => item.textContent === '任务版本 2 · 有新版本可选'));
  console.log('task UI tests passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
