'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {tagName, textContent: '', value: '', hidden: false, disabled: false, open: false, children: [], className: '', dataset: {},
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
    querySelector() { return this.button; }, reset() { this.resetCalled = true; }, remove() {}, click() {}, setAttribute() {}};
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) { const result = find(child, predicate); if (result) return result; }
  return null;
}
async function flush() { await Promise.resolve(); await Promise.resolve(); await new Promise(resolve => setImmediate(resolve)); }
function submission(id, projectId, owner, name, kind = 'major') {
  return {submission_id: id, project_id: projectId, version: id, kind, created_at: '2026-10-10T03:04:05Z', content_sha256: id.padEnd(64, 'a').slice(0, 64),
    project: {project_id: projectId, owner_uid: owner === 'ly' ? 1001 : 2002, owner_username: owner, name, source: 'manual'}};
}
function projectItem(preview, versionCount = 1, next = null) {
  return {project: preview.project, preview, version_count: versionCount, latest_at: preview.created_at, next_cursor: next};
}
function load(fetch) {
  const ids = ['#notice', '#output', '#create', '#import', '#package-import', '#template-ref', '#editor', '#file-name', '#editor-state', '#save', '#compare-left', '#compare-right', '#compare', '#baseline-compare', '#component-access-nav', '#asset-candidates-nav', '#demo', '#jobs-back', '#download-data-references', '#submission-filters', '#submission-owner-label', '#submission-owner', '#submission-query', '#submission-kind', '#submission-from-date', '#submission-to-date', '#submission-filter-clear', '#submission-refresh', '#submission-compare-status', '#submission-message', '#submission-list', '#submission-more'];
  const nodes = new Map(ids.map(id => [id, node(id)]));
  nodes.get('#submission-kind').value = 'all';
  const context = {
    window: {__researchkitAppTest: true}, location: {hash: ''}, history: {replaceState() {}}, URLSearchParams,
    sessionStorage: {getItem: () => null, setItem() {}}, Headers: class {set() {}}, File: class {}, FormData: class {},
    URL: {createObjectURL() { return 'blob:test'; }, revokeObjectURL() {}}, setTimeout() { return 1; }, clearTimeout() {},
    document: {querySelector: id => nodes.get(id), querySelectorAll: () => [], addEventListener() {}, dispatchEvent() {}, createElement: tag => node(tag), body: {append() {}}},
    fetch, confirm: () => true, CustomEvent: class {}, Intl,
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/app.js', 'utf8'), context, {filename: 'app.js'});
  return {hooks: context.window.__researchkitAppTest, nodes};
}

(async () => {
  const appSource = fs.readFileSync('core/researchkit/web_static/app.js', 'utf8');
  assert.match(appSource, /const selectedVersions = \{\}/, 'submission confirmation does not shadow the versions() refresh function');
  assert.match(appSource, /submission-prepare`, idempotentRequest\(retry/, 'submission preparation retries keep a stable request id');
  const left = submission('left-version', 'project-a', 'ly', '同名项目');
  const other = submission('student-version', 'project-b', 'test', '同名项目', 'minor');
  const requests = [];
  const app = load(async path => {
    requests.push(path);
    if (path.startsWith('/api/submission-projects?')) return {ok: true, json: async () => ({items: [projectItem(left, 3)], next_cursor: null})};
    if (path.startsWith('/api/submission-projects/project-a/versions?')) return {ok: true, json: async () => ({items: [left, submission('older-version', 'project-a', 'ly', '同名项目', 'minor')], next_cursor: null})};
    throw new Error(`unexpected ${path}`);
  });
  app.hooks.setMe({role: 'teacher', uid: 1001});
  app.nodes.get('#submission-query').value = '同名';
  app.nodes.get('#submission-owner').value = 'ly';
  app.nodes.get('#submission-kind').value = 'major';
  app.nodes.get('#submission-from-date').value = '2026-10-01';
  app.nodes.get('#submission-to-date').value = '2026-10-10';
  await app.hooks.versions();
  assert.equal(requests[0], '/api/submission-projects?q=%E5%90%8C%E5%90%8D&kind=major&from_date=2026-10-01&to_date=2026-10-10&limit=20');
  assert.ok(find(app.nodes.get('#submission-list'), item => /所属账号：ly/.test(item.textContent)));
  assert.equal(find(app.nodes.get('#submission-list'), item => /所属账号：test/.test(item.textContent)), null, 'personal fixed submissions are current-user only');
  assert.ok(requests.every(path => !/[?&]owner=/.test(path)), 'a forged legacy owner field is never sent');
  assert.ok(find(app.nodes.get('#submission-list'), item => item.textContent === '项目 ID：project-a · 自建项目'));
  assert.match(find(app.nodes.get('#submission-list'), item => /最新匹配提交/.test(item.textContent)).textContent, /北京时间/);
  assert.equal(requests.filter(path => path.includes('/versions?')).length, 0, 'history is not loaded before opening details');
  app.nodes.get('#submission-query').value = '尚未查询的新条件';
  const hasSummary = (item, pattern) => item.tagName === 'details' && (item.children || []).some(child => pattern.test(child.textContent));
  const ownerDetails = find(app.nodes.get('#submission-list'), item => hasSummary(item, /所属账号：ly/));
  ownerDetails.open = true; ownerDetails.ontoggle();
  const details = find(app.nodes.get('#submission-list'), item => hasSummary(item, /查看历史版本/));
  details.open = true; details.ontoggle(); await flush();
  assert.equal(requests.filter(path => path.includes('/versions?')).length, 1);
  assert.match(requests.find(path => path.includes('/versions?')), /q=%E5%90%8C%E5%90%8D/, 'history keeps the filters applied by the project query');
  assert.ok(find(app.nodes.get('#submission-list'), item => item.textContent.includes('older-version')));
  assert.equal(find(app.nodes.get('#submission-list'), item => hasSummary(item, /查看历史版本/)).open, true, 'history remains open while its request is rendered');
  await app.hooks.versions();
  const refreshedOwner = find(app.nodes.get('#submission-list'), item => hasSummary(item, /所属账号：ly/));
  assert.equal(refreshedOwner.open, true, 'account expansion survives refresh');
  refreshedOwner.ontoggle(); await flush();
  assert.equal(requests.filter(path => path.includes('/versions?')).length, 2, 'restored visible history reloads under the new query');

  app.hooks.setEditor('case.py', {submission_id: 'left-version', path: 'case.py', text: 'left'}, true);
  app.nodes.get('#compare-left').onclick();
  app.hooks.setEditor('case.py', {submission_id: 'right-version', path: 'case.py', text: 'right'}, true);
  app.nodes.get('#compare-right').onclick();
  await app.hooks.versions();
  assert.match(app.nodes.get('#submission-compare-status').textContent, /left-version/);
  assert.match(app.nodes.get('#submission-compare-status').textContent, /right-version/);

  let oldReply, newReply;
  const race = load(path => {
    if (!oldReply) return new Promise(resolve => { oldReply = resolve; });
    return new Promise(resolve => { newReply = resolve; });
  });
  race.nodes.get('#submission-query').value = 'old';
  const oldLoad = race.hooks.versions();
  race.nodes.get('#submission-query').value = 'new';
  const newLoad = race.hooks.versions();
  newReply({ok: true, json: async () => ({items: [projectItem(submission('new-version', 'new-project', 'ly', '新项目'))], next_cursor: null})});
  await newLoad;
  oldReply({ok: true, json: async () => ({items: [projectItem(submission('old-version', 'old-project', 'ly', '旧项目'))], next_cursor: null})});
  await oldLoad;
  assert.ok(find(race.nodes.get('#submission-list'), item => /新项目/.test(item.textContent)));
  assert.equal(find(race.nodes.get('#submission-list'), item => /旧项目/.test(item.textContent)), null, 'late query response cannot replace a newer filter');

  let firstPage = true, staleMore, morePath;
  const paging = load(path => {
    if (path.includes('cursor=cursor-1')) { morePath = path; return new Promise(resolve => { staleMore = resolve; }); }
    if (firstPage) { firstPage = false; return Promise.resolve({ok: true, json: async () => ({items: [projectItem(submission('page-one', 'page-one-project', 'ly', '第一页'))], next_cursor: 'cursor-1'})}); }
    return Promise.resolve({ok: true, json: async () => ({items: [projectItem(submission('fresh-page', 'fresh-project', 'ly', '新筛选'))], next_cursor: null})});
  });
  await paging.hooks.versions();
  paging.nodes.get('#submission-query').value = 'changed-but-not-queried';
  find(paging.nodes.get('#submission-more'), item => item.textContent === '加载更多项目').onclick();
  assert.equal(morePath.includes('q=changed-but-not-queried'), false, 'next page keeps the filters applied by the first page');
  paging.nodes.get('#submission-query').value = 'fresh';
  await paging.hooks.versions();
  staleMore({ok: true, json: async () => ({items: [projectItem(submission('stale-page', 'stale-project', 'ly', '旧分页'))], next_cursor: null})});
  await flush();
  assert.ok(find(paging.nodes.get('#submission-list'), item => /新筛选/.test(item.textContent)));
  assert.equal(find(paging.nodes.get('#submission-list'), item => /旧分页/.test(item.textContent)), null, 'late next page cannot enter a refreshed filter');

  const failing = load(async () => ({ok: false, status: 503, json: async () => ({error: 'temporarily unavailable'})}));
  await failing.hooks.versions();
  assert.match(failing.nodes.get('#submission-message').textContent, /temporarily unavailable/);
  assert.ok(find(failing.nodes.get('#submission-list'), item => item.textContent === '重试读取固定提交'));
})().catch(error => { console.error(error); process.exitCode = 1; });
