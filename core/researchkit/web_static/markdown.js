(() => {
  'use strict';

  // README files are untrusted input.  This deliberately builds every node
  // with textContent instead of assigning HTML, so a dataset description can
  // never turn into executable markup in the local Web application.
  const SAFE_LINK = /^(https?:\/\/[^\s]+|#[^\s#]+)$/i;
  const plain = value => String(value ?? '').replace(/\r\n?/g, '\n');
  const el = (tag, className = '') => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  };
  const text = (node, value) => { node.textContent = String(value ?? ''); };

  function appendInline(parent, source) {
    const value = plain(source);
    // A small CommonMark-shaped inline parser.  Its recognized syntax is
    // intentionally limited; unrecognized text remains literal text.
    const token = /(\[[^\]\n]{1,500}\]\([^\s)]+\)|`[^`\n]{0,2000}`|\*\*[^*\n]{1,2000}\*\*|__[^_\n]{1,2000}__|\*[^*\n]{1,2000}\*|_[^_\n]{1,2000}_)/g;
    let offset = 0;
    for (const match of value.matchAll(token)) {
      if (match.index > offset) parent.append(document.createTextNode(value.slice(offset, match.index)));
      const raw = match[0];
      const link = raw.match(/^\[([^\]\n]{1,500})\]\(([^\s)]+)\)$/);
      if (link) {
        const href = link[2];
        if (SAFE_LINK.test(href)) {
          const a = el('a'); a.href = href; a.rel = 'noopener noreferrer';
          if (/^https?:/i.test(href)) { a.target = '_blank'; }
          text(a, link[1]); parent.append(a);
        } else {
          parent.append(document.createTextNode(`${link[1]}（链接未显示：只允许 http(s) 或本页 #锚点）`));
        }
      } else if (raw.startsWith('`')) {
        const code = el('code'); text(code, raw.slice(1, -1)); parent.append(code);
      } else if (raw.startsWith('**') || raw.startsWith('__')) {
        const strong = el('strong'); appendInline(strong, raw.slice(2, -2)); parent.append(strong);
      } else {
        const em = el('em'); appendInline(em, raw.slice(1, -1)); parent.append(em);
      }
      offset = match.index + raw.length;
    }
    if (offset < value.length) parent.append(document.createTextNode(value.slice(offset)));
  }

  function cells(line) {
    let value = String(line || '').trim();
    if (value.startsWith('|')) value = value.slice(1);
    if (value.endsWith('|')) value = value.slice(0, -1);
    return value.split('|').map(item => item.trim());
  }
  function isTableRule(line) {
    const values = cells(line);
    return values.length && values.every(cell => /^:?-{3,}:?$/.test(cell));
  }
  function isFence(line) { return /^\s*```/.test(line); }
  function isList(line) { return /^\s*(?:[-+*]|\d+[.)])\s+/.test(line); }
  function isBlockStart(line, next) {
    return !line.trim() || /^#{1,6}\s+/.test(line) || /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)
      || isFence(line) || /^\s*>\s?/.test(line) || isList(line)
      || (line.includes('|') && isTableRule(next || ''));
  }
  function appendTable(parent, header, rule, body) {
    const table = el('table', 'markdown-table');
    const thead = el('thead'), headRow = el('tr');
    cells(header).forEach(value => { const cell = el('th'); appendInline(cell, value); headRow.append(cell); });
    thead.append(headRow); table.append(thead);
    const tbody = el('tbody');
    body.forEach(line => {
      const row = el('tr'); cells(line).forEach(value => { const cell = el('td'); appendInline(cell, value); row.append(cell); }); tbody.append(row);
    });
    table.append(tbody); parent.append(table);
  }
  function appendBlocks(parent, source, depth = 0) {
    const lines = plain(source).split('\n');
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }
      const heading = line.match(/^\s*(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        const node = el(`h${heading[1].length}`);
        node.id = heading[2].toLowerCase().replace(/[^\p{L}\p{N}_\s-]/gu, '').trim().replace(/\s+/g, '-');
        appendInline(node, heading[2]); parent.append(node); index += 1; continue;
      }
      if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { parent.append(el('hr')); index += 1; continue; }
      if (isFence(line)) {
        const language = line.replace(/^\s*```\s*/, '').trim(); const chunks = []; index += 1;
        while (index < lines.length && !isFence(lines[index])) { chunks.push(lines[index]); index += 1; }
        if (index < lines.length) index += 1;
        const pre = el('pre', 'markdown-code'); const code = el('code'); if (language) code.dataset.language = language;
        text(code, chunks.join('\n')); pre.append(code); parent.append(pre); continue;
      }
      if (line.includes('|') && isTableRule(lines[index + 1] || '')) {
        const header = line; index += 2; const body = [];
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) { body.push(lines[index]); index += 1; }
        appendTable(parent, header, null, body); continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const chunks = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) { chunks.push(lines[index].replace(/^\s*>\s?/, '')); index += 1; }
        const quote = el('blockquote');
        // A malformed README can have thousands of nested quote prefixes.
        // Once nesting gets deep, show the remaining text instead of using an
        // unbounded recursive call on untrusted input.
        if (depth >= 16) text(quote, chunks.join('\n'));
        else appendBlocks(quote, chunks.join('\n'), depth + 1);
        parent.append(quote); continue;
      }
      if (isList(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line); const list = el(ordered ? 'ol' : 'ul');
        const marker = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-+*]\s+/;
        while (index < lines.length && isList(lines[index]) && /^\s*\d+[.)]\s+/.test(lines[index]) === ordered) {
          const item = el('li'); appendInline(item, lines[index].replace(marker, '')); list.append(item); index += 1;
        }
        parent.append(list); continue;
      }
      const chunks = [line]; index += 1;
      while (index < lines.length && !isBlockStart(lines[index], lines[index + 1])) { chunks.push(lines[index]); index += 1; }
      const paragraph = el('p'); appendInline(paragraph, chunks.join('\n')); parent.append(paragraph);
    }
  }
  function append(parent, source, options = {}) {
    const host = el(options.tag || 'section', options.className || 'markdown-content');
    const value = plain(source);
    if (!value.trim()) { text(host, options.empty || '暂无说明。'); }
    else appendBlocks(host, value);
    parent.append(host);
    return host;
  }
  function appendReadme(parent, info) {
    const host = el('section', 'readme-full');
    append(host, info?.markdown || '', {className: 'dataset-readme', empty: '暂无说明。'});
    if (info?.complete === false) {
      const note = el('p', 'data-management-warning'); text(note, info.notice || '请由教师刷新扫描以补齐完整说明。'); host.append(note);
    }
    const original = el('details', 'readme-source'), summary = el('summary'); text(summary, info?.original !== undefined ? '查看 README 原文（核对用）' : '查看正文原文（核对用）');
    const source = el('textarea'); source.readOnly = true; source.value = info?.original ?? info?.markdown ?? ''; source.rows = 10;
    source.setAttribute?.('aria-label', 'README 正文原文'); original.append(summary, source); host.append(original);
    parent.append(host); return host;
  }
  window.ResearchKitMarkdown = {append, appendInline, appendReadme, _test: {SAFE_LINK, appendBlocks}};
})();
