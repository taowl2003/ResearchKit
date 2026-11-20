(() => {
  'use strict';
  const body = document.querySelector('#composer-body');
  const message = document.querySelector('#composer-message');
  const projectTitle = document.querySelector('#composer-project');
  const token = sessionStorage.getItem('researchkit-web-token');
  const state = {projectId: null, revision: null, catalog: null, parameters: [], steps: [], draftContracts: [], entry: 'case.py', entryText: '', managed: true, generateEntry: false, preview: null, dirty: false, epoch: 0};

  const setText = (node, value) => { node.textContent = value == null ? '' : String(value); };
  const el = (tag, className = '') => { const node = document.createElement(tag); node.className = className; return node; };
  const appendText = (parent, tag, value, className = '') => { const node = el(tag, className); setText(node, value); parent.append(node); return node; };
  const button = (title, action) => { const node = el('button'); node.type = 'button'; setText(node, title); node.onclick = action; return node; };
  const label = (title, control) => { const node = el('label'); appendText(node, 'span', title); node.append(control); return node; };
  const input = (value = '', type = 'text') => { const node = document.createElement('input'); node.type = type; node.value = value == null ? '' : String(value); return node; };
  const textArea = value => { const node = document.createElement('textarea'); node.value = value == null ? '' : String(value); return node; };
  const select = (options, value) => { const node = document.createElement('select'); options.forEach(([key, title]) => { const option = document.createElement('option'); option.value = key; setText(option, title); option.selected = key === value; node.append(option); }); return node; };
  const note = (parent, value, className = 'composer-note') => appendText(parent, 'p', value, className);
  const api = async (path, options = {}) => {
    const headers = new Headers(options.headers || {});
    headers.set('Authorization', `Bearer ${token || ''}`);
    if (options.body) headers.set('Content-Type', 'application/json');
    const res = await fetch(path, {...options, headers, credentials: 'omit'});
    let response = {};
    try { response = await res.json(); } catch (_) { response = {}; }
    if (!res.ok) { const error = new Error(response.error || `HTTP ${res.status}`); error.status = res.status; throw error; }
    return response;
  };
  const getPlugin = ref => (state.catalog?.plugins || []).find(plugin => plugin.ref === ref);
  const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const typeOf = value => value === null ? 'any' : Array.isArray(value) ? 'sequence' : typeof value === 'object' ? 'mapping' : typeof value === 'number' ? 'number' : typeof value === 'boolean' ? 'boolean' : 'string';
  const complexType = type => type === 'mapping' || type === 'sequence' || type === 'any';
  const rawValue = (value, type) => complexType(type) ? JSON.stringify(value) : type === 'boolean' ? String(value) : value == null ? '' : String(value);
  const rawLiteral = value => JSON.stringify(value);
  const encodeValue = (value, type, description) => {
    if (type === 'number') { if (String(value).trim() === '') throw new Error(`${description} 不能为空。`); const number = Number(value); if (!Number.isFinite(number)) throw new Error(`${description} 必须是数字。`); return number; }
    if (type === 'integer') { if (String(value).trim() === '') throw new Error(`${description} 不能为空。`); const number = Number(value); if (!Number.isSafeInteger(number)) throw new Error(`${description} 必须是整数。`); return number; }
    if (type === 'boolean') { if (value === true || value === false) return value; if (value === 'true') return true; if (value === 'false') return false; throw new Error(`${description} 必须是 true 或 false。`); }
    if (type === 'mapping' || type === 'sequence' || type === 'any') { try { const parsed = JSON.parse(value); if (type === 'mapping' && (!parsed || Array.isArray(parsed) || typeof parsed !== 'object')) throw new Error(); if (type === 'sequence' && !Array.isArray(parsed)) throw new Error(); return parsed; } catch (_) { const expected = type === 'mapping' ? 'JSON 对象' : type === 'sequence' ? 'JSON 数组' : 'JSON 值'; throw new Error(`${description} 必须是有效的 ${expected}。`); } }
    return String(value);
  };
  const showError = error => setText(message, error.status === 409 ? `保存冲突：${error.message}。当前草稿仍保留，请重新读取后决定如何合并。` : error.message);
  const markDirty = () => {
    state.dirty = true; state.preview = null; state.epoch += 1;
    body.querySelector('[data-composer-save]')?.setAttribute('disabled', '');
    body.querySelector('[data-composer-preview]')?.remove();
    setText(message, '草稿已更改，请重新预览后再保存。');
  };
  const parsedRuleType = rule => rule?.type || rule?.kind || (typeof rule?.default === 'number' ? 'number' : typeof rule?.default === 'boolean' ? 'boolean' : 'string');
  const paramRule = (plugin, name) => plugin?.parameters?.[name] || {};
  const clone = value => JSON.parse(JSON.stringify(value));
  const normaliseInput = source => {
    if (!source || typeof source !== 'object') return source;
    if (source.kind === 'literal' && hasOwn(source, 'value')) return {kind: 'literal', raw: rawLiteral(source.value)};
    return clone(source);
  };
  const normalise = record => {
    state.revision = record.revision;
    state.entry = record.entry || 'case.py'; state.entryText = record.entry_text || '';
    state.managed = record.managed !== false; state.generateEntry = false;
    state.parameters = Object.entries(record.parameters || {}).map(([name, value]) => ({name, type: typeOf(value), raw: rawValue(value, typeOf(value))}));
    state.steps = (record.steps || []).map(step => {
      const plugin = getPlugin(step.ref), parameters = Object.create(null);
      Object.entries(step.parameters || {}).forEach(([name, value]) => { parameters[name] = rawValue(value, parsedRuleType(paramRule(plugin, name))); });
      const inputs = Object.create(null);
      Object.entries(step.inputs || {}).forEach(([port, source]) => { inputs[port] = normaliseInput(source); });
      return {alias: step.alias || '', ref: step.ref || '', parameters, inputs};
    });
    state.draftContracts = (record.draft_contracts || []).map(contract => ({id: contract.id || '', type: contract.type || 'number', description: contract.description || ''}));
    state.preview = null; state.dirty = false;
  };
  const payload = () => {
    const parameters = Object.create(null);
    state.parameters.forEach(parameter => {
      const name = parameter.name.trim();
      if (!name) throw new Error('项目参数名称不能为空。');
      if (hasOwn(parameters, name)) throw new Error(`项目参数 ${name} 重复。`);
      parameters[name] = encodeValue(parameter.raw, parameter.type, `项目参数 ${name}`);
    });
    const aliases = new Set();
    const steps = state.steps.map((step, index) => {
      const alias = step.alias.trim();
      if (!alias) throw new Error(`第 ${index + 1} 步缺少别名。`);
      if (aliases.has(alias)) throw new Error(`步骤别名 ${alias} 重复。`);
      aliases.add(alias);
      if (!step.ref) throw new Error(`步骤 ${alias} 尚未选择算法。`);
      const plugin = getPlugin(step.ref), parametersForStep = Object.create(null);
      Object.entries(plugin?.parameters || {}).forEach(([name, rule]) => {
        if (!hasOwn(step.parameters, name)) {
          if (rule.required && !hasOwn(rule, 'default')) throw new Error(`步骤 ${alias} 缺少必填参数 ${name}。`);
          return;
        }
        const value = step.parameters[name];
        parametersForStep[name] = encodeValue(value, parsedRuleType(rule), `步骤 ${alias} 的参数 ${name}`);
      });
      const inputs = Object.create(null);
      Object.entries(step.inputs || {}).forEach(([port, source]) => {
        if (!source?.kind) return;
        if (source.kind === 'literal') { try { inputs[port] = {kind: 'literal', value: JSON.parse(source.raw)}; } catch (_) { throw new Error(`步骤 ${alias} 的输入 ${port} 必须是有效 JSON。`); } }
        else if (source.kind === 'parameter') { if (!hasOwn(parameters, source.name)) throw new Error(`步骤 ${alias} 引用了不存在的项目参数 ${source.name}。`); inputs[port] = {kind: 'parameter', name: source.name}; }
        else if (source.kind === 'output') { const prior = state.steps.slice(0, index).find(candidate => candidate.alias === source.step); if (!prior || !getPlugin(prior.ref)?.outputs?.[source.port]) throw new Error(`步骤 ${alias} 只能引用更早步骤存在的输出。`); inputs[port] = {kind: 'output', step: source.step, port: source.port}; }
      });
      return {alias, ref: step.ref, parameters: parametersForStep, inputs};
    });
    return {expected_revision: state.revision, parameters, steps, draft_contracts: state.draftContracts.map(contract => ({id: contract.id.trim(), type: contract.type, description: contract.description.trim()})), generate_entry: state.generateEntry};
  };
  const fingerprint = data => JSON.stringify(data);
  const contractLabel = reference => { const row = (state.catalog?.contracts || []).find(contract => contract.id === reference); return row ? `${reference} · ${row.type}${row.description ? `（${row.description}）` : ''}` : reference; };
  const pluginSummary = (plugin, field) => Object.entries(plugin?.[field] || {}).map(([name, contract]) => `${name}: ${contractLabel(contract)}`).join('；') || '无';
  const defaultAlias = ref => {
    const base = String(ref || 'step').split('@', 1)[0].replace(/[^A-Za-z0-9_]/g, '_') || 'step';
    let result = base, count = 2;
    while (state.steps.some(step => step.alias === result)) result = `${base}_${count++}`;
    return result;
  };

  function renderProjectParameters(parent) {
    const panel = el('div', 'composer-panel'); appendText(panel, 'h2', '项目参数'); note(panel, '项目参数可作为任一步的输入来源。mapping 和 sequence 使用 JSON 输入。');
    state.parameters.forEach((parameter, index) => {
      const row = el('div', 'composer-grid composer-row');
      const name = input(parameter.name); name.oninput = () => { parameter.name = name.value; markDirty(); };
      name.onchange = () => render();
      const kind = select([['string', '文本'], ['number', '数字'], ['integer', '整数'], ['boolean', '布尔'], ['mapping', 'JSON 对象'], ['sequence', 'JSON 数组'], ['any', '任意 JSON']], parameter.type);
      kind.onchange = () => { parameter.type = kind.value; if (kind.value === 'boolean' && !['true', 'false'].includes(String(parameter.raw))) parameter.raw = 'false'; else if (complexType(kind.value) && parameter.raw === '') parameter.raw = 'null'; markDirty(); render(); };
      const value = parameter.type === 'boolean' ? select([['true', 'true'], ['false', 'false']], String(parameter.raw)) : (complexType(parameter.type) ? textArea(parameter.raw) : input(parameter.raw));
      value.oninput = value.onchange = () => { parameter.raw = value.value; markDirty(); };
      row.append(label('名称', name), label('类型', kind), label('值', value), button('删除', () => { state.parameters.splice(index, 1); markDirty(); render(); })); panel.append(row);
    });
    panel.append(button('添加项目参数', () => { state.parameters.push({name: '', type: 'string', raw: ''}); markDirty(); render(); })); parent.append(panel);
  }

  function inputEditor(parent, step, plugin, port, source, index) {
    if (!source) { source = {kind: 'literal', raw: 'null'}; step.inputs[port] = source; }
    const box = el('div', 'composer-input-source'); appendText(box, 'h4', `${port} · ${contractLabel(plugin?.inputs?.[port] || '未声明契约')}`);
    const sourceType = select([['literal', '常量 JSON'], ['parameter', '项目参数'], ['output', '前置输出']], source?.kind || 'literal');
    const setSource = next => { step.inputs[port] = next; markDirty(); render(); };
    sourceType.onchange = () => {
      if (sourceType.value === 'parameter') setSource({kind: 'parameter', name: state.parameters[0]?.name || ''});
      else if (sourceType.value === 'output') { const prior = state.steps.slice(0, index)[0], firstPort = prior && Object.keys(getPlugin(prior.ref)?.outputs || {})[0]; setSource({kind: 'output', step: prior?.alias || '', port: firstPort || ''}); }
      else setSource({kind: 'literal', raw: 'null'});
    };
    box.append(label('输入来源', sourceType));
    if ((source?.kind || 'literal') === 'literal') {
      const value = textArea(source.raw); value.oninput = () => { step.inputs[port] = {kind: 'literal', raw: value.value}; markDirty(); }; box.append(label('JSON 常量', value));
    } else if (source.kind === 'parameter') {
      const options = state.parameters.map(parameter => [parameter.name, parameter.name || '（未命名参数）']); const picker = select(options.length ? options : [['', '先添加项目参数']], source.name); picker.onchange = () => setSource({kind: 'parameter', name: picker.value}); box.append(label('项目参数', picker));
    } else {
      const choices = [];
      state.steps.slice(0, index).forEach(previous => Object.keys(getPlugin(previous.ref)?.outputs || {}).forEach(output => choices.push([`${previous.alias}\u0000${output}`, `${previous.alias}.${output} · ${contractLabel(getPlugin(previous.ref).outputs[output])}`])));
      const picker = select(choices.length ? choices : [['\u0000', '没有可引用的前置输出']], `${source.step}\u0000${source.port}`); picker.onchange = () => { const [stepName, output] = picker.value.split('\u0000'); setSource({kind: 'output', step: stepName, port: output}); }; box.append(label('前置输出', picker));
    }
    parent.append(box);
  }

  function renderSteps(parent) {
    const panel = el('div', 'composer-panel'); appendText(panel, 'h2', '步骤'); note(panel, '步骤从上到下执行；输入只能使用常量、项目参数或更早步骤的输出。');
    const plugins = (state.catalog?.plugins || []).map(plugin => [plugin.ref, `${plugin.name || plugin.ref} · ${plugin.version || plugin.ref}`]);
    if (!plugins.length) note(panel, '当前账号没有可编排的授权算法。', 'composer-warning');
    state.steps.forEach((step, index) => {
      const plugin = getPlugin(step.ref), card = el('article', 'composer-step'), header = document.createElement('header'); appendText(header, 'h3', `步骤 ${index + 1}`);
      const headerActions = el('div', 'composer-actions'); headerActions.append(button('上移', () => { if (index) { [state.steps[index - 1], state.steps[index]] = [state.steps[index], state.steps[index - 1]]; markDirty(); render(); } }), button('下移', () => { if (index + 1 < state.steps.length) { [state.steps[index + 1], state.steps[index]] = [state.steps[index], state.steps[index + 1]]; markDirty(); render(); } }), button('删除', () => { state.steps.splice(index, 1); markDirty(); render(); })); header.append(headerActions); card.append(header);
      const grid = el('div', 'composer-grid'), ref = select([['', '选择精确算法版本'], ...plugins], step.ref), alias = input(step.alias); ref.onchange = () => { step.ref = ref.value; step.parameters = Object.create(null); step.inputs = Object.create(null); if (!step.alias) step.alias = defaultAlias(step.ref); markDirty(); render(); }; alias.oninput = () => { step.alias = alias.value; markDirty(); }; grid.append(label('精确算法版本', ref), label('步骤别名', alias)); card.append(grid);
      if (plugin) {
        appendText(card, 'p', `输入端口：${pluginSummary(plugin, 'inputs')}`, 'composer-contract'); appendText(card, 'p', `输出端口：${pluginSummary(plugin, 'outputs')}`, 'composer-contract');
        const params = Object.entries(plugin.parameters || {}); if (params.length) { appendText(card, 'h4', '算法参数', 'composer-subtitle'); const parameterGrid = el('div', 'composer-grid'); params.forEach(([name, rule]) => { const type = parsedRuleType(rule), complex = complexType(type), configured = hasOwn(step.parameters, name), current = configured ? step.parameters[name] : '', defaultHint = hasOwn(rule, 'default') ? ` · 默认 ${JSON.stringify(rule.default)}` : '', booleanOptions = rule.required ? [['', hasOwn(rule, 'default') ? `使用默认 ${JSON.stringify(rule.default)}` : '请选择'], ['true', 'true'], ['false', 'false']] : [['', '未设置'], ['true', 'true'], ['false', 'false']], control = type === 'boolean' ? select(booleanOptions, String(current)) : (complex ? textArea(current) : input(current)); if (!configured && hasOwn(rule, 'default') && type !== 'boolean') control.placeholder = complex ? JSON.stringify(rule.default) : String(rule.default); control.oninput = control.onchange = () => { if (type === 'boolean' && control.value === '') delete step.parameters[name]; else step.parameters[name] = control.value; markDirty(); }; const item = el('div'); item.append(label(`${name}${rule.required ? '（必填）' : '（可选）'} · ${type}${defaultHint}`, control)); if (!rule.required && configured) item.append(button('恢复未设置', () => { delete step.parameters[name]; markDirty(); render(); })); parameterGrid.append(item); }); card.append(parameterGrid); }
        const ports = Object.keys(plugin.inputs || {}); if (ports.length) { appendText(card, 'h4', '输入绑定', 'composer-subtitle'); ports.forEach(port => inputEditor(card, step, plugin, port, step.inputs[port], index)); }
      }
      panel.append(card);
    });
    panel.append(button('添加步骤', () => { state.steps.push({alias: '', ref: '', parameters: Object.create(null), inputs: Object.create(null)}); markDirty(); render(); })); parent.append(panel);
  }

  function renderContracts(parent) {
    const panel = el('div', 'composer-panel'); appendText(panel, 'h2', '草稿契约'); note(panel, '草稿只写入项目编排，尚未发布到共享契约库。');
    state.draftContracts.forEach((contract, index) => { const row = el('div', 'composer-grid composer-row'), id = input(contract.id), type = input(contract.type), description = input(contract.description); id.placeholder = 'name@1.0.0'; id.oninput = () => { contract.id = id.value; markDirty(); }; type.oninput = () => { contract.type = type.value; markDirty(); }; description.oninput = () => { contract.description = description.value; markDirty(); }; row.append(label('ID', id), label('类型', type), label('说明', description), button('删除', () => { state.draftContracts.splice(index, 1); markDirty(); render(); })); panel.append(row); });
    panel.append(button('添加草稿契约', () => { state.draftContracts.push({id: '', type: 'number', description: ''}); markDirty(); render(); })); parent.append(panel);
  }

  function renderPreview(parent) {
    const panel = el('div', 'composer-panel'); appendText(panel, 'h2', '预览与保存');
    const entryCheckbox = input('', 'checkbox'); entryCheckbox.checked = state.generateEntry; entryCheckbox.onchange = () => { state.generateEntry = entryCheckbox.checked; markDirty(); render(); }; const entryLabel = label('生成 case.py 流程骨架', entryCheckbox); entryLabel.classList.add('composer-checkbox'); panel.append(entryLabel); note(panel, '选中后会更新 case.py；未选时保留现有流程。先预览将改动，再明确保存。');
    const actions = el('div', 'composer-actions'), reload = button('重新读取项目', reloadProject), preview = button('预览项目配置', previewProject), save = button('保存编排', saveProject); save.dataset.composerSave = ''; save.disabled = !state.preview; actions.append(reload, preview, save); panel.append(actions);
    if (state.preview) { const previewContent = el('div'); previewContent.dataset.composerPreview = ''; appendText(previewContent, 'h3', '将修改的文件'); const changed = el('ul'); (state.preview.changed_paths || []).forEach(path => appendText(changed, 'li', path)); if (!changed.childElementCount) appendText(changed, 'li', '服务端未报告文件改动。'); previewContent.append(changed); appendText(previewContent, 'h3', 'project.yaml 预览'); appendText(previewContent, 'pre', state.preview.project_yaml || '', 'composer-preview'); if (state.generateEntry) { appendText(previewContent, 'h3', 'case.py 骨架预览'); appendText(previewContent, 'pre', state.preview.entry_text || '', 'composer-preview'); } panel.append(previewContent); }
    parent.append(panel);
  }

  function render() {
    body.replaceChildren();
    if (!state.projectId) { appendText(body, 'p', '请从“我的项目”卡片中选择“编排项目”。', 'composer-empty'); return; }
    if (!state.managed) note(body, '这是尚未由 Web 编排管理的项目。空项目可从这里开始；若项目已有 IDE 配置的算法，服务端会拒绝自动转换并保留现有文件。', 'composer-warning');
    renderProjectParameters(body); renderSteps(body); renderContracts(body); renderPreview(body);
  }
  async function previewProject() {
    const projectId = state.projectId, epoch = state.epoch;
    try {
      setText(message, '正在生成预览…');
      const data = payload(), result = await api(`/api/projects/${encodeURIComponent(projectId)}/composition/preview`, {method: 'POST', body: JSON.stringify(data)});
      if (state.projectId !== projectId || state.epoch !== epoch) return;
      state.preview = result; state.preview.fingerprint = fingerprint(data);
      setText(message, '预览已生成。确认项目配置、case.py（如已选择）和文件清单后，再保存。'); render();
    } catch (error) { if (state.projectId === projectId && state.epoch === epoch) showError(error); }
  }
  async function saveProject() {
    const projectId = state.projectId, epoch = state.epoch;
    try {
      const data = payload();
      if (!state.preview || state.preview.fingerprint !== fingerprint(data)) throw new Error('请先为当前草稿生成预览，再保存。');
      setText(message, '正在保存编排…');
      const result = await api(`/api/projects/${encodeURIComponent(projectId)}/composition`, {method: 'PUT', body: JSON.stringify(data)});
      document.dispatchEvent(new CustomEvent('researchkit:projects-changed'));
      if (state.projectId !== projectId || state.epoch !== epoch) {
        if (state.projectId === projectId) setText(message, '先前预览对应的编排已保存；本地草稿在保存期间已改变，请重新读取后继续。');
        return;
      }
      normalise(result); setText(message, '编排已保存到项目。可回到项目列表继续编辑或提交固定版本。'); render();
    } catch (error) { if (state.projectId === projectId && state.epoch === epoch) showError(error); }
  }
  async function loadProject(projectId, algorithmRef) {
    const epoch = state.epoch + 1; state.epoch = epoch;
    try {
      setText(message, '正在读取项目编排…');
      const [catalog, record] = await Promise.all([api('/api/catalog'), api(`/api/projects/${encodeURIComponent(projectId)}/composition`)]);
      if (state.epoch !== epoch) return;
      state.catalog = catalog; state.projectId = projectId; normalise(record);
      if (algorithmRef && !state.steps.some(step => step.ref === algorithmRef)) {
        const plugin = getPlugin(algorithmRef); if (!plugin) throw new Error('这个算法版本当前不可用，请刷新算法库。');
        let alias = defaultAlias(algorithmRef), count = 2; const existing = new Set(state.steps.map(step => step.alias));
        const base = alias; while (existing.has(alias)) alias = base + count++;
        const parameters = Object.create(null); Object.entries(plugin.parameters || {}).forEach(([name, rule]) => { if (hasOwn(rule, 'default')) parameters[name] = rawValue(rule.default, parsedRuleType(rule)); });
        state.steps.push({alias, ref: algorithmRef, parameters, inputs:Object.create(null)}); state.dirty = true;
      }
      setText(projectTitle, `项目：${projectId} · 算法配置与编排`); setText(message, '选择精确算法版本并配置输入。'); render();
    } catch (error) { if (state.epoch === epoch) showError(error); }
  }
  async function open(projectId, algorithmRef) {
    if (!projectId) { show(); return; }
    if (state.projectId === projectId && state.dirty) { setText(message, '当前项目有未保存草稿，已保留。需要放弃草稿时，请使用“重新读取项目”。'); return; }
    if (state.projectId && state.projectId !== projectId && state.dirty && !confirm('当前编排草稿尚未保存。确定放弃草稿并切换项目吗？')) return;
    await loadProject(projectId, algorithmRef);
  }
  async function reloadProject() {
    if (!state.projectId) return;
    if (state.dirty && !confirm('重新读取会放弃当前编排草稿。确定继续吗？')) return;
    await loadProject(state.projectId);
  }
  function show() { if (!state.projectId) render(); }
  document.querySelector('#composer-back').onclick = () => { if (state.dirty && !confirm('当前编排草稿尚未保存。返回项目列表吗？')) return; document.dispatchEvent(new CustomEvent('researchkit:show-projects')); };
  if (window.__researchkitComposerTest) window.__researchkitComposerTest = {state, rawValue, normaliseInput, normalise, payload, markDirty, open, previewProject, saveProject};
  window.ResearchKitComposer = {open, show};
})();
