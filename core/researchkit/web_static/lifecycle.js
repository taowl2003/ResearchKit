(() => {
  'use strict';

  const STATE_LABELS = {active: '使用中', retired: '已下架', trashed: '已移入回收站'};
  const KIND_LABELS = {algorithm: '算法', template: '案例模板', dataset: '数据集'};
  const REFERENCE_TYPE_LABELS = {
    'fixed-submission': '固定提交', 'platform-template': '平台案例模板', 'asset-candidate': '资产候选',
    'template-instance': '模板创建的项目', distribution: '分发任务', 'package-bundle': '教师包', package_bundle: '教师包',
    'trusted-baseline': '可信基线', 'package-baseline': '教师包基线', 'derived-dataset': '派生数据',
    'web-project-summary': '网页项目摘要', project: '项目', submission: '固定提交',
  };
  const HISTORY_ACTION_LABELS = {retire: '下架', retired: '下架', restore: '恢复', trash: '移入回收站', trashed: '移入回收站'};
  const text = (node, value) => { node.textContent = value == null ? '' : String(value); };
  const node = (tag, className = '') => { const item = document.createElement(tag); item.className = className; return item; };
  const paragraph = (host, value, className = '') => { const item = node('p', className); text(item, value); host.append(item); return item; };
  const button = (label, action) => { const item = node('button'); item.type = 'button'; text(item, label); item.onclick = action; return item; };
  const referenceLabel = reference => {
    const fields = [reference?.label, reference?.owner_username && `账号：${reference.owner_username}`,
      reference?.project_id && `项目：${reference.project_id}`, reference?.submission_id && `提交：${reference.submission_id}`,
      reference?.detail].filter(Boolean);
    return fields.join(' · ') || '未命名引用';
  };

  /*
   * The caller owns the surrounding card.  This small isolated panel owns only
   * its own pending requests, so a late response for one asset cannot replace
   * another card or a newly selected dataset detail.
   */
  function append(host, options) {
    if (!host || !options?.api || !options.kind || !options.ref) return null;
    const panel = node('section', 'asset-lifecycle');
    const title = node('h3'); text(title, '引用与清理'); panel.append(title);
    const summary = paragraph(panel, `状态：${STATE_LABELS[options.lifecycle?.state] || '正在读取'}。`);
    const controls = node('div', 'actions'); panel.append(controls);
    let report = null, requestEpoch = 0, loading = false;

    const path = offset => `/api/asset-lifecycle?kind=${encodeURIComponent(options.kind)}&ref=${encodeURIComponent(options.ref)}&offset=${offset}&limit=50`;
    const currentRevision = () => report?.revision || options.lifecycle?.revision || null;
    const setBusy = value => { loading = value; [...(controls.children || [])].forEach(item => { item.disabled = value || item.dataset?.lifecycleDisabled === 'true'; }); };
    const writeMessage = value => { if (typeof options.message === 'function') options.message(value); else paragraph(panel, value, 'asset-lifecycle-message'); };

    function renderReport(value) {
      report = value;
      const state = value?.state || options.lifecycle?.state || 'active';
      text(summary, `状态：${STATE_LABELS[state] || state}。已查到 ${Number(value?.reference_count || 0)} 项引用。`);
      controls.replaceChildren();
      const references = Array.isArray(value?.references) ? value.references : [];
      const referencesHost = node('div', 'asset-lifecycle-references');
      if (references.length) {
        const list = node('ul');
        references.forEach(item => { const entry = node('li'); text(entry, `${REFERENCE_TYPE_LABELS[item.type] || item.type || '引用'}：${referenceLabel(item)}`); list.append(entry); });
        referencesHost.append(list);
      } else paragraph(referencesHost, '未查到平台内引用。未提交的 IDE 修改和平台外程序不在本次检查范围内。', 'asset-lifecycle-note');
      (value?.notes || []).forEach(note => paragraph(referencesHost, note, 'asset-lifecycle-note'));
      const offset = Number(value?.offset || 0), limit = Number(value?.limit || 50), count = Number(value?.reference_count || 0);
      if (!value?.can_trash) paragraph(referencesHost, '存在未能完成的核对，不能移入回收站。请查看上方说明。', 'asset-lifecycle-warning');
      else if (count) paragraph(referencesHost, '移入回收站会保留这些引用与原文件；永久清理时才会逐项核对依赖。', 'asset-lifecycle-note');
      if (offset > 0) referencesHost.append(button('上一页引用', () => load(Math.max(0, offset - limit))));
      if (offset + references.length < count) referencesHost.append(button('下一页引用', () => load(offset + limit)));
      const history = Array.isArray(value?.history) ? value.history.slice(0, 10) : [];
      if (history.length) {
        const log = node('details', 'asset-lifecycle-history');
        const summary = node('summary'); text(summary, `最近操作记录（${history.length}）`); log.append(summary);
        history.forEach(item => paragraph(log, `${HISTORY_ACTION_LABELS[item.action] || item.action || '操作'} · ${item.actor_username || '未知账号'} · ${item.created_at || '时间未知'}`));
        referencesHost.append(log);
      }
      panel.querySelector?.('.asset-lifecycle-references')?.remove?.();
      panel.append(referencesHost);
      controls.append(button('刷新引用', () => load(offset)));
      if (state === 'active') controls.append(button('下架', () => change('retire')));
      if (state === 'retired' || state === 'trashed') controls.append(button('恢复', () => change('restore')));
      if (state !== 'trashed') {
        const trash = button('移入回收站', () => change('trash'));
        trash.disabled = !value?.can_trash;
        if (!value?.can_trash) trash.dataset.lifecycleDisabled = 'true';
        controls.append(trash);
      }
      if (state === 'trashed') controls.append(button('去回收站永久清理', () => {
        document.dispatchEvent(new CustomEvent('researchkit:show-cleanup', {detail: {state: 'trashed'}}));
      }));
    }
    async function load(offset = 0) {
      const epoch = ++requestEpoch; setBusy(true);
      try {
        const value = await options.api(path(offset));
        if (epoch !== requestEpoch) return;
        renderReport(value || {});
      } catch (error) {
        if (epoch === requestEpoch) writeMessage(`无法读取${KIND_LABELS[options.kind] || '资产'}引用：${error.message || '请求失败'}`);
      } finally { if (epoch === requestEpoch) setBusy(false); }
    }
    async function change(action) {
      if (loading || !currentRevision()) return;
      const epoch = ++requestEpoch; setBusy(true);
      try {
        const value = await options.api('/api/asset-lifecycle', {method: 'PUT', body: JSON.stringify({kind: options.kind, ref: options.ref, action, expected_revision: currentRevision()})});
        if (epoch !== requestEpoch) return;
        renderReport(value || {});
        const labels = {
          retire: '已下架：仅从新的选择列表隐藏，保留原文件和历史记录；下架不撤销授权。',
          restore: '已恢复。',
          trash: '已移入回收站：仅从新的选择列表隐藏，保留原文件和历史记录；下架不撤销授权。',
        };
        writeMessage(labels[action]);
        if (typeof options.onChanged === 'function') await options.onChanged(value || {});
      } catch (error) {
        if (epoch === requestEpoch) writeMessage(action === 'trash' && error?.status === 409
          ? `不能移入回收站：${error.message}` : `操作未完成：${error.message || '请求失败'}`);
      } finally { if (epoch === requestEpoch) setBusy(false); }
    }
    const inspect = button('查看引用与清理', () => load(0));
    controls.append(inspect);
    if (options.lifecycle?.state && options.lifecycle.state !== 'active') text(summary, `状态：${STATE_LABELS[options.lifecycle.state] || options.lifecycle.state}。`);
    host.append(panel);
    return {load, panel};
  }

  window.ResearchKitLifecycle = {append, STATE_LABELS, KIND_LABELS};
})();
