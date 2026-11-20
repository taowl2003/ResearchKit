'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {tagName, textContent: '', value: '', checked: false, hidden: false, disabled: false, open: false, children: [], className: '', dataset: {},
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    querySelector() { return this.button; }, reset() {}, remove() {}, click() {}, setAttribute() {}};
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) { const hit = find(child, predicate); if (hit) return hit; }
  return null;
}
function summary(details) { return (details.children || []).find(child => child.tagName === 'summary')?.textContent || ''; }
function load(fetch, brokenStorage = false) {
  const ids = ['#notice', '#output', '#create', '#import', '#package-import', '#template-ref', '#editor', '#file-name', '#editor-state', '#save', '#compare-left', '#compare-right', '#compare', '#baseline-compare', '#component-access-nav', '#asset-candidates-nav', '#demo', '#jobs-back', '#download-data-references', '#submission-filters', '#submission-owner-label', '#submission-owner', '#submission-query', '#submission-kind', '#submission-from-date', '#submission-to-date', '#submission-filter-clear', '#submission-refresh', '#submission-compare-status', '#submission-message', '#submission-list', '#submission-more', '#project-list', '#project-query', '#project-include-archived', '#distribution-list', '#distribution-intro', '#distribution-query', '#asset-message', '#asset-create', '#asset-candidate-list', '#asset-candidate-query', '#asset-detail', '#asset-detail-title', '#asset-detail-content', '#asset-file-name', '#asset-file-preview'];
  const nodes = new Map(ids.map(id => [id, node(id)])); nodes.get('#submission-kind').value = 'all';
  const storage = new Map();
  const sessionStorage = brokenStorage ? {getItem() { throw new Error('storage blocked'); }, setItem() { throw new Error('storage blocked'); }} : {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value)};
  const context = {window: {__researchkitAppTest: true}, location: {hash: ''}, history: {replaceState() {}}, URLSearchParams, sessionStorage,
    Headers: class {set() {}}, File: class {}, FormData: class {}, URL: {createObjectURL() { return 'blob:test'; }, revokeObjectURL() {}}, setTimeout() { return 1; }, clearTimeout() {},
    document: {querySelector: id => nodes.get(id), querySelectorAll: () => [], addEventListener() {}, dispatchEvent() {}, createElement: tag => node(tag), body: {append() {}}},
    fetch, confirm: () => true, CustomEvent: class {}, Intl};
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/app.js', 'utf8'), context, {filename: 'app.js'});
  return {hooks: context.window.__researchkitAppTest, nodes, storage};
}

(async () => {
  const requests = [];
  const active = {project_id: 'project-active', name: '活动项目', path: 'projects/active', source: 'manual', files: []};
  const archived = {project_id: 'project-archived', name: '历史项目', path: 'projects/history', source: 'teacher-assigned', files: [], archived: true};
  const app = load(async (path, options = {}) => {
    requests.push({path, options});
    if (path === '/api/projects/project-active/archive') {
      active.archived = true; return {ok: true, json: async () => ({project_id: active.project_id, archived: true})};
    }
    if (path === '/api/projects/project-archived/restore') {
      archived.archived = false; return {ok: true, json: async () => ({project_id: archived.project_id, archived: false})};
    }
    if (path === '/api/projects' || path === '/api/projects?include_archived=true') {
      return {ok: true, json: async () => [active, archived].filter(row => path.includes('include_archived') || !row.archived)};
    }
    if (path.startsWith('/api/review/submission-projects/source-a/versions?')) return {ok:true,json:async()=>({items:[{submission_id:'sub-a',version:'1.1',created_at:'2026-11-12T00:00:00Z'},{submission_id:'old-a',version:'1.0',created_at:'2026-11-11T00:00:00Z'}],next_cursor:null})};
    if (path.startsWith('/api/review/submission-projects?')) return {ok: true, json: async () => ({items: [
      {project: {project_id: 'source-a', owner_uid: 2002, owner_username: 'test', name: '学生来源'}, preview: {submission_id: 'sub-a', version: '1.1', kind: 'minor', created_at: '2026-11-12T00:00:00Z'}, latest_at:'2026-11-12T00:00:00Z',version_count: 2},
      {project: {project_id: 'source-b', owner_uid: 2002, owner_username: 'test', name: '较早项目'}, preview: {submission_id: 'sub-b', version: '1.0', kind: 'major', created_at: '2026-11-10T00:00:00Z'},version_count:1},
      {project: {project_id: 'source-ly', owner_uid: 1001, owner_username: 'ly', name: '教师来源'}, preview: {submission_id: 'sub-ly', version: '1.0', kind: 'major', created_at: '2026-11-11T00:00:00Z'},version_count:1}
    ], next_cursor: null})};
    if (path === '/api/distributions') return {ok: true, json: async () => [{distribution_id: 'dist-a', target_uid: 2002, target_username: 'test', source_project_name: 'alpha', source_version: '1.0', status: 'pending'}, {distribution_id: 'dist-b', target_uid: 2002, target_username: 'test', source_project_name: 'beta', source_version: '1.1', status: 'claimed'}]};
    throw new Error(`unexpected ${path}`);
  });
  app.hooks.setMe({uid: 1001, username: 'ly', role: 'teacher'});
  await app.hooks.projects();
  const projectSource = find(app.nodes.get('#project-list'), item => item.tagName === 'details' && /自建项目/.test(summary(item)));
  assert.equal(projectSource.open, false, 'project sources default to collapsed');
  const projectExpand = find(app.nodes.get('#project-list'), item => item.textContent === '全部展开'); projectExpand.onclick();
  assert.equal(find(app.nodes.get('#project-list'), item => item.tagName === 'details' && /自建项目/.test(summary(item))).open, true, 'project expand-all preserves current visible groups');
  assert.equal(find(app.nodes.get('#project-list'), item => /历史项目/.test(summary(item))), null, 'archived projects stay out of the default list');
  await app.hooks.projects();
  assert.equal(find(app.nodes.get('#project-list'), item => item.tagName === 'details' && /自建项目/.test(summary(item))).open, true, 'refresh keeps expanded source');
  assert.equal(find(app.nodes.get('#project-list'), item => item.tagName === 'details' && /活动项目/.test(summary(item))).open, true, 'expand all includes project cards');
  const archive = find(app.nodes.get('#project-list'), item => item.textContent === '归档项目'); await archive.onclick();
  assert.ok(requests.some(request => request.path === '/api/projects/project-active/archive' && request.options.method === 'POST'));
  assert.equal(find(app.nodes.get('#project-list'), item => /活动项目/.test(summary(item))), null, 'archiving refreshes and removes active project from default list');
  app.nodes.get('#project-include-archived').checked = true; await app.nodes.get('#project-include-archived').onchange();
  assert.ok(requests.some(request => request.path === '/api/projects?include_archived=true'), 'archived checkbox changes the server query');
  assert.ok(find(app.nodes.get('#project-list'), item => /已归档/.test(summary(item))), 'archived projects are labelled before opening');
  app.nodes.get('#project-query').value = '历史'; app.nodes.get('#project-query').oninput();
  assert.ok(find(app.nodes.get('#project-list'), item => /历史项目/.test(summary(item))), 'project search filters already loaded records');
  const restore = find(app.nodes.get('#project-list'), item => item.textContent === '恢复项目'); await restore.onclick();
  app.nodes.get('#project-include-archived').checked = false; await app.nodes.get('#project-include-archived').onchange();
  assert.ok(find(app.nodes.get('#project-list'), item => /历史项目/.test(summary(item))), 'restored project returns to default list');
  assert.equal(find(app.nodes.get('#project-list'), item => /已归档/.test(summary(item))), null);

  app.hooks.setEditor('case.py', {submission_id: 'left', path: 'case.py', text: 'left'}, true); app.nodes.get('#compare-left').onclick();
  app.hooks.setEditor('case.py', {submission_id: 'right', path: 'case.py', text: 'right'}, true); app.nodes.get('#compare-right').onclick(); app.hooks.renderProjects();
  assert.match(app.nodes.get('#submission-compare-status').textContent, /left.*right/, 'list redraw does not reset compare selections');

  await app.hooks.loadReviewProjects();
  assert.ok(find(app.nodes.get('#asset-candidate-list'), item => /学生来源/.test(item.textContent)), 'teacher review lists fixed submissions by their trusted source project');
  const owner = find(app.nodes.get('#asset-candidate-list'), item=>item.className==='review-owner-group' && find(item,child=>child.textContent==='来源账号：test'));
  assert.equal(owner.children.filter(item=>item.tagName==='article').length,1,'only the most recent project is directly visible for each account');
  const otherProjects = find(owner,item=>item.tagName==='details' && /更多提交/.test(summary(item)));
  assert.equal(otherProjects.open,false,'other projects start collapsed');
  const history = find(owner,item=>item.tagName==='details' && /更多版本/.test(summary(item)));
  assert.equal(history.open,false);
  await app.hooks.loadReviewHistory('source-a');
  assert.ok(find(app.nodes.get('#asset-candidate-list'),item=>item.textContent==='old-a'),'history reads work without a next_cursor on the project preview');
  otherProjects.open=true;otherProjects.ontoggle();
  await app.hooks.loadReviewProjects();
  assert.equal(find(app.nodes.get('#asset-candidate-list'), item=>item.tagName==='details' && /更多提交/.test(summary(item))).open,true,'refresh keeps this account expansion');
  app.nodes.get('#asset-candidate-query').value = 'sub-a'; app.nodes.get('#asset-candidate-query').oninput();
  assert.ok(find(app.nodes.get('#asset-candidate-list'), item => /sub-a/.test(item.textContent)), 'review search includes fixed submission IDs');

  await app.hooks.distributions();
  const teacherTarget = find(app.nodes.get('#distribution-list'), item => item.tagName === 'details' && /目标账号：test/.test(summary(item)));
  assert.equal(teacherTarget.open, false, 'teacher target groups default to collapsed');
  app.nodes.get('#distribution-query').value = 'beta'; app.nodes.get('#distribution-query').oninput();
  assert.ok(find(app.nodes.get('#distribution-list'), item => item.tagName === 'details' && /已领取/.test(summary(item))), 'distribution search filters visible records');
  app.nodes.get('#distribution-query').value = ''; app.nodes.get('#distribution-query').oninput();
  app.hooks.setMe({uid: 2002, username: 'test', role: 'student'}); await app.hooks.distributions();
  assert.equal(app.hooks.foldOpen('projects', 'project:project-active'), false, 'another account does not inherit expansion');
  assert.ok(find(app.nodes.get('#distribution-list'), item => item.tagName === 'details' && /待领取或领取待完成/.test(summary(item))), 'students group pending and claiming distributions');
  assert.ok(find(app.nodes.get('#distribution-list'), item => item.tagName === 'details' && /已领取/.test(summary(item))), 'students retain claimed group');

  const blocked = load(async () => ({ok: true, json: async () => []}), true); blocked.hooks.setMe({uid: 9, username: 'blocked', role: 'student'});
  blocked.hooks.saveFoldOpen('projects', 'project:x', true);
  assert.equal(blocked.hooks.foldOpen('projects', 'project:x'), true, 'fold state falls back to memory when session storage is blocked');
})().catch(error => { console.error(error); process.exitCode = 1; });
