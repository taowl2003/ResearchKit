(() => {
  'use strict';

  // Assignment creation deliberately only prepares materials. It never edits
  // a selected case's entry point, composition, or parameter bindings.
  const state = {
    api: null, post: null, getMe: null, catalog: {}, datasets: [], preview: null,
    epoch: 0, libraryEpoch: 0, dispatching: false, requestId: null, payloadKey: null,
    managementModule: 'all',
    tagFilters: {
      assignAlgorithm: new Set(),
      assignDataset: new Set(),
      libAlgorithm: new Set(),
      mgmtTemplate: new Set(),
      mgmtAlgorithm: new Set(),
      mgmtDataset: new Set(),
    },
    selectedMaterials: {algorithm: new Set(), dataset: new Set()},
    personalVisibility: {hiddenAlgorithms: new Set(), identity: null, showHiddenAlgorithms: false},
  };
  const $ = selector => document.querySelector(selector);
  const uuid = () => globalThis.crypto?.randomUUID?.() || `rk-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const element = (tag, value, className = '') => {
    const node = document.createElement(tag); if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value); return node;
  };
  const markdown = (parent, value, className = '') => {
    if (window.ResearchKitMarkdown?.append) return window.ResearchKitMarkdown.append(parent, value, {className, empty: '暂无说明。'});
    const fallback = element('small', value || '暂无说明。', className); parent.append(fallback); return fallback;
  };
  const say = value => { const node = $('#assignment-message'); if (node) node.textContent = String(value || ''); };
  const resultMessage = (value, status) => {
    const host = $('#assignment-result'); if (!host) return;
    host.hidden = false; host.className = `action-result ${status || ''}`; host.textContent = value;
  };
  const teacher = () => state.getMe?.()?.role === 'teacher';
  const names = value => [...new Set(String(value || '').split(/[\s,;，；]+/).map(item => item.trim()).filter(Boolean))];
  const rows = value => Array.isArray(value) ? value : (Array.isArray(value?.items) ? value.items : []);
  const checked = selector => [...document.querySelectorAll(selector)].filter(node => node.checked).map(node => node.value);
  const itemTags = item => Array.isArray(item?.tags) ? item.tags.map(t => String(t).trim()).filter(Boolean) : [];
  const lifecycleLabel = lifecycle => ({retired: '已下架', trashed: '已移入回收站'}[lifecycle?.state] || '使用中');

  // The public catalog intentionally omits inactive versions, so students do
  // not accidentally select them.  Teacher management needs the other side
  // of that boundary: an inactive version remains a real platform resource
  // while it protects a dependency, and must therefore remain discoverable
  // and deletable from its own management page.
  function inactiveManagementEntries(kind, catalogEntries, accessRows) {
    const entries = (catalogEntries || []).filter(item => !(kind === 'algorithm' && item?.source === 'task'));
    const existing = new Set(entries.map(item => String(item?.ref || '')).filter(Boolean));
    (accessRows || []).forEach(row => {
      if (!row || row.kind !== kind || typeof row.ref !== 'string' || existing.has(row.ref)) return;
      const state = row.lifecycle?.state;
      if (state !== 'retired' && state !== 'trashed') return;
      const divider = row.ref.lastIndexOf('@');
      if (divider <= 0 || divider === row.ref.length - 1) return;
      const name = row.display_name || row.name || row.ref.slice(0, divider);
      entries.push({
        ref: row.ref,
        name,
        version: row.ref.slice(divider + 1),
        description: typeof row.description === 'string' ? row.description : '',
        tags: itemTags(row),
        family_id: typeof row.family_id === 'string' && row.family_id ? row.family_id : `${kind}:${row.ref}`,
        parent_ref: typeof row.parent_ref === 'string' ? row.parent_ref : null,
        display_name: typeof row.display_name === 'string' ? row.display_name : undefined,
        source: row.source || null,
        lifecycle: row.lifecycle,
        management_inactive: true,
      });
      existing.add(row.ref);
    });
    return entries;
  }

  function personalVisibilityIdentity() {
    const current = state.getMe?.() || {};
    return `${current.uid ?? ''}:${current.username || ''}`;
  }

  async function loadPersonalAlgorithmVisibility() {
    const identity = personalVisibilityIdentity();
    const value = await state.api('/api/personal-resource-visibility');
    if (identity !== personalVisibilityIdentity()) return false;
    state.personalVisibility.identity = identity;
    state.personalVisibility.hiddenAlgorithms = new Set(Array.isArray(value?.hidden_algorithms) ? value.hidden_algorithms : []);
    return true;
  }

  async function setPersonalAlgorithmVisibility(ref, hidden) {
    const identity = personalVisibilityIdentity();
    await state.api('/api/personal-resource-visibility', {
      method: 'PUT', body: JSON.stringify({kind: 'algorithm', ref, hidden}),
    });
    if (identity !== personalVisibilityIdentity()) return false;
    if (hidden) state.personalVisibility.hiddenAlgorithms.add(ref);
    else state.personalVisibility.hiddenAlgorithms.delete(ref);
    return true;
  }

  function filterByTags(items, selectedTags) {
    if (!selectedTags || !selectedTags.size) return items;
    const req = [...selectedTags];
    return items.filter(item => {
      const tags = new Set(itemTags(item));
      return req.every(tag => tags.has(tag));
    });
  }

  function createTagFilterBar(items, selectedTags, onChange) {
    const allTags = [...new Set(items.flatMap(itemTags))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
    if (!allTags.length) return null;
    const matched = filterByTags(items, selectedTags);
    const available = new Set(matched.flatMap(itemTags));
    const bar = element('div', undefined, 'tag-filter-bar');
    bar.append(element('span', '标签筛选（多选交集）：', 'tag-filter-label'));
    allTags.forEach(tag => {
      const selected = selectedTags.has(tag);
      const canSelect = selected || available.has(tag);
      const chip = element('button', tag, `tag-chip${selected ? ' selected' : ''}${canSelect ? '' : ' unavailable'}`);
      chip.type = 'button';
      chip.disabled = !canSelect;
      chip.onclick = () => {
        if (!canSelect) return;
        if (selected) selectedTags.delete(tag);
        else selectedTags.add(tag);
        onChange();
      };
      bar.append(chip);
    });
    const clear = element('button', '清空筛选', 'tag-clear-btn');
    clear.type = 'button';
    clear.disabled = !selectedTags.size;
    clear.onclick = () => { selectedTags.clear(); onChange(); };
    bar.append(clear);
    return bar;
  }

  const resetPreview = () => {
    state.preview = null;
    const panel = $('#assignment-preview-panel'); if (panel) panel.hidden = true;
    const dispatch = $('#assignment-dispatch');
    if (dispatch) { dispatch.disabled = true; dispatch.textContent = '确认并分发'; }
  };
  const syncMaterialSelection = (selector, selKey) => {
    const selected = state.selectedMaterials[selKey];
    [...(document.querySelectorAll?.(`${selector} input[type="checkbox"]`) || [])].forEach(item => {
      if (item.value) {
        if (item.checked) selected.add(item.value);
        else selected.delete(item.value);
      }
    });
    return [...selected];
  };
  const values = () => ({
    name: $('#assignment-name')?.value.trim() || '',
    template_ref: $('#assignment-mode')?.value === 'resources' ? '' : ($('#assignment-template')?.value || ''),
    algorithm_refs: syncMaterialSelection('#assignment-algorithms', 'algorithm'),
    dataset_ids: syncMaterialSelection('#assignment-datasets', 'dataset'),
    targets: names($('#assignment-targets')?.value),
    instructions: $('#assignment-instructions')?.value.trim() || '',
    include_baseline_algorithms: true,
    ...($('#assignment-parent')?.value ? {parent_task_id: $('#assignment-parent').value} : {}),
  });
  const payload = () => {
    const value = values();
    if (!value.name) throw new Error('请填写任务名称。');
    if ($('#assignment-mode')?.value !== 'resources' && !value.template_ref) throw new Error('请选择正式案例；只发送算法和数据时，请选择“资源包”。');
    if (!value.template_ref && !value.algorithm_refs.length && !value.dataset_ids.length) throw new Error('资源包至少选择一个算法或数据集。');
    if (!value.targets.length) throw new Error('请至少填写一个接收账号。');
    const key = JSON.stringify(value);
    if (key !== state.payloadKey) { state.payloadKey = key; state.requestId = uuid(); }
    return {request_id: state.requestId, ...value};
  };
  function renderTemplates() {
    const select = $('#assignment-template'); if (!select) return;
    const previous = select.value; select.replaceChildren();
    const blank = element('option', '选择已发布案例'); blank.value = ''; select.append(blank);
    (state.catalog.templates || []).forEach(item => {
      const option = element('option', `${item.name || item.ref} · ${item.version || item.ref}`);
      option.value = item.ref; select.append(option);
    });
    select.value = previous;
  }
  function renderMaterials(hostSelector, list, kind) {
    const host = $(hostSelector); if (!host) return;
    const selKey = kind === '算法' ? 'algorithm' : 'dataset';
    if (host.querySelectorAll) {
      checked(`${hostSelector} input[type="checkbox"]`).forEach(v => state.selectedMaterials[selKey].add(v));
      [...host.querySelectorAll('input[type="checkbox"]')].forEach(box => {
        if (!box.checked) state.selectedMaterials[selKey].delete(box.value);
      });
    }
    host.replaceChildren();
    if (!list.length) { host.append(element('p', kind === '算法' ? '当前没有可附加的已发布算法。' : '当前没有可附加的数据集。', 'asset-note')); return; }
    const selectedSummary = element('p', '', 'asset-note selected-materials-summary');
    const refreshSelectedSummary = () => {
      const picks = [...state.selectedMaterials[selKey]];
      selectedSummary.textContent = picks.length ? `已选资源（${picks.length}，筛选切换会保留勾选）：${picks.join('、')}` : '当前未勾选。';
    };
    refreshSelectedSummary();
    host.append(selectedSummary);
    const filterSet = kind === '算法' ? state.tagFilters.assignAlgorithm : state.tagFilters.assignDataset;
    const tagBar = createTagFilterBar(list, filterSet, () => renderMaterials(hostSelector, list, kind));
    if (tagBar) host.append(tagBar);
    const filtered = filterByTags(list, filterSet);
    if (!filtered.length) { host.append(element('p', '当前标签筛选下没有匹配资源。', 'asset-note')); return; }
    filtered.forEach(item => {
      const id = kind === '算法' ? item.ref : item.dataset_id;
      if (!id) return;
      const row = element('details', undefined, 'resource-row assignment-dataset');
      const summary = element('summary');
      const box = document.createElement('input'); box.type = 'checkbox'; box.value = id;
      box.checked = state.selectedMaterials[selKey].has(id);
      box.onchange = () => {
        if (box.checked) state.selectedMaterials[selKey].add(id);
        else state.selectedMaterials[selKey].delete(id);
        refreshSelectedSummary();
        resetPreview();
      };
      summary.append(box, element('span', `${item.name || id} · ${id}`));
      const tags = itemTags(item);
      if (tags.length) {
        const tagHost = element('span', undefined, 'dataset-tags');
        tags.forEach(tag => tagHost.append(element('span', tag, 'dataset-tag')));
        summary.append(tagHost);
      }
      const body = element('div', undefined, 'resource-row-body');
      const description = item.description || item.usage;
      if (description) {
        if (String(item.provenance_ref || '').startsWith('README:')) markdown(body, description, 'dataset-readme');
        else body.append(element('small', description));
      } else {
        body.append(element('small', '暂无补充说明。'));
      }
      row.append(summary, body);
      host.append(row);
    });
  }
  function renderInputs() {
    renderTemplates();
    renderMaterials('#assignment-algorithms', state.catalog.plugins || [], '算法');
    const data = new Map(); state.datasets.forEach(item => { if (item?.dataset_id) data.set(item.dataset_id, item); });
    renderMaterials('#assignment-datasets', [...data.values()], '数据集');
    const dataNote = $('#assignment-datasets-note'); if (dataNote) dataNote.textContent = data.size ? `可附加 ${data.size} 个已授权数据集；只提供材料，不替换案例原有数据。` : '可不附加数据集；案例自身的数据引用会完整保留。';
    updateMode();
  }
  function updateMode() {
    const resources = $('#assignment-mode')?.value === 'resources', select = $('#assignment-template');
    if (select) { select.required = !resources; select.disabled = resources; }
    const note = $('#assignment-mode-note');
    if (note) note.textContent = resources ? '只发送所选算法和数据，不创建案例基线。分发后自动进入学生的算法库和数据资源，不需要领取。' : '学生点击“开始任务”创建自己的项目；算法和数据自动成为可用资源。基础案例保持原样。';
  }
  function references(title, entries, empty) {
    const section = element('section'); section.append(element('h3', title));
    if (!entries?.length) section.append(element('p', empty, 'asset-note'));
    else {
      const list = element('ul'); entries.forEach(item => list.append(element('li', typeof item === 'string' ? item : (item.name || item.ref || item.dataset_id || '未知资源')))); section.append(list);
    }
    return section;
  }
  function renderPreview(value) {
    const host = $('#assignment-preview-content'); if (!host) return; host.replaceChildren();
    const template = value.template_ref || values().template_ref;
    host.append(element('p', template ? `基础案例：${template}` : '资源包：不含基础案例'));
    if (template) {
      host.append(element('p', '基础案例的代码、已有算法和已有数据引用会原样保留。'));
      host.append(references('案例已有算法', value.source_algorithms || value.template_algorithms || [], '该案例没有登记可显示的算法。'));
      host.append(references('案例已有数据', value.source_data || value.template_datasets || [], '该案例没有登记可显示的数据引用。'));
    } else host.append(element('p', '算法和数据分发后自动可用，学生按需加入自己的项目。'));
    host.append(references('额外算法工具', value.algorithm_refs || values().algorithm_refs, '未附加算法；学生仍可使用案例已有算法。'));
    host.append(references('额外数据材料', value.dataset_ids || values().dataset_ids, '未附加数据；学生仍可使用案例原有数据。'));
    host.append(element('p', `接收账号：${(value.targets || values().targets).map(item => typeof item === 'string' ? item : item.username).join('、') || '未返回'}`));
    host.append(element('p', '额外材料不会自动写入 case.py、改变算法参数或替换案例数据；学生领取后自行决定如何使用。', 'asset-note'));
    (value.warnings || []).forEach(item => host.append(element('p', item, 'assignment-warning')));
  }
  async function load() {
    if (!teacher()) return;
    const epoch = ++state.epoch; say('正在读取正式案例、可附加算法和数据集…');
    try {
      const [catalog, datasets] = await Promise.all([state.api('/api/catalog'), state.api('/api/datasets')]);
      if (epoch !== state.epoch) return;
      state.catalog = catalog || {}; state.datasets = rows(datasets);
      try { const derived = await state.api('/api/derived-datasets?limit=100'); if (epoch === state.epoch) state.datasets.push(...rows(derived)); } catch (_) {}
      if (epoch !== state.epoch) return;
      renderInputs();
      const tasks = await state.api('/api/tasks'); if (epoch !== state.epoch) return;
      state.tasks = rows(tasks);
      const parent = $('#assignment-parent'); if (parent) {
        const previous = parent.value; parent.replaceChildren(); const option = element('option', '新建独立任务／资源包'); option.value = ''; parent.append(option);
        const latest = new Map(); rows(tasks).forEach(task => { const key = task.family_id || task.task_id; if (!latest.has(key) || Number(task.task_version || 1) > Number(latest.get(key).task_version || 1)) latest.set(key, task); });
        [...latest.values()].filter(task => task.kind !== 'trial').forEach(task => { const option = element('option', `${task.name} · 原版本 ${task.task_version || 1}`); option.value = task.task_id; option.dataset.mode = task.delivery_mode || 'project'; parent.append(option); }); parent.value = previous;
      }
      say('可新建任务，或选择原任务发布新版本。完整任务选择基础案例；资源包只需算法或数据。');
    } catch (error) { if (epoch === state.epoch) say(error.message); }
  }
  function fillVersion(task) {
    if (!task) return;
    const set = (selector, value) => { const node = $(selector); if (node) node.value = value; };
    set('#assignment-parent', task.task_id); set('#assignment-mode', task.delivery_mode || 'project');
    set('#assignment-name', task.name || ''); set('#assignment-targets', (task.recipients || []).map(item => item.username || item.target_username).join(', '));
    set('#assignment-instructions', task.instructions || '');
    const template = (task.materials || []).find(item => item.kind === 'template'); if (template) set('#assignment-template', template.ref);
    for (const [selector, kind] of [['#assignment-algorithms', 'algorithm'], ['#assignment-datasets', 'dataset']]) {
      const refs = new Set((task.materials || []).filter(item => item.kind === kind && !item.supplement_id && item.metadata?.source !== 'baseline').map(item => item.ref));
      const key = kind === 'algorithm' ? 'algorithm' : 'dataset';
      state.selectedMaterials[key] = new Set(refs);
      if ($(selector)?.querySelectorAll) {
        document.querySelectorAll(selector + ' input[type="checkbox"]').forEach(box => { box.checked = refs.has(box.value); });
      }
    }
    updateMode(); resetPreview(); say(`正在为“${task.name}”发布新版本，原版本和已有个人项目保留。`);
  }
  async function prepareVersion(task) { await load(); fillVersion(task); }
  async function preview(event) {
    event?.preventDefault(); if (!teacher()) return;
    const button = $('#assignment-preview'); if (button) button.disabled = true;
    const previous = $('#assignment-result'); if (previous) { previous.hidden = true; previous.textContent = ''; }
    try {
      say('正在生成任务预览…'); const result = await state.post('/api/assignments/preview', payload());
      state.preview = result || {}; renderPreview(state.preview);
      const panel = $('#assignment-preview-panel'); if (panel) panel.hidden = false;
      const dispatch = $('#assignment-dispatch');
      if (dispatch) { dispatch.textContent = '确认并分发'; dispatch.disabled = false; }
      say('预览已生成，核对材料和接收账号后即可确认分发。');
    } catch (error) { say(error.message); }
    finally { if (button) button.disabled = false; }
  }
  async function dispatch() {
    if (!teacher() || state.dispatching) return;
    const button = $('#assignment-dispatch'); if (button) button.disabled = true;
    state.dispatching = true;
    let didDispatch = false;
    try {
      const request = payload(); resultMessage(`正在分发“${request.name}”…`, 'pending');
      const result = await state.post('/api/assignments', request);
      const count = result?.distributions?.length || result?.recipients?.length || 0;
      const taskId = result?.task_id || result?.task?.task_id;
      const message = `分发成功：“${request.name}”${count ? `，${count} 个接收记录` : ''}。接收账号：${request.targets.join('、')}。${taskId ? `任务 ID：${taskId}。` : ''}可在下方“已布置任务”查看，学生在“项目 → 教师任务”开始任务，资源包自动可用。`;
      say(message); resultMessage(message, 'success');
      state.selectedMaterials.algorithm.clear(); state.selectedMaterials.dataset.clear();
      resetPreview(); state.payloadKey = null; state.requestId = null;
      didDispatch = true;
      if (button) { button.textContent = '已分发'; button.disabled = true; }
      document.dispatchEvent(new CustomEvent('researchkit:tasks-changed'));
      document.dispatchEvent(new CustomEvent('researchkit:distributions-changed'));
    } catch (error) { say(error.message); resultMessage(`分发未完成：${error.message}`, 'error'); }
    finally { state.dispatching = false; if (button && !didDispatch) button.disabled = !state.preview; }
  }
  function projectChoice() {
    const select = element('select');
    const blank = element('option', '选择自己的目标项目'); blank.value = ''; select.append(blank);
    (state.getProjects?.() || []).filter(item => !item.archived && !item.unavailable).forEach(item => {
      const option = element('option', item.name || item.project_id); option.value = item.project_id; select.append(option);
    });
    return select;
  }
  function parseOptionalJsonObject(raw, label) {
    const text = String(raw || '').trim();
    if (!text) return undefined;
    let parsed;
    try { parsed = JSON.parse(text); }
    catch (_) { throw new Error(`${label}必须是合法 JSON 对象`); }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error(`${label}必须是 JSON 键值对象（如 {"k": "v"}）`);
    }
    return parsed;
  }

  function algorithmUse(card, ref, isLocal = false) {
    const panel = element('div', undefined, 'resource-use'), select = projectChoice();
    const aliasInput = element('input', undefined, 'algo-alias-input');
    aliasInput.placeholder = '步骤别名（可选，如 scale）';
    const paramsInput = element('input', undefined, 'algo-params-input');
    paramsInput.placeholder = '覆盖参数 JSON（可选，如 {"factor": 2}）';
    const message = element('p'); message.setAttribute?.('aria-live', 'polite');
    const add = element('button', '加入项目'); add.type = 'button';
    add.onclick = async () => {
      if (!select.value) { message.textContent = '请先选择自己的目标项目。'; return; }
      add.disabled = true;
      try {
        const aliasVal = String(aliasInput.value || '').trim();
        const paramsObj = parseOptionalJsonObject(paramsInput.value, '覆盖参数');
        const body = isLocal
          ? {local_algorithm_id: ref, ...(aliasVal ? {alias: aliasVal, bind: true} : {}), ...(paramsObj ? {parameters: paramsObj} : {})}
          : {algorithm_refs: [ref], ...(aliasVal ? {alias: aliasVal, bind: true} : {}), ...(paramsObj ? {parameters: paramsObj} : {})};
        await state.post(`/api/projects/${encodeURIComponent(select.value)}/algorithm-materials`, body);
        message.className = 'action-result success';
        message.textContent = aliasVal
          ? `已按别名“${aliasVal}”绑定并写入 project.yaml；依赖锁已同步更新。`
          : '算法材料已加入项目；已有版本不会重复添加。';
      } catch (error) { message.className = 'action-result error'; message.textContent = error.message; }
      finally { add.disabled = false; }
    };
    panel.append(select, aliasInput, paramsInput, add, message); card.append(panel);
  }

  function appendPermissionControls(panel, kind, ref, rowAccess, dsAccess, write, refresh) {
    if (kind === 'contract') return;
    const isDataset = kind === 'dataset';
    const record = isDataset ? dsAccess.get(ref) : rowAccess;
    if (!record && isDataset) {
      // Public raw/tree datasets or non-derived datasets: allow derived ACL only when derived/present in dsAccess or derived record
    }
    const cascaded = Array.isArray(record?.cascaded_from) ? record.cascaded_from : [];
    const cascadedTargets = Array.isArray(record?.cascaded_targets) ? record.cascaded_targets : [];
    if (cascaded.length) {
      const cHost = element('div');
      cHost.append(element('span', `来自案例授权：${cascaded.join('、')}`, 'cascade-badge'));
      if (cascadedTargets.length) {
        cHost.append(element('span', `（自动覆盖账号：${cascadedTargets.map(t => t.username).filter(Boolean).join(', ')}）`, 'asset-note'));
      }
      panel.append(cHost);
    }
    if (!record && isDataset) return;
    if (!record) return;
    const form = element('form', undefined, 'access-form');
    const pubLabel = element('label', undefined, 'access-checkbox');
    const pubBox = document.createElement('input'); pubBox.type = 'checkbox'; pubBox.checked = Boolean(record.public);
    pubLabel.append(pubBox, element('span', '公开给全部获准用户'));
    const targetsInput = element('input');
    targetsInput.value = (record.targets || []).map(t => t.username).filter(Boolean).join(', ');
    targetsInput.placeholder = '额外授权账号（逗号分隔）';
    const targetsLabel = element('label'); targetsLabel.append(element('span', '单独授权账号'), targetsInput);
    const saveBtn = element('button', '保存权限'); saveBtn.type = 'submit';
    form.append(pubLabel, targetsLabel, saveBtn);
    form.onsubmit = async event => {
      event.preventDefault();
      if (!teacher()) { write('只有教师可以修改共享权限。'); return; }
      saveBtn.disabled = true;
      try {
        const targets = names(targetsInput.value);
        if (isDataset) {
          await state.post(`/api/derived-datasets/${encodeURIComponent(ref)}/access`, {public: Boolean(pubBox.checked), targets});
        } else {
          await state.api('/api/component-access', {
            method: 'PUT',
            body: JSON.stringify({kind, ref, public: Boolean(pubBox.checked), targets, expected_revision: record.revision}),
          });
        }
        await refresh(`已更新 ${ref} 的访问权限。`);
      } catch (error) { write(error.message); }
      finally { saveBtn.disabled = false; }
    };
    panel.append(form);
  }

  function appendMetadataControls(panel, kind, ref, item, write, refresh) {
    if (kind !== 'template' && kind !== 'algorithm') return;
    const form = element('form', undefined, 'access-form');
    const tagsInput = element('input');
    tagsInput.value = itemTags(item).join(', ');
    tagsInput.placeholder = '标签（逗号分隔）';
    const descInput = element('input');
    descInput.value = item.description || '';
    descInput.placeholder = '资源说明';
    const saveBtn = element('button', '保存标签与说明'); saveBtn.type = 'submit';
    form.append(tagsInput, descInput, saveBtn);
    form.onsubmit = async event => {
      event.preventDefault();
      if (!teacher()) return;
      saveBtn.disabled = true;
      try {
        await state.post('/api/assets/metadata', {
          kind, ref,
          tags: names(tagsInput.value),
          description: descInput.value.trim(),
        });
        await refresh(`已更新 ${ref} 的标签与说明。`);
      } catch (error) { write(error.message); }
      finally { saveBtn.disabled = false; }
    };
    panel.append(form);
  }

  function appendDirectCleanupControls(panel, kind, ref, write, refresh) {
    if (!['template', 'algorithm', 'dataset'].includes(kind)) return;
    const box = element('div', undefined, 'direct-cleanup-panel');
    const status = element('p', undefined, 'asset-note');
    const labels = {
      template: {noun: '案例', start: '删除案例', confirm: '确认删除案例'},
      algorithm: {noun: '算法', start: '删除算法', confirm: '确认删除算法'},
      dataset: {noun: '数据登记', start: '移除数据登记', confirm: '确认移除数据登记'},
    };
    const label = labels[kind];
    const checkBtn = element('button', label.start, 'danger-btn'); checkBtn.type = 'button';
    const actionArea = element('div');
    checkBtn.onclick = async () => {
      if (!teacher()) { write('只有教师可以执行清理。'); return; }
      checkBtn.disabled = true; actionArea.replaceChildren(); status.textContent = '正在检查平台引用与依赖…';
      try {
        const info = await state.api(`/api/asset-cleanup/check?kind=${encodeURIComponent(kind)}&ref=${encodeURIComponent(ref)}`);
        if (!info.can_delete) {
          status.textContent = info.message || '存在案例引用，暂时不能删除。请先清理以下案例后再删除。';
          const blockers = Array.isArray(info.blocking_cases) ? info.blocking_cases : [];
          if (blockers.length) {
            const ul = element('ul');
            blockers.forEach(c => ul.append(element('li', `占用案例：${typeof c === 'string' ? c : (c.name ? `${c.name} (${c.ref})` : c.ref)}`)));
            actionArea.append(ul);
          }
          return;
        }
        status.textContent = kind === 'dataset'
          ? '没有平台案例引用。确认后只移除平台中的数据登记，原始或派生数据文件会保留。'
          : `没有平台案例引用。确认后删除这个平台${label.noun}版本；不会顺带删除其他资源。已有个人项目中的代码副本仍保留；后续入库若需要重建已删除的资源版本，请在审核时重新选择入库。`;
        const confirmBtn = element('button', label.confirm, 'danger-btn'); confirmBtn.type = 'button';
        confirmBtn.onclick = async () => {
          confirmBtn.disabled = true;
          try {
            const result = await state.post('/api/asset-cleanup/direct', {kind, ref, confirmation: '永久清理'});
            let receipt = `已删除平台${label.noun}：${ref}。`;
            if (kind === 'dataset') {
              const manual = result?.manual_cleanup || {};
              receipt = `已移除平台数据登记：${ref}。原始或派生数据文件仍保留。`;
              if (manual.message) receipt += ` ${manual.message}`;
              if (manual.path) receipt += ` 路径：${manual.path}`;
            }
            state.managementReceipt = receipt;
            await refresh();
          } catch (error) { status.textContent = `清理失败：${error.message}`; confirmBtn.disabled = false; }
        };
        actionArea.append(confirmBtn);
      } catch (error) {
        status.textContent = `引用检查失败：${error.message}`;
      } finally { checkBtn.disabled = false; }
    };
    box.append(checkBtn, status, actionArea);
    panel.append(box);
  }

  async function renderLocalAlgorithms() {
    const host = $('#local-algorithms-list');
    if (!host) return;
    const message = $('#local-algo-message');
    const write = msg => { if (message) message.textContent = msg; };
    host.replaceChildren();
    try {
      const items = await state.api('/api/local-algorithms');
      const list = Array.isArray(items) ? items : [];
      if (!list.length) {
        host.append(element('p', '暂无个人本地算法，可通过上方表单创建。', 'asset-note'));
        return;
      }
      list.forEach(item => {
        const row = element('details', undefined, 'resource-row');
        const summary = element('summary');
        summary.append(element('span', `${item.name}@${item.version}（本地）`));
        const tags = itemTags(item);
        if (tags.length) {
          const tHost = element('span', undefined, 'dataset-tags');
          tags.forEach(t => tHost.append(element('span', t, 'dataset-tag')));
          summary.append(tHost);
        }
        const body = element('div', undefined, 'resource-row-body');
        if (item.description) body.append(element('p', item.description));
        const card = element('article', undefined, 'card resource-card');
        algorithmUse(card, item.id, true);
        const del = element('button', '删除本地算法', 'danger-btn'); del.type = 'button';
        del.onclick = async () => {
          del.disabled = true;
          try {
            await state.post(`/api/local-algorithms/${encodeURIComponent(item.id)}/delete`, {});
            write(`已删除本地算法：${item.name}@${item.version}`);
            await renderLocalAlgorithms();
          } catch (error) { write(error.message); del.disabled = false; }
        };
        card.append(del);
        body.append(card);
        row.append(summary, body);
        host.append(row);
      });
    } catch (error) { write(`读取本地算法失败：${error.message}`); }
  }

  async function loadLibrary() {
    await renderLibrary(false);
    if ($('#local-algorithms-list')) await renderLocalAlgorithms();
  }
  async function loadManagement(notice = '') { return renderLibrary(true, notice); }
  function syncTeacherModuleNav() {
    document.querySelectorAll?.('[data-page="resource-management"]')?.forEach(btn => {
      const mod = btn.dataset?.module || '';
      btn.setAttribute?.('aria-current', mod === state.managementModule ? 'page' : 'false');
    });
    if (typeof CustomEvent === 'function' && document.dispatchEvent) {
      document.dispatchEvent(new CustomEvent('researchkit:management-module-changed', {detail: {module: state.managementModule}}));
    }
  }
  function setManagementModule(module) {
    state.managementModule = ['template', 'algorithm', 'dataset'].includes(module) ? module : 'template';
    syncTeacherModuleNav();
  }
  async function renderLibrary(management, notice = '') {
    if (management && !teacher()) return;
    const prefix = management ? '#resource-management' : '#resource-library';
    const host = $(prefix + '-list'), message = $(prefix + '-message'); if (!host) return;
    const epoch = ++state.libraryEpoch, role = state.getMe?.()?.role, identity = personalVisibilityIdentity();
    const current = () => epoch === state.libraryEpoch && state.getMe?.()?.role === role && personalVisibilityIdentity() === identity;
    const write = value => { if (current() && message) { message.textContent = value; message.className = 'asset-message action-result'; } };
    host.replaceChildren(); write(management ? '正在读取平台资源…' : '正在读取可用算法…');
    const paths = management
      ? ['/api/catalog', '/api/component-access?include_inactive=true', '/api/datasets', '/api/derived-datasets/access']
      : ['/api/catalog', '/api/personal-resource-visibility'];
    const results = await Promise.allSettled(paths.map(path => state.api(path)));
    if (!current()) return;
    if (results[0].status !== 'fulfilled') { write(`资源读取失败：${results[0].reason?.message || '请重试'}`); return; }
    const catalog = results[0].value || {}, access = new Map(), dsAccess = new Map();
    if (!management) {
      if (results[1]?.status !== 'fulfilled') { write(`个人显示偏好读取失败：${results[1]?.reason?.message || '请刷新重试'}`); return; }
      const visibility = results[1].value || {};
      state.personalVisibility.identity = personalVisibilityIdentity();
      state.personalVisibility.hiddenAlgorithms = new Set(Array.isArray(visibility.hidden_algorithms) ? visibility.hidden_algorithms : []);
    }
    const canManage = management && results[1]?.status === 'fulfilled' && Array.isArray(results[1].value);
    if (canManage) results[1].value.forEach(row => access.set(`${row.kind}:${row.ref}`, row));
    if (management && results[3]?.status === 'fulfilled' && results[3].value && typeof results[3].value === 'object') {
      Object.entries(results[3].value).forEach(([did, info]) => dsAccess.set(did, info));
    }

    if (management) {
      const module = ['template', 'algorithm', 'dataset'].includes(state.managementModule) ? state.managementModule : 'template';
      if (module !== state.managementModule) setManagementModule(module);
      const pageTitle = $('#resource-management-title');
      const pageIntro = $('#resource-management-intro');
      const title = {template: '案例管理', algorithm: '算法管理', dataset: '数据管理'}[module];
      if (pageTitle) pageTitle.textContent = title;
      if (pageIntro) pageIntro.textContent = `${title}只显示已入库的平台资源；每个版本可单独管理授权或删除。`;
      const dataManager = $('#data-management-nav');
      if (dataManager) dataManager.hidden = module !== 'dataset';
    }

    const allSections = management
      ? [['案例管理', 'template', inactiveManagementEntries('template', catalog.templates, canManage ? results[1].value : [])],
         ['算法管理', 'algorithm', inactiveManagementEntries('algorithm', catalog.plugins, canManage ? results[1].value : [])],
         ['数据管理', 'dataset', results[2]?.status === 'fulfilled'
           ? rows(results[2].value).filter(item => !['private', 'review'].includes(String(item?.visibility || '').toLowerCase())) : []]]
      : [['可用算法', 'algorithm', catalog.plugins || []]];
    const sections = management ? allSections.filter(([, kind]) => kind === state.managementModule) : allSections;

    let versionCount = 0, resourceCount = 0;
    sections.forEach(([label, kind, rawEntries]) => {
      const section = element('section', undefined, 'resource-library-section'); section.append(element('h2', label));
      const filterMap = {template: 'mgmtTemplate', algorithm: 'mgmtAlgorithm', dataset: 'mgmtDataset'};
      const filterSet = management ? state.tagFilters[filterMap[kind]] : (kind === 'algorithm' ? state.tagFilters.libAlgorithm : null);
      if (filterSet) {
        const bar = createTagFilterBar(rawEntries, filterSet, () => renderLibrary(management, notice));
        if (bar) section.append(bar);
      }
      if (!management && kind === 'algorithm') {
        const toggle = document.createElement('label'); toggle.className = 'inline-check';
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = state.personalVisibility.showHiddenAlgorithms;
        checkbox.onchange = () => { state.personalVisibility.showHiddenAlgorithms = checkbox.checked; return renderLibrary(false); };
        toggle.append(checkbox, element('span', '显示已移除算法'));
        section.append(toggle, element('p', '从我的算法库移除只改变当前账号的显示偏好，不影响他人或已加入项目的资源。', 'asset-note'));
      }
      const personalEntries = !management && kind === 'algorithm' && !state.personalVisibility.showHiddenAlgorithms
        ? rawEntries.filter(item => !state.personalVisibility.hiddenAlgorithms.has(item?.ref)) : rawEntries;
      const entries = filterSet ? filterByTags(personalEntries, filterSet) : personalEntries;
      if (!entries.length) {
        const allPersonallyHidden = !management && kind === 'algorithm' && rawEntries.length && !personalEntries.length;
        section.append(element('p', allPersonallyHidden ? '已从本人列表移除，可勾选“显示已移除算法”恢复。' : (rawEntries.length ? '当前标签筛选下没有匹配资源。' : '当前没有可用版本。'), 'asset-note'));
      }
      const groups = new Map();
      entries.forEach(item => {
        const ref = item.ref || item.id || item.dataset_id; if (!ref) return;
        const key = kind === 'dataset' ? item.family_id || ref : String(ref).split('@')[0];
        if (!groups.has(key)) groups.set(key, []); groups.get(key).push(item);
      });
      resourceCount += groups.size;
      groups.forEach((versions, identity) => {
        const group = element('details', undefined, 'resource-version-group');
        group.append(element('summary', `${versions[0].name || identity} · ${versions.length} 个版本`));
        if (versions.length === 1) group.open = true;
        versions.forEach(item => {
          const ref = item.ref || item.id || item.dataset_id; versionCount += 1;
          const card = element('article', undefined, 'card resource-card');
          const row = element('details', undefined, 'resource-row');
          if (management) row.open = false;
          const summary = element('summary');
          summary.append(element('span', item.name ? `${item.name} · ${ref}` : ref));
          const tags = itemTags(item);
          if (tags.length) {
            const tagHost = element('span', undefined, 'dataset-tags');
            tags.forEach(tag => tagHost.append(element('span', tag, 'dataset-tag')));
            summary.append(tagHost);
          }
          const body = element('div', undefined, 'resource-row-body');
          body.append(element('h3', ref));
          if (!management && kind === 'algorithm' && item.source === 'task') {
            body.append(element('p', '来源：历史任务保留资源。', 'asset-note'));
          }
          if (management && item.lifecycle?.state && item.lifecycle.state !== 'active') {
            body.append(element('p', `状态：${lifecycleLabel(item.lifecycle)}。仍在平台库内，引用依赖保留；需要时删除案例后再清理算法或数据。`, 'asset-note'));
          }
          if (management && kind === 'template') {
            const source = access.get(`${kind}:${ref}`)?.source;
            if (source) {
              body.append(element('p', `来源账号：${source.owner_username || '历史账号'} · 来源项目：${source.project_name || '历史项目'}`));
              body.append(element('p', `来源项目 ID：${source.project_id || '历史记录不可用'} · 固定提交：${source.submission_id}`, 'path'));
            } else body.append(element('p', '来源：平台预置案例或来源记录已清理。'));
            body.append(element('p', '个人项目的来源案例，可在“项目”中展开后核对。删除这个平台案例不会删除已有个人项目，也不会删除其算法和数据。'));
          }
          if (item.parent_ref || item.parent_dataset_id) {
            body.append(element('p', `继承版本来源：${item.parent_ref || item.parent_dataset_id}`, 'path'));
          }
          if (item.description) markdown(body, item.description);
          if (!management) {
            const hidden = state.personalVisibility.hiddenAlgorithms.has(ref);
            if (!hidden) algorithmUse(body, ref);
            const visibility = element('div', undefined, 'resource-use');
            const visibilityMessage = element('p'); visibilityMessage.setAttribute?.('aria-live', 'polite');
            const visibilityButton = element('button', hidden ? '恢复显示' : '从我的算法库移除'); visibilityButton.type = 'button';
            visibilityButton.onclick = async () => {
              if (!current()) return;
              visibilityButton.disabled = true;
              try {
                const applied = await setPersonalAlgorithmVisibility(ref, !hidden);
                if (!current() || !applied) return;
                await renderLibrary(false);
              } catch (error) {
                if (!current()) return;
                visibilityMessage.className = 'action-result error'; visibilityMessage.textContent = `更新个人显示偏好失败：${error.message}`;
                visibilityButton.disabled = false;
              }
            };
            visibility.append(visibilityButton, visibilityMessage); body.append(visibility);
          }
          if (management) {
            const panel = element('div', undefined, 'resource-management'); panel.append(element('strong', '教师管理'));
            appendPermissionControls(panel, kind, ref, access.get(`${kind}:${ref}`), dsAccess, write, m => loadManagement(m));
            appendMetadataControls(panel, kind, ref, item, write, m => loadManagement(m));
            appendDirectCleanupControls(panel, kind, ref, write, m => loadManagement(m));
            body.append(panel);
          }
          row.append(summary, body);
          card.append(row);
          group.append(card);
        }); section.append(group);
      }); host.append(section);
    });
    const receipt = state.managementReceipt;
    state.managementReceipt = '';
    write([receipt, notice, management ? `共 ${resourceCount} 个平台资源，${versionCount} 个版本。` : `共 ${versionCount} 个可用版本。`,
      management && !canManage ? '部分授权信息暂不可读；已下架／回收站资源也暂不可读，列表可能不完整，请刷新重试。' : '',
      management && results[2]?.status !== 'fulfilled' ? '数据目录暂不可读。' : ''].filter(Boolean).join(' '));
  }
  function leaveLibrary() { state.libraryEpoch += 1; }
  function init(config) {
    Object.assign(state, config || {});
    const form = $('#assignment-form'); if (form) form.onsubmit = preview;
    const dispatchButton = $('#assignment-dispatch'); if (dispatchButton) dispatchButton.onclick = dispatch;
    const mode = $('#assignment-mode'); if (mode) mode.onchange = () => { updateMode(); resetPreview(); };
    const refresh = $('#resource-library-refresh'); if (refresh) refresh.onclick = () => loadLibrary();
    const managerRefresh = $('#resource-management-refresh'); if (managerRefresh) managerRefresh.onclick = () => loadManagement();
    const parent = $('#assignment-parent'); if (parent) parent.onchange = () => { if (parent.value) fillVersion((state.tasks || []).find(task => task.task_id === parent.value)); else resetPreview(); };
    const localForm = $('#local-algo-form');
    if (localForm) {
      localForm.onsubmit = async event => {
        event.preventDefault();
        const btn = $('#local-algo-create'), msg = $('#local-algo-message');
        if (btn) btn.disabled = true;
        try {
          const paramsSpec = parseOptionalJsonObject($('#local-algo-params')?.value, '参数规范');
          const inputsSpec = parseOptionalJsonObject($('#local-algo-inputs')?.value, '输入端口');
          const outputsSpec = parseOptionalJsonObject($('#local-algo-outputs')?.value, '输出端口');
          await state.post('/api/local-algorithms', {
            name: $('#local-algo-name')?.value.trim() || '',
            version: $('#local-algo-version')?.value.trim() || '1.0.0',
            tags: names($('#local-algo-tags')?.value),
            description: $('#local-algo-description')?.value.trim() || '',
            plugin_py: $('#local-algo-code')?.value || '',
            ...(paramsSpec ? {parameters: paramsSpec} : {}),
            ...(inputsSpec ? {inputs: inputsSpec} : {}),
            ...(outputsSpec ? {outputs: outputsSpec} : {}),
          });
          localForm.reset?.();
          if ($('#local-algo-version')) $('#local-algo-version').value = '1.0.0';
          if (msg) msg.textContent = '本地算法创建成功，可加入已有项目。';
          await renderLocalAlgorithms();
        } catch (error) { if (msg) msg.textContent = error.message; }
        finally { if (btn) btn.disabled = false; }
      };
    }
    updateMode();
    ['#assignment-name', '#assignment-template', '#assignment-targets', '#assignment-instructions', '#assignment-algorithms', '#assignment-datasets'].forEach(selector => {
      const node = $(selector); if (node) node.oninput = node.onchange = resetPreview;
    });
  }
  window.ResearchKitAssignments = {
    init, enterAssignments: load, loadLibrary, loadManagement, setManagementModule, prepareVersion, leaveLibrary, preview, dispatch,
    _test: {state, names, payload, renderPreview, filterByTags, createTagFilterBar, loadPersonalAlgorithmVisibility, setPersonalAlgorithmVisibility},
  };
})();
