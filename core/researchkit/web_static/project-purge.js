(() => {
  'use strict';

  const KIND_LABELS = {
    template: '已发布案例',
    algorithm: '已发布算法',
    derived_dataset: '衍生数据集',
    dataset: '数据集',
    task_reset: '任务状态重置',
    project_jobs: '运行记录',
    project_submissions: '版本提交',
    project_record: '项目记录',
    workspace_copy: '本地工作区副本',
  };

  let config = null;
  let onComplete = null;
  const state = {
    projectId: null,
    preview: null,
    selectedKeys: new Set(),
    resetTask: true,
    busy: false,
  };

  const $ = selector => document.querySelector(selector);
  const make = (tag, className = '', textContent = null) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (textContent != null) node.textContent = String(textContent);
    return node;
  };

  function ensureDialog() {
    let dialog = $('#project-purge-dialog');
    if (dialog) return dialog;
    dialog = make('dialog', 'purge-dialog');
    dialog.id = 'project-purge-dialog';
    document.body.append(dialog);
    return dialog;
  }

  function renderModal(errorText = '', receipt = null) {
    const dialog = ensureDialog();
    dialog.replaceChildren();
    const box = make('div', 'purge-dialog-body');
    const header = make('div', 'purge-dialog-header');
    const title = make('h2', '', receipt ? '按项目清理结果' : '按项目清理（三步确认）');
    const closeBtn = make('button', '', '关闭');
    closeBtn.type = 'button';
    closeBtn.disabled = state.busy;
    closeBtn.onclick = () => dialog.close();
    header.append(title, closeBtn);
    box.append(header);

    if (errorText) {
      box.append(make('p', 'action-result error', errorText));
    }

    if (receipt) {
      const statusCls = receipt.status === 'completed' ? 'action-result success' : 'action-result error';
      box.append(make('p', statusCls, `项目「${receipt.project_name || receipt.project_id}」清理完成（审计编号：${receipt.audit_id}）。`));
      const grid = make('div', 'purge-summary-grid');

      const delSec = make('section', 'purge-panel purge-panel-delete');
      delSec.append(make('h3', '', `成功删除（${(receipt.deleted || []).length}）`));
      const delList = make('ul');
      (receipt.deleted || []).forEach(item => {
        delList.append(make('li', '', `${KIND_LABELS[item.kind] || item.kind}：${item.name || item.ref}`));
      });
      if (!(receipt.deleted || []).length) delList.append(make('li', '', '无'));
      delSec.append(delList);

      const retSec = make('section', 'purge-panel purge-panel-retain');
      retSec.append(make('h3', '', `已保留内容（${(receipt.retained || []).length}）`));
      const retList = make('ul');
      (receipt.retained || []).forEach(item => {
        retList.append(make('li', '', `${KIND_LABELS[item.kind] || item.kind}：${item.name || item.ref}（${item.reason || '已保留'}）`));
      });
      if (!(receipt.retained || []).length) retList.append(make('li', '', '无'));
      retSec.append(retList);

      grid.append(delSec, retSec);
      box.append(grid);

      if ((receipt.failed || []).length) {
        const failSec = make('section', 'purge-panel purge-panel-warn');
        failSec.append(make('h3', '', `失败项目（${receipt.failed.length}）`));
        const failList = make('ul');
        receipt.failed.forEach(item => {
          failList.append(make('li', '', `${item.ref || item.kind}：${item.error || '清理失败'}`));
        });
        failSec.append(failList);
        box.append(failSec);
      }

      const footer = make('div', 'purge-dialog-footer');
      const doneBtn = make('button', '', '完成并刷新列表');
      doneBtn.type = 'button';
      doneBtn.onclick = () => { dialog.close(); if (onComplete) onComplete(receipt); };
      footer.append(doneBtn);
      box.append(footer);
      dialog.append(box);
      return;
    }

    const preview = state.preview;
    if (!preview) {
      box.append(make('p', '', '正在检查项目依赖、提交记录、衍生数据与入库资产，请稍候…'));
      dialog.append(box);
      return;
    }

    const p = preview.project || {};

    // Section 1: Project private content
    const sec1 = make('section', 'purge-panel');
    sec1.append(make('h3', '', '第一步 / 1. 项目自身内容（将连带清理）'));
    sec1.append(make('p', 'path', `项目名称：${p.project_name} · 所有者：${p.owner_username} · 项目 ID：${p.project_id}`));
    const wsPath = p.workspace?.in_workspace ? (p.workspace.relative_path || p.workspace.path) : '当前登录用户本地工作区中无此副本目录';
    sec1.append(make('p', 'path', `个人工作区本地副本：${wsPath}（管理端清理仅删除平台中央记录，个人目录请由各自工作区的“删除本地副本”处理）`));
    sec1.append(make('p', '', `包含：版本提交 ${p.submission_count || 0} 次（其中固定提交 ${p.fixed_submission_count || 0} 次）、快照文件 ${p.file_count || 0} 个（${p.file_bytes || 0} 字节）、运行记录 ${p.job_count || 0} 条、产物索引 ${p.artifact_count || 0} 个。`));
    if (p.has_task_link) {
      const taskLabel = make('label', 'inline-check purge-check');
      const taskCb = make('input');
      taskCb.type = 'checkbox';
      taskCb.checked = state.resetTask;
      taskCb.disabled = state.busy;
      taskCb.onchange = () => { state.resetTask = taskCb.checked; refreshPreview(); };
      const taskNames = (p.task_links || []).map(x => x.task_name || x.source_project_name || x.distribution_id).filter(Boolean).join('、');
      taskLabel.append(taskCb, document.createTextNode(` 将关联任务/分发（${taskNames || '任务记录'}）恢复为“待领取/可重新开始”（保留教师任务本身）`));
      sec1.append(taskLabel);
    }
    box.append(sec1);

    // Section 2: Produced items (selectable)
    const sec2 = make('section', 'purge-panel');
    sec2.append(make('h3', '', '第二步 / 2. 本项目产出的关联资产（由管理员勾选删除或保留）'));
    const produced = preview.produced_items || [];
    if (!produced.length) {
      sec2.append(make('p', 'dataset-note', '该项目尚未产出已发布案例、已发布算法或衍生数据集。'));
    } else {
      produced.forEach(item => {
        const row = make('div', `purge-item ${item.can_delete ? '' : 'purge-item-blocked'}`);
        const label = make('label', 'inline-check purge-check');
        const cb = make('input');
        cb.type = 'checkbox';
        cb.checked = Boolean(item.selected);
        cb.disabled = state.busy || !item.can_delete;
        cb.onchange = () => {
          if (cb.checked) state.selectedKeys.add(item.key);
          else state.selectedKeys.delete(item.key);
          refreshPreview();
        };
        const statusLabel = item.kind === 'derived_dataset' ? '衍生数据' : (item.status === 'published' ? '已正式入库' : '候选副本');
        label.append(cb, make('strong', '', `[${KIND_LABELS[item.kind] || item.kind}] ${item.name || item.ref} `), make('span', 'badge', statusLabel));
        row.append(label);
        if (item.selection_blocker) {
          row.append(make('p', 'asset-lifecycle-warning', `阻挡：${item.selection_blocker}`));
        }
        (item.blockers || []).forEach(b => {
          row.append(make('p', 'asset-lifecycle-warning', `保留原因：${b.label}${b.detail ? '（' + b.detail + '）' : ''}。建议：${b.suggestion || ''}`));
        });
        (item.advisories || []).forEach(note => {
          row.append(make('p', 'dataset-note', note));
        });
        sec2.append(row);
      });
    }
    box.append(sec2);

    // Section 3: Protected shared items
    const sec3 = make('section', 'purge-panel purge-panel-retain');
    sec3.append(make('h3', '', '第二步 / 3. 默认保留的共享与外部资源（受保护 · 绝不删除）'));
    const shared = preview.retained_shared_items || [];
    if (!shared.length) {
      sec3.append(make('p', 'dataset-note', '未检测到额外的共享数据集或共享算法绑定。'));
    } else {
      const ul = make('ul');
      shared.forEach(item => {
        ul.append(make('li', '', `[${KIND_LABELS[item.kind] || item.kind}] ${item.ref} — ${item.reason}`));
      });
      sec3.append(ul);
    }
    box.append(sec3);

    // Step 3 Summary & Confirmation
    const sec4 = make('section', 'purge-panel');
    sec4.append(make('h3', '', '第三步 / 最终确认（按依赖顺序执行，执行前二次复核引用）'));
    (preview.global_blockers || []).forEach(b => {
      sec4.append(make('p', 'asset-lifecycle-warning', `全局阻挡：${b.label}${b.detail ? '（' + b.detail + '）' : ''}。${b.suggestion || ''}`));
    });
    const orderList = make('ol', 'purge-order');
    (preview.execution_order || []).forEach(step => {
      orderList.append(make('li', '', step.label));
    });
    sec4.append(make('p', '', '将按以下顺序删除或重置：'), orderList);
    if ((preview.will_retain_produced || []).length) {
      sec4.append(make('p', 'dataset-note', `本次明确保留（不删除）的产出资产：${preview.will_retain_produced.map(x => x.ref).join('、')}`));
    }

    const form = make('form', 'asset-review-form');
    const confirmLabel = make('label', '', `输入“${preview.confirmation_phrase || '确认清理项目'}”后执行`);
    const confirmInput = make('input');
    confirmInput.required = true;
    confirmInput.maxLength = 30;
    confirmInput.placeholder = preview.confirmation_phrase || '确认清理项目';
    confirmInput.disabled = state.busy || !preview.can_execute;
    confirmLabel.append(confirmInput);
    const submitBtn = make('button', 'danger-btn', '确认执行按项目清理');
    submitBtn.type = 'submit';
    submitBtn.disabled = state.busy || !preview.can_execute;
    form.append(confirmLabel, submitBtn);
    form.onsubmit = async event => {
      event.preventDefault();
      if (confirmInput.value.trim() !== (preview.confirmation_phrase || '确认清理项目')) {
        renderModal(`请准确输入“${preview.confirmation_phrase || '确认清理项目'}”后再执行。`);
        return;
      }
      await executePurge();
    };
    sec4.append(form);
    box.append(sec4);

    dialog.append(box);
  }

  async function refreshPreview() {
    if (!config || !state.projectId) return;
    state.busy = true;
    renderModal();
    try {
      const preview = await config.post(`/api/projects/${encodeURIComponent(state.projectId)}/purge-preview`, {
        selected_keys: Array.from(state.selectedKeys),
        reset_task: state.resetTask,
      });
      state.preview = preview;
      state.selectedKeys = new Set((preview.produced_items || []).filter(x => x.selected).map(x => x.key));
      state.resetTask = Boolean(preview.project?.reset_task);
      state.busy = false;
      renderModal();
    } catch (error) {
      state.busy = false;
      renderModal(error?.message || '无法刷新清理预览');
    }
  }

  async function executePurge() {
    if (!config || !state.projectId || !state.preview) return;
    state.busy = true;
    renderModal();
    try {
      const receipt = await config.post(`/api/projects/${encodeURIComponent(state.projectId)}/purge`, {
        plan_token: state.preview.plan_token,
        confirmation: state.preview.confirmation_phrase || '确认清理项目',
        selected_keys: Array.from(state.selectedKeys),
        reset_task: state.resetTask,
      });
      state.busy = false;
      renderModal('', receipt);
      if (onComplete) onComplete(receipt);
    } catch (error) {
      state.busy = false;
      renderModal(error?.message || '按项目清理执行失败');
    }
  }

  async function open(projectId, completeCallback = null) {
    if (!config || !projectId) return;
    onComplete = completeCallback;
    state.projectId = projectId;
    state.preview = null;
    state.selectedKeys = new Set();
    state.resetTask = true;
    state.busy = true;
    const dialog = ensureDialog();
    renderModal();
    if (typeof dialog.showModal === 'function' && !dialog.open) dialog.showModal();
    else dialog.setAttribute('open', 'open');
    try {
      const preview = await config.api(`/api/projects/${encodeURIComponent(projectId)}/purge-preview`);
      state.preview = preview;
      state.selectedKeys = new Set((preview.produced_items || []).filter(x => x.selected).map(x => x.key));
      state.resetTask = Boolean(preview.project?.reset_task);
      state.busy = false;
      renderModal();
    } catch (error) {
      state.busy = false;
      renderModal(error?.message || '读取项目清理预览失败');
    }
  }

  async function renderManagementPanel(host, auditHost, onDone) {
    if (!config || !host) return;
    host.replaceChildren(make('p', '', '正在加载可清理项目与最近审计记录…'));
    if (auditHost) auditHost.replaceChildren();
    try {
      const [projects, auditRows] = await Promise.all([
        config.api('/api/project-purge/projects'),
        config.api('/api/project-purge/audit'),
      ]);
      host.replaceChildren();
      if (!Array.isArray(projects) || !projects.length) {
        host.append(make('p', 'dataset-note', '当前平台中暂无可清理的用户项目。'));
      } else {
        projects.forEach(item => {
          const card = make('article', 'card');
          card.append(make('h3', '', `${item.name}（${item.owner_username}）`));
          card.append(make('p', 'path', `项目 ID：${item.project_id}${item.in_workspace ? ' · 位于当前本地工作区' : ''}`));
          card.append(make('p', '', `提交 ${item.submission_count}（固定 ${item.fixed_submission_count}）· 运行 ${item.job_count} · 衍生数据 ${item.derived_dataset_count} · 已入库关联 ${item.published_asset_count}${item.has_task_link ? ' · 关联任务领取' : ''}`));
          const rowActions = make('div', 'actions');
          const btn = make('button', 'danger-btn', '按项目清理...');
          btn.type = 'button';
          btn.onclick = () => open(item.project_id, () => { renderManagementPanel(host, auditHost, onDone); if (onDone) onDone(); });
          rowActions.append(btn);
          card.append(rowActions);
          host.append(card);
        });
      }
      if (auditHost) {
        auditHost.replaceChildren();
        if (!Array.isArray(auditRows) || !auditRows.length) {
          auditHost.append(make('p', 'dataset-note', '暂无按项目清理审计记录。'));
        } else {
          auditRows.forEach(entry => {
            const card = make('article', 'card');
            card.append(make('h4', '', `${entry.project_name}（所有者：${entry.owner_username}）`));
            card.append(make('p', 'path', `执行人：${entry.actor_username} · 时间：${entry.created_at} · 审计 ID：${entry.audit_id}`));
            card.append(make('p', '', `删除 ${(entry.deleted || []).length} 项：${(entry.deleted || []).map(x => x.name || x.ref).join('、') || '无'}`));
            card.append(make('p', 'dataset-note', `保留 ${(entry.retained || []).length} 项`));
            auditHost.append(card);
          });
        }
      }
    } catch (error) {
      host.replaceChildren(make('p', 'action-result error', error?.message || '读取按项目清理清单失败'));
    }
  }

  window.ResearchKitProjectPurge = {
    configure(nextConfig) { config = nextConfig; },
    open,
    renderManagementPanel,
  };
})();
