(() => {
  'use strict';

  const KIND_LABELS = {algorithm: '算法', template: '案例模板', dataset: '数据集'};
  const SOURCE_LABELS = {central: '平台自带（中央库）', web: '网页发布', system: '系统库', raw: '原始数据', derived: '派生数据'};
  const STATE_LABELS = {retired: '已下架', trashed: '回收站'};
  const text = (host, value) => { host.textContent = value == null ? '' : String(value); };
  const make = (tag, className = '') => { const value = document.createElement(tag); value.className = className; return value; };
  const paragraph = (host, value, className = '') => { const item = make('p', className); text(item, value); host.append(item); return item; };
  const button = (label, action) => { const item = make('button'); item.type = 'button'; text(item, label); item.onclick = action; return item; };
  const itemKey = item => `${item.kind}:${item.ref}`;

  let config = null;
  const state = {
    page: null, kind: 'all', query: '', offset: 0, limit: 50, selected: new Map(),
    listEpoch: 0, previewEpoch: 0, pollEpoch: 0, detailEpoch: 0, pollTimer: null, plan: null, previewButton: null,
  };

  const isTeacher = () => config?.getMe?.()?.role === 'teacher';
  const section = () => document.querySelector(`#${state.page === 'trashed' ? 'cleanup-bin' : 'retired-bin'}`);
  const controls = () => ({
    list: document.querySelector(`#${state.page === 'trashed' ? 'cleanup-bin' : 'retired-bin'}-list`),
    message: document.querySelector(`#${state.page === 'trashed' ? 'cleanup-bin' : 'retired-bin'}-message`),
    pagination: document.querySelector(`#${state.page === 'trashed' ? 'cleanup-bin' : 'retired-bin'}-pagination`),
    detail: document.querySelector(`#${state.page === 'trashed' ? 'cleanup-bin' : 'retired-bin'}-detail`),
  });
  const write = value => { const target = controls().message; if (target) text(target, value); };
  function invalidatePreview(hide = true) {
    state.previewEpoch += 1; state.plan = null;
    if (hide) { const detail = controls().detail; if (detail) { detail.hidden = true; detail.replaceChildren(); } }
  }
  const clearSelection = () => { state.selected.clear(); invalidatePreview(); };
  const active = page => page === state.page && Boolean(section()) && !section().hidden;
  function stopPolling() { if (state.pollTimer) clearTimeout(state.pollTimer); state.pollTimer = null; state.pollEpoch += 1; }
  const cleanupInProgress = item => item?.cleanup_status === 'queued' || item?.cleanup_status === 'running';
  const cleanupFailed = item => item?.cleanup_status === 'failed';
  const cleanupLocked = item => Boolean(item?.request_id);
  const cleanupStatusLabel = item => ({queued: '清理任务已排队', running: '正在清理', completed: '清理已完成', failed: '清理失败'})[item?.cleanup_status] || '';
  const itemSignature = items => (items || []).map(item => `${item.kind}:${item.ref}`).sort().join('\n');
  const planMatchesSelection = plan => plan === state.plan && itemSignature(plan?.items) === itemSignature(selectedItems()) && (plan?.items || []).length === state.selected.size;

  async function showCleanupProgress(item) {
    if (!item?.request_id || !config || !isTeacher()) return;
    const epoch = ++state.detailEpoch, page = state.page;
    write('正在读取清理进度…');
    try {
      const value = await config.api(`/api/asset-cleanup/${encodeURIComponent(item.request_id)}`);
      if (epoch !== state.detailEpoch || !active(page)) return;
      const updated = {...item, cleanup_status: value?.status || item.cleanup_status, request_id: value?.request_id || item.request_id, cleanup_error: value?.error || ''};
      renderDetails(updated);
      if (updated.cleanup_status === 'completed') { write('永久清理已完成。个人项目、固定提交和历史记录未被顺带删除。'); await load(true); }
      else write(updated.cleanup_status === 'failed' ? `清理失败：${updated.cleanup_error || '服务未返回原因'}` : cleanupStatusLabel(updated));
    } catch (error) { if (epoch === state.detailEpoch && active(page)) write(`无法读取清理进度：${error.message || '请求失败'}`); }
  }

  async function retryCleanup(item, trigger) {
    if (!item?.request_id || !cleanupFailed(item) || !config || !isTeacher()) return;
    const epoch = ++state.detailEpoch, page = state.page; trigger.disabled = true;
    try {
      const value = await config.post(`/api/asset-cleanup/${encodeURIComponent(item.request_id)}/retry`, {});
      if (epoch !== state.detailEpoch || !active(page)) return;
      const updated = {...item, cleanup_status: value?.status || 'queued', request_id: value?.request_id || item.request_id, cleanup_error: value?.error || ''};
      renderDetails(updated);
      write('已重试同一批已确认的清理项目；重试不会扩大清理范围。');
      await handleExecution({...value, status: updated.cleanup_status, request_id: updated.request_id});
    } catch (error) { if (epoch === state.detailEpoch && active(page)) { write(`重试未启动：${error.message || '请求失败'}`); trigger.disabled = false; } }
  }

  function renderDetails(item) {
    const host = controls().detail;
    if (!host || !item) return;
    host.hidden = false; host.replaceChildren();
    const title = make('h2'); text(title, `${KIND_LABELS[item.kind] || item.kind}：${item.name || item.ref}`); host.append(title);
    paragraph(host, `状态：${STATE_LABELS[item.state] || item.state} · 来源：${SOURCE_LABELS[item.source] || item.source || '未知'} · 引用：${item.ref}`, 'path');
    if (item.request_id) {
      paragraph(host, `${cleanupStatusLabel(item)}${item.cleanup_error ? `：${item.cleanup_error}` : ''}` || '存在已提交的清理任务。', cleanupInProgress(item) || cleanupFailed(item) ? 'asset-lifecycle-warning' : 'asset-lifecycle-note');
      const progress = button('查看清理进度', () => showCleanupProgress(item)); host.append(progress);
      if (cleanupFailed(item)) {
        const retry = button('重试这次清理', () => retryCleanup(item, retry)); host.append(retry);
        paragraph(host, '重试只会处理这次已确认的同一批资产，不会重新扩大清理范围。');
      }
    }
    if (item.state === 'retired' && !cleanupLocked(item)) {
      paragraph(host, '恢复后会重新出现在新的选择列表中；已有文件、授权和历史记录不会改变。');
      const restore = button('恢复', async () => {
        restore.disabled = true;
        try {
          await config.api('/api/asset-lifecycle', {method: 'PUT', body: JSON.stringify({kind: item.kind, ref: item.ref, action: 'restore', expected_revision: item.revision})});
          write('已恢复。'); host.hidden = true; await load();
        } catch (error) { write(`恢复未完成：${error.message || '请求失败'}`); restore.disabled = false; }
      });
      host.append(restore);
    } else if (item.state !== 'retired') {
      paragraph(host, '永久清理只可在回收站列表勾选后执行。系统会先核对引用和待删除内容；不会顺带删除个人项目、固定提交或历史记录。');
    }
    if (!cleanupLocked(item) && window.ResearchKitLifecycle?.append) {
      window.ResearchKitLifecycle.append(host, {kind: item.kind, ref: item.ref, lifecycle: item, api: config.api,
        message: write, onChanged: async () => { host.hidden = true; await load(); }});
    }
  }

  function renderPlan(value) {
    const host = controls().detail;
    if (!host) return;
    host.hidden = false; host.replaceChildren();
    const title = make('h2'); text(title, '永久清理预览'); host.append(title);
    paragraph(host, '以下预览只处理回收站中勾选的资产。不会顺带删除个人项目、固定提交或历史记录。');
    (value.notes || []).forEach(note => paragraph(host, note, 'asset-lifecycle-note'));
    const list = make('div', 'cards');
    (value.items || []).forEach(entry => {
      const row = make('article', 'card');
      const name = make('h3'); text(name, `${KIND_LABELS[entry.kind] || entry.kind}：${entry.name || entry.ref}`); row.append(name);
      paragraph(row, entry.can_delete ? `可清理 · 预计 ${Number(entry.estimated_bytes || 0)} 字节` : '暂不能清理。');
      (entry.blockers || []).forEach(blocker => paragraph(row, `阻挡：${blocker.label || '未知关联'}${blocker.detail ? ` · ${blocker.detail}` : ''}${blocker.suggestion ? `。建议：${blocker.suggestion}` : ''}`, 'asset-lifecycle-warning'));
      (entry.advisories || []).forEach(note => paragraph(row, note, 'asset-lifecycle-note'));
      list.append(row);
    });
    host.append(list);
    if (Array.isArray(value.order) && value.order.length) paragraph(host, `建议处理顺序：${value.order.map(entry => `${KIND_LABELS[entry.kind] || entry.kind}：${entry.ref}`).join(' → ')}`);
    paragraph(host, `预计清理内容：${Number(value.total_bytes || 0)} 字节。`);
    if (!value.can_execute) { paragraph(host, '当前预览存在阻挡项，不能永久清理。请先按建议处理关联对象，然后重新预览。', 'asset-lifecycle-warning'); return; }
    const form = make('form', 'asset-review-form');
    const label = make('label'); text(label, '输入“永久清理”后确认');
    const input = make('input'); input.required = true; input.maxLength = 20; input.placeholder = '永久清理'; label.append(input);
    const execute = make('button'); execute.type = 'submit'; text(execute, '永久清理所选回收站资产'); form.append(label, execute);
    form.onsubmit = async event => {
      event.preventDefault();
      if (input.value.trim() !== '永久清理') { write('请准确输入“永久清理”后再确认。'); return; }
      if (!planMatchesSelection(value)) { invalidatePreview(); write('选择内容或预览已变化，请重新预览后再确认。'); return; }
      const epoch = ++state.detailEpoch, page = state.page, items = selectedItems();
      execute.disabled = input.disabled = true;
      try {
        const response = await config.post('/api/asset-cleanup/execute', {items, plan_token: state.plan.plan_token, confirmation: '永久清理'});
        if (epoch !== state.detailEpoch || !active(page)) return;
        await handleExecution(response);
      } catch (error) { if (epoch === state.detailEpoch && active(page)) { write(`永久清理未启动：${error.message || '请求失败'}`); execute.disabled = input.disabled = false; } }
    };
    host.append(form);
  }

  function render(items, total) {
    const {list, pagination, detail} = controls();
    if (!list || !pagination) return;
    list.replaceChildren(); pagination.replaceChildren(); if (detail) detail.hidden = true;
    if (!items.length) paragraph(list, '当前没有符合条件的资产。');
    items.forEach(item => {
      const row = make('article', 'card'), title = make('h3'); text(title, `${KIND_LABELS[item.kind] || item.kind}：${item.name || item.ref}`); row.append(title);
      paragraph(row, `来源：${SOURCE_LABELS[item.source] || item.source || '未知'} · 引用：${item.ref}`, 'path');
      const actions = make('div', 'actions');
      actions.append(button('打开', () => renderDetails(item)));
      if (state.page === 'trashed') {
        const label = make('label', 'inline-check'), choice = make('input'); choice.type = 'checkbox'; choice.checked = state.selected.has(itemKey(item));
        choice.disabled = cleanupLocked(item) || (!choice.checked && state.selected.size >= 30);
        choice.onchange = () => {
          if (choice.checked) {
            if (state.selected.size >= 30 && !state.selected.has(itemKey(item))) { choice.checked = false; write('每批最多选择 30 项。请先取消一项，再选择其他资产。'); return; }
            state.selected.set(itemKey(item), {kind: item.kind, ref: item.ref});
          } else state.selected.delete(itemKey(item));
          invalidatePreview(); updatePreviewButton();
        };
        label.append(choice, (() => { const value = make('span'); text(value, cleanupLocked(item) ? '已有清理任务，不能重新选择' : choice.disabled ? '本批最多 30 项' : '选择永久清理'); return value; })()); actions.append(label);
      }
      if (item.request_id) paragraph(row, `${cleanupStatusLabel(item)}${item.cleanup_error ? `：${item.cleanup_error}` : ''}`, cleanupInProgress(item) || cleanupFailed(item) ? 'asset-lifecycle-warning' : 'asset-lifecycle-note');
      row.append(actions); list.append(row);
    });
    if (state.offset > 0) pagination.append(button('上一页', () => { state.offset = Math.max(0, state.offset - state.limit); load(); }));
    if (state.offset + items.length < Number(total || 0)) pagination.append(button('下一页', () => { state.offset += state.limit; load(); }));
    if (state.page === 'trashed') {
      const preview = button(`预览永久清理（已选 ${state.selected.size} 项）`, previewCleanup); preview.id = 'cleanup-preview'; preview.disabled = !state.selected.size; state.previewButton = preview; pagination.append(preview);
    }
  }
  function updatePreviewButton() { const item = state.previewButton || document.querySelector('#cleanup-preview'); if (item) { item.disabled = !state.selected.size; text(item, `预览永久清理（已选 ${state.selected.size} 项）`); } }
  const selectedItems = () => [...state.selected.values()];

  async function load(preserveMessage = false) {
    if (!config || !isTeacher() || !active(state.page)) return;
    const epoch = ++state.listEpoch, params = new URLSearchParams({state: state.page, kind: state.kind, q: state.query, offset: String(state.offset), limit: String(state.limit)});
    if (!preserveMessage) write('正在读取…');
    try {
      const value = await config.api(`/api/asset-bin?${params.toString()}`);
      if (epoch !== state.listEpoch || !active(state.page)) return;
      render(Array.isArray(value?.items) ? value.items : [], value?.total);
      if (!preserveMessage) write(`共 ${Number(value?.total || 0)} 项${STATE_LABELS[state.page] || ''}资产。`);
    } catch (error) { if (epoch === state.listEpoch && active(state.page)) write(`无法读取列表：${error.message || '请求失败'}`); }
  }

  async function previewCleanup() {
    if (!selectedItems().length || !active('trashed')) return;
    if (state.selected.size > 30) { write('每批最多选择 30 项。'); return; }
    const epoch = ++state.previewEpoch, items = selectedItems(); write('正在核对永久清理范围…');
    try {
      const value = await config.post('/api/asset-cleanup/preview', {items});
      if (epoch !== state.previewEpoch || !active('trashed')) return;
      state.plan = value || null; renderPlan(value || {}); write(value?.can_execute ? '请核对预览，再输入确认文字。' : '发现关联或核对阻挡，当前不能永久清理。');
    } catch (error) { if (epoch === state.previewEpoch && active('trashed')) write(`无法生成清理预览：${error.message || '请求失败'}`); }
  }
  async function poll(requestId) {
    const epoch = ++state.pollEpoch;
    const check = async () => {
      try {
        const value = await config.api(`/api/asset-cleanup/${encodeURIComponent(requestId)}`);
        if (epoch !== state.pollEpoch || !active('trashed')) return;
        await handleExecution(value, true);
      } catch (error) { if (epoch === state.pollEpoch && active('trashed')) write(`无法读取清理状态：${error.message || '请求失败'}`); }
    };
    await check();
  }
  async function handleExecution(value, fromPoll = false) {
    const status = value?.status;
    if (status === 'queued' || status === 'running') {
      write(status === 'queued' ? '永久清理任务已排队。' : '永久清理任务正在执行。');
      if (!fromPoll) { clearSelection(); await load(true); }
      if (!fromPoll && value?.request_id) await poll(value.request_id);
      else if (value?.request_id && active('trashed')) { stopPolling(); state.pollTimer = setTimeout(() => { poll(value.request_id); }, 1000); }
      return;
    }
    stopPolling();
    if (status === 'completed') { clearSelection(); write('永久清理已完成。个人项目、固定提交和历史记录未被顺带删除。'); await load(true); return; }
    write(`永久清理未完成：${value?.error || '服务未返回完成结果'}`);
    clearSelection(); await load(true);
  }

  function bind(page) {
    const prefix = page === 'trashed' ? 'cleanup-bin' : 'retired-bin';
    const kind = document.querySelector(`#${prefix}-kind`), query = document.querySelector(`#${prefix}-query`);
    if (kind) kind.onchange = () => { state.kind = kind.value || 'all'; state.offset = 0; clearSelection(); load(); };
    if (query) query.oninput = () => { state.query = query.value || ''; state.offset = 0; clearSelection(); load(); };
  }
  function configure(value) { config = value; bind('retired'); bind('trashed'); }
  function enter(page) {
    if (!config || !isTeacher()) return;
    stopPolling(); state.detailEpoch += 1; state.page = page === 'trashed' ? 'trashed' : 'retired'; state.kind = 'all'; state.query = ''; state.offset = 0; clearSelection();
    const prefix = state.page === 'trashed' ? 'cleanup-bin' : 'retired-bin';
    const kind = document.querySelector(`#${prefix}-kind`), query = document.querySelector(`#${prefix}-query`); if (kind) kind.value = 'all'; if (query) query.value = '';
    load();
  }
  function leave() { stopPolling(); state.listEpoch += 1; state.previewEpoch += 1; state.detailEpoch += 1; }
  window.ResearchKitCleanup = {configure, enter, leave, load};
})();
