'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function node(tagName = '') {
  return {
    tagName, textContent: '', children: [], className: '', dataset: {}, href: '', rel: '', target: '',
    append(...items) { this.children.push(...items); },
  };
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children || []) { const hit = find(child, predicate); if (hit) return hit; }
  return null;
}
function allText(root) { return [root.textContent || '', ...(root.children || []).map(allText)].join('\n'); }

const context = {
  window: {},
  document: {createElement: tag => node(tag), createTextNode: value => { const valueNode = node('#text'); valueNode.textContent = String(value); return valueNode; }},
};
vm.runInNewContext(fs.readFileSync('core/researchkit/web_static/markdown.js', 'utf8'), context, {filename: 'markdown.js'});

const host = node('section');
context.window.ResearchKitMarkdown.append(host, [
  '# 降雨说明', '',
  '这是 **重点**，包含 `rainfall.csv` 和 [安全链接](https://example.test/a)。', '',
  '- 日期列', '- 降雨列', '',
  '| 文件 | 用途 |', '| --- | --- |', '| rainfall.csv | 原始记录 |', '',
  '```python', 'print("只显示，不执行")', '```', '',
  '<img src=x onerror=alert(1)> [危险](javascript:alert(1))',
].join('\n'));

const content = host.children[0];
assert.equal(content.className, 'markdown-content');
assert.ok(find(content, item => item.tagName === 'h1'), 'headings render as semantic elements');
assert.ok(find(content, item => item.tagName === 'ul'), 'lists render as semantic elements');
assert.ok(find(content, item => item.tagName === 'table'), 'README tables render as tables');
assert.ok(find(content, item => item.tagName === 'pre'), 'fenced code renders as inert preformatted text');
const link = find(content, item => item.tagName === 'a');
assert.equal(link.href, 'https://example.test/a');
assert.equal(link.target, '_blank');
assert.equal(link.rel, 'noopener noreferrer');
assert.match(allText(content), /<img src=x onerror=alert\(1\)>/, 'HTML remains literal text');
assert.match(allText(content), /链接未显示：只允许 http\(s\) 或本页 #锚点/, 'unsafe protocols never become links');
assert.equal(find(content, item => item.innerHTML !== undefined), null, 'renderer never assigns HTML strings');

const hostile = node('section');
const deepQuote = `${'>'.repeat(10000)} no stack overflow`;
assert.doesNotThrow(() => context.window.ResearchKitMarkdown.append(hostile, deepQuote), 'deep quote prefixes cannot exhaust the call stack');
assert.doesNotThrow(() => context.window.ResearchKitMarkdown.append(hostile, `\`\`\`\n${'x'.repeat(32 * 1024)}`), 'an unterminated long code fence remains inert text');
assert.doesNotThrow(() => context.window.ResearchKitMarkdown.append(hostile, ''), 'an empty README has a stable display');
const readme = node('section');
const original = '\ufeff---\ntags: [教学]\n---\n# 说明\n</textarea><script>bad()</script>';
context.window.ResearchKitMarkdown.appendReadme(readme, {markdown:'# 说明\n</textarea><script>bad()</script>', original, complete:true});
const sourceDetails = find(readme, item => item.tagName === 'details');
assert.equal(Boolean(sourceDetails.open), false, 'raw source stays collapsed');
assert.equal(find(sourceDetails, item => item.tagName === 'textarea').value, original, 'exact raw README is available for reference');
assert.equal(find(readme, item => item.tagName === 'script'), null, 'full README and source do not create executable HTML');
console.log('markdown UI passed: headings, list, table, code, text-only HTML, safe links');
