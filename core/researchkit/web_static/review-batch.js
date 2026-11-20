(() => {
  'use strict';
  const el = (tag, value, className) => {
    const node = document.createElement(tag);
    if (value !== undefined) node.textContent = String(value ?? '');
    if (className) node.className = className;
    return node;
  };
  const field = (host, title, node) => { const label = el('label'); label.append(el('span', title), node); host.append(label); return node; };
  const input = (value = '') => { const n = el('input'); n.value = String(value ?? ''); return n; };
  const select = entries => { const n = el('select'); entries.forEach(([v, t]) => { const o = el('option', t); o.value = v; n.append(o); }); n.value = entries[0][0]; return n; };
  const tags = value => String(value || '').split(/[,，\n]+/).map(s => s.trim()).filter(Boolean);

  function render(host, detail, {submissionId, post, onPublished}) {
    const submission = detail.submission || detail;
    const existing = detail.existing_assets || {};
    const rows = [];
    const notice = el('p', '', 'action-result'); notice.setAttribute('aria-live', 'polite');
    const selection = el('div', undefined, 'review-batch-selection');
    const preview = el('section', undefined, 'review-batch-preview'); preview.hidden = true;
    let inFlight = false, done = false, pending = null;
    const invalidate = () => { if (!inFlight && !done) { pending = null; preview.hidden = true; } };
    selection.addEventListener('input', invalidate); selection.addEventListener('change', invalidate);
    host.append(el('p', '可选择本案例，以及多个算法、多个私有派生数据；也可以只入库算法或数据。先预览，确认后统一入库，任何一项失败都不保存本批次。'));
    function resource(kind, source) {
      const box = el('details', undefined, 'review-batch-resource');
      const summary = el('summary');
      const check = input(); check.type = 'checkbox'; check.checked = false;
      const label = el('label', undefined, 'review-batch-choice');
      const identity = kind === 'template' ? (submission.project?.name || submission.project_name || '本次固定提交') : source.alias || source.name || source.dataset_id;
      label.append(check, el('span', `${kind === 'template' ? '案例' : kind === 'algorithm' ? '算法' : '私有派生数据'}：${identity}${source.ref ? ` · ${source.ref}` : ''}`));
      label.onclick = event => event.stopPropagation();
      summary.append(label); box.append(summary);
      const fields = el('div', undefined, 'review-publish-grid');
      const mode = field(fields, '入库方式', select([['new', '作为新资源'], ['new_version', '作为已有资源的新版本']]));
      const parents = kind === 'template' ? existing.templates || [] : kind === 'algorithm' ? existing.algorithms || [] : existing.derived || [];
      const parent = field(fields, '原资源（新版本时选择）', select([['', '请选择原资源'], ...parents.map(r => [r.ref || r.dataset_id, `${r.name || r.ref || r.dataset_id} · ${r.ref || r.dataset_id}`])]));
      const name = field(fields, '资源名称（可沿用来源）', input(kind === 'algorithm' ? source.name : ''));
      const version = input(kind === 'algorithm' ? source.suggested_next_version || source.version : '');
      if (kind !== 'derived') field(fields, '版本（留空使用系统建议）', version);
      const tagInput = field(fields, '标签（逗号分隔；新版本可留空继承）', input(Array.isArray(source.tags) ? source.tags.join(', ') : ''));
      const description = el('textarea'); description.value = source.description || '';
      field(fields, '说明（新版本可留空继承）', description);
      const scope = kind === 'derived' ? field(fields, '数据类别', select([['derived', '平台派生数据集'], ['public', '公共数据集']])) : null;
      const readme = kind === 'derived' ? el('textarea') : null;
      if (readme) { readme.value = source.readme_markdown || ''; field(fields, 'README.md（可选）', readme); }
      if (kind === 'derived') fields.append(el('p', `数据 ID：${source.dataset_id}`, 'path'));
      if (kind === 'algorithm') fields.append(el('p', `项目别名：${source.alias} · ${source.modified_platform_algorithm ? '平台算法已改动；随案例入库时必须发布原算法的新版本。' : '项目内本地算法；不选择时保留在案例内部。'}`));
      box.append(fields); selection.append(box);
      const row = {kind, source, check, mode, parent, name, version, tagInput, description, scope, readme, box}; rows.push(row);
      const sync = () => { parent.parentNode.hidden = mode.value !== 'new_version'; name.parentNode.hidden = mode.value === 'new_version'; };
      mode.onchange = () => {
        if (kind === 'algorithm' && !source.modified_platform_algorithm) version.value = mode.value === 'new_version' ? '' : source.version || '';
        sync(); invalidate();
      }; sync();
      return row;
    }
    const caseRow = resource('template', {});
    const parentCase = detail.case_info?.parent_template_ref;
    if (parentCase && (existing.templates || []).some(r => r.ref === parentCase)) { caseRow.mode.value = 'new_version'; caseRow.parent.value = parentCase; caseRow.mode.onchange(); }
    const algorithms = Array.isArray(detail.algorithms) ? detail.algorithms : [];
    algorithms.forEach(algorithm => {
      if (!algorithm.is_local_plugin && !algorithm.modified_platform_algorithm) {
        selection.append(el('p', `算法 ${algorithm.alias}：沿用平台版本 ${algorithm.upstream_ref || algorithm.ref}，无需重复入库。`));
      } else {
        const row = resource('algorithm', algorithm);
        if (algorithm.modified_platform_algorithm) { row.mode.value = 'new_version'; row.parent.value = algorithm.upstream_ref || algorithm.ref; row.mode.onchange(); }
      }
    });
    const datasets = Array.isArray(detail.datasets) ? detail.datasets : [];
    datasets.forEach(dataset => {
      if (dataset.needs_resolution || dataset.scope === 'private_derived') resource('derived', dataset);
      else selection.append(el('p', `数据 ${dataset.name || dataset.dataset_id} · ${dataset.dataset_id}：沿用已有平台数据引用，无需重复入库。`));
    });
    const syncForced = () => {
      rows.filter(r => r.kind === 'algorithm' && r.source.modified_platform_algorithm).forEach(row => {
        row.check.disabled = caseRow.check.checked;
        if (caseRow.check.checked) { row.check.checked = true; row.mode.value = 'new_version'; row.parent.value = row.source.upstream_ref || row.source.ref; row.mode.onchange(); }
        row.mode.disabled = caseRow.check.checked;
        row.parent.disabled = caseRow.check.checked;
      });
      invalidate();
    };
    caseRow.check.onchange = syncForced;
    const build = () => {
      const items = rows.filter(r => r.check.checked).map(row => {
        const item = {kind: row.kind, publish_mode: row.mode.value};
        const t = tags(row.tagInput.value), d = row.description.value.trim();
        if (item.publish_mode === 'new' && (!t.length || !d)) throw new Error(`请为所选${row.source.alias || row.source.name || '案例或数据'}填写标签和说明。`);
        if (item.publish_mode === 'new_version') {
          if (!row.parent.value) throw new Error('请选择新版本对应的原资源。');
          item.parent_ref = row.parent.value;
        } else if (row.name.value.trim()) item.name = row.name.value.trim();
        if (row.version.value.trim()) item.version = row.version.value.trim();
        if (t.length) item.tags = t;
        if (d) item.description = d;
        if (row.kind === 'algorithm') item.alias = row.source.alias;
        if (row.kind === 'derived') {
          item.dataset_id = row.source.dataset_id; item.dataset_scope = row.scope.value;
          if (row.readme.value.trim()) item.readme_markdown = row.readme.value;
        }
        if (row.kind === 'template') {
          const selectedAlgs = new Set(rows.filter(r => r.kind === 'algorithm' && r.check.checked).map(r => r.source.alias));
          const selectedData = new Set(rows.filter(r => r.kind === 'derived' && r.check.checked).map(r => r.source.dataset_id));
          item.embedded_algorithms = {};
          algorithms.filter(a => a.is_local_plugin && !selectedAlgs.has(a.alias)).forEach(a => { item.embedded_algorithms[a.alias] = {action: 'keep_local'}; });
          item.embedded_datasets = {};
          datasets.filter(a => a.needs_resolution && !selectedData.has(a.dataset_id)).forEach(a => { item.embedded_datasets[a.dataset_id] = {action: 'keep_private'}; });
        }
        return item;
      });
      if (!items.length) throw new Error('请至少选择案例、算法或私有派生数据中的一项。');
      if (items.length > 32) throw new Error('每批最多选择32项，请分批审核。');
      return items;
    };
    const previewButton = el('button', '预览所选入库'); previewButton.type = 'button'; previewButton.id = 'review-batch-preview';
    previewButton.onclick = () => {
      if (inFlight || done) return;
      try {
        pending = build(); notice.textContent = ''; preview.replaceChildren(); preview.hidden = false;
        preview.append(el('h3', `本次共入库 ${pending.length} 项`), el('p', `固定提交：${submissionId}`, 'path'));
        pending.forEach(item => preview.append(el('p', `${item.kind === 'template' ? '案例' : item.kind === 'algorithm' ? '算法' : '派生数据'}：${item.alias || item.dataset_id || submission.project?.name || '本案例'} · ${item.publish_mode === 'new_version' ? `原资源 ${item.parent_ref} 的新版本` : '新资源'}${item.kind === 'derived' ? ' · 使用上述准确数据 ID' : item.version ? ` · ${item.version}` : ' · 系统按固定提交确定版本'}`)));
        const confirm = el('button', '确认全部入库'); confirm.type = 'button'; confirm.id = 'review-batch-confirm';
        confirm.onclick = async () => {
          if (inFlight || done || !pending) return;
          // Compare again even if an input event was missed by a browser helper.
          try {
            if (JSON.stringify(build()) !== JSON.stringify(pending)) { invalidate(); notice.textContent = '选项已变化，请重新预览。'; return; }
          } catch (error) { invalidate(); notice.className = 'action-result error'; notice.textContent = error.message; return; }
          inFlight = true; confirm.disabled = true; previewButton.disabled = true;
          selection.querySelectorAll('input,select,textarea').forEach(n => { n.disabled = true; });
          try {
            const result = await post(`/api/review/submissions/${encodeURIComponent(submissionId)}/publish-batch`, {expected_sha256: submission.content_sha256, items: pending});
            if (!result.ok) throw new Error((result.failures || []).map(f => f.error || f.reason).filter(Boolean).join('；') || '本次入库未完成，所有所选项均未保存。');
            done = true;
            notice.className = 'action-result success';
            notice.textContent = `本批次入库成功：${(result.published || []).map(r => r.ref || r.dataset_id || r.name).filter(Boolean).join('、')}。来源项目不变。`;
            confirm.textContent = '已全部入库';
            try { if (onPublished) await onPublished(); } catch (_) { notice.textContent += ' 列表暂未刷新，可稍后手动刷新。'; }
          } catch (error) {
            notice.className = 'action-result error'; notice.textContent = `入库失败：${error.message}`;
            confirm.disabled = false; previewButton.disabled = false;
            selection.querySelectorAll('input,select,textarea').forEach(n => { n.disabled = false; }); syncForced();
          } finally { inFlight = false; }
        };
        preview.append(confirm);
      } catch (error) { pending = null; preview.hidden = true; notice.className = 'action-result error'; notice.textContent = error.message; }
    };
    host.append(selection, previewButton, preview, notice);
    (detail.publications || []).filter(p => ['published', 'reused'].includes(p.status)).forEach(p => host.append(el('p', `已发布：${p.ref || p.name || p.alias || '资源'}`, 'action-result success')));
  }
  window.ResearchKitReviewBatch = {render};
})();
