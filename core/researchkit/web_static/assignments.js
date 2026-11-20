(() => {
  'use strict';

  const state = {api: null, post: null, getMe: null, catalog: null, datasets: [], preview: null,
    requestEpoch: 0, draftEpoch: 0, previewing: false, dispatching: false, payloadKey: null, requestId: null,
    parameterInputs: [], previewCompositionText: null};
  const byId = id => document.querySelector(id);
  const setText = (node, value) => { if (node) node.textContent = String(value || ''); };
  const element = (tag, value, className = '') => {
    const node = document.createElement(tag); if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value); return node;
  };
  const appendText = (node, tag, value, className = '') => { const child = element(tag, value, className); node.append(child); return child; };
  const currentTeacher = () => state.getMe?.()?.role === 'teacher';
  const message = value => setText(byId('#assignment-message'), value);
  const requestId = () => {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, marker => { const value = Math.floor(Math.random() * 16); return (marker === 'x' ? value : (value & 3) | 8).toString(16); });
  };
  const accountNames = value => [...new Set(String(value || '').split(/[\s,;，；]+/).map(name => name.trim()).filter(Boolean))];
  const datasetRows = value => Array.isArray(value) ? value : (Array.isArray(value?.items) ? value.items : []);
  const selectedDatasetIds = () => {
    const host = byId('#assignment-datasets');
    if (!host?.querySelectorAll) return [];
    return [...host.querySelectorAll('input[type="checkbox"]')].filter(box => box.checked).map(box => box.value);
  };
  const formValues = () => ({
    name: byId('#assignment-name')?.value?.trim() || '',
    template_ref: byId('#assignment-template')?.value || '', dataset_ids: selectedDatasetIds(),
    targets: accountNames(byId('#assignment-targets')?.value), instructions: byId('#assignment-instructions')?.value?.trim() || '',
  });
  const clearPreview = () => {
    state.draftEpoch += 1;
    state.preview = null;
    state.previewCompositionText = null;
    const panel = byId('#assignment-preview-panel'); if (panel) panel.hidden = true;
    const dispatch = byId('#assignment-dispatch'); if (dispatch) dispatch.disabled = true;
  };
  const compositionForRequest = () => {
    const field = byId('#assignment-composition');
    if (!field?.value?.trim()) return {value: state.preview?.composition ?? null, manuallyEdited: false};
    try {
      return {value: JSON.parse(field.value), manuallyEdited: field.value !== state.previewCompositionText};
    } catch (_) { throw new Error('高级任务编排必须是有效 JSON。'); }
  };
  const payload = (withComposition = false) => {
    const result = formValues();
    if (!result.name) throw new Error('请填写任务名称。');
    if (!result.template_ref) throw new Error('请选择案例。');
    if (!result.targets.length) throw new Error('请至少填写一个领取账号。');
    if (withComposition) {
      const composition = compositionForRequest();
      result.composition = composition.value;
      const overrides = {};
      state.parameterInputs.forEach(({alias, key, field, managed}) => {
        let value; try { value = JSON.parse(field.value); } catch (_) { throw new Error(`算法 ${alias} 的 ${key} 参数需要有效 JSON 值，例如 3 或 "文字"。`); }
        // Once the advanced JSON has been changed, it is the explicit source of
        // truth.  The old convenience controls must never overwrite it.
        if (managed) {
          if (result.composition && !composition.manuallyEdited) {
            const step = result.composition.steps.find(row => row.alias === alias);
            if (step) { step.parameters ||= {}; step.parameters[key] = value; }
          }
        } else { overrides[alias] ||= {}; overrides[alias][key] = value; }
      });
      if (Object.keys(overrides).length) result.algorithm_parameters = overrides;
    } else result.composition = null;
    const key = JSON.stringify(result);
    if (state.payloadKey !== key) { state.payloadKey = key; state.requestId = requestId(); }
    return {request_id: state.requestId, ...result};
  };
  const activeDatasets = rows => {
    const unique = new Map();
    rows.forEach(row => { if (row && typeof row.dataset_id === 'string' && row.dataset_id) unique.set(row.dataset_id, row); });
    return [...unique.values()].sort((left, right) => String(left.name || left.dataset_id).localeCompare(String(right.name || right.dataset_id), 'zh-CN'));
  };
  function renderDatasets() {
    const host = byId('#assignment-datasets'), note = byId('#assignment-datasets-note');
    if (!host) return;
    host.replaceChildren(); const rows = activeDatasets(state.datasets);
    if (!rows.length) { setText(note, '当前没有可分发的数据集。可先在“数据集管理”审核登记，或不选择数据集继续布置。'); return; }
    setText(note, `可选择 ${rows.length} 个已授权数据集；选择完整数据集不会复制原始文件。`);
    rows.forEach(row => {
      const label = element('label', undefined, 'assignment-dataset');
      const box = document.createElement('input'); box.type = 'checkbox'; box.value = row.dataset_id;
      const name = row.name || row.dataset_id;
      label.append(box, element('span', `${name} · ${row.dataset_id}`));
      if (row.description) label.append(element('small', row.description));
      host.append(label);
    });
  }
  function renderTemplateOptions() {
    const select = byId('#assignment-template'); if (!select) return;
    const selected = select.value;
    select.replaceChildren(); const blank = element('option', '选择精确案例版本'); blank.value = ''; select.append(blank);
    (state.catalog?.templates || []).forEach(template => {
      const option = element('option', `${template.name || template.ref} · ${template.version || template.ref}`);
      option.value = template.ref; select.append(option);
    });
    select.value = selected;
  }
  function pluginFor(ref) {
    return (state.catalog?.plugins || []).find(row => row.ref === ref) || null;
  }
  function compositionText(value) { return JSON.stringify(value, null, 2); }
  function currentManagedComposition() {
    const field = byId('#assignment-composition');
    if (field?.value?.trim()) {
      try { return JSON.parse(field.value); } catch (_) { throw new Error('高级任务编排必须是有效 JSON。'); }
    }
    if (state.preview?.composition) return JSON.parse(JSON.stringify(state.preview.composition));
    throw new Error('请先选择一个可编排案例并生成预览。');
  }
  function appendAlgorithmAdder(host, preview) {
    if (!preview?.composition || !Array.isArray(preview.composition.steps)) return;
    const panel = element('section', undefined, 'assignment-add-algorithm'); appendText(panel, 'h3', '添加算法步骤');
    appendText(panel, 'p', '请选择已发布的精确算法版本，并明确填写输入绑定。系统不会猜测科学计算步骤之间如何连接。', 'asset-note');
    const plugins = state.catalog?.plugins || [];
    if (!plugins.length) { appendText(panel, 'p', '当前没有可加入的已授权算法。'); host.append(panel); return; }
    const skeletonLabel = element('label', undefined, 'inline-check'), skeleton = document.createElement('input');
    skeleton.type = 'checkbox'; skeleton.checked = Boolean(preview.composition.generate_entry);
    skeleton.onchange = () => {
      try {
        const composition = currentManagedComposition(); composition.generate_entry = skeleton.checked;
        const field = byId('#assignment-composition'); if (field) field.value = compositionText(composition);
        message(skeleton.checked ? '分发时将重新生成 case.py，执行编排步骤；原入口会被替换。' : '保留原 case.py；请确认入口本身调用了所需算法。');
      } catch (error) { message(error.message); }
    };
    skeletonLabel.append(skeleton, element('span', '按本次编排重新生成 case.py（会替换原入口，包含新增步骤）'));
    panel.append(skeletonLabel);
    appendText(panel, 'p', '例如将 scale 的输出连接为输入：{"value":{"kind":"output","step":"scale","port":"value"}}。保留原入口时，新增绑定不会自动插入手写逻辑。', 'asset-note');
    const ref = document.createElement('select');
    const blank = element('option', '选择算法版本'); blank.value = ''; ref.append(blank);
    plugins.forEach(plugin => { const option = element('option', `${plugin.name || plugin.ref} · ${plugin.version || plugin.ref}`); option.value = plugin.ref; ref.append(option); });
    const alias = document.createElement('input'); alias.placeholder = '步骤别名，例如 scale_2';
    const inputs = document.createElement('textarea'); inputs.value = '{}'; inputs.className = 'assignment-inline-json';
    const parameterHost = element('div', undefined, 'assignment-add-parameters');
    const parameterFields = [];
    const renderParameters = () => {
      parameterHost.replaceChildren(); parameterFields.splice(0);
      const plugin = pluginFor(ref.value); if (!plugin) return;
      const entries = Object.entries(plugin.parameters || {});
      if (!entries.length) { appendText(parameterHost, 'p', '此算法没有可配置参数。', 'asset-note'); return; }
      appendText(parameterHost, 'h4', '算法参数');
      entries.forEach(([key, rule]) => {
        const label = element('label', `${key}${rule.required ? '（必填）' : '（可选）'}`), field = document.createElement('input');
        if (Object.prototype.hasOwnProperty.call(rule, 'default')) field.value = JSON.stringify(rule.default);
        else field.placeholder = rule.required ? '请输入 JSON 值' : '不设置则省略';
        label.append(field); parameterHost.append(label); parameterFields.push({key, rule, field});
      });
    };
    ref.onchange = () => { if (!alias.value) alias.value = String(ref.value || '').split('@')[0].replace(/[^A-Za-z0-9_]/g, '_'); renderParameters(); };
    const add = element('button', '加入任务编排'); add.type = 'button';
    add.onclick = () => {
      try {
        if (!ref.value) throw new Error('请选择要加入的算法。');
        const stepAlias = alias.value.trim(); if (!stepAlias) throw new Error('请填写步骤别名。');
        let boundInputs; try { boundInputs = JSON.parse(inputs.value); } catch (_) { throw new Error('输入绑定必须是有效 JSON 对象。'); }
        if (!boundInputs || Array.isArray(boundInputs) || typeof boundInputs !== 'object') throw new Error('输入绑定必须是 JSON 对象。');
        const parameters = {};
        parameterFields.forEach(({key, rule, field}) => {
          if (!field.value.trim()) { if (rule.required && !Object.prototype.hasOwnProperty.call(rule, 'default')) throw new Error(`算法参数 ${key} 必须填写。`); return; }
          try { parameters[key] = JSON.parse(field.value); } catch (_) { throw new Error(`算法参数 ${key} 必须是有效 JSON 值。`); }
        });
        const composition = currentManagedComposition();
        if (!Array.isArray(composition.steps)) throw new Error('该案例不是可编排案例；领取后可在“编排项目”中添加算法。');
        if (composition.steps.some(step => step.alias === stepAlias)) throw new Error(`步骤别名 ${stepAlias} 已存在。`);
        composition.steps.push({alias: stepAlias, ref: ref.value, parameters, inputs: boundInputs});
        state.preview.composition = composition; state.previewCompositionText = compositionText(composition);
        const field = byId('#assignment-composition'); if (field) field.value = state.previewCompositionText;
        renderCompositionPreview(state.preview); message('已加入任务草稿。确认分发时服务端会再次核验算法、契约和输入绑定。');
      } catch (error) { message(error.message); }
    };
    const refLabel = element('label', '算法版本'); refLabel.append(ref); panel.append(refLabel);
    const aliasLabel = element('label', '步骤别名'); aliasLabel.append(alias); panel.append(aliasLabel);
    const inputLabel = element('label', '输入绑定 JSON（必须明确填写）'); inputLabel.append(inputs); panel.append(inputLabel, parameterHost, add); host.append(panel);
  }
  function renderCompositionPreview(preview) {
    const host = byId('#assignment-preview-content'); if (!host) return;
    host.replaceChildren();
    appendText(host, 'p', `案例：${preview.template_ref || byId('#assignment-template')?.value || '未返回'}`);
    const steps = preview?.composition?.steps;
    if (Array.isArray(steps) && steps.length) {
      appendText(host, 'h3', '案例已有算法与参数');
      const list = element('ul');
      steps.forEach((step, index) => list.append(element('li', `${index + 1}. ${step.alias || '未命名步骤'} · ${step.ref || '未知算法'} · 参数 ${JSON.stringify(step.parameters || {})}`)));
      host.append(list);
    } else appendText(host, 'p', '本案例保留原有入口代码和算法绑定。领取后也可在“编排项目”中继续修改。');
    state.parameterInputs = [];
    const bindings = Array.isArray(steps) && steps.length
      ? steps.map(step => ({alias: step.alias, parameters: step.parameters || {}, managed: true, ref: step.ref}))
      : Object.entries(preview.algorithm_bindings || {}).map(([alias, row]) => ({alias, parameters: row.parameters || {}, ref: row.ref || '', managed: false}));
    if (bindings.length) {
      appendText(host, 'h3', '调整本次任务的算法参数');
      bindings.forEach(binding => {
        appendText(host, 'p', `${binding.alias}${binding.ref ? ' · ' + binding.ref : ''}`);
        Object.entries(binding.parameters).forEach(([key, value]) => {
          const label = element('label', `${binding.alias} / ${key}`), field = document.createElement('input');
          field.value = JSON.stringify(value); label.append(field); host.append(label);
          state.parameterInputs.push({alias: binding.alias, key, field, managed: binding.managed});
        });
      });
      appendText(host, 'p', '填写数字时无需引号，例如 3。新算法和步骤连接可在下方高级编排中明确填写，或领取后在“编排项目”操作。');
    }
    appendAlgorithmAdder(host, preview);
    const refs = preview.data_references || preview.dataset_ids || [];
    appendText(host, 'p', `数据集：${Array.isArray(refs) && refs.length ? refs.map(row => typeof row === 'string' ? row : row.dataset_id || '未知').join('、') : '未选择'}`);
    const targets = preview.targets || formValues().targets;
    appendText(host, 'p', `领取账号：${Array.isArray(targets) && targets.length ? targets.map(row => typeof row === 'string' ? row : row.username).join('、') : '未返回'}`);
    if (preview.warnings?.length) preview.warnings.forEach(value => appendText(host, 'p', value, 'assignment-warning'));
  }
  function renderResources() {
    const host = byId('#resource-library-list'); if (!host) return;
    host.replaceChildren();
    const catalog = state.catalog || {};
    const sections = [
      ['案例', catalog.templates || [], row => row.ref],
      ['算法', catalog.plugins || [], row => row.ref],
      ['契约', catalog.contracts || [], row => row.id || row.ref],
    ];
    let count = 0;
    sections.forEach(([title, rows, identity]) => {
      const card = element('article', undefined, 'card'); appendText(card, 'h3', title);
      if (!rows.length) appendText(card, 'p', '当前没有可用版本。');
      rows.forEach(row => { count += 1; appendText(card, 'p', `${row.name || identity(row)} · ${identity(row)}`, 'path'); });
      host.append(card);
    });
    if (!count) appendText(host, 'p', '正在读取统一资源库，或当前账号尚无可用资源。');
  }
  function renderStorage(value) {
    const host = byId('#resource-library-list'); if (!host || !value || !currentTeacher()) return;
    const card = element('article', undefined, 'card assignment-storage'); appendText(card, 'h3', '中央存储概览');
    const fmt = number => Number.isFinite(Number(number)) ? Number(number).toLocaleString('zh-CN') : '—';
    appendText(card, 'p', `文件引用：${fmt(value.file_references)}；不同文件内容：${fmt(value.unique_files)}。`);
    appendText(card, 'p', `历史逻辑大小：${fmt(value.logical_bytes)} B；去重后的内容大小：${fmt(value.unique_bytes)} B；已节省：${fmt(value.saved_bytes)} B。`);
    appendText(card, 'p', `数据库文件：${fmt(value.database_bytes)} B；其中可供后续写入复用的空闲页：${fmt(value.reclaimable_bytes)} B。升级备份另占空间；删除记录不保证文件立即缩小。`, 'asset-note');
    appendText(card, 'p', '固定提交、审核候选和已发布资源各自保留历史记录；相同文件内容只保存一份。没有任何记录引用的内容由服务端自动回收。', 'asset-note');
    host.append(card);
  }
  async function loadLibrary() {
    const epoch = ++state.requestEpoch;
    try {
      const catalog = await state.api('/api/catalog'); if (epoch !== state.requestEpoch) return;
      state.catalog = catalog || {}; renderTemplateOptions(); renderResources();
      if (currentTeacher()) {
        try { const storage = await state.api('/api/storage'); if (epoch === state.requestEpoch) renderStorage(storage); } catch (error) { if (epoch === state.requestEpoch) setText(byId('#resource-library-message'), `资源库已读取；存储概览暂不可用：${error.message}`); }
      }
    } catch (error) { if (epoch === state.requestEpoch) setText(byId('#resource-library-message'), error.message); }
  }
  async function loadAssignmentInputs() {
    if (!currentTeacher()) return;
    const epoch = ++state.requestEpoch; message('正在读取案例和可分发数据集…');
    try {
      const requests = [state.api('/api/catalog'), state.api('/api/datasets')];
      const values = await Promise.all(requests); if (epoch !== state.requestEpoch) return;
      state.catalog = values[0] || {}; state.datasets = datasetRows(values[1]);
      try {
        const derived = await state.api('/api/derived-datasets?limit=100');
        if (epoch === state.requestEpoch) state.datasets.push(...datasetRows(derived));
      } catch (_) { /* Derived data is optional for task creation. */ }
      if (epoch !== state.requestEpoch) return;
      renderTemplateOptions(); renderDatasets(); renderResources(); message('选择案例、数据集和领取账号后生成预览。');
    } catch (error) { if (epoch === state.requestEpoch) message(error.message); }
  }
  async function preview(event) {
    if (event) event.preventDefault(); if (!currentTeacher() || state.previewing) return;
    const epoch = ++state.requestEpoch; state.previewing = true; clearPreview(); const draftEpoch = state.draftEpoch;
    const previewButton = byId('#assignment-preview'); if (previewButton) previewButton.disabled = true;
    try {
      message('正在生成任务预览…');
      const result = await state.post('/api/assignments/preview', payload(false));
      if (epoch !== state.requestEpoch || draftEpoch !== state.draftEpoch) return;
      state.preview = result || {}; const field = byId('#assignment-composition');
      state.previewCompositionText = state.preview.composition ? compositionText(state.preview.composition) : null;
      if (field) field.value = state.previewCompositionText || '';
      renderCompositionPreview(state.preview); const panel = byId('#assignment-preview-panel'); if (panel) panel.hidden = false;
      const dispatch = byId('#assignment-dispatch'); if (dispatch) dispatch.disabled = false;
      message('预览已生成。确认案例、算法、数据集和领取账号后再分发。');
    } catch (error) { if (epoch === state.requestEpoch) message(error.message); }
    finally { state.previewing = false; if (previewButton) previewButton.disabled = false; }
  }
  async function dispatch() {
    if (!currentTeacher() || state.dispatching) return;
    const epoch = ++state.requestEpoch; state.dispatching = true;
    const button = byId('#assignment-dispatch'); if (button) button.disabled = true;
    try {
      message('正在冻结任务基线并创建分发记录…');
      const result = await state.post('/api/assignments', payload(true)); if (epoch !== state.requestEpoch) return;
      const baseline = result?.baseline_submission_id || result?.submission_id || '已创建';
      const count = Array.isArray(result?.distributions) ? result.distributions.length : 0;
      message(`任务已分发：固定基线 ${baseline}；${count} 个领取记录。学生（包括 LY 本人）可在“分发与领取”领取。`);
      clearPreview(); const composition = byId('#assignment-composition'); if (composition) composition.value = '';
      state.payloadKey = null; state.requestId = null;
      document.dispatchEvent?.(new CustomEvent('researchkit:distributions-changed'));
    } catch (error) { if (epoch === state.requestEpoch) message(error.message); }
    finally { state.dispatching = false; if (button) button.disabled = !state.preview; }
  }
  function init(config) {
    Object.assign(state, config || {});
    const form = byId('#assignment-form'), dispatchButton = byId('#assignment-dispatch');
    if (form) form.onsubmit = preview;
    if (dispatchButton) dispatchButton.onclick = dispatch;
    ['#assignment-name', '#assignment-template', '#assignment-targets', '#assignment-instructions'].forEach(id => {
      const control = byId(id); if (control) control.oninput = control.onchange = clearPreview;
    });
    const datasets = byId('#assignment-datasets'); if (datasets) datasets.onchange = clearPreview;
    loadLibrary();
  }
  function enterAssignments() { loadAssignmentInputs(); }
  window.ResearchKitAssignments = {init, enterAssignments, loadLibrary, preview, dispatch, _test: {state, accountNames, payload, renderCompositionPreview, activeDatasets}};
})();
