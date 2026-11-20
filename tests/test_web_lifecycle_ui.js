'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {
    tagName, textContent: '', children: [], className: '', dataset: {}, disabled: false, type: '',
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; },
    remove() { this.removed = true; },
    querySelector(selector) { return find(this, item => selector === '.asset-lifecycle-references' && item.className === 'asset-lifecycle-references' && !item.removed); },
  };
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) { const found = find(child, predicate); if (found) return found; }
  return null;
}
function textOf(root) { return [root.textContent || '', ...(root.children || []).filter(item => !item.removed).map(textOf)].join('\n'); }
function button(root, label) { return find(root, item => item.tagName === 'button' && item.textContent === label); }
function flush() { return new Promise(resolve => setImmediate(resolve)); }

function load(api) {
  const context = {window: {}, document: {createElement: tag => node(tag)}, encodeURIComponent, JSON};
  vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/lifecycle.js', 'utf8'), context, {filename: 'lifecycle.js'});
  const host = node('div'), messages = [];
  const panel = context.window.ResearchKitLifecycle.append(host, {
    kind: 'dataset', ref: 'dataset-v1', lifecycle: {state: 'active', revision: 'old'}, api,
    message: value => messages.push(value),
  });
  return {host, panel, messages};
}

(async () => {
  const calls = [];
  let report = {kind: 'dataset', ref: 'dataset-v1', state: 'active', revision: 'one', reference_count: 55,
    references: [{type: 'fixed-submission', label: '<img src=x>', owner_username: 'ly', project_id: 'project-1'}],
    notes: ['未提交的 IDE 修改不在检查范围内。'], can_trash: false,
    history: [{action: 'retire', actor_username: 'ly', created_at: '2026-10-24T12:00:00+08:00'}], offset: 0, limit: 50};
  const app = load(async (path, options) => {
    calls.push({path, options});
    if (path.startsWith('/api/asset-lifecycle?')) return report;
    if (path === '/api/asset-lifecycle' && options.method === 'PUT') {
      const body = JSON.parse(options.body);
      assert.equal(body.expected_revision, report.revision);
      if (body.action === 'retire') return report = {...report, state: 'retired', revision: 'two', can_trash: false};
      if (body.action === 'restore') return report = {...report, state: 'active', revision: 'three'};
      if (body.action === 'trash') return report = {...report, state: 'trashed', revision: 'four'};
    }
    throw new Error(`unexpected ${path}`);
  });
  await button(app.host, '查看引用与清理').onclick();
  assert.match(textOf(app.host), /已查到 55 项引用/);
  assert.match(textOf(app.host), /<img src=x>/, 'reference labels are text, never HTML');
  assert.match(textOf(app.host), /固定提交：<img src=x>/, 'backend reference types are displayed in Chinese');
  assert.match(textOf(app.host), /下架 · ly · 2026-10-24T12:00:00\+08:00/, 'recent lifecycle history is shown as a folded record');
  assert.equal(button(app.host, '移入回收站').disabled, true, 'an incomplete lifecycle verification blocks a recycle-bin move');
  await button(app.host, '下一页引用').onclick();
  assert.match(calls.at(-1).path, /offset=50/, 'reference list is paged');
  await button(app.host, '下架').onclick();
  assert.equal(JSON.parse(calls.at(-1).options.body).action, 'retire');
  assert.match(textOf(app.host), /已下架/);
  assert.match(app.messages.at(-1), /仅从新的选择列表隐藏/);

  report = {...report, can_trash: true};
  await button(app.host, '刷新引用').onclick();
  assert.equal(button(app.host, '移入回收站').disabled, false, 'known references do not prevent reversible recycle-bin moves');
  assert.match(textOf(app.host), /永久清理时才会逐项核对依赖/);

  report = {...report, references: [], reference_count: 0, can_trash: false, offset: 0};
  await button(app.host, '刷新引用').onclick();
  assert.match(textOf(app.host), /存在未能完成的核对/);
  assert.equal(button(app.host, '移入回收站').disabled, true, 'unknown verification state also blocks recycle-bin moves');
  report = {...report, can_trash: true};
  await button(app.host, '刷新引用').onclick();
  assert.equal(button(app.host, '移入回收站').disabled, false);
  await button(app.host, '移入回收站').onclick();
  assert.equal(JSON.parse(calls.at(-1).options.body).action, 'trash');
  assert.match(textOf(app.host), /已移入回收站/);
  assert.match(app.messages.at(-1), /保留原文件和历史记录/);
  await button(app.host, '恢复').onclick();
  assert.match(textOf(app.host), /使用中/);

  let first;
  const late = load(path => {
    if (!first) return new Promise(resolve => { first = resolve; });
    return Promise.resolve({state: 'active', revision: 'new', reference_count: 0, references: [], notes: ['最新结果'], can_trash: true, offset: 0, limit: 50});
  });
  const oldRequest = button(late.host, '查看引用与清理').onclick();
  await flush();
  const newRequest = button(late.host, '查看引用与清理').onclick();
  await newRequest;
  first({state: 'retired', revision: 'old', reference_count: 1, references: [{type: '旧', label: '迟到响应'}], notes: [], can_trash: false, offset: 0, limit: 50});
  await oldRequest;
  assert.match(textOf(late.host), /最新结果/);
  assert.doesNotMatch(textOf(late.host), /迟到响应/, 'late reports do not overwrite the newer response');

  const failed = load(async () => { const error = new Error('服务拒绝'); error.status = 403; throw error; });
  await button(failed.host, '查看引用与清理').onclick();
  assert.match(failed.messages.at(-1), /无法读取数据集引用：服务拒绝/);

  const index = fs.readFileSync('core/researchkit/web_static/index.html', 'utf8');
  const appSource = fs.readFileSync('core/researchkit/web_static/app.js', 'utf8');
  assert.match(index, /id="component-access-include-inactive"/);
  assert.match(index, /id="data-management-include-inactive"/);
  const scripts = [...index.matchAll(/<script src="([^"]+)"><\/script>/g)].map(match => match[1]);
  for (const consumer of ['/static/data-manager.js', '/static/app.js']) {
    assert.ok(scripts.indexOf('/static/markdown.js') >= 0 && scripts.indexOf('/static/markdown.js') < scripts.indexOf(consumer), 'untrusted README renderer loads before every consumer');
    assert.ok(scripts.indexOf('/static/readme-export.js') >= 0 && scripts.indexOf('/static/readme-export.js') < scripts.indexOf(consumer), 'export helper loads before both dataset pages');
  }
  assert.match(appSource, /component-access\$\{componentAccessState\.includeInactive/);
  assert.match(appSource, /me\.role !== 'teacher'/, 'students do not receive a teacher navigation entry');
  process.stdout.write('web lifecycle UI tests passed\n');
})().catch(error => { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; });
