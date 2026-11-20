(() => {
  'use strict';

  const state = {config: null, projectEpoch: new Map()};
  const make = (tag, value, cls = '') => { const node = document.createElement(tag); if (cls) node.className = cls; if (value != null) node.textContent = String(value); return node; };
  const button = (label, action) => { const node = make('button', label); node.type = 'button'; node.onclick = action; return node; };
  const note = (host, value, cls = '') => host.append(make('p', value, cls));
  const safe = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  const clone = value => JSON.parse(JSON.stringify(value));
  const status = record => String(record?.status || record?.state || 'unknown');
  const scalar = value => value == null || ['string', 'number', 'boolean'].includes(typeof value);
  const bytes = value => `${Number(value || 0).toLocaleString('zh-CN')} B`;
  const projectPath = id => `/api/projects/${encodeURIComponent(id)}/experiments`;
  const resultPath = (base, path, download = false) => `${base}/result-file?${new URLSearchParams({path, ...(download ? {download: 'true'} : {})})}`;
  const SVG_TAGS = new Set(['svg', 'g', 'path', 'polyline', 'polygon', 'line', 'rect', 'circle', 'ellipse', 'text', 'tspan', 'defs', 'clipPath', 'title', 'desc']);
  const SVG_ATTRS = new Set(['width', 'height', 'viewBox', 'x', 'y', 'x1', 'x2', 'y1', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'd', 'points', 'fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'opacity', 'fill-opacity', 'stroke-opacity', 'font-size', 'font-family', 'font-weight', 'text-anchor', 'dominant-baseline', 'transform', 'id']);

  function inertSvg(raw) {
    const source = new DOMParser().parseFromString(raw, 'image/svg+xml').documentElement;
    if (!source || source.nodeName.toLowerCase() !== 'svg') throw new Error('SVG 格式无效');
    const clean = node => {
      const name = String(node.nodeName || '').split(':').pop();
      if (!SVG_TAGS.has(name)) return null;
      const result = document.createElementNS('http://www.w3.org/2000/svg', name);
      [...node.attributes || []].forEach(attr => { if (SVG_ATTRS.has(attr.name) && !/(?:url\s*\(|javascript:|data:|https?:|[<>])/i.test(attr.value)) result.setAttribute(attr.name, attr.value); });
      [...node.childNodes || []].forEach(child => {
        if (child.nodeType === 3) result.append(document.createTextNode(child.textContent || ''));
        else { const nested = clean(child); if (nested) result.append(nested); }
      });
      return result;
    };
    return clean(source);
  }

  function documentEditor(initial, defaults, rules, onSave, schemeId = '', lockId = false) {
    const form = make('form', null, 'experiment-editor');
    const source = safe(clone(initial));
    const algorithmDefaults = safe(defaults?.algorithm_parameters), algorithmSource = safe(source.algorithm_parameters);
    const algorithmParameters = {};
    new Set([...Object.keys(algorithmDefaults), ...Object.keys(algorithmSource)]).forEach(alias => {
      algorithmParameters[alias] = {...safe(algorithmDefaults[alias]), ...safe(algorithmSource[alias])};
    });
    const draft = {
      schema: String(source.schema || 'researchkit.experiment/v1'),
      name: String(source.name || schemeId || ''),
      description: String(source.description || ''),
      case_parameters: {...safe(defaults?.case_parameters), ...safe(source.case_parameters)},
      algorithm_parameters: algorithmParameters,
    };
    const id = make('input'); id.maxLength = 80; id.value = String(schemeId || ''); id.disabled = lockId;
    const title = make('input'); title.maxLength = 120; title.value = draft.name;
    const description = make('textarea'); description.rows = 3; description.maxLength = 2000; description.value = draft.description;
    const json = make('textarea'); json.rows = 10;
    const message = make('p', '', 'action-result');
    const bind = (label, input) => { const row = make('label'); row.append(make('span', label), input); form.append(row); };
    const sync = () => { json.value = JSON.stringify(draft, null, 2); };
    const invalid = new Map(), parameterInputs = [];
    const parse = (input, sample) => {
      if (typeof sample === 'number') { if (!input.value.trim()) throw new Error('数值不能为空。'); const value = Number(input.value); if (!Number.isFinite(value)) throw new Error('数值必须是有限数。'); return value; }
      if (typeof sample === 'boolean') return input.checked;
      return sample === null && !input.value ? null : scalar(sample) ? input.value : JSON.parse(input.value);
    };
    const parameterInput = (host, label, sample, update) => {
      const input = scalar(sample) ? make('input') : make('textarea'); parameterInputs.push(input);
      input.value = scalar(sample) ? String(sample ?? '') : JSON.stringify(sample);
      if (typeof sample === 'boolean') { input.type = 'checkbox'; input.checked = sample; }
      const row = make('label'); row.append(make('span', label), input); host.append(row);
      input.oninput = input.onchange = () => { try { update(parse(input, sample)); invalid.delete(input); sync(); } catch (error) { invalid.set(input, `${label}: ${error.message}`); message.textContent = invalid.get(input); } };
    };
    bind('方案 ID', id); bind('方案名称', title); bind('说明', description);
    title.oninput = () => { draft.name = title.value; };
    description.oninput = () => { draft.description = description.value; };
    const caseGroup = make('fieldset'); caseGroup.append(make('legend', '案例参数'));
    Object.entries(draft.case_parameters).forEach(([key, value]) => parameterInput(caseGroup, key, value, next => { draft.case_parameters[key] = next; }));
    form.append(caseGroup);
    const algorithmGroup = make('fieldset'); algorithmGroup.append(make('legend', '算法参数'));
    Object.entries(draft.algorithm_parameters).forEach(([alias, values]) => {
      const aliasGroup = make('fieldset'); aliasGroup.append(make('legend', alias));
      const parameters = safe(values); draft.algorithm_parameters[alias] = parameters;
      Object.entries(parameters).forEach(([key, value]) => parameterInput(aliasGroup, key, value, next => { parameters[key] = next; }));
      algorithmGroup.append(aliasGroup);
    });
    form.append(algorithmGroup);
    const advanced = make('details'); advanced.append(make('summary', '高级：参数 JSON（名称和说明以上方为准）'), json); form.append(advanced); sync();
    json.oninput = () => { parameterInputs.forEach(input => { input.disabled = true; }); invalid.clear(); message.textContent = '正在使用高级 JSON；上方参数表单已停用，避免覆盖。保存后可重新展开编辑。'; };
    const save = button('保存方案', async () => {
      save.disabled = true;
      try {
        if (invalid.size) throw new Error([...invalid.values()][0]);
        const raw = JSON.parse(json.value);
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('方案必须是 JSON 映射。');
        const value = {
          schema: String(raw.schema || 'researchkit.experiment/v1'),
          name: title.value.trim(), description: description.value.trim(),
          case_parameters: safe(raw.case_parameters), algorithm_parameters: safe(raw.algorithm_parameters),
        };
        if (!id.value.trim()) throw new Error('请填写方案 ID。');
        if (!value.name) throw new Error('请填写方案名称。');
        await onSave(id.value.trim(), value); message.className = 'action-result success'; message.textContent = '方案已保存；请在 IDE 中使用 --scheme 运行。';
      } catch (error) { message.className = 'action-result error'; message.textContent = error.message; }
      finally { save.disabled = false; }
    });
    form.append(save, message); return form;
  }

  function preview(host, base, file) {
    const panel = make('div', null, 'experiment-preview');
    const close = button('关闭预览', () => panel.remove());
    panel.append(make('h4', file.path), close, make('p', '正在读取预览…'));
    host.append(panel);
    state.config.fetchFile(resultPath(base, file.path)).then(async response => {
      if (response.ok === false) throw new Error(`HTTP ${response.status}：产物不可读取`);
      const contentType = response.headers.get('content-type') || '';
      const target = panel.querySelector('p'); target.remove();
      if (contentType.startsWith('image/png') || contentType.startsWith('image/jpeg') || contentType.startsWith('image/gif')) {
        const image = new Image(); image.alt = file.path; image.src = URL.createObjectURL(await response.blob()); image.onload = image.onerror = () => URL.revokeObjectURL(image.src); panel.append(image); return;
      }
      const raw = await response.text();
      if (contentType.includes('image/svg+xml')) { const svg = inertSvg(raw); svg.classList.add('experiment-preview-svg'); panel.append(svg); return; }
      const out = make('pre', raw, 'experiment-preview-text'); panel.append(out);
    }).catch(error => note(panel, `预览失败：${error.message}`, 'action-result error'));
  }

  function download(base, file) {
    state.config.fetchFile(resultPath(base, file.path, true)).then(async response => {
      if (response.ok === false) throw new Error(`HTTP ${response.status}：产物不可下载`);
      const link = document.createElement('a'); link.href = URL.createObjectURL(await response.blob()); link.download = file.path.split('/').pop() || 'result'; link.hidden = true;
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(link.href), 0);
    }).catch(error => state.config.report?.(error.message));
  }

  function files(host, base, rows) {
    const box = make('details', null, 'experiment-files');
    box.append(make('summary', `产物文件（${rows.length}）`));
    rows.forEach(file => {
      const row = make('div', null, 'experiment-file'); row.append(make('span', `${file.path} · ${bytes(file.size)}`));
      row.append(button('预览', () => preview(box, base, file)), button('下载', () => download(base, file))); box.append(row);
    });
    host.append(box);
  }

  function runCard(host, base, run, writable, refresh) {
    const record = safe(run.record), filesList = Array.isArray(run.files) ? run.files : [];
    const row = make('article', null, 'experiment-run');
    row.append(make('h4', `${run.scheme_id || '未命名方案'} · 批次 ${run.batch_id || '未知'}`));
    note(row, `本地运行记录：${status(record)}${run.revision ? ` · 修订 ${run.revision}` : ''}`);
    if (record.error || record.reason) note(row, record.error || record.reason);
    const metadata = make('details'); metadata.append(make('summary', '本次参数与结果摘要'), make('pre', JSON.stringify({parameters: record.parameters || {}, summary: record.summary ?? {}}, null, 2))); row.append(metadata);
    note(row, '此记录来自 IDE 本地运行；网页只保存和展示产物，审核时由教师人工判断。', 'path');
    if (filesList.length) files(row, base, filesList);
    if (writable) {
      const deleting = ['running', 'queued'].includes(status(record));
      const remove = button('删除此批次产物', async () => {
        if (!confirm(`删除 ${run.scheme_id}/${run.batch_id} 的产物？`)) return;
        remove.disabled = true;
        try { await state.config.post(`${base.replace('/experiments', '')}/experiment-results/delete`, {scheme_id: run.scheme_id, batch_id: run.batch_id, confirmation: `${run.scheme_id}/${run.batch_id}`, expected_revision: run.revision}); await refresh(); }
        catch (error) { note(row, error.message, 'action-result error'); remove.disabled = false; }
      });
      remove.disabled = deleting; row.append(remove);
      if (deleting) note(row, '本地运行中，暂不能删除产物。');
    }
    host.append(row);
  }

  function flatten(value, prefix = '', result = {}) {
    Object.entries(safe(value)).forEach(([key, item]) => {
      const name = prefix ? `${prefix}.${key}` : key;
      if (item && typeof item === 'object' && !Array.isArray(item)) flatten(item, name, result);
      else result[name] = item;
    });
    return result;
  }
  function comparisons(host, runs) {
    if (runs.length < 2) return;
    const box = make('details', null, 'experiment-compare'); box.append(make('summary', '比较两批本地产物'));
    const left = make('select'), right = make('select'), result = make('pre');
    runs.forEach(run => [left, right].forEach(select => { const option = make('option', `${run.scheme_id}/${run.batch_id}`); option.value = JSON.stringify([run.scheme_id, run.batch_id]); select.append(option); }));
    if (right.options[1]) right.selectedIndex = 1;
    const compare = () => {
      const find = select => runs.find(run => JSON.stringify([run.scheme_id, run.batch_id]) === select.value);
      const a = find(left), b = find(right), ap = flatten(a?.record?.parameters), bp = flatten(b?.record?.parameters);
      const keys = [...new Set([...Object.keys(ap), ...Object.keys(bp)])].sort();
      const metricsA = flatten(a?.record?.summary), metricsB = flatten(b?.record?.summary);
      const metricKeys = [...new Set([...Object.keys(metricsA), ...Object.keys(metricsB)])].sort();
      const show = value => value === undefined ? '—' : JSON.stringify(value);
      result.textContent = ['参数对照：', ...keys.map(key => `${key}: ${show(ap[key])} → ${show(bp[key])}`), '', '同名结果摘要（请确认含义、单位和数据来源一致）：', ...metricKeys.map(key => `${key}: ${show(metricsA[key])} → ${show(metricsB[key])}`)].join('\n');
    };
    left.onchange = right.onchange = compare; box.append(left, right, result); compare(); host.append(box);
  }
  function otherFiles(host, base, data) {
    const batched = new Set((data.runs || []).flatMap(run => (run.files || []).map(file => file.path)));
    const unbatched = (data.result_files || []).filter(file => !batched.has(file.path));
    if (unbatched.length) { note(host, '其他已有产物（没有方案批次记录）'); files(host, base, unbatched); }
    return unbatched.length;
  }

  function compactTargets(runs) {
    const latest = new Map();
    const order = run => [Date.parse(run.record?.started_at || '') || 0, run.batch_id];
    const newer = (a, b) => order(a)[0] - order(b)[0] || String(order(a)[1]).localeCompare(String(order(b)[1]));
    runs.filter(run => run.record?.status === 'succeeded').forEach(run => {
      if (!latest.has(run.scheme_id) || newer(run, latest.get(run.scheme_id)) > 0) latest.set(run.scheme_id, run);
    });
    return runs.filter(run => run.revision && ['succeeded', 'failed', 'interrupted'].includes(run.record?.status)
      && run !== latest.get(run.scheme_id));
  }

  async function renderProject(host, project) {
    const projectId = typeof project === 'string' ? project : project.project_id;
    if (!projectId) return;
    const epoch = (state.projectEpoch.get(projectId) || 0) + 1; state.projectEpoch.set(projectId, epoch);
    host.replaceChildren(make('p', '正在读取 IDE 实验方案与产物…'));
    try {
      const data = await state.config.api(projectPath(projectId)); if (state.projectEpoch.get(projectId) !== epoch) return;
      host.replaceChildren(); const base = `/api/projects/${encodeURIComponent(projectId)}`;
      const title = make('section', null, 'experiment-panel'); title.append(make('h3', 'IDE 实验方案与产物'));
      note(title, '网页只管理方案和查看 IDE 写入的产物；请在自己的 IDE 中选择解释器并运行，不会在网页执行代码。');
      if (!data.has_runner) {
        const enable = button('准备 IDE 实验入口', async () => { enable.disabled = true; try { await state.config.post(`${base}/experiments/enable`, {}); await renderProject(host, projectId); } catch (error) { note(title, error.message, 'action-result error'); enable.disabled = false; } });
        title.append(enable, make('p', '会创建 run_experiment.py 和辅助文件，不会改写 case.py。'));
      }
      if (data.helper_update_available) {
        const update = button('启用最新结果保存方式', async () => {
          update.disabled = true;
          try {
            await state.config.post(`${base}/experiments/update-helper`, {});
            await renderProject(host, projectId);
            note(host, '已更新标准实验辅助文件；新运行每方案只保留最新成功结果。旧产物可点“整理旧产物”。', 'action-result success');
          } catch (error) { note(title, error.message, 'action-result error'); update.disabled = false; }
        });
        title.append(update); note(title, '只更新标准 experiment_support.py，保留 case.py、方案与产物。更新前请结束 IDE 中的实验。');
      } else if (data.result_retention === 'latest-success') {
        note(title, '每个方案保留最新一次成功产物；运行失败保留上次成功结果及最近一次错误、参数，不累积失败产物。');
      } else if (data.result_retention === 'custom') {
        note(title, '实验辅助文件有自定义修改，平台不会自动覆盖；请在 IDE 核对保存规则。');
      }
      host.append(title);
      (data.warnings || []).forEach(value => note(host, value, 'action-result error'));
      const schemeHost = make('section', null, 'experiment-panel'); schemeHost.append(make('h3', '方案'));
      const save = async (id, document, expected_sha256 = null) => { await state.config.post(projectPath(projectId), {id, document, expected_sha256}); await renderProject(host, projectId); note(host, `已保存方案：${id}`, 'action-result success'); };
      schemeHost.append(documentEditor({}, data.defaults, data.algorithm_rules, (id, document) => save(id, document)));
      (data.schemes || []).forEach(scheme => {
        const row = make('article', null, 'experiment-scheme'); row.append(make('h4', scheme.document?.name || scheme.id), make('p', `${scheme.path || scheme.id} · ${scheme.sha256 || ''}`, 'path'));
        const edit = button('编辑', () => { row.append(documentEditor(scheme.document, data.defaults, data.algorithm_rules, (id, document) => save(id, document, scheme.sha256), scheme.id, true)); edit.disabled = true; });
        const copy = button('复制', () => { const next = clone(scheme.document); next.name = `${next.name || scheme.id} 副本`; row.append(documentEditor(next, data.defaults, data.algorithm_rules, (id, document) => save(id, document), `${scheme.id}-copy`)); copy.disabled = true; });
        const remove = button('删除方案（保留产物）', async () => { if (!confirm(`删除方案 ${scheme.id}？已有产物会保留。`)) return; remove.disabled = true; try { await state.config.post(`${projectPath(projectId)}/delete`, {id: scheme.id, expected_sha256: scheme.sha256}); await renderProject(host, projectId); } catch (error) { note(row, error.message, 'action-result error'); remove.disabled = false; } });
        row.append(edit, copy, remove); schemeHost.append(row);
      }); host.append(schemeHost);
      const runHost = make('section', null, 'experiment-panel'); runHost.append(make('h3', 'IDE 产物')); comparisons(runHost, data.runs || []);
      const obsolete = compactTargets(data.runs || []);
      if (obsolete.length) {
        const compact = button('整理旧产物', async () => {
          if (!confirm(`将删除当前项目的 ${obsolete.length} 个旧成功或失败批次，只保留各方案最新成功产物。运行中及无法识别的目录保留，固定提交和托管数据不变。确定整理？`)) return;
          compact.disabled = true; let removed = 0;
          try {
            for (const run of obsolete) {
              await state.config.post(`${base}/experiment-results/delete`, {scheme_id: run.scheme_id, batch_id: run.batch_id,
                confirmation: `${run.scheme_id}/${run.batch_id}`, expected_revision: run.revision});
              removed += 1;
            }
            await renderProject(host, projectId); note(host, `已整理 ${removed} 个旧批次。`, 'action-result success');
          } catch (error) {
            await renderProject(host, projectId); note(host, `已整理 ${removed} 个；其余未继续处理：${error.message}`, 'action-result error');
          }
        });
        runHost.append(compact);
      }
      (data.runs || []).forEach(run => runCard(runHost, base, run, true, () => renderProject(host, projectId)));
      otherFiles(runHost, base, data);
      if (!(data.runs || []).length) note(runHost, '尚无 IDE 写入的批次产物。'); host.append(runHost);
    } catch (error) { host.replaceChildren(); note(host, error.message, 'action-result error'); }
  }

  async function renderSubmission(host, submissionId, options = {}) {
    const review = Boolean(options.review), base = review ? `/api/review/submissions/${encodeURIComponent(submissionId)}` : `/api/submissions/${encodeURIComponent(submissionId)}`;
    const panel = make('section', null, 'experiment-panel'); panel.append(make('h3', '固定提交中的实验方案与产物'), make('p', '只读快照；这些记录来自 IDE，本页不会运行项目。'));
    host.append(panel);
    try {
      const data = await state.config.api(`${base}/experiments`);
      (data.schemes || []).forEach(scheme => { const row = make('article', null, 'experiment-scheme'); row.append(make('h4', scheme.document?.name || scheme.id), make('pre', JSON.stringify(scheme.document || {}, null, 2))); panel.append(row); });
      comparisons(panel, data.runs || []); (data.runs || []).forEach(run => runCard(panel, base, run, false, () => {}));
      otherFiles(panel, base, data);
      if (!(data.schemes || []).length && !(data.runs || []).length && !(data.result_files || []).length) note(panel, '该固定提交没有实验方案或产物。');
    } catch (error) { note(panel, `读取实验快照失败：${error.message}`, 'action-result error'); }
  }

  window.ResearchKitExperiments = {
    mount(config) { state.config = config; return {renderProject, renderSubmission}; },
    renderProject, renderSubmission,
  };
})();
