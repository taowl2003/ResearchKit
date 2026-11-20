'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {
    tagName, id: '', textContent: '', value: '', hidden: false, disabled: false, required: false, maxLength: 0,
    type: '', className: '', rows: 0, placeholder: '', selected: false, children: [], dataset: {},
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
  };
}
function walk(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) { const found = walk(child, predicate); if (found) return found; }
  return null;
}
function all(root, predicate, result = []) {
  if (predicate(root)) result.push(root);
  for (const child of root.children || []) all(child, predicate, result);
  return result;
}
function textOf(root) { return [root.textContent || '', ...(root.children || []).map(textOf)].join('\n'); }
function byId(root, id) { return walk(root, value => value.id === id); }
function button(root, label) { return walk(root, value => value.tagName === 'button' && value.textContent === label); }
function flush() { return new Promise(resolve => setImmediate(resolve)); }
async function settle() { await flush(); await flush(); }
function setInput(item, value) { item.value = value; if (item.oninput) item.oninput(); }

function load(api, role = 'student') {
  const root = node('section'); root.id = 'feedback-root'; root.hidden = false;
  const unread = node('span'); unread.id = 'feedback-unread'; unread.hidden = true;
  const body = node('body'); body.append(root, unread);
  const document = {
    body,
    createElement: tag => node(tag),
    querySelector(selector) { return selector.startsWith('#') ? byId(body, selector.slice(1)) : null; },
  };
  const context = {window: {}, document, URLSearchParams, encodeURIComponent, Math, Number, String, Object, JSON,
    setTimeout(fn) { context.timer = fn; return 1; }, clearTimeout() {},
  };
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/feedback.js', 'utf8'), context, {filename: 'feedback.js'});
  context.window.ResearchKitFeedback.configure({
    api,
    post: (path, value) => api(path, {method: 'POST', body: JSON.stringify(value)}),
    getMe: () => ({role, username: role === 'teacher' ? 'ly' : 'alice'}),
  });
  return {hooks: context.window.ResearchKitFeedback, root, unread, context};
}

const thread = (overrides = {}) => ({thread_id: 'thread-1', subject: '运行失败 <img>', status: 'open', owner_username: 'alice', created_at: '2026-11-14', updated_at: '2026-11-14', unread_count: 1, last_preview: '<script>preview</script>', ...overrides});
const messages = () => ({thread: thread(), messages: [
  {message_id: 3, author_username: 'alice', author_role: 'student', body: '<img src=x onerror=alert(1)>', created_at: '09:00'},
  {message_id: 5, author_username: 'ly', author_role: 'teacher', body: '请补充日志。', created_at: '09:01'},
], has_more: true, next_before_id: 3});

(async () => {
  const calls = [];
  const student = load(async (path, options = {}) => {
    calls.push({path, options});
    if (path === '/api/feedback?page=1&status=all') return {threads: [thread()], total: 1, page: 1, page_size: 20, unread_count: 2};
    if (path === '/api/feedback/thread-1') return messages();
    if (path === '/api/feedback/thread-1?before_id=3') return {thread: thread(), messages: [{message_id: 1, author_username: 'alice', author_role: 'student', body: '第一条', created_at: '08:00'}], has_more: false, next_before_id: null};
    if (path === '/api/feedback/thread-1/read') return {unread_count: 0};
    throw new Error(`unexpected ${path}`);
  });
  student.hooks.enter(); await settle();
  assert.equal(student.unread.textContent, '2', 'configure and enter update only the unread badge');
  assert.ok(byId(student.root, 'feedback-compose-form'), 'every user has a feedback form');
  assert.equal(button(student.root, '更新状态'), null, 'students cannot render teacher-only status controls');
  assert.ok(button(student.root, '运行失败 <img>'), 'thread title uses text, not markup');
  await button(student.root, '运行失败 <img>').onclick(); await settle();
  const detail = byId(student.root, 'feedback-detail');
  assert.match(textOf(detail), /<img src=x onerror=alert\(1\)>/, 'message body remains literal text');
  assert.equal(walk(detail, value => value.tagName === 'img'), null, 'feedback text never creates markup nodes');
  const reads = calls.filter(call => call.path === '/api/feedback/thread-1/read');
  assert.equal(reads.length, 1, 'rendered thread is marked read once');
  assert.equal(JSON.parse(reads[0].options.body).message_id, 5, 'read acknowledgement uses the last displayed message ID');
  await button(detail, '加载更早消息').onclick(); await settle();
  assert.match(textOf(detail), /第一条/, 'older message page is appended to the conversation');
  assert.equal(calls.filter(call => call.path.endsWith('/read')).length, 1, 'loading older history does not falsely mark another range read');

  const teacherCalls = [];
  const teacher = load(async (path, options = {}) => {
    teacherCalls.push({path, options});
    if (path === '/api/feedback?page=1&status=all') return {threads: [thread()], unread_count: 1};
    if (path === '/api/feedback/thread-1') return {...messages(), has_more: false, next_before_id: null};
    if (path === '/api/feedback/thread-1/read') return {unread_count: 0};
    if (path === '/api/feedback/thread-1/status') return {thread_id: 'thread-1', status: 'in_progress'};
    throw new Error(`unexpected ${path}`);
  }, 'teacher');
  teacher.hooks.enter(); await settle();
  await button(teacher.root, '运行失败 <img>').onclick(); await settle();
  assert.ok(button(teacher.root, '更新状态'), 'teacher can change an item status');
  setInput(byId(teacher.root, 'feedback-reply-body'), '改状态后仍需保留的草稿');
  const select = walk(byId(teacher.root, 'feedback-detail'), value => value.tagName === 'select'); select.value = 'in_progress';
  await button(teacher.root, '更新状态').onclick(); await settle();
  const statusCall = teacherCalls.find(call => call.path === '/api/feedback/thread-1/status');
  assert.deepEqual(JSON.parse(statusCall.options.body), {status: 'in_progress'});
  assert.equal(byId(teacher.root, 'feedback-reply-body').value, '改状态后仍需保留的草稿', 'updating a thread status preserves its reply draft');

  let sendAttempts = 0;
  const retries = [];
  const sender = load(async (path, options = {}) => {
    if (path === '/api/feedback?page=1&status=all') return {threads: [], unread_count: 0};
    if (path === '/api/feedback') {
      retries.push(JSON.parse(options.body)); sendAttempts += 1;
      if (sendAttempts === 1) throw new Error('temporary network error');
      return {thread_id: 'new-thread'};
    }
    if (path === '/api/feedback/new-thread') return {thread: thread({thread_id: 'new-thread', subject: '保存草稿'}), messages: [], has_more: false, next_before_id: null};
    throw new Error(`unexpected ${path}`);
  });
  sender.hooks.enter(); await settle();
  const subject = byId(sender.root, 'feedback-subject'), body = byId(sender.root, 'feedback-body'), form = byId(sender.root, 'feedback-compose-form'), send = byId(sender.root, 'feedback-send');
  subject.value = '保存草稿'; body.value = '网络失败后仍应保留';
  await form.onsubmit({preventDefault() {}}); await settle();
  assert.equal(subject.value, '保存草稿'); assert.equal(body.value, '网络失败后仍应保留');
  assert.equal(send.textContent, '重试发送');
  await form.onsubmit({preventDefault() {}}); await settle();
  assert.equal(retries.length, 2);
  assert.equal(retries[0].request_id, retries[1].request_id, 'network retry keeps the same idempotency key');
  assert.match(retries[0].request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  assert.equal(subject.value, ''); assert.equal(body.value, '');

  let rejectLostResponse, lostAttempt = 0;
  const lostPayloads = [];
  const lost = load((path, options = {}) => {
    if (path === '/api/feedback?page=1&status=all') return Promise.resolve({threads: [], unread_count: 0});
    if (path === '/api/feedback') {
      lostPayloads.push(JSON.parse(options.body)); lostAttempt += 1;
      if (lostAttempt === 1) return new Promise((_, reject) => { rejectLostResponse = reject; });
      return Promise.resolve({thread_id: 'lost-thread'});
    }
    if (path === '/api/feedback/lost-thread') return Promise.resolve({thread: thread({thread_id: 'lost-thread', subject: '丢响应'}), messages: [], has_more: false, next_before_id: null});
    throw new Error(`unexpected ${path}`);
  });
  lost.hooks.enter(); await settle();
  setInput(byId(lost.root, 'feedback-subject'), '丢响应'); setInput(byId(lost.root, 'feedback-body'), '服务可能已经收到这一条');
  const lostFirst = byId(lost.root, 'feedback-compose-form').onsubmit({preventDefault() {}}); await flush();
  lost.hooks.leave(); lost.hooks.enter(); await settle();
  rejectLostResponse(new Error('response lost')); await lostFirst; await settle();
  assert.equal(byId(lost.root, 'feedback-body').value, '服务可能已经收到这一条', 'a lost response keeps the draft after leaving and re-entering');
  await byId(lost.root, 'feedback-compose-form').onsubmit({preventDefault() {}}); await settle();
  assert.equal(lostPayloads[0].request_id, lostPayloads[1].request_id, 'a response-lost retry keeps the original idempotency key');

  let finishPending;
  const pending = load((path) => {
    if (path.startsWith('/api/feedback?')) return Promise.resolve({threads: [], unread_count: 0});
    if (path === '/api/feedback') return new Promise(resolve => { finishPending = resolve; });
    throw new Error(`unexpected ${path}`);
  });
  pending.hooks.enter(); await settle();
  setInput(byId(pending.root, 'feedback-subject'), '发送中切页');
  setInput(byId(pending.root, 'feedback-body'), '不能把已经成功的内容误当成新消息再发');
  const sending = byId(pending.root, 'feedback-compose-form').onsubmit({preventDefault() {}});
  await flush(); pending.hooks.leave(); pending.hooks.enter(); await settle();
  finishPending({thread_id: 'already-sent'}); await sending; await settle();
  assert.equal(byId(pending.root, 'feedback-body').value, '', 'late success clears the recreated form as well as its draft');

  const drafts = load(async path => {
    if (path === '/api/feedback?page=1&status=all') return {threads: [thread(), thread({thread_id: 'thread-2', subject: '第二个会话'})], unread_count: 0};
    if (path === '/api/feedback/thread-1') return messages();
    if (path === '/api/feedback/thread-1?before_id=3') return {thread: thread(), messages: [{message_id: 1, author_username: 'alice', author_role: 'student', body: '最早消息', created_at: '08:00'}], has_more: false, next_before_id: null};
    if (path === '/api/feedback/thread-2') return {thread: thread({thread_id: 'thread-2', subject: '第二个会话'}), messages: [], has_more: false, next_before_id: null};
    if (path.endsWith('/read')) return {unread_count: 0};
    throw new Error(`unexpected ${path}`);
  }, 'teacher');
  drafts.hooks.enter(); await settle();
  setInput(byId(drafts.root, 'feedback-subject'), '离开后仍在');
  setInput(byId(drafts.root, 'feedback-body'), '页面切换不应丢失这一段');
  drafts.hooks.leave(); drafts.hooks.enter(); await settle();
  assert.equal(byId(drafts.root, 'feedback-subject').value, '离开后仍在', 'leaving and re-entering preserves the compose subject');
  assert.equal(byId(drafts.root, 'feedback-body').value, '页面切换不应丢失这一段', 'leaving and re-entering preserves the compose body');
  await button(drafts.root, '运行失败 <img>').onclick(); await settle();
  setInput(byId(drafts.root, 'feedback-reply-body'), '第一会话的回复草稿');
  await button(byId(drafts.root, 'feedback-detail'), '加载更早消息').onclick(); await settle();
  assert.equal(byId(drafts.root, 'feedback-reply-body').value, '第一会话的回复草稿', 'loading older messages preserves a reply draft');
  await button(drafts.root, '第二个会话').onclick(); await settle();
  await button(drafts.root, '运行失败 <img>').onclick(); await settle();
  assert.equal(byId(drafts.root, 'feedback-reply-body').value, '第一会话的回复草稿', 'switching conversations preserves each reply draft');

  const pagerCalls = [];
  const pager = load(async path => {
    pagerCalls.push(path);
    const parsed = new URL(`https://example.invalid${path}`);
    const page = Number(parsed.searchParams.get('page'));
    const status = parsed.searchParams.get('status');
    const offset = (page - 1) * 20;
    return {
      threads: Array.from({length: page === 3 ? 5 : 20}, (_, index) => thread({thread_id: `${status}-${offset + index}`, subject: `${status} 第 ${offset + index + 1} 条`})),
      total: 45, page, page_size: 20, unread_count: 0,
    };
  });
  pager.hooks.enter(); await settle();
  assert.ok(button(pager.root, '下一页'), 'a list larger than one page exposes next page');
  await button(pager.root, '下一页').onclick(); await settle();
  assert.ok(button(pager.root, 'all 第 21 条'), 'next page replaces the thread list');
  const filter = byId(pager.root, 'feedback-status-filter'); filter.value = 'resolved'; await filter.onchange(); await settle();
  assert.ok(pagerCalls.some(path => path === '/api/feedback?page=1&status=resolved'), 'status filtering resets to page one and keeps its selected state');

  let resolveThread, readAfterLeave = 0;
  const late = load((path, options = {}) => {
    if (path === '/api/feedback?page=1&status=all') return Promise.resolve({threads: [thread()], unread_count: 1});
    if (path === '/api/feedback/thread-1') return new Promise(resolve => { resolveThread = resolve; });
    if (path === '/api/feedback/thread-1/read') { readAfterLeave += 1; return Promise.resolve({unread_count: 0}); }
    throw new Error(`unexpected ${path} ${options.method || 'GET'}`);
  });
  late.hooks.enter(); await settle();
  const openLate = button(late.root, '运行失败 <img>').onclick(); await flush();
  late.hooks.leave();
  resolveThread(messages()); await openLate; await settle();
  assert.equal(readAfterLeave, 0, 'a response after leaving does not mark unseen messages read');

  let replyAttempts = 0;
  const replyPayloads = [];
  const replier = load(async (path, options = {}) => {
    if (path === '/api/feedback?page=1&status=all') return {threads: [thread()], unread_count: 0};
    if (path === '/api/feedback/thread-1') return {...messages(), has_more: false, next_before_id: null};
    if (path === '/api/feedback/thread-1/read') return {unread_count: 0};
    if (path === '/api/feedback/thread-1/reply') {
      replyPayloads.push(JSON.parse(options.body)); replyAttempts += 1;
      if (replyAttempts === 1) throw new Error('temporary failure');
      return {thread_id: 'thread-1', message_id: 6};
    }
    throw new Error(`unexpected ${path}`);
  });
  replier.hooks.enter(); await settle(); await button(replier.root, '运行失败 <img>').onclick(); await settle();
  const replyBody = byId(replier.root, 'feedback-reply-body'); const replyForm = walk(byId(replier.root, 'feedback-detail'), value => value.tagName === 'form' && value !== byId(replier.root, 'feedback-compose-form'));
  replyBody.value = '会话回复草稿';
  await replyForm.onsubmit({preventDefault() {}}); await settle();
  assert.equal(replyBody.value, '会话回复草稿');
  await replyForm.onsubmit({preventDefault() {}}); await settle();
  assert.equal(replyPayloads[0].request_id, replyPayloads[1].request_id, 'reply retries keep its idempotency key');
  assert.equal(replyBody.value, '');

  process.stdout.write('web feedback UI tests passed\n');
})().catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
