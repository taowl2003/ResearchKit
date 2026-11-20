'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const {Blob} = require('node:buffer');
const vm = require('node:vm');

function node(tagName = '') {
  return {
    tagName, textContent: '', value: '', hidden: false, disabled: false, children: [], className: '', dataset: {}, type: '', open: false,
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    querySelector(selector) { return find(this, item => matches(item, selector)); },
    querySelectorAll(selector) { return collect(this, item => matches(item, selector)); },
  };
}
function matches(item, selector) {
  if (selector.startsWith('.')) return String(item.className || '').split(/\s+/).includes(selector.slice(1));
  if (selector === '[data-data-review-action]') return Boolean(item.dataset?.dataReviewAction);
  return false;
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) { const found = find(child, predicate); if (found) return found; }
  return null;
}
function collect(root, predicate, result = []) {
  if (predicate(root)) result.push(root);
  for (const child of root.children || []) collect(child, predicate, result);
  return result;
}
function allText(root) { return [root.textContent || '', ...(root.children || []).map(allText)].join('\n'); }
async function flush() { for (let step = 0; step < 8; step += 1) await Promise.resolve(); await new Promise(resolve => setImmediate(resolve)); }

function makeRecord(id, kind = 'new') {
  return {
    candidate_id: id, revision: 4, relative_root: `ocean/${id}`, kind, status: 'pending',
    original_metadata: {name: `English ${id}`, description: `<img src=x onerror=alert(1)> original English`, usage: 'English use', wiki_ref: `wiki-${id}`, provenance_ref: 'wiki source'},
    current_metadata: {name: `中文 ${id}`, description: '已有中文说明', usage: '已有用途', format: 'CSV', license: '内部教学', student_conditions: '课堂使用'},
    draft_metadata: {name: `中文 ${id}`, description: '中文草稿说明', usage: '中文用途', format: 'CSV', license: '内部教学', student_conditions: '课堂使用', wiki_ref: `wiki-${id}`, provenance_ref: 'wiki source'},
    source_metadata_changed: true, tags: id === 'one' ? ['海洋', '教学'] : ['气候'],
  };
}

function load(role = 'teacher', statusOverrides = {}, listPage = null) {
  const ids = ['#data-management-status', '#data-management-counts', '#data-management-refresh', '#data-management-filter', '#data-management-query', '#data-management-tags', '#data-management-tag-mode', '#data-management-tag-clear',
    '#data-management-export', '#data-management-export-status', '#data-management-list', '#data-management-pagination', '#data-management-detail', '#data-management-detail-title', '#data-management-detail-content'];
  const nodes = new Map(ids.map(id => [id, node(id)]));
  nodes.get('#data-management-filter').value = 'pending';
  const calls = [], posts = [], timers = [], cleared = [];
  let late, accessPost;
  const records = {one: makeRecord('one'), two: makeRecord('two'), late: makeRecord('late')};
  const summary = record => ({candidate_id: record.candidate_id, revision: record.revision, kind: record.kind, status: record.status,
    relative_root: record.relative_root, dataset_id: null, registered_dataset_id: null, error: record.error || null,
    updated_at: '2026-10-14T08:00:00+08:00', name: `目录候选 ${record.candidate_id}`, description: `摘要 ${record.candidate_id}`,
    source_metadata_changed: Boolean(record.source_metadata_changed), tags: record.tags || []});
  const status = {...{configured: true, worker_online: true, last_scan: '2026-10-13T08:00:00+08:00', active_job: 'scan-1'}, ...statusOverrides,
    counts: {...{pending: 2, queued: 1, publishing: 0, published: 3, ignored: 0, failed: 0, missing: 0, error: 0}, ...(statusOverrides.counts || {})}};
  const context = {
    window: {confirm: () => true}, Blob, URL: {createObjectURL() { throw new Error('unused'); }},
    document: {
      querySelector: id => nodes.get(id), createElement: tag => node(tag), createTextNode: value => { const text = node('#text'); text.textContent = value; return text; },
    },
    setTimeout(fn, ms) { const token = {fn, ms, cleared: false}; timers.push(token); return token; },
    clearTimeout(token) { token.cleared = true; cleared.push(token); },
    encodeURIComponent, JSON, Map, Set,
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/lifecycle.js', 'utf8'), context, {filename: 'lifecycle.js'});
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/markdown.js', 'utf8'), context, {filename: 'markdown.js'});
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/readme-export.js', 'utf8'), context, {filename: 'readme-export.js'});
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/data-manager.js', 'utf8'), context, {filename: 'data-manager.js'});
  const manager = context.window.ResearchKitDataManager;
  manager.init({
    getMe: () => ({role}),
    api: async path => {
      calls.push(path);
      if (path === '/api/data-management') return {...status, counts: {...status.counts}};
      if (path.includes('/candidates?')) {
        const params = new URLSearchParams(path.split('?')[1]);
        if (listPage) return listPage(params);
        const offset = Number(params.get('offset'));
        const items = offset ? [summary(records.two)] : [summary(records.one)];
        return {items, total: 100, offset, limit: 50, available_tags: ['教学', '气候', '海洋']};
      }
      if (path.endsWith('/late')) return new Promise(resolve => { late = resolve; });
      const id = decodeURIComponent(path.split('/').pop());
      if (records[id]) return records[id];
      throw new Error(`unexpected ${path}`);
    },
    post: async (path, body) => { posts.push({path, body}); if (path.endsWith('/access') && accessPost) return accessPost(path, body); return {ok: true}; },
  });
  return {manager, nodes, calls, posts, timers, cleared, records, resolveLate: value => late(value), setAccessPost: handler => { accessPost = handler; }};
}

(async () => {
  const app = load();
  app.manager.enter();
  await flush();
  assert.equal(app.posts[0].path, '/api/data-management/scan', 'opening the teacher page requests an idempotent scan');
  assert.ok(app.calls.some(path => path.includes('status=pending')), `default list is pending review: ${app.calls.join(', ')}`);
  assert.equal(app.manager._test.state.tagMode, 'all', 'multiple labels default to their intersection');
  assert.ok(app.calls.some(path => path.includes('tag_mode=all')), 'the default mode is sent to the broker');
  assert.match(allText(app.nodes.get('#data-management-list')), /目录候选 one/);
  assert.match(allText(app.nodes.get('#data-management-list')), /摘要 one/);
  assert.equal(app.nodes.get('#data-management-refresh').disabled, false, 'configured worker enables scanning');
  assert.equal(app.timers.length, 1, 'queued work begins a five-second polling cycle');
  assert.equal(app.timers[0].ms, 5000);

  const oceanTag = find(app.nodes.get('#data-management-tags'), item => item.textContent === '海洋');
  await oceanTag.onclick(); await flush();
  assert.equal([...app.manager._test.state.tags].join(','), '海洋', 'selecting a tag is retained in UI state');
  assert.ok(app.calls.some(path => decodeURIComponent(path).includes('tags=["海洋"]')), 'the selected tags are sent as JSON');
  app.nodes.get('#data-management-tag-mode').value = 'any'; app.nodes.get('#data-management-tag-mode').onchange(); await flush();
  assert.equal(app.manager._test.state.tagMode, 'any', 'broadening to any label is an explicit user choice');
  app.nodes.get('#data-management-tag-clear').onclick(); await flush();
  assert.equal([...app.manager._test.state.tags].length, 0, 'clearing tags restores the unfiltered list');

  await app.manager.openCandidate('one');
  const detail = app.nodes.get('#data-management-detail-content');
  const english = find(detail, item => item.tagName === 'details' && item.className === 'data-management-original');
  assert.ok(english, 'original English evidence is folded by default');
  assert.equal(english.open, false);
  const textareas = collect(detail, item => item.tagName === 'textarea');
  assert.equal(textareas[0].value, '中文草稿说明', 'Chinese draft is preferred over English source');
  assert.ok(allText(english).includes('<img src=x onerror=alert(1)> original English'), 'evidence is rendered as text');
  assert.equal(english.innerHTML, undefined, 'the UI never injects source text as HTML');
  assert.ok(allText(detail).includes('海洋') && allText(detail).includes('教学'), 'detail view displays source tags');

  textareas[0].value = '手工改过的中文说明'; textareas[0].oninput();
  await app.manager.refresh(false);
  assert.equal(app.manager._test.state.drafts.get('one').metadata.description, '手工改过的中文说明', 'refresh does not discard an open review draft');
  const boundary = find(detail, item => item.type === 'checkbox');
  const approve = find(detail, item => item.dataset?.dataReviewAction === 'approve');
  assert.equal(approve.disabled, true, `new datasets require a boundary confirmation: ${JSON.stringify(app.manager._test.state.drafts.get('one'))}`);
  boundary.checked = true; boundary.onchange();
  assert.equal(approve.disabled, false);
  await approve.onclick();
  const review = app.posts.find(call => call.path.endsWith('/candidates/one/review'));
  assert.equal(review.body.decision, 'approve');
  assert.equal(review.body.metadata.description, '手工改过的中文说明');
  assert.equal(review.body.readers, 'platform-users');
  assert.ok(!Object.hasOwn(review.body, 'boundaryConfirmed'), 'the boundary confirmation stays a local UI gate');
  assert.deepEqual(Object.keys(review.body.metadata).sort(), ['description', 'format', 'license', 'name', 'student_conditions', 'usage'], 'the browser never submits editable source evidence fields');

  app.records.metadata = {...makeRecord('metadata', 'metadata-change'), readers: [{uid: 2002, username: 'test'}], status: 'pending'};
  await app.manager.openCandidate('metadata');
  assert.ok(allText(app.nodes.get('#data-management-detail-content')).includes('使用范围保持不变'), 'metadata-only updates keep the existing authorization read-only');
  assert.ok(allText(app.nodes.get('#data-management-detail-content')).includes('指定账号：test'), 'registry reader objects are displayed using their usernames');
  const metadataApprove = find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction === 'approve');
  await metadataApprove.onclick();
  const metadataReview = app.posts.find(call => call.path.endsWith('/candidates/metadata/review'));
  assert.ok(!Object.hasOwn(metadataReview.body, 'readers'), 'metadata-only review leaves existing readers unchanged');

  app.records.access = {...makeRecord('access', 'unchanged'), status: 'published', registered_dataset_id: 'shared-demo',
    readers: 'platform-users', access_token: 'a'.repeat(64), access_history: [], lifecycle: {state: 'active', revision: 'lifecycle-1'}};
  await app.manager.openCandidate('access');
  const accessHost = app.nodes.get('#data-management-detail-content');
  const accessButton = () => find(accessHost, item => item.dataset?.dataReviewAction === 'access-edit');
  assert.equal(accessButton().disabled, false, 'registered data has a permission editor');
  assert.equal(find(accessHost, item => /查看引用与清理|下架|回收站/.test(item.textContent)), null,
    'data registration review has no lifecycle or physical-cleanup entrance');
  assert.equal(app.calls.some(path => path.startsWith('/api/asset-lifecycle?')), false,
    'data registration review never requests legacy lifecycle cleanup');
  accessButton().onclick();
  let scope = find(accessHost, item => item.dataset?.accessControl === 'scope');
  let accounts = find(accessHost, item => item.dataset?.accessControl === 'accounts');
  const accessSave = () => find(accessHost, item => item.dataset?.dataReviewAction === 'access-save');
  scope.value = 'accounts'; scope.onchange();
  assert.equal(accessSave().disabled, true, 'empty target accounts cannot be saved');
  accounts.value = 'ly, test'; accounts.oninput();
  await accessSave().onclick();
  const permissionPost = app.posts.find(call => call.path.endsWith('/access'));
  assert.equal(permissionPost.body.access_token, 'a'.repeat(64));
  assert.equal(permissionPost.body.readers.join(','), 'ly,test');
  assert.deepEqual(Object.keys(permissionPost.body).sort(), ['access_token','readers','revision']);
  app.records.access.access_history = [{status:'queued', readers:[{uid:1,username:'ly'}], before_readers:'platform-users', reviewer_username:'ly', created_at:'now'}];
  await app.manager.openCandidate('access');
  assert.equal(accessButton().disabled, true, 'pending access edits cannot be overwritten');
  assert.match(allText(accessHost), /等待生效/);
  assert.match(allText(accessHost), /当前使用范围：所有平台用户/, 'requested permissions are not displayed as already applied');
  app.records.access = {...app.records.access, readers:[{uid:1,username:'ly'}], access_token:'b'.repeat(64),
    access_history:[{...app.records.access.access_history[0], status:'succeeded'}]};
  await app.manager.refresh(false);
  assert.match(allText(accessHost), /当前使用范围：指定账号：ly/, 'polling refreshes a completed access task');
  accessButton().onclick();
  accounts = find(accessHost, item => item.dataset?.accessControl === 'accounts');
  accounts.value = 'ly, test'; accounts.oninput();
  app.records.access.access_token = 'c'.repeat(64);
  await app.manager.openCandidate('access');
  assert.equal(accessSave().disabled, true, 'stale permission drafts cannot silently overwrite a newer scope');
  assert.match(allText(accessHost), /授权或候选已经更新/);
  find(accessHost, item => item.textContent === '读取最新使用范围').onclick(); await flush();
  assert.equal(accessButton().disabled, false);
  accessButton().onclick();
  app.setAccessPost(async () => { throw new Error('授权已变化，请刷新'); });
  await accessSave().onclick();
  assert.match(allText(accessHost), /授权已变化，请刷新/);
  assert.ok(app.manager._test.state.accessDrafts.has('access'), 'failed permission saves preserve the draft');
  find(accessHost, item => item.textContent === '读取最新使用范围').onclick(); await flush();
  accessButton().onclick();
  let rejectAccess;
  app.setAccessPost(() => new Promise((resolve, reject) => { rejectAccess = reject; }));
  const savingAccess = accessSave().onclick();
  assert.equal(find(accessHost, item => item.dataset?.accessControl === 'scope').disabled, true, 'in-flight permission requests freeze the inputs');
  await app.manager.openCandidate('two');
  rejectAccess(new Error('属于上一个数据集的错误')); await savingAccess;
  assert.ok(!allText(accessHost).includes('属于上一个数据集的错误'), 'late permission failures cannot corrupt another candidate');
  app.setAccessPost(null);

  app.nodes.get('#data-management-filter').value = 'active';
  app.nodes.get('#data-management-filter').onchange();
  await flush();
  assert.ok(app.calls.some(path => path.includes('status=active')), 'the Chinese in-progress filter is paged by the server across scanning and publication states');
  app.manager._test.renderList({items: [{candidate_id: 'one', revision: 4, kind: 'new', status: 'queued', relative_root: 'ocean/one', name: '目录候选 one', description: '摘要 one'}], total: 100, offset: 0, limit: 50});
  const next = find(app.nodes.get('#data-management-pagination'), item => item.textContent === '下一页');
  await next.onclick();
  assert.ok(app.calls.some(path => path.includes('status=active') && path.includes('offset=50')), 'active candidates retain server pagination beyond the first 50 rows');
  app.nodes.get('#data-management-filter').value = 'attention';
  app.nodes.get('#data-management-filter').onchange();
  await flush();
  assert.ok(app.calls.some(path => path.includes('status=attention')), 'the attention filter asks the server for failed, missing, and error candidates');

  app.records.stale = makeRecord('stale');
  await app.manager.openCandidate('stale');
  const staleDescription = collect(app.nodes.get('#data-management-detail-content'), item => item.tagName === 'textarea')[0];
  staleDescription.value = '不要自动覆盖的本地草稿'; staleDescription.oninput();
  app.records.stale = {...app.records.stale, revision: 5};
  await app.manager.openCandidate('stale');
  assert.ok(allText(app.nodes.get('#data-management-detail-content')).includes('候选来源已经更新'), 'a newer source revision warns before replacing a local draft');
  const reload = find(app.nodes.get('#data-management-detail-content'), item => item.textContent === '读取最新候选并重审');
  reload.onclick(); await flush();
  assert.equal(app.manager._test.state.drafts.get('stale').revision, 5, 'confirmed reload drops only the selected old draft and reads the current candidate');

  app.manager.openCandidate('late');
  app.manager.openCandidate('two');
  await flush();
  app.resolveLate(app.records.late);
  await flush();
  assert.equal(allText(app.nodes.get('#data-management-detail-content')).includes('中文 late'), false, 'a stale detail response cannot replace another candidate');

  const changed = {...makeRecord('changed', 'data-change'), error: '仅比较目录结构和文件属性，未比较文件内容；新增 1 项，移除 0 项，属性变化 1 项；属性变化 "data.csv"（修改时间）'};
  app.manager._test.renderDetail(changed);
  const changedApprove = find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction === 'approve');
  assert.equal(changedApprove.disabled, true, 'changed or unreadable original directories cannot overwrite an old dataset version');
  assert.ok(allText(app.nodes.get('#data-management-detail-content')).includes('保留旧版本'), 'the replacement guidance is visible in Chinese');
  assert.ok(allText(app.nodes.get('#data-management-detail-content')).includes('新增 1 项，移除 0 项，属性变化 1 项'), 'the scan summary is shown');
  assert.ok(allText(app.nodes.get('#data-management-detail-content')).includes('data.csv"（修改时间）'), 'the changed path and fields are shown');
  assert.ok(allText(app.nodes.get('#data-management-detail-content')).includes('不一定意味着文件内容改变'), 'metadata differences are not described as changed file content');
  assert.equal(find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction === 'ignore').disabled, false, 'a changed candidate can still be ignored');
  app.manager._test.renderList({items: [changed], total: 1, offset: 0, limit: 50});
  assert.ok(allText(app.nodes.get('#data-management-list')).includes('data.csv"（修改时间）'), 'the list also shows the scan details');

  const missing = {...makeRecord('missing', 'missing'), error: '无法访问目录：权限不足'};
  app.manager._test.renderDetail(missing);
  assert.ok(allText(app.nodes.get('#data-management-detail-content')).includes('无法访问目录：权限不足'), 'unreadable directories show the backend reason');
  assert.equal(find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction === 'approve').disabled, true);

  const withdrawn = {...makeRecord('old-child', 'boundary-change'), status: 'withdrawn',
    error: '已由上层数据集目录 GIS/new 覆盖，已自动收起；没有登记或删除数据。'};
  app.manager._test.renderList({items: [withdrawn], total: 1, offset: 0, limit: 50});
  assert.match(allText(app.nodes.get('#data-management-list')), /自动收起（历史）/);
  assert.match(allText(app.nodes.get('#data-management-list')), /查看历史原因/);
  assert.match(allText(app.nodes.get('#data-management-list')), /GIS\/new/);
  app.manager._test.renderDetail(withdrawn);
  assert.match(allText(app.nodes.get('#data-management-detail-content')), /未登记候选/);
  assert.equal(find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction), null,
    'automatically withdrawn history has no review, ignore, or registration actions');
  app.nodes.get('#data-management-filter').value = 'withdrawn';
  app.nodes.get('#data-management-filter').onchange(); await flush();
  assert.ok(app.calls.some(path => path.includes('status=withdrawn')), 'automatic withdrawals are only requested by their explicit history filter');
  assert.match(fs.readFileSync('core/researchkit/web_static/index.html', 'utf8'), /value="withdrawn">自动收起（历史）/);

  const queued = {...makeRecord('queued', 'metadata-change'), status: 'queued'};
  app.manager._test.renderDetail(queued);
  assert.equal(find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction === 'approve').disabled, true, 'queued publication cannot be submitted twice');
  assert.equal(find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction === 'ignore').disabled, false, 'queued publication can still be cancelled');
  const publishing = {...makeRecord('publishing', 'metadata-change'), status: 'publishing'};
  app.manager._test.renderDetail(publishing);
  assert.equal(find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction === 'approve').disabled, true, 'active publication cannot be submitted twice');
  assert.equal(find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction === 'ignore').disabled, true, 'active publication cannot be cancelled mid-write');

  const readme = {...makeRecord('readme'), readme_managed: true,
    original_metadata: {...makeRecord('readme').original_metadata, description: '# 降雨数据\n\n<script>alert(1)</script> 中文说明\n\n| 文件 | 说明 |\n| --- | --- |\n| rain.csv | 样例 |'},
    current_metadata: {...makeRecord('readme').current_metadata, description: '旧说明'}};
  app.manager._test.renderDetail(readme);
  const readmeHost = app.nodes.get('#data-management-detail-content');
  const readmeDescription = collect(readmeHost, item => item.tagName === 'textarea')[0];
  assert.match(readmeDescription.value, /<script>alert\(1\)<\/script> 中文说明/, 'README original text is used as the description');
  assert.equal(readmeDescription.readOnly, true, 'README description is edited in its file');
  assert.equal(readmeDescription.innerHTML, undefined, 'README source is never injected as HTML');
  assert.ok(allText(readmeHost).includes('<script>alert(1)</script> 中文说明'), 'README evidence is rendered as text');
  assert.ok(find(readmeHost, item => item.tagName === 'h1'), 'README headings are rendered for reviewers');
  assert.ok(find(readmeHost, item => item.tagName === 'table'), 'README tables are rendered for reviewers');
  assert.equal(find(readmeHost, item => item.type === 'checkbox'), null, 'README defines the boundary without another checkbox');
  const readmeApprove = find(readmeHost, item => item.dataset?.dataReviewAction === 'approve');
  assert.equal(readmeApprove.disabled, false);
  assert.match(readmeApprove.textContent, /自动准备权限/);
  await readmeApprove.onclick();
  assert.equal(app.posts.find(call => call.path.endsWith('/candidates/readme/review')).body.decision, 'approve');

  const emptyReadme = {...readme, candidate_id: 'empty-readme', original_metadata: {...readme.original_metadata, description: ''}};
  app.manager._test.renderDetail(emptyReadme);
  const emptyDescription = collect(app.nodes.get('#data-management-detail-content'), item => item.tagName === 'textarea')[0];
  assert.equal(emptyDescription.value, '', 'empty README must not fall back to an older description');
  assert.equal(emptyDescription.readOnly, true);

  const fullBody = '# 完整数据说明\n\n' + '正文测试。\n'.repeat(5000) + '\n## 最后章节\n\n**尾部必须显示**\n';
  const fullRecord = {...readme, candidate_id:'full-readme', readme:{markdown:fullBody,complete:true},
    original_metadata:{...readme.original_metadata, description:'短摘要'}};
  app.records['full-readme'] = fullRecord;
  const beforeRequests = app.calls.filter(path=>path.endsWith('/full-readme')).length;
  const selectedBefore = app.manager._test.state.selectedId;
  app.manager._test.renderList({items:[{...fullRecord,description:'短摘要'}],total:1,offset:0,limit:50});
  const previewList = app.nodes.get('#data-management-list');
  const expand = find(previewList,item=>item.tagName==='details' && item.className==='readme-expand');
  assert.equal(expand.open,false,'individual README bodies start collapsed');
  assert.equal(app.calls.filter(path=>path.endsWith('/full-readme')).length,beforeRequests,'list rendering does not fetch full bodies');
  assert.equal(allText(previewList).includes('尾部必须显示'),false);
  expand.open=true;await expand.ontoggle();
  assert.ok(find(previewList,item=>item.tagName==='h2' && allText(item).includes('最后章节')),'expanded README reaches its final rendered heading');
  assert.ok(find(previewList,item=>item.tagName==='strong' && allText(item).includes('尾部必须显示')));
  assert.equal(app.manager._test.state.selectedId,selectedBefore,'reading a README does not open or change a review');
  expand.open=false;await expand.ontoggle();expand.open=true;await expand.ontoggle();
  assert.equal(app.calls.filter(path=>path.endsWith('/full-readme')).length,beforeRequests+1,'reopening a loaded README does not refetch or duplicate it');
  app.manager._test.renderDetail(fullRecord);
  const fullReview=app.nodes.get('#data-management-detail-content');
  assert.ok(find(fullReview,item=>item.tagName==='h2' && allText(item).includes('最后章节')),'review shows full Markdown by default');
  assert.equal(allText(fullReview).includes('审核草稿：#'),false,'README markdown is not duplicated as a raw review diff');
  const rawSource=find(fullReview,item=>item.tagName==='details' && item.className==='readme-source');
  assert.equal(rawSource.open,false,'raw README text is only an optional folded reference');
  assert.equal(find(rawSource,item=>item.tagName==='textarea').value,fullBody);

  const unknown = {...makeRecord('unknown'), current_metadata: null, draft_metadata: {name: 'English unknown', description: 'English only', usage: 'English use'}};
  app.manager._test.renderDetail(unknown);
  assert.equal(collect(app.nodes.get('#data-management-detail-content'), item => item.tagName === 'textarea')[0].value, '', 'unreviewed English source text is not prefilled as a Chinese description');
  assert.ok(allText(app.nodes.get('#data-management-detail-content')).includes('尚无已审核的中文说明'), 'new English-only candidates explicitly ask for Chinese text');

  const wikiChanged = {...makeRecord('wiki-change', 'metadata-change'), sourcewordVerified: true, previous_original_metadata: {...makeRecord('old').original_metadata, description: 'older English description'}};
  app.manager._test.renderDetail(wikiChanged);
  const wikiEvidence = find(app.nodes.get('#data-management-detail-content'), item => item.tagName === 'details' && item.className === 'data-management-original');
  assert.ok(allText(wikiEvidence).includes('已核验'));
  assert.ok(allText(wikiEvidence).includes('知识库原文变更'));
  assert.ok(allText(wikiEvidence).includes('之前：older English description'));

  const published = {...makeRecord('published'), status: 'published'};
  app.manager._test.renderDetail(published);
  assert.equal(find(app.nodes.get('#data-management-detail-content'), item => item.dataset?.dataReviewAction === 'approve'), null, 'published datasets are shown read-only instead of offering a second publication');

  app.manager.leave();
  assert.ok(app.cleared.length >= 1, 'leaving the page stops polling');

  // Combined export snapshots filters and traverses every page, not only the visible 50 rows.
  const exportRows = Array.from({length:101}, (_,i) => ({...makeRecord(`export-${i}`),readme_managed:true}));
  const exportApp = load('teacher',{},params => {
    assert.equal(params.get('status'),'published'); assert.equal(params.get('query'),'海洋');
    assert.equal(params.get('tag_mode'),'all'); assert.equal(params.get('tags'),'["海试实测","侧扫声呐"]');
    const offset=Number(params.get('offset')),limit=Number(params.get('limit'));
    return {items:exportRows.slice(offset,offset+limit),total:101,offset,limit};
  });
  Object.assign(exportApp.manager._test.state,{filter:'published',query:'海洋',tags:['海试实测','侧扫声呐'],tagMode:'all',offset:50});
  exportRows.forEach(record=>{record.readme={original:'# 全文',markdown:'# 全文',complete:true};exportApp.records[record.candidate_id]=record;});
  const entries=await exportApp.manager._test.exportEntries();
  assert.equal(entries.length,101); assert.equal(exportApp.calls.filter(path=>path.includes('/candidates?')).length,2);
  assert.ok(exportApp.calls.some(path=>path.includes('offset=100')));
  assert.equal((await entries[100].load()).original,'# 全文');
  exportApp.records['export-0']={...exportRows[0],revision:99};
  await assert.rejects(entries[0].load(),/刚有更新/,'do not silently export a record changed since listing');

  const student = load('student');
  student.manager.enter();
  await flush();
  assert.equal(student.calls.length, 0, 'students cannot load the teacher data-management endpoint');
  assert.equal(student.posts.length, 0, 'students cannot queue a data scan from the browser');

  const unavailable = load('teacher', {configured: false, worker_online: false});
  unavailable.manager.enter();
  await flush();
  assert.equal(unavailable.posts.length, 0, 'an unconfigured installation does not enqueue scans');
  assert.equal(unavailable.calls.filter(path => path.includes('/candidates?')).length, 0, 'an unconfigured installation does not repeatedly call the candidate list');
  assert.equal(unavailable.nodes.get('#data-management-refresh').disabled, true, 'the refresh button remains disabled until the once-only backend installation is available');
  assert.match(unavailable.nodes.get('#data-management-list').textContent, /管理员完成一次安装/);

  const index = fs.readFileSync('core/researchkit/web_static/index.html', 'utf8');
  const shell = fs.readFileSync('core/researchkit/web_static/app.js', 'utf8');
  assert.match(index, /id="data-management-nav"[^>]*>数据集登记与审核/);
  assert.match(index, /id="data-management-query" type="search" maxlength="256"/);
  assert.match(index, /<label class="inline-check" hidden><input id="data-management-include-inactive" type="checkbox"/,
    'legacy inactive filter remains compatible but is not a normal review-page control');
  assert.match(index, /data-page="resource-management" data-module="dataset">返回数据管理/);
  assert.match(index, /<script src="\/static\/lifecycle\.js"><\/script>/);
  assert.match(index, /<script src="\/static\/data-manager\.js"><\/script>/);
  assert.match(shell, /me\.role !== 'teacher'/);
  assert.match(shell, /ResearchKitDataManager\?\.init/);
  process.stdout.write('web data management UI tests passed\n');
})().catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
